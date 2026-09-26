"""jira: agent-facing tools."""

from __future__ import annotations

import json
import re
from typing import Annotated, Any

from ...core.instance import Instance
from ...core.tools import ToolError, tool
from .jql import _JQL
from .model import (
    LINK_TYPES,
    SITE,
    STATUSES,
    TYPES,
    V1,
    WORKFLOW,
    _apply_fields,
    _board,
    _comment,
    _date_iso,
    _err,
    _fields,
    _get,
    _iso,
    _issue_json,
    _new_issue,
    _new_sprint,
    _paged,
    _resolve_user,
    _sprint,
    _sprint_json,
    _user_json,
)


@tool("jira_search", read_only=True)
def jira_search(ctx: Instance,
                jql: Annotated[str, "JQL query string (Jira Query Language). Examples: 'project = OPS AND status = \"In Progress\" ORDER BY priority DESC', 'assignee = currentUser() AND updated >= -7d'"],
                fields: Annotated[str | None, "Comma-separated fields to return, or '*all'"] = None,
                limit: Annotated[int | None, "Maximum number of results (1-50)"] = 10,
                start_at: Annotated[int | None, "Starting index for pagination (0-based)"] = 0,
                projects_filter: Annotated[str | None, "Comma-separated list of project keys to filter results"] = None,
                expand: Annotated[str | None, "Fields to expand: 'renderedFields', 'transitions', 'changelog'"] = None,
                page_token: Annotated[str | None, "Pagination token from a previous search result (Cloud only)"] = None,
                use_display_names: Annotated[bool | None, "Use human-readable names for custom fields"] = False) -> dict[str, Any]:
    """Search Jira issues using JQL (Jira Query Language)"""
    s = ctx.search_view("issues")
    q = _JQL(s, jql, ctx.now())
    projects = {p.strip().upper() for p in (projects_filter or "").split(",") if p.strip()}
    hits = [i for i in s["issues"].values() if q.matches(i) and (not projects or i["project"] in projects)]
    hits = q.sort(hits)
    lim = max(1, min(limit or 10, 50))
    start = int(page_token) if page_token else (start_at or 0)
    page = hits[start:start + lim]
    out = {"total": len(hits), "start_at": start, "max_results": lim,
           "issues": [_issue_json(s, i, comment_limit=0) for i in page]}
    if start + lim < len(hits):
        out["next_page_token"] = str(start + lim)
    return out


@tool("jira_get_issue", read_only=True)
def jira_get_issue(ctx: Instance,
                   issue_key: Annotated[str, "Jira issue key (e.g., 'PROJ-123')"],
                   fields: Annotated[str | None, "Comma-separated list of fields to return, or '*all'"] = None,
                   expand: Annotated[str | None, "Fields to expand, e.g. 'renderedFields', 'transitions', 'changelog'"] = None,
                   comment_limit: Annotated[int | None, "Maximum number of comments to include (0 for none)"] = 10,
                   properties: Annotated[str | None, "Comma-separated issue properties to return"] = None,
                   update_history: Annotated[bool | None, "Whether to update the issue view history"] = True,
                   include: Annotated[str | None, "Extra sections to include"] = None,
                   use_display_names: Annotated[bool | None, "Use human-readable names for custom fields"] = False) -> dict[str, Any]:
    """Get details of a specific Jira issue including its Epic links and relationship information"""
    s = ctx.state
    i = _get(s, issue_key)
    out = _issue_json(s, i, comment_limit=10 if comment_limit is None else comment_limit)
    if expand and "transitions" in expand:
        out["transitions"] = [{"id": t, "name": n, "to_status": to} for t, n, to in WORKFLOW[i["status"]]]
    return out


