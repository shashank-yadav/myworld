"""Several tools in one environment: one company, one clock, and services that talk to each other."""

import time

import pytest

from toolsim.env import Environment, EnvRun
from toolsim.rl import ToolEnv

ALL = ["gmail", "calendar", "slack", "github", "jira", "drive", "linear", "notion"]


def world(*servers, **extra):
    return EnvRun(Environment.from_dict({"name": "w", "servers": {s: {} for s in servers}, **extra}))


def data(r):
    assert not r.is_error, r.text
    return r.data if isinstance(r.data, (dict, list)) else r.text


def inbox(run, who="alex@acme.com", server="gmail"):
    return sorted(m["subject"] for m in run.instances[server].state["mailboxes"][who]["messages"].values())


def invite(run, attendees, as_="alex@acme.com", **kw):
    args = {"summary": "Planning", "start": "2026-09-24T10:00:00", "end": "2026-09-24T10:30:00",
            "attendees": [{"email": a} for a in attendees], **kw}
    return data(run.instances["calendar"].call("create-event", args, as_=as_))["event"]


# -- one company across every tool ---------------------------------------------------------------

def test_every_tool_starts_and_answers_as_the_same_person():
    run = world(*ALL)
    for server, inst in run.instances.items():
        assert inst.actor, server
        assert inst.resolve_actor("alex@acme.com"), f"{server} doesn't know alex@acme.com"


def test_colleagues_have_a_mailbox_a_calendar_and_a_drive():
    run = world("gmail", "calendar", "drive")
    for who in ("john@acme.com", "priya@acme.com"):
        assert who in run.instances["gmail"].state["mailboxes"], who
        assert not run.instances["calendar"].call("list-events", {"timeMin": "2026-09-21T00:00:00",
                                                                  "timeMax": "2026-09-26T00:00:00"}, as_=who).is_error
    assert "outsider@other.io" not in run.instances["gmail"].state["mailboxes"], "only the company's own domain"


# -- calendar -> gmail -----------------------------------------------------------------------------

def test_invites_updates_and_cancellations_arrive_by_email():
    run = world("gmail", "calendar")
    cal = run.instances["calendar"]
    ev = invite(run, ["john@acme.com", "ext@other.io"])
    assert any(s.startswith("Invitation: Planning @") for s in inbox(run, "john@acme.com"))
    assert not any("Invitation" in s for s in inbox(run)), "the organizer isn't invited to their own meeting"

    data(cal.call("respond-to-event", {"eventId": ev["id"], "response": "accepted"}, as_="john@acme.com"))
    assert any(s.startswith("Accepted: Planning @") for s in inbox(run))

    data(cal.call("update-event", {"eventId": ev["id"], "start": "2026-09-24T11:00:00", "end": "2026-09-24T11:30:00"}))
    assert any(s.startswith("Updated invitation: Planning @") for s in inbox(run, "john@acme.com"))
    n = len(inbox(run, "john@acme.com"))
    data(cal.call("update-event", {"eventId": ev["id"], "description": "agenda"}))
    assert len(inbox(run, "john@acme.com")) == n, "a description edit isn't worth an email"

    data(cal.call("delete-event", {"eventId": ev["id"]}))
    assert any(s.startswith("Canceled event: Planning @") for s in inbox(run, "john@acme.com"))


def test_send_updates_none_and_external_only():
    run = world("gmail", "calendar")
    invite(run, ["john@acme.com"], sendUpdates="none")
    assert not any("Invitation" in s for s in inbox(run, "john@acme.com"))
    invite(run, ["john@acme.com"], summary="Ext", sendUpdates="externalOnly")
    assert not any("Invitation: Ext" in s for s in inbox(run, "john@acme.com")), "john is internal"


def test_colleagues_who_answer_on_their_own_email_the_organizer():
    run = EnvRun(Environment.from_dict({"name": "w", "servers": {
        "gmail": {}, "calendar": {"seed": {"auto_respond": {"john@acme.com": "decline"}}}}}))
    invite(run, ["john@acme.com"])
    assert not any(s.startswith("Declined") for s in inbox(run))
    run.advance(3600)
    assert any(s.startswith("Declined: Planning @") for s in inbox(run))
    timeline = run.timeline()
    assert [x.get("event") for x in timeline if x.get("event")][-2:] == ["auto_respond", "deliver_email"]


