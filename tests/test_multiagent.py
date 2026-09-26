"""Several agents in one environment, each acting as a different person.

The agents are scripted but connect exactly as real ones would: through the host, each with
its own MCP URLs (``?agent=...&as=...``) taken from the environment's agent configs.
"""

import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from myworld.host import HostConfig, create_app

ENVS = Path(__file__).parent.parent / "envs"


class Agent:
    def __init__(self, client: TestClient, config: dict):
        self.client = client
        self.urls = {s: urlsplit(v["url"]) for s, v in config["mcpServers"].items()}
        self.n = 0

    def call(self, server: str, tool: str, **args):
        self.n += 1
        u = self.urls[server]
        r = self.client.post(f"{u.path}?{u.query}", json={"jsonrpc": "2.0", "id": self.n, "method": "tools/call",
                                                         "params": {"name": tool, "arguments": args}})
        assert r.status_code == 200, r.text
        res = r.json()["result"]
        text = res["content"][0]["text"]
        try:
            return res["isError"], json.loads(text)
        except ValueError:
            return res["isError"], text


@pytest.fixture
def world():
    c = TestClient(create_app(config=HostConfig(env_dirs=[ENVS])))
    run = c.post("/envs", json={"file": "schedule-with-john.yaml", "id": "w"}).json()
    return c, Agent(c, run["agents"]["alex"]), Agent(c, run["agents"]["john"])


def alex_proposes(alex: Agent) -> None:
    alex.call("calendar", "get-freebusy", calendars=[{"id": "primary"}, {"id": "john@acme.com"}],
              timeMin="2026-09-22T08:00:00", timeMax="2026-09-23T18:00:00")
    alex.call("gmail", "send_email", to=["john@acme.com"], subject="Q4 planning: Tue 10:00?",
              body="Hi John, does Tuesday 10:00-10:30 PT work for Q4 planning?")


def john_replies(john: Agent) -> None:
    _, inbox = john.call("gmail", "search_emails", query="is:unread subject:Q4")
    msg_id = inbox.split()[1]
    _, email = john.call("gmail", "read_email", messageId=msg_id)
    thread = email.split()[2]
    _, fb = john.call("calendar", "get-freebusy", calendars=[{"id": "primary"}],
                      timeMin="2026-09-22T10:00:00", timeMax="2026-09-22T10:30:00")
    assert fb["calendars"]["primary"]["busy"] == [], "John is free at 10:00"
    john.call("gmail", "send_email", to=["alex@acme.com"], subject="Re: Q4 planning: Tue 10:00?",
              body="Tuesday 10:00 works.", threadId=thread)


def alex_invites(alex: Agent) -> str:
    _, reply = alex.call("gmail", "search_emails", query="from:john@acme.com subject:Q4")
    assert "Re: Q4 planning" in reply
    _, ev = alex.call("calendar", "create-event", summary="Q4 planning", start="2026-09-22T10:00:00",
                      end="2026-09-22T10:30:00", attendees=[{"email": "john@acme.com"}])
    return ev["event"]["id"]


def john_accepts(john: Agent) -> None:
    _, events = john.call("calendar", "list-events", timeMin="2026-09-22T00:00:00", timeMax="2026-09-23T00:00:00")
    invite = next(e for e in events["events"] if e["summary"] == "Q4 planning")
    john.call("calendar", "respond-to-event", eventId=invite["id"], response="accepted")


def test_two_agents_schedule_a_meeting(world):
    c, alex, john = world
    alex_proposes(alex)
    john_replies(john)
    alex_invites(alex)
    john_accepts(john)
    report = c.get("/envs/w/grade").json()
    assert report["passed"], report

    timeline = c.get("/envs/w/calls").json()["calls"]
    seqs = [x["global_seq"] for x in timeline]
    assert seqs == sorted(set(seqs)), "one ordering across servers (world events, like invite emails, take numbers too)"
    assert [x["agent"] for x in timeline][:4] == ["alex", "alex", "john", "john"]
    assert {x["actor"] for x in timeline if x["agent"] == "john"} == {"john@acme.com"}
    times = [x["at"] for x in timeline]
    assert times == sorted(times), "one clock across Gmail and Calendar"


