"""Regression tests for the production-readiness audit."""

import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from toolsim.core.instance import Instance
from toolsim.env import Environment, EnvRun
from toolsim.host import Host, HostConfig, create_app
from toolsim.importers import IMPORTERS
from toolsim.services import get_service

ENVS = Path(__file__).parent.parent / "envs"


def rpc(tool, args=None, mid=1):
    return {"jsonrpc": "2.0", "id": mid, "method": "tools/call", "params": {"name": tool, "arguments": args or {}}}


# -- tool robustness ------------------------------------------------------------------------------

def test_unexpected_input_is_rolled_back_and_reported():
    i = Instance(get_service("notion"))
    before = len(i.state["pages"])
    wiki = next(p["id"] for p in i.state["pages"].values() if p["title"] == "Engineering Wiki")
    # the second page is not an object: a proper validation error, and the first page is NOT created
    r = i.call("notion-create-pages", {"parent": {"page_id": wiki}, "pages": [{"properties": {"title": "ok"}}, "oops"]})
    assert r.is_error and "pages[1]" in r.text and len(i.state["pages"]) == before


def test_bugs_inside_tools_become_500s_with_rollback(monkeypatch):
    i = Instance(get_service("gmail"))
    tool = i.tools["send_email"]
    original = tool.fn

    def half_then_crash(ctx, **kw):
        original(ctx, **kw)
        raise RuntimeError("boom")
    monkeypatch.setattr(tool, "fn", half_then_crash)
    n = len(i.state["mailboxes"]["alex@acme.com"]["messages"])
    r = i.call("send_email", {"to": ["a@b.co"], "subject": "s", "body": "b"})
    assert r.is_error and "Internal error while handling send_email" in r.text
    assert len(i.state["mailboxes"]["alex@acme.com"]["messages"]) == n, "the half-done write was rolled back"
    assert i.calls[-1]["internal_error"] == "RuntimeError: boom"


@pytest.mark.parametrize("args,expect", [
    ({"to": "john@acme.com", "subject": "s", "body": "b"}, None),               # a string becomes [string], as MCP servers accept
    ({"to": [1, 2], "subject": "s", "body": "b"}, "to[0]"),                     # wrong item type
    ({"to": ["a@b.co"], "subject": {"x": 1}, "body": "b"}, "subject"),         # object where a string belongs
    ({"to": ["a@b.co"], "subject": "s", "body": "b", "cc": "x@y.z"}, None),
])
def test_deep_argument_validation(args, expect):
    r = Instance(get_service("gmail")).call("send_email", args)
    if expect is None:
        assert not r.is_error, r.text
    else:
        assert r.is_error and expect in r.text


def test_bad_seeds_and_faults_are_clear_errors():
    with pytest.raises(ValueError, match="invalid seed for slack"):
        Instance(get_service("slack"), {"channels": [{"no_name": True}]})
    with pytest.raises(ValueError, match="invalid fault"):
        Instance(get_service("slack"), faults=[{"kind": "timeout", "bogus": 1}])


# -- host security ----------------------------------------------------------------------------------