@tool("jira_create_issue")
def jira_create_issue(ctx: Instance,
                      project_key: Annotated[str, "The JIRA project key (e.g. 'PROJ', 'DEV', 'SUPPORT')"],
                      summary: Annotated[str, "Summary/title of the issue"],
                      issue_type: Annotated[str, "Issue type (e.g. 'Task', 'Bug', 'Story', 'Epic', 'Subtask')"],
                      assignee: Annotated[str | None, "Assignee's user identifier (email, display name, or account ID)"] = None,
                      description: Annotated[str | None, "Issue description in Markdown"] = None,
                      components: Annotated[str | None, "Comma-separated list of component names"] = None,
                      additional_fields: Annotated[str | None, "JSON string of additional fields, e.g. '{\"priority\": {\"name\": \"High\"}, \"labels\": [\"x\"], \"parent\": \"PROJ-1\"}'"] = None) -> dict[str, Any]:
    """Create a new Jira issue with optional Epic link or parent for subtasks"""
    s = ctx.state
    p = s["projects"].get(project_key.upper())
    if p is None:
        raise _err("", 400, project=f"Specify a valid project ID or key (got '{project_key}')")
    if issue_type not in TYPES:
        raise _err("", 400, issuetype="Specify an issue type (valid: " + ", ".join(TYPES) + ")")
    if not summary.strip():
        raise _err("", 400, summary="You must specify a summary of the issue.")
    extra = _fields(additional_fields)
    if issue_type == "Subtask" and not extra.get("parent"):
        raise _err("", 400, parent="Sub-task issues must have a parent.")
    i = _new_issue(ctx, s, p["key"], summary, issue_type, description, _resolve_user(s, assignee), "Medium", [],
                   reporter=s["me"], parent=(extra.get("parent") or {}).get("key") if isinstance(extra.get("parent"), dict)
                   else extra.get("parent"))
    if i["parent"]:
        _get(s, i["parent"])
    if components:
        i["components"] = [c.strip() for c in components.split(",") if c.strip()]
    _apply_fields(s, i, {k: v for k, v in extra.items() if k != "parent"})
    return {"message": "Issue created successfully", "issue": _issue_json(s, i)}


@tool("jira_batch_create_issues")
def jira_batch_create_issues(ctx: Instance,
                             issues: Annotated[str, "JSON array string of issue objects: [{project_key, summary, issue_type, description?, assignee?, components?}]"],
                             validate_only: Annotated[bool | None, "If true, only validates the issues without creating them"] = False) -> dict[str, Any]:
    """Create multiple Jira issues in a batch"""
    try:
        items = json.loads(issues)
        assert isinstance(items, list)
    except (ValueError, AssertionError):
        raise _err("issues must be a JSON array string") from None
    created = []
    for it in items:
        if validate_only:
            if it.get("project_key", "").upper() not in ctx.state["projects"]:
                raise _err("", 400, project=f"Specify a valid project ID or key (got '{it.get('project_key')}')")
            continue
        r = jira_create_issue.fn(ctx, project_key=it.get("project_key", ""), summary=it.get("summary", ""),
                                 issue_type=it.get("issue_type", "Task"), assignee=it.get("assignee"),
                                 description=it.get("description"), components=it.get("components"))
        created.append(r["issue"]["key"])
    return {"message": "Issues validated successfully" if validate_only else f"Created {len(created)} issues",
            "issues": created}


