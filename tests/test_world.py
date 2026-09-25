"""The world runtime: components, journal, checkpoints, branches, replays, diffs, evaluation."""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from toolsim.env import Environment, EnvRun
from toolsim.host import HostConfig, create_app
from toolsim.world import DirectoryComponent, RemoteComponent, SQLiteComponent, World, serve_component

SCHEMA = "CREATE TABLE orders(id INTEGER PRIMARY KEY, customer TEXT, status TEXT);" \
         "INSERT INTO orders(customer, status) VALUES ('globex', 'paid'), ('initech', 'paid');"


def test_a_bare_world_of_a_directory_and_a_database():
    w = World()
    ws = w.add("workspace", DirectoryComponent(files={"README.md": "# app\n", "src/app.py": "print('v1')\n"}))
    w.add("db", SQLiteComponent(sql=SCHEMA))
    start = w.checkpoint()
    w.mutate("db", "sql", statement="UPDATE orders SET status = 'refunded' WHERE customer = ?", params=["globex"])
    w.mutate("workspace", "replace", path="src/app.py", old="v1", new="v2")
    changes = w.diff(start)
    assert {c["path"] for c in changes["db"]} == {"tables.orders.1.status"}
    assert changes["workspace"] == [{"path": "files.src/app.py", "op": "changed", "before": "print('v1')\n",
                                     "after": "print('v2')\n"}]
    graded = w.evaluate([{"server": "db", "state": "tables.orders", "where": {"customer": "globex",
                                                                               "status": "refunded"}},
                         {"server": "workspace", "state": "files", "where": {"src/app.py~": "v2"}, "count": 0}])
    assert graded["checks"][0]["passed"]
    earlier = w.branch(1)  # after the first mutation only
    assert earlier.components["db"].view()["tables"]["orders"][0]["status"] == "refunded"
    assert "v1" in (earlier.components["workspace"].root / "src/app.py").read_text()
    assert "v2" in (ws.root / "src/app.py").read_text(), "branches never touch the original"
    w.close()
    earlier.close()


def test_restore_puts_a_directory_back_exactly():
    d = DirectoryComponent(files={"a.txt": "a"})
    snap = d.snapshot()
    (d.root / "new").mkdir()
    (d.root / "new/b.bin").write_bytes(b"\x00\xff")
    (d.root / "a.txt").write_text("changed")
    assert "<binary 2 bytes" in d.view()["files"]["new/b.bin"]
    d.restore(snap)
    assert sorted(p.name for p in d.root.rglob("*")) == ["a.txt"] and (d.root / "a.txt").read_text() == "a"
    with pytest.raises(ValueError):
        d.mutate("write", path="../escape.txt", content="x")
    d.close()


def test_any_process_can_be_a_component_over_http():
    url, server = serve_component(SQLiteComponent(sql=SCHEMA))
    try:
        w = World()
        w.add("crm", RemoteComponent(url))
        cp = w.checkpoint()
        rows = w.mutate("crm", "sql", statement="SELECT customer FROM orders ORDER BY id")
        assert [r["customer"] for r in rows] == ["globex", "initech"]
        w.mutate("crm", "sql", statement="DELETE FROM orders WHERE customer = 'initech'")
        assert w.diff(cp)["crm"][0] == {"path": "tables.orders.2", "op": "removed", "before": {
            "id": 2, "customer": "initech", "status": "paid"}}
        other = w.branch(cp)
        assert len(other.components["crm"].view()["tables"]["orders"]) == 2, "a clone served by the process"
        assert len(w.components["crm"].view()["tables"]["orders"]) == 1
        assert [m["name"] for m in w.components["crm"].mutations()] == ["sql"]
    finally:
        server.shutdown()
    with pytest.raises(ValueError):
        RemoteComponent("file:///etc/passwd")


