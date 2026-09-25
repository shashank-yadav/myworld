import json

from toolsim.core.instance import Instance
from toolsim.services import get_service


def svc(name, **kw):
    i = Instance(get_service(name), **kw)
    call = lambda tool, **a: i.call(tool, a)  # noqa: E731
    return i, call


# -- gmail --------------------------------------------------------------------------------

def test_gmail_search_syntax():
    i, call = svc("gmail")
    subjects = lambda q: [l[9:] for l in call("search_emails", query=q).text.splitlines() if l.startswith("Subject: ")]  # noqa: E731
    assert subjects("from:john is:unread") == ["Q4 planning"]
    assert subjects("in:sent") == ["Launch checklist"]
    assert subjects("has:attachment") == ["Your receipt from Acme Cloud"]
    assert set(subjects("Q4 OR receipt")) == {"Q4 planning", "Your receipt from Acme Cloud"}
    assert subjects("is:unread -category:promotions") == ["Q4 planning"]
    assert subjects('subject:"launch checklist"') == ["Re: Launch checklist", "Launch checklist"]


def test_gmail_labels_threads_and_errors():
    i, call = svc("gmail")
    q4 = next(m for m in i.state["mailboxes"]["alex@acme.com"]["messages"].values() if m["subject"] == "Q4 planning")
    assert "Thread ID" in call("read_email", messageId=q4["id"]).text
    assert call("read_email", messageId="nope").text == "Error: Requested entity was not found."
    assert call("modify_email", messageId=q4["id"], removeLabelIds=["UNREAD"]).is_error is False
    assert "UNREAD" not in i.state["mailboxes"]["alex@acme.com"]["messages"][q4["id"]]["labelIds"]
    assert call("modify_email", messageId=q4["id"], addLabelIds=["Label_999"]).is_error
    r = call("send_email", to=["john@acme.com"], subject="Re: Q4 planning", body="Tue?", threadId=q4["threadId"])
    reply = i.state["mailboxes"]["alex@acme.com"]["messages"][r.text.rsplit(" ", 1)[-1]]
    assert reply["threadId"] == q4["threadId"] and reply["labelIds"] == ["SENT"]
    assert call("send_email", to=["not-an-address"], subject="x", body="y").is_error
    assert call("create_label", name="Clients").is_error  # already exists
    assert "found existing" in call("get_or_create_label", name="clients").text
    assert call("delete_label", id="INBOX").is_error  # system labels are protected


def test_gmail_attachment_download_is_simulated():
    i, call = svc("gmail")
    m = next(m for m in i.state["mailboxes"]["alex@acme.com"]["messages"].values() if m["attachments"])
    r = call("download_attachment", messageId=m["id"], attachmentId=m["attachments"][0]["attachmentId"], savePath="/tmp/x")
    assert "Saved to: /tmp/x/receipt-1847-2213.pdf" in r.text
    assert i.state["mailboxes"]["alex@acme.com"]["downloads"][0]["path"] == "/tmp/x/receipt-1847-2213.pdf"


# -- calendar -----------------------------------------------------------------------------

def test_calendar_freebusy_and_timezones():
    i, call = svc("calendar")
    fb = json.loads(call("get-freebusy", calendars=[{"id": "primary"}, {"id": "john@acme.com"}, {"id": "ghost@x.io"}],
                         timeMin="2026-09-22T08:00:00", timeMax="2026-09-22T18:00:00").text)
    assert fb["calendars"]["john@acme.com"]["busy"] == [{"start": "2026-09-22T09:00:00-07:00", "end": "2026-09-22T10:00:00-07:00"}]
    assert fb["calendars"]["primary"]["busy"][0]["start"].startswith("2026-09-22T11:00")
    assert fb["calendars"]["ghost@x.io"]["errors"][0]["reason"] == "notFound"
    ev = json.loads(call("create-event", summary="Sync", start="2026-09-22T17:00:00Z", end="2026-09-22T17:30:00Z").text)["event"]
    assert ev["start"]["dateTime"] == "2026-09-22T10:00:00-07:00"  # stored in the calendar's zone
    assert call("get-current-time").text.count("Monday") == 1


