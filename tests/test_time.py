"""Clock modes: virtual (fast, deterministic) and realtime (follows the wall clock, optionally faster)."""

import datetime as dt
import json
import time

import pytest
from fastapi.testclient import TestClient

from myworld.core.instance import Instance
from myworld.env import Environment, EnvRun, parse_time
from myworld.host import HostConfig, create_app
from myworld.rl import ToolEnv
from myworld.services import get_service

MAIL_LATER = {"name": "t", "servers": {"gmail": {}}, "events": [
    {"server": "gmail", "action": "deliver_email", "at": "+10m",
     "params": {"sender": "a@x.io", "subject": "ten minutes later", "body": "."}}]}


def subjects(run):
    return {m["subject"] for m in run.instances["gmail"].state["mailboxes"]["alex@acme.com"]["messages"].values()}


def test_parse_time():
    assert parse_time(None) is None and parse_time("virtual") is None and parse_time({"mode": "virtual"}) is None
    assert parse_time("realtime") == 1.0 and parse_time({"speed": 60}) == 60.0 and parse_time(30) == 30.0
    for bad in ("sometimes", 0, -1, {"mode": "warp"}, True):
        with pytest.raises(ValueError):
            parse_time(bad)


def test_virtual_time_only_moves_when_something_takes_time():
    run = EnvRun(Environment.from_dict(MAIL_LATER))
    t0 = run.clock.now
    time.sleep(0.05)
    run.tick()
    assert run.clock.now == t0 and "ten minutes later" not in subjects(run)
    run.advance(600)
    assert "ten minutes later" in subjects(run)


def test_realtime_follows_the_wall_clock_and_fires_events_between_calls():
    run = EnvRun(Environment.from_dict({**MAIL_LATER, "time": {"speed": 100_000}}))  # 100k x: 10 min in 6 ms
    time.sleep(0.02)
    run.tick()
    assert "ten minutes later" in subjects(run), "no call needed: the world moved on by itself"
    real = EnvRun(Environment.from_dict({**MAIL_LATER, "time": "realtime"}))
    g = real.instances["gmail"]
    t0 = real.clock.now
    g.call("list_email_labels", {})
    g.call("list_email_labels", {})
    assert (real.clock.now - t0) < dt.timedelta(seconds=1), "calls take their real duration, not 1-3 s each"


def test_realtime_standalone_instance_delivers_bounces_on_schedule():
    svc = get_service("gmail")
    g = Instance(svc, speed=100_000)
    g.call("send_email", {"to": ["jhon@acme.com"], "subject": "Hi", "body": "."})
    time.sleep(0.01)  # ~1000 simulated seconds
    g.tick()
    assert any("mailer-daemon" in m["from"] for m in g.state["mailboxes"]["alex@acme.com"]["messages"].values())


def test_snapshots_in_realtime_resume_from_the_saved_time():
    run = EnvRun(Environment.from_dict({**MAIL_LATER, "time": {"speed": 100_000}}))
    snap = run.snapshot()
    time.sleep(0.02)
    run.tick()
    run.restore(snap)
    assert run.clock.now == dt.datetime.fromisoformat(snap["clock"])
    assert "ten minutes later" not in subjects(run), "restored to before the event"


def test_episodes_can_wait_and_count_thinking_time():
    spec = {"name": "ci", "servers": {"github": {}},
            "checks": [{"server": "github", "state": "repos[acme/api].pulls", "where": {"merged": True}}]}
    env = ToolEnv(spec)
    obs, _ = env.reset()
    assert [t["name"] for t in obs["tools"]][-2:] == ["wait", "submit"]
    o = {"owner": "acme", "repo": "api"}
    env.step({"tool": "github__create_branch", "arguments": {**o, "branch": "docs"}})
    env.step({"tool": "github__push_files", "arguments": {**o, "branch": "docs", "message": "docs",
                                                         "files": [{"path": "docs/a.md", "content": "hi"}]}})
    n = json.loads(env.step({"tool": "github__create_pull_request", "arguments": {
        **o, "title": "Docs", "head": "docs", "base": "main"}})[0]["content"])["number"]
    status = lambda: json.loads(env.step({"tool": "github__get_pull_request_status",  # noqa: E731
                                          "arguments": {**o, "pull_number": n}})[0]["content"])["state"]
    assert status() == "pending"
    obs, *_ = env.step({"tool": "wait", "arguments": {"seconds": 300}})
    assert obs["content"].startswith("Waited 300 seconds") and status() == "success"
    assert env.step({"tool": "wait", "arguments": {"seconds": 99999}})[0]["is_error"]
    t0 = env.run.clock.now
    env.step({"tool": "github__list_issues", "arguments": o}, elapsed=30)
    assert (env.run.clock.now - t0).total_seconds() >= 30
    assert "wait" not in [t["name"] for t in ToolEnv(spec, allow_wait=False).reset()[0]["tools"]]


