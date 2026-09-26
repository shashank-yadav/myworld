import io
import json

from fastapi.testclient import TestClient

from myworld.core import mcp
from myworld.core.world_mcp import WorldMCP, handle as world_handle, serve_stdio as serve_world_stdio
from myworld.env import Environment, EnvRun
from myworld.core.instance import Instance
from myworld.host import create_app
from myworld.services import get_service


def rpc(i, method, params=None, mid=1):
    return mcp.handle(i, {"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})


def test_protocol_basics():
    i = Instance(get_service("calendar"))
    init = rpc(i, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t"}})
    assert init["result"]["protocolVersion"] == "2025-03-26"
    assert init["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert mcp.handle(i, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = rpc(i, "tools/list")["result"]["tools"]
    assert {t["name"] for t in tools} >= {"list-events", "create-event", "get-freebusy"}
    r = rpc(i, "tools/call", {"name": "get-current-time", "arguments": {}})["result"]
    assert r["isError"] is False and "Monday" in r["content"][0]["text"]
    assert rpc(i, "tools/call", {"name": "nope"})["error"]["code"] == -32602
    assert rpc(i, "bogus/method")["error"]["code"] == -32601


def test_stdio_transport():
    i = Instance(get_service("slack"))
    stdin = io.StringIO("\n".join(json.dumps(m) for m in [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]) + "\nnot json\n")
    out = io.StringIO()
    mcp.serve_stdio(i, stdin, out)
    lines = [json.loads(l) for l in out.getvalue().splitlines()]
    assert [l.get("id") for l in lines] == [1, 2, None]
    assert len(lines[1]["result"]["tools"]) == 8
    assert lines[2]["error"]["code"] == -32700


def test_world_mcp_exposes_one_shared_multi_tool_world():
    env = Environment.load("examples/worlds/invoice-review.yaml")
    w = WorldMCP(EnvRun(env))
    init = world_handle(w, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["serverInfo"]["name"] == "myworld"
    tools = world_handle(w, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"world_task", "world_grade", "gmail__search_emails", "slack__slack_get_channel_history",
            "drive__read_sheet_values"} <= names

    task = world_handle(w, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                            "params": {"name": "world_task", "arguments": {}}})["result"]
    assert "vendor invoices" in task["content"][0]["text"]
    mail = world_handle(w, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                            "params": {"name": "gmail__search_emails",
                                       "arguments": {"query": "subject:Invoice"}}})["result"]
    assert not mail["isError"]
    assert "global-logistics.example" in mail["content"][0]["text"]


def test_world_stdio_transport():
    env = Environment.load("examples/worlds/invoice-review.yaml")
    stdin = io.StringIO("\n".join(json.dumps(m) for m in [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "world_snapshot", "arguments": {}}},
    ]) + "\n")
    out = io.StringIO()
    serve_world_stdio(WorldMCP(EnvRun(env)), stdin, out)
    lines = [json.loads(l) for l in out.getvalue().splitlines()]
    assert [l.get("id") for l in lines] == [1, 2, 3]
    assert any(t["name"] == "drive__modify_sheet_values" for t in lines[1]["result"]["tools"])
    assert "snapshot_id" in lines[2]["result"]["content"][0]["text"]


def test_host_runs_isolated_instances_with_control_plane():
    c = TestClient(create_app())
    a = c.post("/instances", json={"service": "gmail", "id": "a"}).json()
    c.post("/instances", json={"service": "gmail", "id": "b"})
    init = c.post("/instances/a/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init.headers.get("mcp-session-id")
    assert c.post("/instances/a/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    send = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "send_email", "arguments": {"to": ["x@y.co"], "subject": "hi", "body": "b"}}}
    assert "sent successfully" in c.post("/instances/a/mcp", json=send).json()["result"]["content"][0]["text"]
    sent = lambda iid: sum("SENT" in m["labelIds"] for m in c.get(f"/instances/{iid}/state").json()["state"]["mailboxes"]["alex@acme.com"]["messages"].values())  # noqa: E731
    assert sent("a") == sent("b") + 1, "instances never share state"
    assert c.get("/instances/a/calls").json()["calls"][0]["tool"] == "send_email"
    assert a["mcp_url"].endswith("/instances/a/mcp")

    snap = c.post("/instances/a/snapshot").json()["snapshot_id"]
    fork = c.post("/instances/a/fork").json()
    c.post("/instances/a/mcp", json=send)
    assert sent(fork["id"]) == sent("a") - 1
    c.post("/instances/a/restore", json={"snapshot_id": snap})
    assert sent("a") == sent(fork["id"])
    c.post("/instances/a/reset")
    assert sent("a") == sent("b")

    c.put("/instances/b/faults", json=[{"tool": "send_email", "kind": "server_error"}])
    r = c.post("/instances/b/mcp", json=send).json()["result"]
    assert r["isError"] and "Backend Error" in r["content"][0]["text"]
    assert c.put("/instances/b/faults", json=[{"kind": "nope"}]).status_code == 400
    assert c.post("/instances", json={"service": "fax"}).status_code == 400
    assert c.post("/instances/zzz/mcp", json={}).status_code == 404
    assert set(c.get("/services").json()) >= {"gmail", "calendar", "slack", "github", "jira"}