def test_calendar_permissions_and_lifecycle():
    i, call = svc("calendar")
    assert "writer access" in call("create-event", calendarId="team@acme.com", summary="x",
                                   start="2026-09-22T10:00:00", end="2026-09-22T11:00:00").text
    assert "free/busy access only" in call("list-events", calendarId="john@acme.com").text
    assert "time range is empty" in call("create-event", summary="x", start="2026-09-22T10:00:00",
                                         end="2026-09-22T09:00:00").text
    ev = json.loads(call("create-event", summary="Q4", start="2026-09-22T10:00:00", end="2026-09-22T10:30:00",
                         attendees=[{"email": "john@acme.com"}]).text)["event"]
    assert [a["email"] for a in ev["attendees"]] == ["alex@acme.com", "john@acme.com"]
    moved = json.loads(call("update-event", eventId=ev["id"], start="2026-09-22T14:00:00", end="2026-09-22T14:30:00").text)
    assert moved["event"]["sequence"] == 1
    assert not call("delete-event", eventId=ev["id"]).is_error
    assert "deleted" in call("delete-event", eventId=ev["id"]).text  # 410 Gone
    assert "Q4" not in call("list-events", timeMin="2026-09-22T00:00:00", timeMax="2026-09-23T00:00:00").text


# -- slack --------------------------------------------------------------------------------

def test_slack_membership_rules():
    i, call = svc("slack")
    chans = {c["name"]: c["id"] for c in json.loads(call("slack_list_channels").text)["channels"]}
    assert "leadership" not in chans  # private and the bot isn't in it
    assert json.loads(call("slack_post_message", channel_id=chans["random"], text="hi").text)["error"] == "not_in_channel"
    assert json.loads(call("slack_get_channel_history", channel_id=chans["random"]).text)["error"] == "not_in_channel"
    assert json.loads(call("slack_post_message", channel_id="C_NOPE", text="hi").text)["error"] == "channel_not_found"
    assert json.loads(call("slack_post_message", channel_id=chans["general"], text="  ").text)["error"] == "no_text"
    ok = json.loads(call("slack_post_message", channel_id=chans["general"], text="Deploy done").text)
    assert ok["ok"] and ok["message"]["bot_id"]


def test_slack_threads_reactions_pagination():
    i, call = svc("slack")
    general = next(c["id"] for c in i.state["channels"].values() if c["name"] == "general")
    parent = next(m for m in i.state["messages"][general] if m.get("reply_count"))
    call("slack_reply_to_thread", channel_id=general, thread_ts=parent["ts"], text="+1 from the bot")
    thread = json.loads(call("slack_get_thread_replies", channel_id=general, thread_ts=parent["ts"]).text)["messages"]
    assert thread[0]["reply_count"] == 3 and thread[-1]["text"] == "+1 from the bot"
    top = json.loads(call("slack_get_channel_history", channel_id=general).text)["messages"]
    assert all(not m.get("parent_user_id") for m in top)  # replies stay in the thread
    assert json.loads(call("slack_add_reaction", channel_id=general, timestamp=parent["ts"], reaction=":tada:").text)["ok"]
    assert json.loads(call("slack_add_reaction", channel_id=general, timestamp=parent["ts"], reaction="tada").text)["error"] == "already_reacted"
    assert json.loads(call("slack_add_reaction", channel_id=general, timestamp=parent["ts"], reaction="notanemoji").text)["error"] == "invalid_name"
    page1 = json.loads(call("slack_get_users", limit=2).text)
    page2 = json.loads(call("slack_get_users", limit=2, cursor=page1["response_metadata"]["next_cursor"]).text)
    assert {u["id"] for u in page1["members"]}.isdisjoint({u["id"] for u in page2["members"]})


# -- github -------------------------------------------------------------------------------

O = {"owner": "acme", "repo": "api"}


