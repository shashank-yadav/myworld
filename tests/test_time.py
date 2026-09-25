"""Clock modes: virtual (fast, deterministic) and realtime (follows the wall clock, optionally faster)."""

import datetime as dt
import json
import time

import pytest
from fastapi.testclient import TestClient

from toolsim.core.instance import Instance
from toolsim.env import Environment, EnvRun, parse_time
from toolsim.host import HostConfig, create_app
from toolsim.rl import ToolEnv
from toolsim.services import get_service

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