SPEC = {"name": "support", "servers": {"gmail": {}},
        "components": {"db": {"type": "sqlite", "sql": SCHEMA},
                       "workspace": {"type": "directory", "files": {"notes.md": "todo\n"}}},
        "events": [{"component": "db", "mutate": "sql", "at": "+10m",
                    "params": {"statement": "INSERT INTO orders(customer, status) VALUES ('hooli', 'paid')"}}],
        "checks": [{"server": "db", "state": "tables.orders", "where": {"customer": "globex", "status": "refunded"}},
                   {"server": "gmail", "state": "messages", "where": {"subject~": "Refund"}, "min": 1}]}


def act(run: EnvRun) -> None:
    run.mutate("db", "sql", statement="UPDATE orders SET status = 'refunded' WHERE customer = 'globex'")
    run.instances["gmail"].call("send_email", {"to": ["john@acme.com"], "subject": "Refund done", "body": "."})
    run.advance(900)  # the +10m event fires
    run.mutate("workspace", "append", path="notes.md", content="refunded globex\n")
    run.submit("Refunded globex.")


def test_environments_are_worlds():
    run = EnvRun(Environment.from_dict(SPEC))
    act(run)
    assert run.grade()["passed"]
    kinds = [e["kind"] for e in run.journal]
    assert kinds == ["mutate", "call", "advance", "mutate", "answer"]
    assert [r["customer"] for r in run.components["db"].view()["tables"]["orders"]] == ["globex", "initech", "hooli"]
    changes = run.diff(0)
    assert {"db", "workspace", "gmail"} <= set(changes)
    assert any(c["path"].startswith("messages.") and c["op"] == "added" for c in changes["gmail"])
    assert run.replay() == [], "the whole run reproduces exactly"


def test_branching_at_any_step_and_independence():
    run = EnvRun(Environment.from_dict(SPEC))
    act(run)
    before_send = run.branch(1)
    assert before_send.components["db"].view()["tables"]["orders"][0]["status"] == "refunded"
    assert not any("Refund" in m["subject"] for m in before_send.instances["gmail"].state["mailboxes"]
                   ["alex@acme.com"]["messages"].values())
    assert len(before_send.journal) == 1 and before_send.answer is None
    before_send.mutate("workspace", "write", path="notes.md", content="another future\n")
    assert "refunded globex" in (run.components["workspace"].root / "notes.md").read_text()
    whole = run.branch()
    assert whole.snapshot()["instances"]["gmail"]["state"] == run.snapshot()["instances"]["gmail"]["state"]
    assert whole.answer == "Refunded globex." and whole.grade()["passed"]
    fork = run.fork("f")
    fork.mutate("db", "sql", statement="DELETE FROM orders")
    assert len(run.components["db"].view()["tables"]["orders"]) == 3
    cp = run.checkpoint()
    run.mutate("db", "sql", statement="DELETE FROM orders WHERE customer = 'hooli'")
    assert len(run.diff(cp)["db"]) == 1
    run.restore(run.world.checkpoints[cp])
    assert len(run.journal) == cp and len(run.components["db"].view()["tables"]["orders"]) == 3


def test_realtime_runs_replay_deterministically():
    spec = {**SPEC, "time": {"speed": 1000}}
    run = EnvRun(Environment.from_dict(spec))
    g = run.instances["gmail"]
    g.call("send_email", {"to": ["jhon@acme.com"], "subject": "typo", "body": "."})
    time.sleep(0.2)  # ~200 simulated seconds: the bounce arrives on its own
    run.tick()
    g.call("search_emails", {"query": "from:mailer-daemon"})
    assert any(e["kind"] == "time" for e in run.journal), "wall time is part of the record"
    assert run.replay() == []
    replayed = run.branch()
    assert replayed.clock.now == run.clock.now or replayed.clock.speed == 1000


