"""Realism features added in the 2026-09-25.1 versions (older versions must not change)."""

import json

from myworld.core.instance import Instance
from myworld.services import get_service

V0, V1 = "2026-09-25", "2026-09-25.1"


def inbox(g: Instance, box: str = "alex@acme.com") -> list[dict]:
    return sorted(g.state["mailboxes"][box]["messages"].values(), key=lambda m: int(m["internalDate"]))


# -- gmail --------------------------------------------------------------------------------

def gmail(version=V1, **seed):
    svc = get_service("gmail")
    return Instance(svc, {**svc.default_seed(), **seed}, version=version)


def test_gmail_bounces_unknown_and_typo_recipients():
    g = gmail()
    for to in ("jhon@acme.com", "priya@acme.co", "someone@gmial.com", "john@acme.com", "friend@example.org"):
        assert not g.call("send_email", {"to": [to], "subject": "Hi", "body": "."}).is_error
    g.advance(600)
    g.call("list_email_labels", {})
    bounces = [m for m in inbox(g) if "mailer-daemon" in m["from"]]
    assert len(bounces) == 3
    assert {b["subject"] for b in bounces} == {"Delivery Status Notification (Failure)"}
    assert any("jhon@acme.com" in b["body"] for b in bounces)
    assert any("domain acme.co couldn't be found" in b["body"] for b in bounces)


def test_gmail_bounce_arrives_later_and_threads_with_the_original():
    g = gmail()
    g.call("send_email", {"to": ["jhon@acme.com"], "subject": "Q4", "body": "."})
    assert not any("mailer-daemon" in m["from"] for m in inbox(g)), "bounces aren't instant"
    g.advance(600)
    g.call("list_email_labels", {})
    sent = next(m for m in inbox(g) if m["subject"] == "Q4")
    bounce = next(m for m in inbox(g) if "mailer-daemon" in m["from"])
    assert bounce["threadId"] == sent["threadId"]
    assert 20_000 <= int(bounce["internalDate"]) - int(sent["internalDate"]) <= 121_000, "stamped at its due time"


def test_gmail_out_of_office_once_per_sender_and_rule_replies():
    g = gmail(auto_replies=[
        {"person": "john@acme.com", "out_of_office": "I'm out until Monday."},
        {"person": "priya@acme.com", "match": {"subject~": "checklist"}, "reply": "Both done now.", "delay": "10m"}])
    for _ in range(2):
        g.call("send_email", {"to": ["john@acme.com"], "subject": "Q4", "body": "."})
    g.call("send_email", {"to": ["priya@acme.com"], "subject": "Launch checklist status?", "body": "."})
    g.call("send_email", {"to": ["priya@acme.com"], "subject": "Lunch?", "body": "."})
    g.advance(3600)
    g.call("list_email_labels", {})
    got = [(m["from"], m["subject"]) for m in inbox(g) if "INBOX" in m["labelIds"]]
    assert ("John Park <john@acme.com>", "Automatic reply: Q4") in got
    assert sum(s == "Automatic reply: Q4" for _, s in got) == 1, "out-of-office answers each sender once"
    assert ("Priya Shah <priya@acme.com>", "Re: Launch checklist status?") in got
    assert not any(s == "Re: Lunch?" for _, s in got), "rules only answer what they match"


def test_gmail_daily_send_quota():
    g = gmail(daily_send_limit=2)
    assert not g.call("send_email", {"to": ["john@acme.com", "priya@acme.com"], "subject": "a", "body": "."}).is_error
    r = g.call("send_email", {"to": ["john@acme.com"], "subject": "b", "body": "."})
    assert r.is_error and "Daily user sending limit exceeded" in r.text
    g.advance(86400)
    assert not g.call("send_email", {"to": ["john@acme.com"], "subject": "c", "body": "."}).is_error, "resets daily"


def test_gmail_old_version_is_unchanged():
    g = gmail(version=V0, daily_send_limit=0)
    assert not g.call("send_email", {"to": ["jhon@acme.com"], "subject": "Hi", "body": "."}).is_error
    g.advance(600)
    g.call("list_email_labels", {})
    assert not any("mailer-daemon" in m["from"] for m in inbox(g))


def test_scheduled_replies_survive_snapshot_restore():
    g = gmail()
    snap = g.snapshot()
    g.call("send_email", {"to": ["jhon@acme.com"], "subject": "Hi", "body": "."})
    assert g.pending
    g.restore(snap)
    assert not g.pending
    g.advance(600)
    g.call("list_email_labels", {})
    assert not any("mailer-daemon" in m["from"] for m in inbox(g))