def test_github_files_and_blob_shas():
    from toolsim.services.github import blob_sha
    i, call = svc("github")
    f = json.loads(call("get_file_contents", path="README.md", **O).text)
    assert f["sha"] == blob_sha("# Acme API\n\nPayments and billing API.\n")
    import subprocess
    git = subprocess.run(["git", "hash-object", "--stdin"], input=b"# Acme API\n\nPayments and billing API.\n",
                         capture_output=True).stdout.decode().strip()
    assert f["sha"] == git, "blob SHAs match real git"
    assert "wasn't supplied" in call("create_or_update_file", path="README.md", content="x", message="m", branch="main", **O).text
    assert "does not match" in call("create_or_update_file", path="README.md", content="x", message="m", branch="main",
                                    sha="0" * 40, **O).text
    listing = json.loads(call("get_file_contents", path="", **O).text)
    assert [e["name"] for e in listing][:2] == ["src", "tests"]


def test_github_pull_request_lifecycle():
    i, call = svc("github")
    pr = json.loads(call("get_pull_request", pull_number=4, **O).text)
    assert pr["mergeable_state"] == "clean" and pr["head"]["ref"] == "fix/flaky-retry"
    assert "already exists" in call("create_pull_request", title="dup", head="fix/flaky-retry", base="main", **O).text
    call("create_branch", branch="empty", **O)
    assert "No commits between" in call("create_pull_request", title="x", head="empty", base="main", **O).text
    status = json.loads(call("get_pull_request_status", pull_number=4, **O).text)
    assert status["state"] == "success"
    merged = json.loads(call("merge_pull_request", pull_number=4, merge_method="squash", **O).text)
    assert merged["merged"]
    assert "not mergeable" in call("merge_pull_request", pull_number=4, **O).text
    readme = json.loads(call("get_file_contents", path="src/retry.py", **O).text)
    import base64
    assert "rng.random()" in base64.b64decode(readme["content"]).decode()
    assert json.loads(call("get_issue", issue_number=4, **O).text)["state"] == "closed"


def test_github_conflicts_and_required_checks():
    i, call = svc("github")
    # change the same file on main: the PR now conflicts
    f = json.loads(call("get_file_contents", path="src/retry.py", **O).text)
    call("create_or_update_file", path="src/retry.py", content="# rewritten\n", message="rewrite", branch="main",
         sha=f["sha"], **O)
    assert json.loads(call("get_pull_request", pull_number=4, **O).text)["mergeable_state"] == "dirty"
    assert "not mergeable" in call("merge_pull_request", pull_number=4, **O).text
    # a PR whose required check never ran is blocked
    call("create_branch", branch="docs", **O)
    call("push_files", branch="docs", files=[{"path": "docs/x.md", "content": "hi"}], message="docs", **O)
    pr = json.loads(call("create_pull_request", title="Docs", head="docs", base="main", **O).text)
    assert pr["mergeable_state"] == "blocked"
    blocked = json.loads(call("merge_pull_request", pull_number=pr["number"], **O).text)
    assert blocked["message"] == 'Required status check "ci" is expected.' and blocked["status"] == "405"


def test_github_issues_search_and_reviews():
    i, call = svc("github")
    listed = json.loads(call("list_issues", **O).text)
    assert any("pull_request" in x for x in listed), "like the real API, PRs appear in the issues list"
    assert json.loads(call("search_issues", q="repo:acme/api is:issue is:open label:bug").text)["total_count"] == 1
    assert json.loads(call("search_code", q="rng repo:acme/api").text)["total_count"] == 0  # only default branch
    assert json.loads(call("search_code", q="backoff repo:acme/api").text)["total_count"] >= 2
    rev = json.loads(call("create_pull_request_review", pull_number=4, body="LGTM", event="APPROVE", **O).text)
    assert rev["state"] == "APPROVED"
    call("create_branch", branch="mine", **O)
    call("push_files", branch="mine", files=[{"path": "a.txt", "content": "a"}], message="a", **O)
    mine = json.loads(call("create_pull_request", title="Mine", head="mine", base="main", **O).text)
    assert "your own pull request" in call("create_pull_request_review", pull_number=mine["number"], body="ok",
                                           event="APPROVE", **O).text
