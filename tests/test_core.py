import json

import pytest

from toolsim.core.faults import Fault
from toolsim.core.instance import Instance
from toolsim.core.tools import ToolError, tool, validate_args
from toolsim.services import get_service


def test_schema_from_signature():
    from typing import Annotated, Literal

    @tool("demo", read_only=True)
    def demo(ctx, name: Annotated[str, "who"], n: int = 3, mode: Literal["a", "b"] | None = None,
             tags: list[str] | None = None):
        """Say hi"""

    d = demo.mcp_definition()
    assert d["inputSchema"]["required"] == ["name"]
    assert d["inputSchema"]["properties"]["name"] == {"type": "string", "description": "who"}
    assert d["inputSchema"]["properties"]["mode"]["enum"] == ["a", "b"]
    assert d["inputSchema"]["properties"]["tags"] == {"type": "array", "items": {"type": "string"}}
    assert d["annotations"]["readOnlyHint"] and d["annotations"]["idempotentHint"]
    assert validate_args(demo, {"name": "x", "n": "5"}) == {"name": "x", "n": 5}
    with pytest.raises(ToolError, match="Missing"):
        validate_args(demo, {})
    with pytest.raises(ToolError, match="Unknown"):
        validate_args(demo, {"name": "x", "bogus": 1})


def test_instances_are_deterministic_and_isolated():
    a = Instance(get_service("gmail"), rng_seed=7)
    b = Instance(get_service("gmail"), rng_seed=7)
    args = {"to": ["john@acme.com"], "subject": "hi", "body": "x"}
    assert a.call("send_email", args).text == b.call("send_email", args).text  # same seed, same ids
    assert a.now() == b.now()
    a.call("send_email", args)
    assert len(a.state["mailboxes"]["alex@acme.com"]["messages"]) == len(b.state["mailboxes"]["alex@acme.com"]["messages"]) + 1  # no shared state


def test_snapshot_restore_and_reset():
    i = Instance(get_service("slack"))
    general = next(c["id"] for c in i.state["channels"].values() if c["name"] == "general")
    snap = i.snapshot()
    first = i.call("slack_post_message", {"channel_id": general, "text": "hello"}).text
    i.restore(snap)
    assert i.call("slack_post_message", {"channel_id": general, "text": "hello"}).text == first  # fork replays exactly
    i.reset()
    assert all(m["text"] != "hello" for m in i.state["messages"][general])
    assert i.calls == []


def test_failed_calls_leave_no_partial_writes():
    i = Instance(get_service("gmail"))
    before = json.dumps(i.state, sort_keys=True, default=str)
    r = i.call("batch_modify_emails", {"messageIds": list(i.state["mailboxes"]["alex@acme.com"]["messages"])[:1], "addLabelIds": ["NOPE"]})
    assert r.is_error
    assert json.dumps(i.state, sort_keys=True, default=str) == before


def test_timeout_after_commit_really_commits():
    i = Instance(get_service("gmail"), faults=[{"tool": "send_email", "kind": "timeout_after_commit", "on_call": 1}])
    args = {"to": ["john@acme.com"], "subject": "Q4", "body": "Tue 10am"}
    r = i.call("send_email", args)
    assert r.is_error and "timed out" in r.text
    sent = [m for m in i.state["mailboxes"]["alex@acme.com"]["messages"].values() if "SENT" in m["labelIds"] and m["subject"] == "Q4"]
    assert len(sent) == 1, "the email went out even though the caller saw a timeout"
    assert i.calls[-1]["committed"] and i.calls[-1]["fault"] == "timeout_after_commit"
    assert not i.call("send_email", args).is_error  # the fault fired once; a retry duplicates


def test_timeout_before_commit_does_nothing():
    i = Instance(get_service("gmail"), faults=[{"tool": "send_*", "kind": "timeout"}])
    n = len(i.state["mailboxes"]["alex@acme.com"]["messages"])
    assert i.call("send_email", {"to": ["a@b.co"], "subject": "s", "body": "b"}).is_error
    assert len(i.state["mailboxes"]["alex@acme.com"]["messages"]) == n and not i.calls[-1]["committed"]


def test_probabilistic_faults_are_seeded():
    spec = [{"tool": "*", "kind": "rate_limit", "probability": 0.5, "times": None}]
    runs = []
    for _ in range(2):
        i = Instance(get_service("slack"), faults=spec, rng_seed=3)
        runs.append([i.call("slack_get_users", {}).is_error for _ in range(20)])
    assert runs[0] == runs[1] and any(runs[0]) and not all(runs[0])


def test_unknown_fault_kind_rejected():
    with pytest.raises(ValueError):
        Fault(kind="explode")