def _json(r):
    assert not r.is_error, r.text
    return json.loads(r.text)


# -- calendar -----------------------------------------------------------------------------

def cal(version=V1, **seed):
    svc = get_service("calendar")
    return Instance(svc, {**svc.default_seed(), **seed}, version=version)


def starts(c, lo="2026-09-21T00:00:00", hi="2026-12-31T00:00:00", q=None):
    args = {"timeMin": lo, "timeMax": hi}
    r = c.call("search-events", {"query": q, **args}) if q else c.call("list-events", args)
    return [e["start"]["dateTime"][:16] for e in _json(r)["events"]]


def series(c, rule, start="2026-09-21T15:00:00", end="2026-09-21T15:30:00", **kw):
    return _json(c.call("create-event", {"summary": "Sync", "start": start, "end": end,
                                         "recurrence": rule if isinstance(rule, list) else [rule], **kw}))["event"]["id"]


def test_calendar_rrules_expand():
    c = cal()
    series(c, ["RRULE:FREQ=WEEKLY;BYDAY=MO,WE;COUNT=4", "EXDATE;TZID=America/Los_Angeles:20260923T150000"])
    assert starts(c, q="sync") == ["2026-09-21T15:00", "2026-09-28T15:00", "2026-09-30T15:00"], "COUNT counts the EXDATE"
    c = cal()
    series(c, "RRULE:FREQ=MONTHLY;BYDAY=-1FR;UNTIL=20261231T235959Z", start="2026-09-25T10:00:00", end="2026-09-25T11:00:00")
    assert starts(c, q="sync") == ["2026-09-25T10:00", "2026-10-30T10:00", "2026-11-27T10:00", "2026-12-25T10:00"]
    c = cal()
    series(c, "RRULE:FREQ=DAILY;INTERVAL=2;COUNT=3", start="2026-10-31T09:00:00", end="2026-10-31T09:30:00")
    got = _json(c.call("search-events", {"query": "sync"}))["events"]
    assert [e["start"]["dateTime"] for e in got] == ["2026-10-31T09:00:00-07:00", "2026-11-02T09:00:00-08:00",
                                                    "2026-11-04T09:00:00-08:00"], "wall-clock time across DST"


def test_calendar_rejects_bad_rules():
    c = cal()
    for rule in ("RRULE:FREQ=SOMETIMES", "RRULE:FREQ=WEEKLY;COUNT=2;UNTIL=20261001T000000Z", "RRULE:FREQ=WEEKLY;BYDAY=XX"):
        r = c.call("create-event", {"summary": "x", "start": "2026-09-22T10:00:00", "end": "2026-09-22T11:00:00",
                                    "recurrence": [rule]})
        assert r.is_error and "Invalid recurrence rule" in r.text
    assert not c.state["events"].get("x")


def test_calendar_instances_edit_delete_and_split():
    c = cal()
    sid = series(c, "RRULE:FREQ=WEEKLY;COUNT=5", attendees=[{"email": "john@acme.com"}])
    inst = [e for e in _json(c.call("list-events", {"timeMin": "2026-09-21T00:00:00", "timeMax": "2026-11-01T00:00:00"}))["events"]
            if e.get("recurringEventId") == sid]
    assert inst[1]["id"] == f"{sid}_20260928T220000Z" and inst[1]["originalStartTime"]["dateTime"].startswith("2026-09-28T15")
    assert _json(c.call("get-event", {"eventId": inst[1]["id"]}))["event"]["summary"] == "Sync"
    # one instance moves; the rest stay
    _json(c.call("update-event", {"eventId": sid, "modificationScope": "thisEventOnly",
                                  "originalStartTime": "2026-09-28T15:00:00", "start": "2026-09-29T09:00:00",
                                  "end": "2026-09-29T09:30:00"}))
    assert starts(c, q="sync") == ["2026-09-21T15:00", "2026-09-29T09:00", "2026-10-05T15:00", "2026-10-12T15:00",
                                   "2026-10-19T15:00"]
    # an instance id edits only that instance; other scopes need the series id
    assert c.call("update-event", {"eventId": inst[2]["id"], "modificationScope": "all", "summary": "x"}).is_error
    # delete one instance, twice
    assert not c.call("delete-event", {"eventId": inst[2]["id"]}).is_error
    r = c.call("delete-event", {"eventId": inst[2]["id"]})
    assert r.is_error and "Resource has been deleted" in r.text
    assert len(starts(c, q="sync")) == 4
    # this and following
    _json(c.call("update-event", {"eventId": sid, "modificationScope": "thisAndFollowing",
                                  "futureStartDate": "2026-10-12", "location": "Room 9"}))
    evs = _json(c.call("search-events", {"query": "sync", "timeMin": "2026-09-21T00:00:00", "timeMax": "2026-12-01T00:00:00"}))["events"]
    assert [e.get("location") for e in evs] == [None, None, "Room 9", "Room 9"]
    assert len({e["recurringEventId"] for e in evs}) == 2, "the series was split in two"