def test_rest_calls_through_the_gateway_replay_too():
    c = TestClient(create_app(config=HostConfig()))
    env = c.post("/envs", json={"spec": {"name": "w", "servers": {"gmail": {}}}, "id": "w"}).json()
    h = {"Authorization": f"Bearer {env['credentials']['agent']['google_access_token']}"}
    import base64
    raw = base64.urlsafe_b64encode(b"To: john@acme.com\r\nSubject: hi\r\n\r\nhello").decode()
    c.post("/gw/gmail.googleapis.com/gmail/v1/users/me/messages/send", headers=h, json={"raw": raw})
    c.get("/gw/gmail.googleapis.com/gmail/v1/users/me/messages", headers=h, params={"q": "subject:hi"})
    run = c.app.state.host.envs["w"]
    assert [e["tool"] for e in run.journal] == ["gmail.users.messages.send", "gmail.users.messages.list"]
    assert run.replay() == []


def test_inline_specs_cant_touch_host_paths():
    with pytest.raises(ValueError, match="path"):
        EnvRun(Environment.from_dict({"name": "x", "servers": {},
                                      "components": {"d": {"type": "directory", "path": "/etc"}}}, base_dir=None))
    with pytest.raises(ValueError, match="remote"):
        EnvRun(Environment.from_dict({"name": "x", "servers": {},
                                      "components": {"r": {"type": "remote", "url": "http://x"}}}, base_dir=None))


def test_the_runtime_over_http():
    c = TestClient(create_app(config=HostConfig()))
    c.post("/envs", json={"spec": SPEC, "id": "s"})
    c.post("/envs/s/mutate", json={"component": "db", "op": "sql",
                                   "params": {"statement": "UPDATE orders SET status='refunded' WHERE id=1"}})
    cp = c.post("/envs/s/checkpoint").json()["step"]
    c.post("/envs/s/mutate", json={"component": "gmail", "op": "deliver_email",
                                   "params": {"sender": "a@x.io", "subject": "late", "body": "."}})
    assert c.get("/envs/s/journal").json()["steps"] == 2
    assert set(c.get("/envs/s/diff", params={"since": cp}).json()["changes"]) == {"gmail"}
    b = c.post("/envs/s/branch", json={"step": 1, "id": "s-b"}).json()
    assert b["id"] == "s-b" and b["branched_from"]["step"] == 1
    assert c.get("/envs/s-b/journal").json()["steps"] == 1
    assert c.post("/envs/s/replay").json() == {"reproducible": True, "steps": 2, "diverged": []}
    comps = c.get("/envs/s/components").json()
    assert comps["db"]["kind"] == "sqlite" and comps["gmail"]["mutations"]
    assert c.post("/envs/s/mutate", json={"component": "nope", "op": "x"}).status_code == 400


def test_the_store_shares_what_didnt_change_and_keeps_types(tmp_path):
    from toolsim.world.store import Store
    st = Store(tmp_path / "w.db")
    value = {"issues": {4: {"title": "x" * 300}}, "rng": (3, (1, 2), None), "big": ["y" * 300, "z" * 300]}
    assert st.get(st.put(value)) == value, "integer keys and tuples survive"
    run = EnvRun(Environment.from_dict({"name": "w", "servers": {"gmail": {}, "github": {}}}), store=st)
    first = st.put(run.snapshot())
    before = st.stats()["bytes"]
    run.instances["gmail"].call("send_email", {"to": ["john@acme.com"], "subject": "x", "body": "."})
    second = st.put(run.snapshot())
    added = st.stats()["bytes"] - before
    assert first != second and added < before / 2, "the github half and most of gmail are shared"
    cp = run.checkpoint()
    assert set(run.world.checkpoints[cp]) == {"$ref"}
    st.close()
    reopened = Store(tmp_path / "w.db")
    assert reopened.get(second)["instances"]["github"] == run.snapshot()["instances"]["github"]