def test_failed_calls_send_no_mail():
    run = world("gmail", "calendar")
    before = inbox(run, "john@acme.com")
    r = run.instances["calendar"].call("create-event", {"summary": "Bad", "start": "2026-09-24T10:00:00",
                                                        "end": "2026-09-24T09:00:00",
                                                        "attendees": [{"email": "john@acme.com"}]})
    assert r.is_error and inbox(run, "john@acme.com") == before
    run.instances["calendar"].call("get-current-time", {})
    assert inbox(run, "john@acme.com") == before, "nothing leaks into the next call either"


def test_calendar_without_gmail_just_works():
    run = world("calendar", "slack")
    invite(run, ["john@acme.com"])
    assert not any(x.get("event") == "deliver_email" for x in run.timeline())


# -- drive -> gmail --------------------------------------------------------------------------------

def test_sharing_a_file_emails_the_person_who_can_then_open_it():
    run = world("gmail", "drive")
    d = run.instances["drive"]
    f = data(d.call("create_drive_file", {"file_name": "Plan.txt", "content": "ship it"}))
    fid = f["id"] if isinstance(f, dict) and "id" in f else next(
        k for k, v in d.state["files"].items() if v["name"] == "Plan.txt")
    assert "not found" in d.call("get_drive_file_content", {"file_id": fid}, as_="john@acme.com").text
    assert "No valid credentials" in d.call("get_drive_file_content",
                                            {"file_id": fid, "user_google_email": "john@acme.com"}).text, \
        "naming someone else's account doesn't get their access"
    data(d.call("manage_drive_access", {"file_id": fid, "action": "grant", "share_with": "john@acme.com",
                                        "role": "reader"}))
    assert any('shared with you: "Plan.txt"' in s for s in inbox(run, "john@acme.com"))
    assert "ship it" in d.call("get_drive_file_content", {"file_id": fid}, as_="john@acme.com").text
    data(d.call("manage_drive_access", {"file_id": fid, "action": "grant", "share_with": "priya@acme.com",
                                        "send_notification": False}))
    assert not any("Plan.txt" in s for s in inbox(run, "priya@acme.com"))


# -- the environment as a whole --------------------------------------------------------------------

def test_two_mail_servers_are_separate_worlds():
    run = EnvRun(Environment.from_dict({"name": "w", "servers": {
        "work": {"service": "gmail"}, "home": {"service": "gmail"}, "calendar": {}}}))
    invite(run, ["john@acme.com"])
    assert inbox(run, "john@acme.com", "work") == inbox(run, "john@acme.com", "home"), \
        "a company-wide notification reaches every Gmail that has the mailbox"
    run.instances["work"].call("send_email", {"to": ["john@acme.com"], "subject": "work only", "body": "."})
    assert "work only" in inbox(run, "john@acme.com", "work")
    assert "work only" not in inbox(run, "john@acme.com", "home")
    tools = [t["name"] for t in ToolEnv({"name": "w", "servers": {"work": {"service": "gmail"},
                                                                   "home": {"service": "gmail"}}}).reset()[0]["tools"]]
    assert "work__send_email" in tools and "home__send_email" in tools


def test_one_clock_and_one_ordering_across_tools():
    run = world(*ALL)
    run.instances["slack"].call("slack_list_channels", {})
    run.instances["github"].call("list_issues", {"owner": "acme", "repo": "api"})
    invite(run, ["john@acme.com"])
    run.instances["jira"].call("jira_search", {"jql": "project = ENG"})
    items = run.timeline()
    seqs = [x["global_seq"] for x in items]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    ats = [x["at"] for x in items]
    assert ats == sorted(ats)
    clocks = {inst.clock for inst in run.instances.values()}
    assert len(clocks) == 1, "every tool reads the same time"


