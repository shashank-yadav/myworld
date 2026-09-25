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