def test_calendar_freebusy_and_grading_see_every_instance():
    c = cal()
    series(c, "RRULE:FREQ=DAILY;COUNT=10", start="2026-09-21T13:00:00", end="2026-09-21T14:00:00",
           attendees=[{"email": "john@acme.com"}])
    fb = _json(c.call("get-freebusy", {"calendars": [{"id": "primary"}], "timeMin": "2026-09-25T00:00:00",
                                       "timeMax": "2026-09-26T00:00:00"}))
    assert fb["calendars"]["primary"]["busy"] == [{"start": "2026-09-25T13:00:00-07:00", "end": "2026-09-25T14:00:00-07:00"}]
    ev = next(e for e in c.service.grading_view(c.state)["events"] if e["summary"] == "Sync")
    assert ev["conflicts_for"] == ["john@acme.com"], "Wed 23rd's instance clashes with John's afternoon block"


def test_calendar_colleagues_answer_invites_by_policy():
    c = cal(people=[{"email": "priya@acme.com", "name": "Priya Shah", "auto_respond": "if_free", "respond_after": "5m"}],
            auto_respond={"john@acme.com": "accept", "sam@acme.com": "tentative"})
    # Priya is in Design review 11-12 on the 22nd; John accepts everything
    eid = _json(c.call("create-event", {"summary": "Clash", "start": "2026-09-22T11:30:00", "end": "2026-09-22T12:00:00",
                                        "attendees": [{"email": "priya@acme.com"}, {"email": "john@acme.com"},
                                                      {"email": "ext@other.io"}]}))["event"]["id"]
    ok = _json(c.call("create-event", {"summary": "Free", "start": "2026-09-22T14:00:00", "end": "2026-09-22T14:30:00",
                                       "attendees": [{"email": "priya@acme.com"}]}))["event"]["id"]
    status = lambda e: {a["email"]: a["responseStatus"] for a in c.state["events"][e]["attendees"]}  # noqa: E731
    assert status(eid)["priya@acme.com"] == "needsAction", "nobody answers instantly"
    c.advance(3600)
    c.call("get-current-time", {})
    assert status(eid) == {"alex@acme.com": "accepted", "priya@acme.com": "declined", "john@acme.com": "accepted",
                           "ext@other.io": "needsAction"}
    assert status(ok)["priya@acme.com"] == "accepted"


def test_calendar_old_version_is_unchanged():
    c = cal(version=V0)
    series(c, "RRULE:FREQ=WEEKLY;COUNT=5")
    assert starts(c, q="sync") == ["2026-09-21T15:00"], "2026-09-25 never expanded recurrences"
    assert "modificationScope" not in json.dumps(c.list_tools())


# -- slack --------------------------------------------------------------------------------

def slack(version=V1, **seed):
    svc = get_service("slack")
    return Instance(svc, {**svc.default_seed(), **seed}, version=version)


def uid(s, name):
    return next(u["id"] for u in s.state["users"].values() if u["name"] == name)


def cid(s, name):
    return next(c["id"] for c in s.state["channels"].values() if c["name"] == name)


def test_slack_dm_by_user_id():
    s = slack()
    r = _json(s.call("slack_post_message", {"channel_id": uid(s, "john"), "text": "Got a minute?"}))
    assert r["channel"].startswith("D")
    again = _json(s.call("slack_post_message", {"channel_id": uid(s, "john"), "text": "ping"}))
    assert again["channel"] == r["channel"], "one DM per pair"
    assert len(_json(s.call("slack_get_channel_history", {"channel_id": r["channel"]}))["messages"]) == 2
    assert r["channel"] not in s.call("slack_list_channels", {}).text, "DMs aren't channels"
    view = s.service.grading_view(s.state)
    assert {m["channel"] for m in view["messages"] if m["is_dm"]} == {"dm:john"}