def test_env_files_only_from_allowed_dirs(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("password: hunter2\n")
    c = TestClient(create_app())
    r = c.post("/envs", json={"file": str(secret)})
    assert r.status_code == 400 and "disabled" in r.text and "hunter2" not in r.text
    c = TestClient(create_app(config=HostConfig(env_dirs=[ENVS])))
    for name in [str(secret), "../pyproject.toml", "/etc/hosts"]:
        r = c.post("/envs", json={"file": name})
        assert r.status_code == 400 and "localhost" not in r.text and "hunter2" not in r.text
    assert c.post("/envs", json={"file": "merge-when-green.yaml"}).status_code == 200
    r = c.post("/envs", json={"spec": {"name": "x", "servers": {"gmail": {"seed_file": "/etc/hosts"}}}})
    assert r.status_code == 400 and "inline" in r.text


def test_seed_files_stay_inside_the_env_directory(tmp_path):
    (tmp_path / "outside.yaml").write_text("user: {email: x@y.z}\n")
    d = tmp_path / "envs"
    d.mkdir()
    (d / "e.yaml").write_text("name: e\nservers:\n  gmail: {seed_file: ../outside.yaml}\n")
    with pytest.raises(ValueError, match="inside the environment's directory"):
        EnvRun(Environment.load(d / "e.yaml"))
    (d / "bad.yaml").write_text("name: [unclosed\n")
    with pytest.raises(ValueError, match=r"bad.yaml: invalid YAML \(line"):
        Environment.load(d / "bad.yaml")


def test_origin_check():
    c = TestClient(create_app(config=HostConfig(allowed_origins=["https://studio.example"])))
    c.post("/instances", json={"service": "slack", "id": "s"})
    assert c.post("/instances/s/mcp", json=rpc("slack_get_users"), headers={"Origin": "http://evil.example"}).status_code == 403
    assert c.post("/instances/s/mcp", json=rpc("slack_get_users"), headers={"Origin": "http://localhost:3000"}).status_code == 200
    assert c.post("/instances/s/mcp", json=rpc("slack_get_users"), headers={"Origin": "https://studio.example"}).status_code == 200
    assert c.post("/instances/s/mcp", json=rpc("slack_get_users")).status_code == 200, "non-browser clients send no Origin"


def test_token_auth():
    c = TestClient(create_app(config=HostConfig(token="s3cret")))  # noqa: S106
    assert c.get("/healthz").status_code == 200, "health checks never need auth"
    assert c.get("/services").status_code == 401
    assert c.get("/services", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/services", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert c.get("/services?token=s3cret").status_code == 200, "for MCP clients that only take a URL"


def test_limits():
    c = TestClient(create_app(config=HostConfig(max_instances=3, max_snapshots=2, max_hang_s=5, max_body_bytes=2000)))
    for _ in range(3):
        assert c.post("/instances", json={"service": "slack"}).status_code == 200
    assert "limit" in c.post("/instances", json={"service": "slack"}).text
    iid = c.get("/instances").json()["instances"][0]["id"]
    snaps = [c.post(f"/instances/{iid}/snapshot").json()["snapshot_id"] for _ in range(3)]
    assert c.post(f"/instances/{iid}/restore", json={"snapshot_id": snaps[0]}).status_code == 404, "oldest evicted"
    assert c.post(f"/instances/{iid}/restore", json={"snapshot_id": snaps[2]}).status_code == 200
    r = c.put(f"/instances/{iid}/faults", json=[{"kind": "latency", "hang_s": 999}])
    assert r.status_code == 400 and "outside" in r.text
    assert c.post(f"/instances/{iid}/mcp", content=b"x" * 5000, headers={"content-type": "application/json"}).status_code == 413
    assert c.post("/instances", json={"service": "slack", "id": "../../etc"}).status_code == 400


def test_stale_and_mismatched_snapshots_are_4xx():
    c = TestClient(create_app())
    c.post("/envs", json={"spec": {"name": "a", "servers": {"gmail": {}}}, "id": "a"})
    sid = c.post("/envs/a/snapshot").json()["snapshot_id"]
    c.delete("/envs/a")
    c.post("/envs", json={"spec": {"name": "b", "servers": {"slack": {}}}, "id": "b"})
    assert c.post("/envs/b/restore", json={"snapshot_id": sid}).status_code == 400
    c.post("/instances", json={"service": "gmail", "id": "g"})
    isnap = c.post("/instances/g/snapshot").json()["snapshot_id"]
    c.delete("/instances/g")
    c.post("/instances", json={"service": "slack", "id": "s"})
    assert c.post("/instances/s/restore", json={"snapshot_id": isnap}).status_code == 400
    assert c.post("/instances/s/restore", json={"snapshot_id": "nope"}).status_code == 404
    assert c.delete("/instances/b-slack").status_code == 400, "instances of an env run are deleted with the run"


def test_errors_are_json_not_crashes():
    c = TestClient(create_app(), raise_server_exceptions=False)
    assert c.post("/instances", json={"service": "gmail", "seed": "nope"}).status_code == 400
    assert c.post("/instances", json={"service": "gmail", "faults": "nope"}).status_code == 400
    assert c.post("/envs", json={"spec": {"servers": {}}}).status_code == 400
    assert c.post("/envs", json={}).status_code == 400
    c.post("/envs", json={"spec": {"name": "e", "servers": {"gmail": {}}}, "id": "e"})
    assert c.post("/envs/e/advance", json={"seconds": "soon"}).status_code == 400
    assert c.post("/envs/e/events", json={"server": "gmail", "action": "deliver_email", "params": "x"}).status_code == 400


# -- concurrency ------------------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_a_slow_call_does_not_block_other_clients():
    """Real uvicorn server: a hanging call on one instance must not stall others."""
    port = _free_port()
    host = Host()
    server = uvicorn.Server(uvicorn.Config(create_app(host), host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        base = f"http://127.0.0.1:{port}"
        httpx.post(f"{base}/instances", json={"service": "slack", "id": "slow",
                                              "faults": [{"kind": "latency", "hang_s": 1.5, "times": None}]})
        httpx.post(f"{base}/instances", json={"service": "slack", "id": "fast"})
        slow = threading.Thread(target=lambda: httpx.post(f"{base}/instances/slow/mcp", json=rpc("slack_get_users"), timeout=10))
        slow.start()
        time.sleep(0.2)
        t0 = time.time()
        assert httpx.post(f"{base}/instances/fast/mcp", json=rpc("slack_get_users"), timeout=10).status_code == 200
        assert time.time() - t0 < 0.5, "the fast instance was blocked by the slow one"
        slow.join()
    finally:
        server.should_exit = True
        t.join(timeout=5)


def test_concurrent_creates_and_listings():
    c = TestClient(create_app())
    errors = []

    def create():
        for _ in range(20):
            if c.post("/instances", json={"service": "slack"}).status_code != 200:
                errors.append("create")

    def listing():
        for _ in range(40):
            if c.get("/instances").status_code != 200:
                errors.append("list")
    threads = [threading.Thread(target=create) for _ in range(4)] + [threading.Thread(target=listing) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(c.get("/instances").json()["instances"]) == 80


def test_concurrent_agents_on_one_env_keep_a_consistent_timeline():
    c = TestClient(create_app())
    c.post("/envs", json={"spec": {"name": "e", "servers": {"slack": {}, "github": {}}}, "id": "e"})

    def hammer(server, tool, args):
        for _ in range(25):
            c.post(f"/instances/e-{server}/mcp", json=rpc(tool, args))
    threads = [threading.Thread(target=hammer, args=("slack", "slack_get_users", {})),
               threading.Thread(target=hammer, args=("github", "search_users", {"q": "john"}))]
    [t.start() for t in threads]
    [t.join() for t in threads]
    tl = c.get("/envs/e/calls").json()["calls"]
    assert [x["global_seq"] for x in tl] == list(range(1, 51))
    assert [x["at"] for x in tl] == sorted(x["at"] for x in tl)


# -- CLI and importers --------------------------------------------------------------------------------

def test_importer_missing_path_is_a_clear_error(tmp_path):
    with pytest.raises(ValueError, match="no such file"):
        IMPORTERS["gmail"](tmp_path / "missing.mbox")
    assert not (tmp_path / "missing.mbox").exists(), "the importer must not create files"


def test_cli_errors_are_messages(tmp_path, capsys):
    from toolsim.cli import main
    with pytest.raises(SystemExit) as e:
        main(["import", "gmail", str(tmp_path / "missing.mbox")])
    assert e.value.code == 2 and "no such file" in capsys.readouterr().err
    with pytest.raises(SystemExit) as e:
        main(["serve", "--host", "0.0.0.0"])
    assert "without a token" in str(e.value)