def test_host_time_modes():
    c = TestClient(create_app(config=HostConfig()))
    r = c.post("/envs", json={"spec": MAIL_LATER, "id": "rt", "time": {"speed": 100_000}}).json()
    assert r["time"] == {"mode": "realtime", "speed": 100_000.0}
    time.sleep(0.02)
    tl = c.get("/envs/rt/timeline").json()["timeline"]
    assert any(x.get("event") == "deliver_email" for x in tl), "reading the timeline brings the world up to date"
    assert c.post("/envs", json={"spec": MAIL_LATER, "id": "v"}).json()["time"] == {"mode": "virtual"}
    assert c.post("/envs", json={"spec": MAIL_LATER, "time": "warp"}).status_code == 400
    fast = TestClient(create_app(config=HostConfig(speed=60)))
    assert fast.post("/envs", json={"spec": MAIL_LATER}).json()["time"]["speed"] == 60
    assert fast.post("/envs", json={"spec": MAIL_LATER, "time": "virtual"}).json()["time"] == {"mode": "virtual"}
    assert fast.post("/envs", json={"spec": {**MAIL_LATER, "time": "virtual"}}).json()["time"] == {"mode": "virtual"}
    assert fast.post("/instances", json={"service": "slack", "id": "s", "time": "virtual"}).status_code == 200


def test_anchoring_a_world_to_the_wall_clock_moves_every_date_by_whole_weeks():
    from myworld.env import _shift_text, anchor
    assert _shift_text("Tue 2026-09-22T11:00:00-07:00, all day 2026-09-24", 21) == \
        "Tue 2026-10-13T11:00:00-07:00, all day 2026-10-15"
    assert _shift_text("RRULE:FREQ=WEEKLY;UNTIL=20261231T000000Z", 7) == "RRULE:FREQ=WEEKLY;UNTIL=20270107T000000Z"
    assert _shift_text("Meet on Sep 22 or September 23, 2026", 14) == "Meet on Oct 6 or October 7, 2026"
    assert _shift_text("version 2026-09-25.1 id 20260922", 7).startswith("version 2026-10-02"), "only date shapes move"
    spec = {"name": "t", "task": "Book it for 2026-09-22.", "servers": {"calendar": {}, "gmail": {}},
            "checks": [{"server": "calendar", "state": "events", "where": {"start.dateTime~": "2026-09-22"}}]}
    wall = dt.datetime(2026, 10, 16, 18, 0, tzinfo=dt.timezone.utc)  # a Friday, 3.5 weeks later
    env = anchor(Environment.from_dict(spec), wall)
    assert env.now == wall.isoformat() and env.task == "Book it for 2026-10-13."
    assert env.checks[0]["where"]["start.dateTime~"] == "2026-10-13"
    run = EnvRun(env)
    assert run.clock.now == wall
    starts = [e["start"]["dateTime"][:10] for e in run.instances["calendar"].state["events"].values()]
    assert starts and all(s >= "2026-10-12" for s in starts), "seeded events moved 3 weeks (weekdays kept)"
    dates = [m["date"] for m in run.instances["gmail"].state["mailboxes"]["alex@acme.com"]["messages"].values()]
    assert all("Oct 2026" in d for d in dates)
    live = EnvRun(Environment.from_dict({**spec, "now": "wallclock"}))
    assert abs((live.clock.now - dt.datetime.now(dt.timezone.utc)).total_seconds()) < 60