@tool("jira_update_issue", idempotent=True)
def jira_update_issue(ctx: Instance,
                      issue_key: Annotated[str, "Jira issue key (e.g., 'PROJ-123')"],
                      fields: Annotated[str | None, "JSON string of fields to update, e.g. '{\"summary\": \"New title\", \"priority\": {\"name\": \"High\"}}'"] = None,
                      additional_fields: Annotated[str | None, "JSON string of additional fields to update"] = None,
                      components: Annotated[str | None, "Comma-separated list of component names"] = None,
                      attachments: Annotated[str | None, "Comma-separated file paths to attach"] = None,
                      attachments_base64: Annotated[str | None, "JSON array of base64 attachments"] = None,
                      transition: Annotated[str | None, "Transition name or ID to apply as part of the update"] = None,
                      comment: Annotated[str | None, "Comment to add as part of the update"] = None,
                      comment_visibility: Annotated[str | None, "JSON visibility restriction for the comment"] = None,
                      worklog: Annotated[str | None, "Time to log, e.g. '1h 30m'"] = None,
                      worklog_started: Annotated[str | None, "ISO start time for the worklog"] = None,
                      return_fields: Annotated[str | None, "Fields to return"] = "*all") -> dict[str, Any]:
    """Update an existing Jira issue including changing status, adding Epic links, updating fields, etc."""
    s = ctx.state
    i = _get(s, issue_key)
    _apply_fields(s, i, {**_fields(fields), **_fields(additional_fields)})
    if components is not None:
        i["components"] = [c.strip() for c in components.split(",") if c.strip()]
    if attachments:
        i.setdefault("attachments", []).extend(p.strip().rsplit("/", 1)[-1] for p in attachments.split(","))
    if transition:
        _transition(s, i, transition, ctx)
    if comment:
        _comment(ctx, i, s["me"], comment)
    if worklog:
        _worklog(ctx, i, worklog, None, worklog_started)
    i["updated"] = _iso(ctx)
    return {"message": "Issue updated successfully", "issue": _issue_json(s, i)}


@tool("jira_delete_issue", destructive=True)
def jira_delete_issue(ctx: Instance, issue_key: Annotated[str, "Jira issue key (e.g. PROJ-123)"]) -> dict[str, Any]:
    """Delete an existing Jira issue"""
    s = ctx.state
    i = _get(s, issue_key)
    if any(x.get("parent") == i["key"] for x in s["issues"].values()):
        raise _err("The issue has subtasks. Delete the subtasks first, or delete them together.", 400)
    del s["issues"][i["key"]]
    s["links"] = [l for l in s["links"] if i["key"] not in (l["inward"], l["outward"])]
    for x in s["issues"].values():
        if x["epic"] == i["key"]:
            x["epic"] = None
    return {"message": f"Issue {i['key']} has been deleted successfully."}


@tool("jira_assign_issue", idempotent=True)
def jira_assign_issue(ctx: Instance,
                      issue_key: Annotated[str, "Jira issue key (e.g., 'PROJ-123')"],
                      assignee: Annotated[str | None, "User to assign (email, display name, or account ID). Omit to unassign."] = None) -> dict[str, Any]:
    """Assign or unassign a Jira issue"""
    s = ctx.state
    i = _get(s, issue_key)
    i["assignee"] = _resolve_user(s, assignee)
    i["updated"] = _iso(ctx)
    return {"message": f"Issue {i['key']} assigned to {i['assignee'] or 'nobody'}", "issue": _issue_json(s, i, 0)}


def _transition(s: dict[str, Any], i: dict[str, Any], transition: str, ctx: Instance) -> None:
    options = WORKFLOW[i["status"]]
    t = next((t for t in options if transition in (t[0], t[1]) or transition.lower() in (t[1].lower(), t[2].lower())), None)
    if t is None:
        raise _err(f"Transition '{transition}' is not valid for issue {i['key']} in status '{i['status']}'. "
                   f"Available: " + ", ".join(f"{tid} ({name})" for tid, name, _ in options), 400)
    i["status"] = t[2]
    done = STATUSES[t[2]] == "done"
    i["resolution"] = "Done" if done else None
    i["resolutiondate"] = _iso(ctx) if done else None


