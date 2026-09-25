"""A coordinator in front of several hosts: placement, routing, moves, rebalancing."""

from fastapi.testclient import TestClient

from toolsim.cluster import Cluster, create_coordinator
from toolsim.host import HostConfig, create_app

SPEC = {"name": "support", "servers": {"gmail": {}},
        "components": {"db": {"type": "sqlite", "sql": "CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT);"}}}


def cluster(n: int = 3):
    workers = {f"http://w{i}": TestClient(create_app(config=HostConfig())) for i in range(n)}
    c = Cluster(list(workers), client=lambda url: workers[url])
    return TestClient(create_coordinator(c)), c, workers


def test_runs_spread_route_and_move():
    coord, c, workers = cluster()
    for i in range(6):
        coord.post("/envs", json={"spec": SPEC, "id": f"r{i}"})
    per_worker = {w: len(cl.get("/envs").json()["envs"]) for w, cl in workers.items()}
    assert sorted(per_worker.values()) == [2, 2, 2], "least-loaded placement"
    home = c.placement["r0"]
    agents = workers[home].get("/envs/r0").json()["agents"]["agent"]["mcpServers"]["gmail"]["url"]
    assert agents.startswith("http://testserver/instances/"), "agents talk to their worker directly"
    coord.post("/envs/r0/mutate", json={"component": "db", "op": "sql",
                                        "params": {"statement": "INSERT INTO t(v) VALUES ('x')"}})
    assert coord.get("/envs/r0/journal").json()["steps"] == 1, "routed to the run's worker"
    branch = coord.post("/envs/r0/branch", json={"step": 0, "id": "r0-b"}).json()
    assert branch["id"] == "r0-b" and c.placement["r0-b"] == home
    target = next(w for w in workers if w != home)
    moved = coord.post("/envs/r0/move", json={"to": target}).json()
    assert moved["moved"] and c.placement["r0"] == target
    assert workers[home].get("/envs/r0").status_code == 404
    assert coord.get("/envs/r0/components").json()["db"]["state"]["tables"]["t"] == [{"id": 1, "v": "x"}]
    assert coord.post("/envs/r0/replay").json()["reproducible"]
    assert len(coord.get("/envs").json()["envs"]) == 7
    assert coord.get("/envs/nope/journal").status_code == 404


def test_rebalance_evens_the_load():
    coord, c, workers = cluster(2)
    first = next(iter(workers))
    for i in range(5):  # all on one machine, e.g. created there directly
        workers[first].post("/envs", json={"spec": SPEC, "id": f"x{i}"})
    moves = coord.post("/cluster/rebalance").json()["moves"]
    assert sum(m["moved"] for m in moves) == 2
    assert sorted(len(cl.get("/envs").json()["envs"]) for cl in workers.values()) == [2, 3]
    assert all(v["ok"] for v in coord.get("/cluster").json()["workers"].values())