def test_slack_only_posting_accepts_channel_names():
    s = slack()
    assert not s.call("slack_post_message", {"channel_id": "#general", "text": "hi"}).is_error
    for tool, args in [("slack_get_channel_history", {}), ("slack_reply_to_thread", {"thread_ts": "1.1", "text": "x"}),
                       ("slack_add_reaction", {"timestamp": "1.1", "reaction": "eyes"})]:
        r = s.call(tool, {"channel_id": "general", **args})
        assert r.is_error and "channel_not_found" in r.text, tool
    old = slack(version=V0)
    assert not old.call("slack_get_channel_history", {"channel_id": "general"}).is_error
    assert old.call("slack_post_message", {"channel_id": uid(old, "john"), "text": "x"}).is_error


def test_slack_responders_need_real_mentions_and_reply_later():
    s = slack(responders=[{"user": "john", "when": {"mention": True}, "reply": "Looking now", "delay": "2m"},
                          {"user": "priya", "reply": "It's out", "times": 2}])
    oncall = cid(s, "api-oncall")
    s.call("slack_post_message", {"channel_id": oncall, "text": "@john can you look at the 500s?"})
    s.advance(600)
    s.call("slack_get_users", {})
    assert not any(m["user"] == uid(s, "john") and m["text"] == "Looking now" for m in s.state["messages"][oncall]), \
        "a plain @john notifies nobody"
    ts = _json(s.call("slack_post_message", {"channel_id": oncall, "text": f"<@{uid(s, 'john')}> can you look?"}))["ts"]
    assert _json(s.call("slack_get_thread_replies", {"channel_id": oncall, "thread_ts": ts}))["messages"][1:] == []
    s.advance(120)
    replies = _json(s.call("slack_get_thread_replies", {"channel_id": oncall, "thread_ts": ts}))["messages"][1:]
    assert [m["text"] for m in replies] == ["Looking now"]
    dm = _json(s.call("slack_post_message", {"channel_id": uid(s, "priya"), "text": "Is v2.4 out?"}))["channel"]
    s.advance(60)
    hist = _json(s.call("slack_get_channel_history", {"channel_id": dm}))["messages"]
    assert hist[0]["text"] == "It's out" and not hist[0].get("thread_ts"), "DMs answer at the top level"


# -- github -------------------------------------------------------------------------------

O = {"owner": "acme", "repo": "api"}


def gh(version=V1, protected=None):
    svc = get_service("github")
    seed = svc.default_seed()
    if protected is not None:
        seed["repos"][0]["protected"] = protected
    return Instance(svc, seed, version=version)


def branch_pr(g, name, files, body=None):
    g.call("create_branch", {**O, "branch": name})
    _json(g.call("push_files", {**O, "branch": name, "message": name, "files": files}))
    return _json(g.call("create_pull_request", {**O, "title": name, "head": name, "base": "main", "body": body}))["number"]


def test_github_protected_branch_rejects_direct_pushes():
    g = gh()
    r = g.call("push_files", {**O, "branch": "main", "files": [{"path": "a", "content": "a"}], "message": "m"})
    assert r.is_error and "Changes must be made through a pull request" in r.text
    assert not g.call("push_files", {**O, "branch": "fix/flaky-retry", "files": [{"path": "a", "content": "a"}],
                                     "message": "m"}).is_error, "unprotected branches take pushes"
    old = gh(version=V0)
    assert not old.call("push_files", {**O, "branch": "main", "files": [{"path": "a", "content": "a"}], "message": "m"}).is_error


def test_github_ci_runs_on_push_and_can_fail():
    g = gh()
    ok = branch_pr(g, "docs", [{"path": "docs/a.md", "content": "hi"}])
    bad = branch_pr(g, "wip", [{"path": "tests/test_wip.py", "content": "def test():\n    assert False\n"}])
    status = lambda n: _json(g.call("get_pull_request_status", {**O, "pull_number": n}))  # noqa: E731
    assert status(ok)["state"] == "pending"
    assert "in progress" in g.call("merge_pull_request", {**O, "pull_number": ok}).text
    g.advance(240)
    assert status(ok)["state"] == "success"
    assert status(bad)["state"] == "failure" and "tests/test_wip.py" in status(bad)["statuses"][0]["description"]
    assert "is failing" in g.call("merge_pull_request", {**O, "pull_number": bad}).text