@tool("jira_transition_issue")
def jira_transition_issue(ctx: Instance,
                          issue_key: Annotated[str, "Jira issue key (e.g., 'PROJ-123')"],
                          transition_id: Annotated[str, "ID of the transition to perform. Use jira_get_transitions to find valid IDs"],
                          fields: Annotated[str | None, "JSON string of fields to update during the transition"] = None,
                          comment: Annotated[str | None, "Comment to add during the transition"] = None) -> dict[str, Any]:
    """Transition a Jira issue to a new status"""
    s = ctx.state
    i = _get(s, issue_key)
    before = i["status"]
    _transition(s, i, str(transition_id), ctx)
    _apply_fields(s, i, _fields(fields))
    if comment:
        _comment(ctx, i, s["me"], comment)
    i["updated"] = _iso(ctx)
    return {"message": f"Issue {i['key']} transitioned from '{before}' to '{i['status']}'", "issue": _issue_json(s, i, 0)}


@tool("jira_get_transitions", read_only=True)
def jira_get_transitions(ctx: Instance, issue_key: Annotated[str, "Jira issue key (e.g., 'PROJ-123')"]) -> list[dict[str, Any]]:
    """Get available status transitions for a Jira issue"""
    i = _get(ctx.state, issue_key)
    return [{"id": tid, "name": name, "to_status": to} for tid, name, to in WORKFLOW[i["status"]]]


@tool("jira_add_comment")
def jira_add_comment(ctx: Instance,
                     issue_key: Annotated[str, "Jira issue key (e.g., 'PROJ-123')"],
                     body: Annotated[str, "Comment text in Markdown format"],
                     visibility: Annotated[str | None, "JSON string for restricted comments"] = None,
                     public: Annotated[bool | None, "JSM/Service Desk only: true = customer-visible"] = None) -> dict[str, Any]:
    """Add a comment to a Jira issue"""
    s = ctx.state
    i = _get(s, issue_key)
    if not body.strip():
        raise _err("", 400, comment="Comment body can not be empty!")
    c = _comment(ctx, i, s["me"], body)
    return {"success": True, "comment": {"id": c["id"], "body": c["body"], "created": c["created"],
                                         "author": _user_json(s, c["author"])}}


_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*([wdhm])")


def _worklog(ctx: Instance, i: dict[str, Any], time_spent: str, comment: str | None, started: str | None) -> dict[str, Any]:
    parts = _DURATION.findall(time_spent.lower())
    if not parts or _DURATION.sub("", time_spent.lower()).strip():
        raise _err("", 400, timeLogged="Worklog must not be null and must be in a valid format, e.g. '1h 30m'.")
    unit = {"w": 5 * 8 * 3600, "d": 8 * 3600, "h": 3600, "m": 60}
    seconds = int(sum(float(n) * unit[u] for n, u in parts))
    w = {"id": str(ctx.next("worklog_id", 10800)), "author": ctx.state["me"], "time_spent": time_spent, "seconds": seconds,
         "comment": comment, "started": started or _iso(ctx)}
    i["worklogs"].append(w)
    return w


@tool("jira_add_worklog")
def jira_add_worklog(ctx: Instance,
                     issue_key: Annotated[str, "Jira issue key (e.g., 'PROJ-123')"],
                     time_spent: Annotated[str, "Time spent, e.g. '1h 30m', '1d', '30m', '4h'"],
                     comment: Annotated[str | None, "Worklog comment in Markdown"] = None,
                     started: Annotated[str | None, "Start time in ISO format (defaults to now)"] = None,
                     original_estimate: Annotated[str | None, "New original estimate value"] = None,
                     remaining_estimate: Annotated[str | None, "New remaining estimate value"] = None) -> dict[str, Any]:
    """Add a worklog entry to a Jira issue"""
    s = ctx.state
    i = _get(s, issue_key)
    w = _worklog(ctx, i, time_spent, comment, started)
    return {"message": "Worklog added successfully", "worklog": {k: w[k] for k in ("id", "time_spent", "started", "comment")}}


@tool("jira_get_all_projects", read_only=True)
def jira_get_all_projects(ctx: Instance,
                          include_archived: Annotated[bool | None, "Whether to include archived projects"] = False) -> list[dict[str, Any]]:
    """Get all Jira projects accessible to the current user"""
    s = ctx.state
    return [{"id": p["id"], "key": p["key"], "name": p["name"], "lead": _user_json(s, p["lead"]),
             "url": f"{SITE}/browse/{p['key']}", "archived": p["archived"]}
            for p in s["projects"].values() if include_archived or not p["archived"]]


