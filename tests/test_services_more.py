import json

from toolsim.core.instance import Instance
from toolsim.services import get_service


def svc(name):
    i = Instance(get_service(name))
    return i, (lambda tool, **a: i.call(tool, a))


def keys(r):
    return [x["key"] for x in json.loads(r.text)["issues"]]


# -- jira ---------------------------------------------------------------------------------

def test_jira_jql():
    i, call = svc("jira")
    assert keys(call("jira_search", jql='project = OPS AND status = "In Progress" ORDER BY key ASC')) == ["OPS-1", "OPS-2"]
    assert keys(call("jira_search", jql="assignee = currentUser()")) == ["OPS-1"]
    assert set(keys(call("jira_search", jql="labels in (billing, backups) AND statusCategory != Done"))) == {"OPS-4", "SUP-1"}
    assert keys(call("jira_search", jql='text ~ "credentials"')) == ["OPS-2"]
    assert keys(call("jira_search", jql="type = Bug AND priority >= High ORDER BY priority DESC")) == ["OPS-4", "SUP-1"]
    assert keys(call("jira_search", jql="resolution = Unresolved AND project in (SUP) AND NOT status = \"In Review\"")) == ["SUP-1"]
    assert "does not exist" in call("jira_search", jql="bogus = 1").text
    assert "incomplete" in call("jira_search", jql='status = "Done" AND (').text


def test_jira_workflow_and_fields():
    i, call = svc("jira")
    ids = [t["id"] for t in json.loads(call("jira_get_transitions", issue_key="OPS-4").text)]
    assert ids == ["11", "41"]
    assert "not valid" in call("jira_transition_issue", issue_key="OPS-4", transition_id="21").text
    assert not call("jira_transition_issue", issue_key="OPS-4", transition_id="11").is_error
    assert i.state["issues"]["OPS-4"]["status"] == "In Progress"
    assert "Use a transition" in call("jira_update_issue", issue_key="OPS-4", fields='{"status": "Done"}').text
    call("jira_transition_issue", issue_key="OPS-4", transition_id="41")
    assert i.state["issues"]["OPS-4"]["resolution"] == "Done"
    created = json.loads(call("jira_create_issue", project_key="SUP", summary="Refund", issue_type="Task",
                              additional_fields='{"priority": {"name": "High"}, "labels": ["billing"]}').text)["issue"]
    assert created["key"] == "SUP-3" and created["priority"]["name"] == "High"
    assert "Sub-task issues must have a parent" in call("jira_create_issue", project_key="SUP", summary="x",
                                                        issue_type="Subtask").text
    assert "is not an Epic" in call("jira_link_to_epic", issue_key="OPS-4", epic_key="OPS-2").text
    assert "valid format" in call("jira_add_worklog", issue_key="OPS-2", time_spent="soon").text
    assert json.loads(call("jira_get_issue", issue_key="OPS-2").text)["epic_key"] == "OPS-1"
    assert "does not exist" in call("jira_get_issue", issue_key="NOPE-1").text


# -- linear -------------------------------------------------------------------------------

def test_linear_issues_states_and_references():
    i, call = svc("linear")
    eng = json.loads(call("list_issues", team="ENG").text)["issues"]
    assert {x["identifier"] for x in eng} == {"ENG-1", "ENG-2", "ENG-3", "ENG-4"}
    assert [x["identifier"] for x in json.loads(call("list_issues", assignee="me").text)["issues"]] == ["ENG-4", "OPS-1"]
    new = json.loads(call("create_issue", title="Idempotency keys", team="eng", priority=2, labels=["Bug"],
                          assignee="john@acme.com", project="Billing v2").text)
    assert new["identifier"] == "ENG-5" and new["assignee"] == "John Park" and new["priority"]["name"] == "High"
    assert "IssueLabel" in call("create_issue", title="x", team="ENG", labels=["Incident"]).text  # OPS-only label
    assert "WorkflowState" in call("update_issue", id="ENG-1", state="Shipped").text
    done = json.loads(call("update_issue", id="ENG-1", state="Done").text)
    assert done["status"] == "Done" and done["completedAt"]
    assert "Entity not found: Issue" in call("get_issue", id="ENG-999").text
    assert json.loads(call("list_cycles", teamId="ENG", type="current").text)["cycles"][0]["number"] == 2