def test_github_required_approvals_and_stale_reviews():
    g = gh(protected={"main": {"required_checks": ["ci"], "required_approvals": 1, "dismiss_stale_reviews": True}})
    n = branch_pr(g, "docs", [{"path": "docs/a.md", "content": "hi"}])
    g.advance(240)
    assert _json(g.call("get_pull_request", {**O, "pull_number": n}))["mergeable_state"] == "blocked"
    assert "approving review is required" in g.call("merge_pull_request", {**O, "pull_number": n}).text
    approve = {**O, "pull_number": n, "body": "ok", "event": "APPROVE"}
    _json(g.call("create_pull_request_review", approve, as_="john-park"))
    _json(g.call("push_files", {**O, "branch": "docs", "message": "more", "files": [{"path": "docs/b.md", "content": "b"}]}))
    g.advance(240)
    assert "approving review is required" in g.call("merge_pull_request", {**O, "pull_number": n}).text, \
        "a push dismisses the stale approval"
    _json(g.call("create_pull_request_review", approve, as_="john-park"))
    assert _json(g.call("merge_pull_request", {**O, "pull_number": n}))["merged"]


def test_github_closing_keywords_close_issues_on_merge():
    g = gh()
    n = branch_pr(g, "fix", [{"path": "docs/keys.md", "content": "rotate"}], body="Fixes #2, and relates to #1")
    g.advance(240)
    _json(g.call("merge_pull_request", {**O, "pull_number": n}))
    issue = lambda k: _json(g.call("get_issue", {**O, "issue_number": k}))  # noqa: E731
    assert issue(2)["state"] == "closed" and issue(2)["state_reason"] == "completed"
    assert issue(1)["state"] == "open", "only closing keywords close issues"


# -- jira ---------------------------------------------------------------------------------

def jira(version=V1):
    return Instance(get_service("jira"), version=version)


def keys(r):
    return [i["key"] for i in _json(r)["issues"]]


def test_jira_boards_sprints_and_jql():
    j = jira()
    board = _json(j.call("jira_get_agile_boards", {"board_name": "ops"}))[0]["id"]
    active = _json(j.call("jira_get_sprints_from_board", {"board_id": board, "state": "active"}))
    assert [s["name"] for s in active] == ["OPS Sprint 7"]
    assert keys(j.call("jira_get_sprint_issues", {"sprint_id": active[0]["id"]})) == ["OPS-2", "OPS-4"]
    assert set(keys(j.call("jira_search", {"jql": "sprint in openSprints()"}))) == {"OPS-2", "OPS-4"}
    assert keys(j.call("jira_search", {"jql": "sprint in closedSprints()"})) == ["OPS-5"]
    assert keys(j.call("jira_search", {"jql": 'sprint = "OPS Sprint 7" AND assignee = john'})) == ["OPS-2"]
    assert "Unable to find JQL function" in j.call("jira_search", {"jql": "sprint in nextSprints()"}).text
    assert "does not support sprints" in j.call("jira_get_sprints_from_board", {"board_id": "2"}).text
    assert "Field 'sprint' does not exist" in jira(V0).call("jira_search", {"jql": "sprint in openSprints()"}).text


def test_jira_sprint_lifecycle_like_jira():
    j = jira()
    s7, s8 = (s["id"] for s in _json(j.call("jira_get_sprints_from_board", {"board_id": "1", "state": "active,future"})))
    _json(j.call("jira_update_issue", {"issue_key": "OPS-3", "fields": json.dumps({"sprint": int(s8)})}))
    assert "only to an active or future sprint" in j.call(
        "jira_update_issue", {"issue_key": "OPS-3", "fields": json.dumps({"sprint": 40})}).text
    assert "not on the board" in j.call("jira_update_issue", {"issue_key": "SUP-1", "fields": json.dumps({"sprint": int(s8)})}).text
    _json(j.call("jira_update_sprint", {"sprint_id": s8, "start_date": "2026-09-28", "end_date": "2026-10-12"}))
    assert "already active" in j.call("jira_update_sprint", {"sprint_id": s8, "state": "active"}).text
    j.call("jira_transition_issue", {"issue_key": "OPS-4", "transition_id": "41"})  # OPS-4 done; OPS-2 isn't
    _json(j.call("jira_update_sprint", {"sprint_id": s7, "state": "closed", "move_incomplete_to": s8}))
    assert "closed sprint" in j.call("jira_update_sprint", {"sprint_id": s7, "goal": "x"}).text
    assert keys(j.call("jira_get_sprint_issues", {"sprint_id": s7})) == ["OPS-2", "OPS-4"], "history keeps both"
    assert keys(j.call("jira_get_sprint_issues", {"sprint_id": s8})) == ["OPS-2", "OPS-3"], "unfinished work carried over"
    _json(j.call("jira_update_sprint", {"sprint_id": s8, "state": "active"}))