@tool("jira_get_project_issues", read_only=True)
def jira_get_project_issues(ctx: Instance,
                            project_key: Annotated[str, "The project key"],
                            limit: Annotated[int | None, "Maximum number of results (1-50)"] = 10,
                            start_at: Annotated[int | None, "Starting index for pagination (0-based)"] = 0) -> dict[str, Any]:
    """Get all issues for a specific Jira project"""
    if project_key.upper() not in ctx.state["projects"]:
        raise _err(f"The value '{project_key}' does not exist for the field 'project'.", 400)
    return jira_search.fn(ctx, jql=f"project = {project_key.upper()} ORDER BY created DESC", limit=limit, start_at=start_at)


@tool("jira_create_issue_link")
def jira_create_issue_link(ctx: Instance,
                           link_type: Annotated[str, "The type of link, e.g. 'Blocks', 'Relates', 'Duplicate', 'Cloners'"],
                           inward_issue_key: Annotated[str, "The key of the inward issue (e.g., 'PROJ-123')"],
                           outward_issue_key: Annotated[str, "The key of the outward issue (e.g., 'PROJ-456')"],
                           comment: Annotated[str | None, "Optional comment to add to the link"] = None) -> dict[str, Any]:
    """Create a link between two Jira issues"""
    s = ctx.state
    lt = next((k for k in LINK_TYPES if k.lower() == link_type.lower()), None)
    if lt is None:
        raise _err(f"No issue link type with name '{link_type}' found.", 404)
    a, b = _get(s, inward_issue_key), _get(s, outward_issue_key)
    if a["key"] == b["key"]:
        raise _err("You cannot link an issue to itself.", 400)
    if any(l["type"] == lt and l["inward"] == a["key"] and l["outward"] == b["key"] for l in s["links"]):
        return {"success": True, "message": "Link already exists"}
    s["links"].append({"id": str(ctx.next("link_id", 10900)), "type": lt, "inward": a["key"], "outward": b["key"]})
    if comment:
        _comment(ctx, a, s["me"], comment)
    return {"success": True, "message": f"Link created between {a['key']} and {b['key']}"}


@tool("jira_link_to_epic", idempotent=True)
def jira_link_to_epic(ctx: Instance,
                      issue_key: Annotated[str, "The key of the issue to link (e.g., 'PROJ-123')"],
                      epic_key: Annotated[str, "The key of the epic to link to (e.g., 'PROJ-456')"]) -> dict[str, Any]:
    """Link an existing issue to an epic"""
    s = ctx.state
    i, e = _get(s, issue_key), _get(s, epic_key)
    if e["issue_type"] != "Epic":
        raise _err(f"{e['key']} is not an Epic.", 400)
    if i["issue_type"] == "Epic":
        raise _err("An Epic can't be added to another Epic.", 400)
    i["epic"] = e["key"]
    i["updated"] = _iso(ctx)
    return {"message": f"Issue {i['key']} has been linked to epic {e['key']}.", "issue": _issue_json(s, i, 0)}


@tool("jira_get_user_profile", read_only=True)
def jira_get_user_profile(ctx: Instance,
                          user_identifier: Annotated[str, "Identifier for the user (email, display name, or account ID)"]) -> dict[str, Any]:
    """Retrieve profile information for a specific Jira user"""
    s = ctx.state
    try:
        uid = _resolve_user(s, user_identifier)
    except ToolError:
        raise _err(f"User '{user_identifier}' not found.", 404) from None
    u = s["users"][uid]
    return {"success": True, "user": {**_user_json(s, uid), "active": u["active"], "time_zone": "America/Los_Angeles"}}