def test_runs_move_between_hosts_and_survive_restarts(tmp_path):
    a = TestClient(create_app(config=HostConfig(store=tmp_path / "a.db")))
    b = TestClient(create_app(config=HostConfig()))
    a.post("/envs", json={"spec": SPEC, "id": "s"})
    a.post("/envs/s/mutate", json={"component": "db", "op": "sql",
                                   "params": {"statement": "UPDATE orders SET status='refunded' WHERE id=1"}})
    moved = b.post("/envs/import", json={"run": a.get("/envs/s/export").json(), "id": "s2"})
    assert moved.status_code == 200
    assert b.get("/envs/s2/journal").json()["steps"] == 1
    assert b.post("/envs/s2/replay").json()["reproducible"]
    assert b.get("/envs/s2/components").json()["db"]["state"]["tables"]["orders"][0]["status"] == "refunded"
    a.post("/envs/s/save", json={"name": "support-demo"})
    restarted = TestClient(create_app(config=HostConfig(store=tmp_path / "a.db")))
    loaded = restarted.post("/envs/load", json={"name": "support-demo", "id": "back"})
    assert loaded.status_code == 200 and restarted.get("/envs/back/journal").json()["steps"] == 1
    assert restarted.post("/envs/load", json={"name": "nope"}).status_code == 400
    assert b.post("/envs/s2/save", json={"name": "x"}).status_code == 400, "no store configured"


def test_any_machine_with_a_cli_is_a_component(tmp_path):
    from toolsim.world import CommandComponent
    box = tmp_path / "box"
    box.mkdir()
    (box / "app.cfg").write_text("mode=a\n")
    machine = CommandComponent({
        "snapshot": "tar -C {dir} -cf - . | base64",
        "restore": "find {dir} -mindepth 1 -delete && echo {snapshot} | base64 -d | tar -C {dir} -xf -",
        "clone": 'd=$(mktemp -d) && tar -C {dir} -cf - . | tar -C "$d" -xf - && printf \'{{"dir": "%s"}}\' "$d"',
        "view": "cd {dir} && for f in $(find . -type f | sort); do echo \"$f $(cat $f)\"; done",
        "mutate.set": "printf %s {line} > {dir}/app.cfg",
    }, handle={"dir": str(box)})
    w = World()
    w.add("box", machine)
    cp = w.checkpoint()
    w.mutate("box", "set", line="mode=b")
    assert w.view()["box"]["lines"] == ["./app.cfg mode=b"]
    other = w.branch(cp)
    assert other.view()["box"]["lines"] == ["./app.cfg mode=a"], "a clone restored to the checkpoint"
    assert (box / "app.cfg").read_text() == "mode=b"
    w.restore(w.checkpoints[cp])
    assert (box / "app.cfg").read_text() == "mode=a\n"
    assert [m["name"] for m in machine.mutations()] == ["set"]


def test_containers_via_docker(tmp_path, monkeypatch):
    import sys as _sys
    from toolsim.world import DockerComponent
    fake = tmp_path / "docker"
    fake.write_text(f"#!/bin/sh\nexec {_sys.executable} {Path(__file__).parent / 'fake_docker.py'} \"$@\"\n")
    fake.chmod(0o755)
    monkeypatch.setenv("FAKE_DOCKER_HOME", str(tmp_path / "dh"))
    box = DockerComponent("python:3.12-slim", name="agent-box", docker=str(fake))
    w = World()
    w.add("box", box)
    cp = w.checkpoint()
    assert w.checkpoints[cp]["components"]["box"]["snapshot"].startswith("sha256:"), "an image layer"
    w.mutate("box", "write", path="work/todo.txt", content="ship it\n")
    assert w.view()["box"]["lines"] == ["A /work/todo.txt"]
    assert w.mutate("box", "exec", cmd="cat work/todo.txt") == "ship it"
    other = w.branch(cp)
    assert other.view()["box"]["lines"] == [], "the branch is a new container from the checkpoint image"
    w.restore(w.checkpoints[cp])
    assert w.view()["box"]["lines"] == []
    log = (tmp_path / "dh" / "log.txt").read_text()
    assert "commit agent-box" in log and "rm -f agent-box" in log
    other.close()
    box.close()