# -- drive: sheets & docs -----------------------------------------------------------------

def drive(version=V1, **extra):
    svc = get_service("drive")
    seed = svc.default_seed()
    seed["files"] += extra.get("files", [])
    return Instance(svc, seed, version=version)


def fid(d, name):
    return next(f["id"] for f in d.state["files"].values() if f["name"] == name)


def rows(text):
    return [json.loads(line.split(": ", 1)[1]) for line in text.splitlines() if line.startswith("Row")]


def test_sheets_formulas_ranges_and_input_modes():
    d = drive(files=[{"name": "Headcount", "type": "sheet",
                      "sheets": {"Plan": "team,hc\neng,12\nsupport,5", "Summary": "total,=SUM(Plan!B2:B9)"}}])
    hc = fid(d, "Headcount")
    read = lambda rng: rows(d.call("read_sheet_values", {"spreadsheet_id": hc, "range_name": rng}).text)  # noqa: E731
    assert read("Summary!A1:B1") == [["total", "17"]]
    d.call("modify_sheet_values", {"spreadsheet_id": hc, "range_name": "Plan!B3", "values": [["1,200"]]})
    assert read("Summary!B1") == [["1212"]], "USER_ENTERED parses 1,200 and formulas recompute"
    d.call("modify_sheet_values", {"spreadsheet_id": hc, "range_name": "Plan!C1:C3",
                                   "values": [["ratio"], ["=B2/0"], ["=B3/B2"]]})
    assert read("Plan!C2:C3") == [["#DIV/0!"], ["100"]]
    d.call("modify_sheet_values", {"spreadsheet_id": hc, "range_name": "'Plan'!D1", "values": [["=B2"]],
                                   "value_input_option": "RAW"})
    assert read("Plan!D1") == [["=B2"]], "RAW keeps formulas as text"
    assert read("Plan!A1:D1") == [["team", "hc", "ratio", "=B2"]]
    assert read("Plan!A10:B20") == [], "blank ranges read as nothing"
    for rng, msg in [("Budget!A1", "Unable to parse range"), ("Plan!A1:B2000", "exceeds grid limits")]:
        assert msg in d.call("read_sheet_values", {"spreadsheet_id": hc, "range_name": rng}).text
    r = d.call("modify_sheet_values", {"spreadsheet_id": hc, "range_name": "Plan!A5:B5", "values": [["x", "1", "2"]]})
    assert r.is_error and "tried writing to column [C]" in r.text


def test_sheets_permissions_and_drive_view_stay_in_sync():
    d = drive(files=[{"name": "Their sheet", "type": "sheet", "owner": "john@acme.com", "shared_role": "reader",
                      "content": "a,b\n1,2"}])
    theirs = fid(d, "Their sheet")
    assert rows(d.call("read_sheet_values", {"spreadsheet_id": theirs}).text) == [["a", "b"], ["1", "2"]]
    r = d.call("modify_sheet_values", {"spreadsheet_id": theirs, "range_name": "A3", "values": [["3"]]})
    assert r.is_error and "insufficientFilePermissions" in r.text
    budget = fid(d, "Q4 budget")
    d.call("modify_sheet_values", {"spreadsheet_id": budget, "range_name": "A4:B4", "values": [["total", "=SUM(B2:B3)"]]})
    assert "total,150000" in d.call("get_drive_file_content", {"file_id": budget}).text
    d.apply_action("edit_content", {"file": "Q4 budget", "content": "team,q4\neng,1"})
    assert rows(d.call("read_sheet_values", {"spreadsheet_id": budget}).text) == [["team", "q4"], ["eng", "1"]]
    assert "already exists" in d.call("create_sheet", {"spreadsheet_id": budget, "sheet_name": "sheet1"}).text


def test_docs_indices_and_replace():
    d = drive()
    plan = fid(d, "Q4 plan")
    d.call("modify_doc_text", {"document_id": plan, "start_index": 1, "text": "DRAFT: "})
    assert "DRAFT: Goals" in d.call("get_doc_content", {"document_id": plan}).text
    body = d.state["files"][plan]["content"]
    assert "must be less than the end index" in d.call("modify_doc_text", {"document_id": plan,
                                                                          "start_index": len(body) + 2, "text": "x"}).text
    assert "newline character" in d.call("modify_doc_text", {"document_id": plan, "start_index": 1,
                                                             "end_index": len(body) + 2, "text": ""}).text
    d.call("modify_doc_text", {"document_id": plan, "start_index": 1, "end_index": 8, "text": ""})
    assert d.state["files"][plan]["content"].startswith("Goals")
    assert "Replaced 1 occurrence(s)" in d.call("find_and_replace_doc", {"document_id": plan, "find_text": "PLAN",
                                                                         "replace_text": "roadmap"}).text
    assert "not a Google Docs document" in d.call("get_doc_content", {"document_id": fid(d, "Q4 budget")}).text
    assert "read_sheet_values" not in json.dumps(drive(version=V0).list_tools())