@tool("jira_get_agile_boards", read_only=True, since=V1)
def jira_get_agile_boards(ctx: Instance,
                          board_name: Annotated[str | None, "(Optional) The name of board, support fuzzy search"] = None,
                          project_key: Annotated[str | None, "(Optional) Jira project key (e.g., 'PROJ-123')"] = None,
                          board_type: Annotated[str | None, "(Optional) The type of jira board (e.g., 'scrum', 'kanban')"] = None,
                          start_at: Annotated[int | None, "Starting index for pagination (0-based)"] = 0,
                          limit: Annotated[int | None, "Maximum number of results (1-50)"] = 10) -> list[dict[str, Any]]:
    """Get jira agile boards by name, project key, or type"""
    boards = [{"id": b["id"], "name": b["name"], "type": b["type"]} for b in ctx.state["boards"].values()
              if (not board_name or board_name.lower() in b["name"].lower())
              and (not project_key or b["project"] == project_key.upper())
              and (not board_type or b["type"] == board_type.lower())]
    return _paged(boards, start_at, limit)


@tool("jira_get_board_issues", read_only=True, since=V1)
def jira_get_board_issues(ctx: Instance,
                          board_id: Annotated[str, "The id of the board (e.g., '1001')"],
                          jql: Annotated[str, "JQL query string (Jira Query Language). Examples: 'assignee = currentUser() AND status = \"In Progress\"'"],
                          fields: Annotated[str | None, "Comma-separated fields to return, or '*all'"] = None,
                          start_at: Annotated[int | None, "Starting index for pagination (0-based)"] = 0,
                          limit: Annotated[int | None, "Maximum number of results (1-50)"] = 10,
                          expand: Annotated[str | None, "Optional fields to expand in the response (e.g., 'changelog')"] = "version") -> dict[str, Any]:
    """Get all issues linked to a specific board filtered by JQL"""
    b = _board(ctx.state, board_id)
    q = f"project = {b['project']}" + (f" AND ({jql})" if jql.strip() and not jql.strip().lower().startswith("order by")
                                       else "") + (f" {jql}" if jql.strip().lower().startswith("order by") else "")
    return jira_search.fn(ctx, jql=q, limit=limit, start_at=start_at)


@tool("jira_get_sprints_from_board", read_only=True, since=V1)
def jira_get_sprints_from_board(ctx: Instance,
                                board_id: Annotated[str, "The id of board (e.g., '1000')"],
                                state: Annotated[str | None, "Sprint state (e.g., 'active', 'future', 'closed')"] = None,
                                start_at: Annotated[int | None, "Starting index for pagination (0-based)"] = 0,
                                limit: Annotated[int | None, "Maximum number of results (1-50)"] = 10) -> list[dict[str, Any]]:
    """Get jira sprints from board by state"""
    b = _board(ctx.state, board_id)
    if b["type"] != "scrum":
        raise _err("The board does not support sprints", 400)
    want = {x.strip().lower() for x in (state or "").split(",") if x.strip()}
    sprints = [_sprint_json(sp) for sp in ctx.state["sprints"].values()
               if sp["origin_board_id"] == b["id"] and (not want or sp["state"] in want)]
    return _paged(sprints, start_at, limit)


@tool("jira_get_sprint_issues", read_only=True, since=V1)
def jira_get_sprint_issues(ctx: Instance,
                           sprint_id: Annotated[str, "The id of sprint (e.g., '10001')"],
                           fields: Annotated[str | None, "Comma-separated fields to return, or '*all'"] = None,
                           start_at: Annotated[int | None, "Starting index for pagination (0-based)"] = 0,
                           limit: Annotated[int | None, "Maximum number of results (1-50)"] = 10) -> dict[str, Any]:
    """Get jira issues from sprint"""
    sp = _sprint(ctx.state, sprint_id)
    return jira_search.fn(ctx, jql=f"sprint = {sp['id']} ORDER BY key ASC", limit=limit, start_at=start_at)