# -- notion -------------------------------------------------------------------------------

def test_notion_pages_databases_permissions():
    i, call = svc("notion")
    titles = lambda q: [r["title"] for r in json.loads(call("notion-search", query=q).text)["results"]]  # noqa: E731
    assert titles("runbook")[0] == "On-call runbook"
    assert titles("salary") == [], "restricted pages are invisible"
    restricted = next(p["id"] for p in i.state["pages"].values() if p["restricted"])
    assert "Could not find page" in call("notion-fetch", id=restricted).text
    ds = next(iter(i.state["data_sources"].values()))
    rows = json.loads(call("notion-query-data-sources", data_source_url=ds["url"], filter={"Tags": "billing"}).text)["results"]
    assert [r["Name"] for r in rows] == ["Invoice currency bug"]
    assert "Invalid status option" in call("notion-create-pages", parent={"data_source_id": ds["id"]},
                                           pages=[{"properties": {"Name": "x", "Status": "Blocked"}}]).text
    ok = json.loads(call("notion-create-pages", parent={"data_source_id": ds["id"]},
                         pages=[{"properties": {"Name": "New", "Status": "Done", "Tags": ["urgent"]}}]).text)
    ds = i.state["data_sources"][ds["id"]]  # failed calls roll back by swapping in a restored copy of the state
    assert ok["pages"][0]["properties"]["Tags"] == ["urgent"] and "urgent" in ds["schema"]["Tags"]["options"]
    rb = next(p for p in i.state["pages"].values() if p["title"] == "On-call runbook")
    call("notion-update-page", page_id=rb["id"], command="replace_content_range",
         selection_with_ellipsis="1. Find...green deploy.", new_str="1. Find the last green build.")
    assert "1. Find the last green build.\n2. Run" in rb["content"]
    assert "Could not find text" in call("notion-update-page", page_id=rb["id"], command="insert_content_after",
                                         selection_with_ellipsis="nope...nada", new_str="x").text
    wiki = next(p for p in i.state["pages"].values() if p["title"] == "Engineering Wiki")
    assert "into itself" in call("notion-move-pages", page_ids=[wiki["id"]], parent={"page_id": rb["id"]}).text


# -- drive --------------------------------------------------------------------------------

def test_drive_queries_permissions_and_policy():
    i, call = svc("drive")
    f = {x["name"]: x for x in i.state["files"].values()}
    assert "Q4 plan" in call("search_drive_files", query="name contains 'q4' and mimeType != 'application/vnd.google-apps.folder'").text
    assert "No files found" in call("search_drive_files", query="old-export").text
    assert "old-export" in call("search_drive_files", query="name contains 'old' and trashed = true").text
    assert f"'{f['Q4 planning']['id']}'" and "Q4 budget" in call("search_drive_files",
                                                                 query=f"'{f['Q4 planning']['id']}' in parents").text
    assert "sufficient permissions" in call("update_drive_file", file_id=f["Vendor contract.pdf"]["id"], name="x").text
    assert not call("update_drive_file", file_id=f["Offsite ideas"]["id"], content="Porto!").is_error  # writer can edit
    assert "sufficient permissions" in call("update_drive_file", file_id=f["Offsite ideas"]["id"], trashed=True).text
    assert "restricted sharing outside" in call("manage_drive_access", file_id=f["Q4 budget"]["id"], action="grant",
                                                share_with="cfo@partner.io").text
    assert "publicly" in call("set_drive_file_permissions", file_id=f["Q4 budget"]["id"], link_sharing="anyone_with_link").text
    assert not call("set_drive_file_permissions", file_id=f["Q4 budget"]["id"], link_sharing="domain").is_error
    assert "Export only supports" in call("get_drive_file_download_url", file_id=f["Q4 budget"]["id"], export_format="docx").text
    assert "engineering,110000" in call("get_drive_file_content", file_id=f["Q4 budget"]["id"]).text