# -- eventually consistent search (2026-09-25.2) ----------------------------------------------

V2 = "2026-09-25.2"


def test_github_search_lags_behind_writes_but_reads_dont():
    g = gh(version=V2)
    n = _json(g.call("create_issue", {**O, "title": "Webhook retries exhaust the pool"}))["number"]
    q = {"q": "repo:acme/api webhook retries"}
    assert _json(g.call("search_issues", q))["total_count"] == 0, "not indexed yet"
    assert _json(g.call("get_issue", {**O, "issue_number": n}))["title"].startswith("Webhook"), "reads are immediate"
    g.call("update_issue", {**O, "issue_number": 1, "state": "closed"})
    assert _json(g.call("search_issues", {"q": "repo:acme/api is:open hammer"}))["total_count"] == 1, "stale state"
    g.advance(60)
    assert _json(g.call("search_issues", q))["total_count"] == 1
    assert _json(g.call("search_issues", {"q": "repo:acme/api is:open hammer"}))["total_count"] == 0
    g1 = gh(version=V1)
    g1.call("create_issue", {**O, "title": "Webhook retries exhaust the pool"})
    assert _json(g1.call("search_issues", q))["total_count"] == 1, "older versions search instantly"


def test_jira_and_drive_search_lag():
    j = Instance(get_service("jira"), version=V2)
    j.call("jira_transition_issue", {"issue_key": "OPS-3", "transition_id": "11"})
    jql = {"jql": 'key = OPS-3 AND status = "In Progress"'}
    assert keys(j.call("jira_search", jql)) == []
    assert _json(j.call("jira_get_issue", {"issue_key": "OPS-3"}))["status"]["name"] == "In Progress"
    j.advance(10)
    assert keys(j.call("jira_search", jql)) == ["OPS-3"]
    d = drive(version=V2)
    d.call("create_drive_file", {"file_name": "incident-review.txt", "content": "x", "folder_id": "root"})
    assert "No files found" in d.call("search_drive_files", {"query": "incident"}).text
    assert "incident-review.txt" in d.call("list_drive_items", {}).text
    d.advance(60)
    assert "incident-review.txt" in d.call("search_drive_files", {"query": "incident"}).text


def test_search_lag_survives_snapshots_and_is_configurable():
    g = gh(version=V2)
    snap = g.snapshot()
    g.call("create_issue", {**O, "title": "Flaky login"})
    g.restore(snap)
    g.call("create_issue", {**O, "title": "Flaky login"})
    assert _json(g.call("search_issues", {"q": "flaky login"}))["total_count"] == 0
    svc = get_service("github")
    fast = Instance(svc, {**svc.default_seed(), "search_lag": {"issues": 0}}, version=V2)
    fast.call("create_issue", {**O, "title": "Flaky login"})
    assert _json(fast.call("search_issues", {"q": "flaky login"}))["total_count"] == 1


# -- linear -------------------------------------------------------------------------------

def linear(version=V1):
    return Instance(get_service("linear"), version=version)


def test_linear_cycles_estimates_label_groups():
    li = linear()
    made = _json(li.call("create_issue", {"title": "Refund webhooks", "team": "ENG", "cycle": "current", "estimate": 3}))
    assert made["cycle"]["number"] == 2 and made["estimate"] == 3
    assert "must be one of 1, 2, 3, 5, 8" in li.call("create_issue", {"title": "x", "team": "ENG", "estimate": 4}).text
    assert "not enabled for team OPS" in li.call("create_issue", {"title": "x", "team": "OPS", "estimate": 1}).text
    assert not li.call("update_issue", {"id": "ENG-2", "labels": ["Bug", "Tech debt"]}).is_error
    assert "both in the 'Type' group" in li.call("update_issue", {"id": "ENG-2", "labels": ["Bug", "Feature"]}).text
    assert "label group" in li.call("update_issue", {"id": "ENG-2", "labels": ["Type"]}).text
    assert "is completed" in li.call("update_issue", {"id": "ENG-2", "cycle": "1"}).text
    _json(li.call("update_issue", {"id": "ENG-2", "cycle": "next"}))
    current = {i["identifier"] for i in _json(li.call("list_issues", {"cycle": "current"}))["issues"]}
    assert current == {"ENG-1", made["identifier"]}
    assert not linear(V0).call("create_issue", {"title": "x", "team": "OPS", "estimate": 7}).is_error, "old version"