@tool("jira_create_sprint", since=V1)
def jira_create_sprint(ctx: Instance,
                       board_id: Annotated[str, "The id of board (e.g., '1000')"],
                       sprint_name: Annotated[str, "Name of the sprint (e.g., 'Sprint 1')"],
                       start_date: Annotated[str, "Start time for sprint (ISO 8601 format)"],
                       end_date: Annotated[str, "End time for sprint (ISO 8601 format)"],
                       goal: Annotated[str | None, "(Optional) Goal of the sprint"] = None) -> dict[str, Any]:
    """Create Jira sprint for a board"""
    s = ctx.state
    b = _board(s, board_id)
    if b["type"] != "scrum":
        raise _err("The board does not support sprints", 400)
    if not sprint_name.strip():
        raise _err("", 400, name="Sprint name is required.")
    if _date_iso(end_date) <= _date_iso(start_date):
        raise _err("", 400, endDate="The sprint end date must be after the start date.")
    return _sprint_json(_new_sprint(ctx, s, b["id"], sprint_name, start_date, end_date, goal))


@tool("jira_update_sprint", since=V1)
def jira_update_sprint(ctx: Instance,
                       sprint_id: Annotated[str, "The id of sprint (e.g., '10001')"],
                       sprint_name: Annotated[str | None, "(Optional) New name for the sprint"] = None,
                       state: Annotated[str | None, "(Optional) New state for the sprint (future|active|closed)"] = None,
                       start_date: Annotated[str | None, "(Optional) New start date for the sprint"] = None,
                       end_date: Annotated[str | None, "(Optional) New end date for the sprint"] = None,
                       goal: Annotated[str | None, "(Optional) New goal for the sprint"] = None,
                       move_incomplete_to: Annotated[str | None, "(Optional) When closing: id of a future sprint for unfinished issues (default: the backlog)"] = None) -> dict[str, Any]:
    """Update jira sprint"""
    s = ctx.state
    sp = _sprint(s, sprint_id)
    if sp["state"] == "closed":
        raise _err("Cannot update a closed sprint.", 400)
    if sprint_name is not None:
        sp["name"] = sprint_name
    if goal is not None:
        sp["goal"] = goal
    if start_date:
        sp["start_date"] = _date_iso(start_date)
    if end_date:
        sp["end_date"] = _date_iso(end_date)
    new = (state or sp["state"]).lower()
    if new not in ("future", "active", "closed"):
        raise _err("", 400, state=f"Invalid sprint state '{state}'. Use future, active or closed.")
    if new == "active" and sp["state"] == "future":
        if not sp["start_date"] or not sp["end_date"]:
            raise _err("", 400, startDate="A sprint needs a start and end date to be started.")
        if any(o["state"] == "active" and o["origin_board_id"] == sp["origin_board_id"] for o in s["sprints"].values()):
            raise _err("Sprint cannot be started: another sprint is already active on this board. Complete it first.", 400)
        sp.update(state="active", activated_date=_iso(ctx).replace("+0000", "Z"))
    elif new == "closed" and sp["state"] != "closed":
        if sp["state"] != "active":
            raise _err("Sprint must be active to be completed.", 400)
        target = _sprint(s, move_incomplete_to) if move_incomplete_to else None
        if target is not None and (target["state"] != "future" or target["origin_board_id"] != sp["origin_board_id"]):
            raise _err("", 400, move_incomplete_to="Unfinished issues can only move to a future sprint on the same board.")
        for i in s["issues"].values():
            if i.get("sprint") == sp["id"]:
                i["closed_sprints"].append(sp["id"])
                i["sprint"] = None if STATUSES[i["status"]] == "done" or target is None else target["id"]
                i["updated"] = _iso(ctx)
        sp.update(state="closed", complete_date=_iso(ctx).replace("+0000", "Z"))
    elif new == "future" and sp["state"] == "active":
        raise _err("An active sprint can't be moved back to future.", 400)
    return _sprint_json(sp)
