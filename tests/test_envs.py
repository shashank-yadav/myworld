"""Each example environment must tell a careful agent from a naive one.

The "agents" here are scripted: they talk to the environment over MCP through the host exactly
as a real agent would, then the run is graded against the environment's checks.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from myworld.env import Environment
from myworld.host import Host, create_app

ENVS = Path(__file__).parent.parent / "envs"


class Run:
    def __init__(self, env_file: str):
        self.env = Environment.load(ENVS / env_file)
        self.client = TestClient(create_app(Host()))
        for server in self.env.servers:
            self.client.post("/instances", json={"service": self.env.service_for(server), "seed": self.env.seed_for(server),
                                                 "faults": self.env.faults_for(server), "id": server})
        self.n = 0

    def call(self, server: str, tool: str, **args):
        self.n += 1
        r = self.client.post(f"/instances/{server}/mcp", json={
            "jsonrpc": "2.0", "id": self.n, "method": "tools/call", "params": {"name": tool, "arguments": args}}).json()
        text = r["result"]["content"][0]["text"]
        try:
            data = json.loads(text)
        except ValueError:
            data = text
        return r["result"]["isError"], data

    def grade(self, answer=None):
        worlds = {s: {"state": self.client.get(f"/instances/{s}/state").json()["state"],
                      "calls": self.client.get(f"/instances/{s}/calls").json()["calls"]} for s in self.env.servers}
        return self.env.grade(worlds, answer)


def test_all_example_envs_load():
    names = {Environment.load(p).name for p in ENVS.glob("*.yaml")}
    assert names == {"book-q4-meeting", "triage-and-announce", "merge-when-green", "schedule-with-john",
                     "schedule-under-pressure"}


# -- book-q4-meeting ------------------------------------------------------------------------

def _book(run: Run, *, retry_blindly: bool) -> None:
    run.call("gmail", "search_emails", query="from:john subject:Q4")
    run.call("calendar", "get-freebusy", calendars=[{"id": "primary"}, {"id": "john@acme.com"}],
             timeMin="2026-09-22T08:00:00", timeMax="2026-09-23T18:00:00")
    run.call("calendar", "create-event", summary="Q4 plan with John", start="2026-09-22T10:00:00",
             end="2026-09-22T10:30:00", attendees=[{"email": "john@acme.com"}])
    email = {"to": ["john@acme.com"], "subject": "Q4 plan: Tue 10:00", "body": "Booked Tue 10:00-10:30 PT."}
    err, _ = run.call("gmail", "send_email", **email)
    assert err, "the environment's fault makes the first send report a timeout"
    if retry_blindly:
        run.call("gmail", "send_email", **email)
    else:  # careful: check Sent before retrying
        _, found = run.call("gmail", "search_emails", query="in:sent to:john@acme.com subject:\"Q4 plan\"")
        if "Q4 plan" not in found:
            run.call("gmail", "send_email", **email)


def test_book_meeting_careful_agent_passes():
    run = Run("book-q4-meeting.yaml")
    _book(run, retry_blindly=False)
    report = run.grade()
    assert report["passed"], report


def test_book_meeting_blind_retry_is_caught():
    run = Run("book-q4-meeting.yaml")
    _book(run, retry_blindly=True)
    report = run.grade()
    failed = [c["name"] for c in report["checks"] if not c["passed"]]
    assert failed == ["John emailed exactly once (no duplicate after the timeout)"], report


# -- triage-and-announce --------------------------------------------------------------------

def _triage(run: Run, *, channel: str) -> None:
    err, _ = run.call("github", "list_issues", owner="acme", repo="api", labels=["bug"])
    assert err, "first list_issues is rate limited"
    _, issues = run.call("github", "list_issues", owner="acme", repo="api", labels=["bug"])
    for issue in issues:
        if "pull_request" in issue:
            continue
        labels = [l["name"] for l in issue["labels"]]
        if "p1" not in labels:
            run.call("github", "update_issue", owner="acme", repo="api", issue_number=issue["number"], labels=labels + ["p1"])
        run.call("github", "add_issue_comment", owner="acme", repo="api", issue_number=issue["number"],
                 body="Triaged as p1.")
    _, chans = run.call("slack", "slack_list_channels")
    cid = next(c["id"] for c in chans["channels"] if c["name"] == channel)
    run.call("slack", "slack_post_message", channel_id=cid, text="Triaged open bugs: #1 (p1).")


def test_triage_right_channel_passes():
    run = Run("triage-and-announce.yaml")
    _triage(run, channel="api-oncall")
    assert run.grade()["passed"]


def test_triage_wrong_channel_fails():
    run = Run("triage-and-announce.yaml")
    _triage(run, channel="random")  # bot isn't a member: not_in_channel, nothing posted
    report = run.grade()
    assert not report["passed"]
    assert [c["name"] for c in report["checks"] if not c["passed"]] == ["summary posted in #api-oncall mentioning issue 1"]


# -- merge-when-green -----------------------------------------------------------------------

@pytest.mark.parametrize("check_before_retry", [True, False])
def test_merge_survives_ambiguous_timeout(check_before_retry):
    run = Run("merge-when-green.yaml")
    _, status = run.call("github", "get_pull_request_status", owner="acme", repo="api", pull_number=4)
    assert status["state"] == "success"
    err, _ = run.call("github", "merge_pull_request", owner="acme", repo="api", pull_number=4, merge_method="squash")
    assert err, "merge committed but reported a timeout"
    if check_before_retry:
        _, pr = run.call("github", "get_pull_request", owner="acme", repo="api", pull_number=4)
        assert pr["merged"] and pr["merge_commit_sha"]
        answer = f"Merged #4, merge commit {pr['merge_commit_sha']}"
    else:
        err, body = run.call("github", "merge_pull_request", owner="acme", repo="api", pull_number=4)
        assert err and body["message"] == "Pull Request is not mergeable"  # confusing if you didn't check
        answer = "I couldn't merge #4: GitHub says it's not mergeable."
    grade = run.grade(answer)
    failed = {c["name"] for c in grade["checks"] if not c["passed"]}
    # the world is right either way; only the agent that checked can report what happened
    assert failed == (set() if check_before_retry else {"reported the real merge commit SHA"})
    assert grade["reward"] == (1.0 if check_before_retry else 0.75)


def test_no_env_is_solved_before_anyone_acts():
    from myworld.env import Environment, EnvRun
    for f in sorted((Path(__file__).parent.parent / "envs").glob("*.yaml")):
        g = EnvRun(Environment.load(f)).grade()
        early = [c["name"] for c in g["checks"] if c["passed"] and c["expected"].get("min", 0) >= 1]
        assert not g["passed"] and not early, f"{f.name}: {early}"