def test_linear_cursor_pagination_walks_everything():
    li = linear()
    seen, cursor = [], None
    while True:
        page = _json(li.call("list_issues", {"limit": 2, **({"cursor": cursor} if cursor else {})}))
        seen += [i["identifier"] for i in page["issues"]]
        if not page["hasNextPage"]:
            break
        cursor = page["nextCursor"]
    assert sorted(seen) == sorted({i["identifier"] for i in li.state["issues"].values()})
    assert "invalid cursor" in li.call("list_issues", {"cursor": "nope"}).text


# -- notion -------------------------------------------------------------------------------

def notion(version=V1):
    return Instance(get_service("notion"), version=version)


def page_id(n, title):
    return next(p["id"] for p in n.state["pages"].values() if p["title"] == title)


def test_notion_access_levels():
    n = notion()
    q4, notes = page_id(n, "Q4 Planning"), page_id(n, "1:1 notes: John")
    r = n.call("notion-update-page", {"page_id": q4, "command": "replace_content", "new_str": "x"})
    assert r.is_error and "restricted_resource" in r.text
    assert "restricted_resource" in n.call("notion-update-page", {"page_id": notes, "command": "replace_content",
                                                                  "new_str": "x"}).text, "children inherit access"
    assert not n.call("notion-create-comment", {"page_id": q4, "content": "Question on hiring"}).is_error
    assert _json(n.call("notion-fetch", {"id": q4}))["metadata"]["access"] == "comment"
    n.apply_action("set_access", {"title": "On-call runbook", "access": "view"})
    rb = page_id(n, "On-call runbook")
    assert "restricted_resource" in n.call("notion-create-comment", {"page_id": rb, "content": "hi"}).text
    assert not notion(V0).call("notion-update-page", {"page_id": page_id(notion(V0), "Q4 Planning"),
                                                      "command": "replace_content", "new_str": "x"}).is_error


def test_notion_typed_filters_and_cursor():
    n = notion()
    tasks = next(iter(n.state["data_sources"].values()))["url"]
    q = lambda f, **kw: _json(n.call("notion-query-data-sources", {"data_source_url": tasks, "filter": f, **kw}))  # noqa: E731
    names = lambda r: [x["Name"] for x in r["results"]]  # noqa: E731
    assert names(q({"Points": {">=": 3}})) == ["Rotate DB credentials", "Right-size staging cluster"]
    assert names(q({"Owner": {"is_empty": True}})) == ["Right-size staging cluster"]
    assert names(q({"Due": {"before": "2026-10-01"}})) == ["Rotate DB credentials"]
    assert names(q({"Status": {"does_not_equal": "Done"}, "Name": {"contains": "db"}})) == ["Rotate DB credentials"]
    assert names(q({"Tags": "billing"})) == ["Invoice currency bug"]
    assert names(q({"Owner": "john@acme.com"})) == ["Rotate DB credentials"]
    assert "not supported" in n.call("notion-query-data-sources", {"data_source_url": tasks, "filter": {"Name": {">": 1}}}).text
    first = q({}, limit=2)
    rest = q({}, limit=2, start_cursor=first["next_cursor"])
    assert first["has_more"] and not rest["has_more"] and len(names(first) + names(rest)) == 3


def test_notion_search_lags_and_duplicate_is_async():
    n = notion()
    wiki = page_id(n, "Engineering Wiki")
    n.call("notion-create-pages", {"parent": {"page_id": wiki}, "pages": [{"properties": {"title": "Incident review"}}]})
    assert _json(n.call("notion-search", {"query": "incident review"}))["results"] == []
    n.advance(120)
    assert _json(n.call("notion-search", {"query": "incident review"}))["results"]
    dup = _json(n.call("notion-duplicate-page", {"page_url": wiki}))
    assert dup["status"] == "in_progress"
    assert "Start here" not in n.state["pages"][dup["page_id"]]["content"]
    n.advance(60)
    n.call("notion-get-users", {})
    assert "Start here" in n.state["pages"][dup["page_id"]]["content"]