def test_agent_that_never_accepts_fails_the_right_checks(world):
    c, alex, john = world
    alex_proposes(alex)
    john_replies(john)
    alex_invites(alex)  # john's agent never accepts
    failed = {x["name"] for x in c.get("/envs/w/grade").json()["checks"] if not x["passed"]}
    assert failed == {"one Q4 event that John accepted", "John's agent accepted the invite itself"}


def test_snapshot_restore_and_fork_the_whole_world(world):
    c, alex, john = world
    alex_proposes(alex)
    snap = c.post("/envs/w/snapshot").json()["snapshot_id"]
    john_replies(john)
    alex_invites(alex)

    fork = c.post("/envs/w/fork", json={"id": "w2"}).json()  # a branch at "invite sent"
    john2 = Agent(c, fork["agents"]["john"])
    john_accepts(john2)
    assert c.get("/envs/w2/grade").json()["passed"]
    assert not c.get("/envs/w/grade").json()["passed"], "the original is untouched by the fork"

    c.post("/envs/w/restore", json={"snapshot_id": snap})
    state = c.get("/instances/w-gmail/state").json()["state"]
    john_box = state["mailboxes"]["john@acme.com"]["messages"].values()
    assert any("Q4 planning" in m["subject"] for m in john_box)
    assert not any(m["subject"].startswith("Re: Q4") for m in state["mailboxes"]["alex@acme.com"]["messages"].values())
    assert len(c.get("/envs/w/calls").json()["calls"]) == 2, "back to right after Alex's proposal"

    c.post("/envs/w/reset")
    assert c.get("/envs/w/calls").json()["calls"] == []


def test_identities_are_checked():
    c = TestClient(create_app(config=HostConfig(env_dirs=[ENVS])))
    bad = {"name": "x", "servers": {"gmail": {}}, "agents": {"eve": {"as": "eve@evil.io"}}}
    r = c.post("/envs", json={"spec": bad})
    assert r.status_code == 400 and "no mailbox for eve@evil.io" in r.json()["detail"]
    ok = c.post("/envs", json={"spec": {"name": "y", "servers": {"gmail": {}}}, "id": "y"}).json()
    url = urlsplit(ok["agents"]["agent"]["mcpServers"]["gmail"]["url"])
    r = c.post(f"{url.path}?agent=eve&as=eve@evil.io", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 403
    from myworld.env import Environment
    with pytest.raises(ValueError, match="unknown server"):
        Environment.from_dict({"name": "z", "servers": {"gmail": {}}, "agents": {"a": {"servers": ["slack"]}}})


def test_single_agent_envs_still_work():
    c = TestClient(create_app(config=HostConfig(env_dirs=[ENVS])))
    run = c.post("/envs", json={"file": "merge-when-green.yaml", "id": "m"}).json()
    assert list(run["agents"]) == ["agent"]
    a = Agent(c, run["agents"]["agent"])
    a.call("github", "get_pull_request_status", owner="acme", repo="api", pull_number=4)
    a.call("github", "merge_pull_request", owner="acme", repo="api", pull_number=4, merge_method="squash")
    assert not c.get("/envs/m/grade").json()["passed"], "no answer yet"
    sha = c.get("/instances/m-github/state").json()["state"]["repos"]["acme/api"]["pulls"]["4"]["merge_commit_sha"]
    graded = c.post("/envs/m/submit", json={"answer": f"Merged, commit {sha}"}).json()
    assert graded["passed"] and graded["reward"] == 1.0


def test_calendar_emails_dont_stand_in_for_the_conversation(world):
    c, alex, john = world
    alex_invites_directly = alex.call("calendar", "create-event", summary="Q4 planning", start="2026-09-22T10:00:00",
                                      end="2026-09-22T10:30:00", attendees=[{"email": "john@acme.com"}])[1]
    _, found = john.call("gmail", "search_emails", query="subject:Invitation")
    assert "Q4 planning" in found, "John's agent sees the invite in his inbox"
    john.call("calendar", "respond-to-event", eventId=alex_invites_directly["event"]["id"], response="accepted")
    _, found = alex.call("gmail", "search_emails", query="from:john@acme.com")
    assert "Accepted: Q4 planning" in found
    checks = {x["name"]: x["passed"] for x in c.get("/envs/w/grade").json()["checks"]}
    assert not checks["John received Alex's proposal"] and not checks["Alex received John's reply"], \
        "skipping the emails isn't rewarded just because Calendar sent some"