def test_forks_carry_pending_work_in_every_tool():
    run = EnvRun(Environment.from_dict({"name": "w", "servers": {
        "gmail": {}, "calendar": {"seed": {"auto_respond": {"john@acme.com": "accept"}}}, "github": {}}}))
    invite(run, ["john@acme.com"])
    run.instances["gmail"].call("send_email", {"to": ["jhon@acme.com"], "subject": "typo", "body": "."})
    snap = run.snapshot()
    run.advance(3600)
    after = inbox(run)
    run.restore(snap)
    assert not any(s.startswith("Accepted") for s in inbox(run)), "restored to before the answer"
    run.advance(3600)
    assert inbox(run) == after, "the same future plays out again"
    assert any(s.startswith("Accepted: Planning") for s in after)
    assert any("Delivery Status" in s or "Undeliverable" in s or "failure" in s.lower() for s in after)


def test_realtime_world_with_several_tools():
    run = EnvRun(Environment.from_dict({"name": "w", "time": {"speed": 100_000}, "servers": {
        "gmail": {}, "calendar": {"seed": {"auto_respond": {"john@acme.com": "accept"}}}, "slack": {}}}))
    invite(run, ["john@acme.com"])
    time.sleep(0.05)  # ~80 simulated minutes
    run.tick()
    assert any(s.startswith("Accepted: Planning") for s in inbox(run))


def test_agent_acting_as_a_colleague_across_tools():
    spec = {"name": "john", "servers": {"gmail": {}, "calendar": {}, "drive": {}},
            "agents": {"john": {"as": "john@acme.com"}},
            "checks": [{"server": "gmail", "state": "mailboxes[alex@acme.com].messages",
                        "where": {"subject~": "Accepted: Planning"}}]}
    env = ToolEnv(spec, agent="john", dense=True)
    env.reset()
    ev = invite(env.run, ["john@acme.com"])  # alex (no agent here) invites john
    obs, *_ = env.step({"tool": "gmail__search_emails", "arguments": {"query": "subject:Invitation"}})
    assert "Planning" in obs["content"], "john sees the invite in his own inbox"
    obs, reward, *_ = env.step({"tool": "calendar__respond-to-event",
                                "arguments": {"eventId": ev["id"], "response": "accepted"}})
    assert {c["actor"] for c in env.run.calls()[1:]} == {"john@acme.com"}
    assert not obs["is_error"] and reward > 0


@pytest.mark.parametrize("service,old", [("gmail", "2026-09-25.1"), ("calendar", "2026-09-25.1"),
                                         ("drive", "2026-09-25.2")])
def test_old_versions_send_nothing(service, old):
    run = EnvRun(Environment.from_dict({"name": "w", "servers": {
        "gmail": {"version": old} if service == "gmail" else {},
        **({service: {"version": old}} if service != "gmail" else {"calendar": {}})}}))
    if service == "drive":
        d = run.instances["drive"]
        data(d.call("create_drive_file", {"file_name": "P.txt", "content": "."}))
        fid = next(k for k, v in d.state["files"].items() if v["name"] == "P.txt")
        data(d.call("manage_drive_access", {"file_id": fid, "action": "grant", "share_with": "john@acme.com"}))
    elif service == "calendar":
        invite(run, ["john@acme.com"])
    else:
        assert "john@acme.com" not in run.instances["gmail"].state["mailboxes"], "no company mailboxes before .2"
        invite(run, ["john@acme.com"])
    assert not any(x.get("event") == "deliver_email" for x in run.timeline())


def test_generated_colleagues_exist_in_every_tool():
    from toolsim.noise import people
    run = EnvRun(Environment.from_dict({"name": "w", "rng_seed": 5, "servers": {s: {"noise": True} for s in ALL}}))
    for p in people(5, "acme.com", 10):
        for server, inst in run.instances.items():
            assert inst.resolve_actor(p["email"]), f"{p['email']} unknown in {server}"


def test_anyone_at_the_company_gets_a_my_drive_on_first_use():
    run = world("gmail", "drive")
    d = run.instances["drive"]
    assert "sam.new@acme.com" not in d.state["roots"]
    data(d.call("create_drive_file", {"file_name": "mine.txt", "content": "."}, as_="sam.new@acme.com"))
    assert "mine.txt" in d.call("list_drive_items", {}, as_="sam.new@acme.com").text
    assert "mine.txt" not in d.call("list_drive_items", {}).text, "it's in their Drive, not alex's"
    with pytest.raises(ValueError):
        d.resolve_actor("someone@other.io")


@pytest.mark.parametrize("kind,error,invites", [("timeout_after_commit", True, 1), ("duplicate_commit", False, 2),
                                                ("server_error", True, 0)])
def test_faults_and_notifications_agree_with_what_happened(kind, error, invites):
    run = EnvRun(Environment.from_dict({"name": "w", "servers": {"gmail": {}, "calendar": {}},
                                        "faults": [{"server": "calendar", "kind": kind, "tool": "create-event"}]}))
    r = run.instances["calendar"].call("create-event", {"summary": "T", "start": "2026-09-24T10:00:00",
                                                        "end": "2026-09-24T10:30:00",
                                                        "attendees": [{"email": "john@acme.com"}]})
    events = [e for e in run.instances["calendar"].state["events"].values() if e["summary"] == "T"]
    assert r.is_error == error
    assert len(events) == invites == sum("Invitation: T" in s for s in inbox(run, "john@acme.com")), \
        "one email per event that really exists, whatever the agent was told"


def test_parallel_episodes_in_multi_tool_worlds():
    from toolsim.rl import EnvPool
    spec = {"name": "invite", "servers": {"gmail": {}, "calendar": {}},
            "checks": [{"server": "gmail", "state": "mailboxes[john@acme.com].messages",
                        "where": {"subject~": "Invitation: Sync"}, "min": 1}]}
    act = {"tool": "calendar__create-event", "arguments": {"summary": "Sync", "start": "2026-09-24T10:00:00",
                                                          "end": "2026-09-24T10:30:00",
                                                          "attendees": [{"email": "john@acme.com"}]}}
    with EnvPool(workers=2, max_steps=3) as pool:
        pool.reset([spec] * 4)
        pool.step([act, None, act, None])
        results = pool.step([{"tool": "submit", "arguments": {"answer": "done"}}] * 4)
    assert [r[1] for r in results] == [1.0, 0.0, 1.0, 0.0]


def test_colleagues_booking_or_cancelling_in_the_world_email_the_invitees():
    run = world("gmail", "calendar")
    run.inject("calendar", "add_event", {"calendar": "john@acme.com", "summary": "Roadmap", "start": "2026-09-24T13:00:00",
                                         "end": "2026-09-24T13:30:00", "attendees": ["alex@acme.com"]})
    assert any(s.startswith("Invitation: Roadmap @") for s in inbox(run))
    run.inject("calendar", "cancel_event", {"summary": "Roadmap"})
    assert any(s.startswith("Canceled event: Roadmap @") for s in inbox(run))


def test_seeded_mail_is_in_every_participant_mailbox():
    run = world("gmail")
    g = run.instances["gmail"].state["mailboxes"]
    from_john = [m for m in g["alex@acme.com"]["messages"].values() if "john@acme.com" in m["from"]]
    assert from_john
    johns = {m["messageId"]: m for m in g["john@acme.com"]["messages"].values()}
    for m in from_john:
        assert johns[m["messageId"]]["labelIds"] == ["SENT"], "what Alex got from John is in John's Sent"
    r = run.instances["gmail"].call("search_emails", {"query": "in:sent"}, as_="john@acme.com")
    assert from_john[0]["subject"] in r.text
    old = EnvRun(Environment.from_dict({"name": "w", "servers": {"gmail": {"version": "2026-09-25.1"}}}))
    assert list(old.instances["gmail"].state["mailboxes"]) == ["alex@acme.com"]


# -- jira / github / linear -> gmail ---------------------------------------------------------------

def mail_to(run, who, needle):
    box = run.instances["gmail"].state["mailboxes"][who]["messages"].values()
    return [m for m in box if needle in m["subject"]]


def test_jira_emails_assignees_and_commenters_but_not_the_actor():
    run = world("gmail", "jira")
    j = run.instances["jira"]
    data(j.call("jira_assign_issue", {"issue_key": "OPS-4", "assignee": "john@acme.com"}))
    [m] = mail_to(run, "john@acme.com", "[JIRA] (OPS-4)")
    assert "Alex Rivera assigned OPS-4 to you." in m["body"] and "jira@acme.atlassian.net" in m["from"]
    assert not mail_to(run, "alex@acme.com", "[JIRA] (OPS-4)"), "you don't get mail about your own changes"
    data(j.call("jira_add_comment", {"issue_key": "OPS-4", "body": "On it."}, as_="john@acme.com"))
    assert any("John Park commented on OPS-4" in m["body"] for m in mail_to(run, "alex@acme.com", "(OPS-4)"))
    before = len(mail_to(run, "alex@acme.com", "(OPS-4)"))
    assert j.call("jira_transition_issue", {"issue_key": "OPS-4", "transition_id": "999"}, as_="john@acme.com").is_error
    assert len(mail_to(run, "alex@acme.com", "(OPS-4)")) == before, "a failed transition emails nobody"


def test_world_events_in_jira_email_you_too():
    run = world("gmail", "jira")
    run.inject("jira", "add_comment", {"issue_key": "OPS-2", "author": "john", "body": "Rotated staging already."})
    assert any("Rotated staging" in m["body"] for m in mail_to(run, "alex@acme.com", "(OPS-2)"))


def test_github_mentions_reviews_and_merges_reach_participants():
    run = world("gmail", "github")
    g = run.instances["github"]
    o = {"owner": "acme", "repo": "api"}
    n = data(g.call("create_issue", {**o, "title": "Flaky deploys", "body": "cc @priya-shah",
                                     "assignees": ["john-park"]}))["number"]
    [m] = mail_to(run, "john@acme.com", f"(Issue #{n})")
    assert m["subject"] == f"[acme/api] Flaky deploys (Issue #{n})" and "you were assigned" in m["body"]
    assert mail_to(run, "priya@acme.com", f"(Issue #{n})"), "@mentioned"
    data(g.call("add_issue_comment", {**o, "issue_number": n, "body": "Looking."}, as_="john@acme.com"))
    for who in ("alex@acme.com", "priya@acme.com"):
        assert any(x["subject"].startswith("Re: [acme/api] Flaky deploys") for x in mail_to(run, who, f"#{n})"))
    assert len(mail_to(run, "john@acme.com", f"(Issue #{n})")) == 1, "not for your own comment"


def test_linear_assignment_mention_and_status_change():
    run = world("gmail", "linear")
    lin = run.instances["linear"]
    data(lin.call("update_issue", {"id": "ENG-2", "assignee": "john@acme.com"}))
    assert any("assigned ENG-2 to you" in m["body"] for m in mail_to(run, "john@acme.com", "ENG-2"))
    data(lin.call("create_comment", {"issueId": "ENG-2", "body": "@priya can you pair?"}))
    assert mail_to(run, "priya@acme.com", "ENG-2")
    data(lin.call("update_issue", {"id": "ENG-2", "state": "In Progress"}, as_="john@acme.com"))
    assert any("changed the status to In Progress" in m["body"] for m in mail_to(run, "alex@acme.com", "ENG-2"))


@pytest.mark.parametrize("server,version", [("jira", "2026-09-25.2"), ("github", "2026-09-25.2"),
                                            ("linear", "2026-09-25.1")])
def test_older_tracker_versions_send_no_mail(server, version):
    run = EnvRun(Environment.from_dict({"name": "w", "servers": {"gmail": {}, server: {"version": version}}}))
    call = {"jira": ("jira_assign_issue", {"issue_key": "OPS-4", "assignee": "john@acme.com"}),
            "github": ("create_issue", {"owner": "acme", "repo": "api", "title": "x", "assignees": ["john-park"]}),
            "linear": ("update_issue", {"id": "ENG-2", "assignee": "john@acme.com"})}[server]
    data(run.instances[server].call(*call))
    assert not any(x.get("event") == "deliver_email" for x in run.timeline())


def test_colleagues_have_their_own_background_mail():
    run = EnvRun(Environment.from_dict({"name": "w", "rng_seed": 2, "servers": {"gmail": {"noise": True}}}))
    boxes = run.instances["gmail"].state["mailboxes"]
    assert len(boxes) > 10 and all(len(b["messages"]) >= 20 for b in boxes.values())
    old = EnvRun(Environment.from_dict({"name": "w", "rng_seed": 2,
                                        "servers": {"gmail": {"noise": True, "version": "2026-09-25.1"}}}))
    alex = lambda r: sorted(m["subject"] for m in r.instances["gmail"].state["mailboxes"]["alex@acme.com"]["messages"].values())  # noqa: E731
    assert set(alex(old)) <= set(alex(run)), "Alex's own generated mail is the same with or without company mail"
