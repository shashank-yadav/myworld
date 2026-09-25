"""Many machines: a coordinator in front of ordinary toolsim hosts (workers).

    toolsim serve --port 9001 &  toolsim serve --port 9002 &
    toolsim coordinator --worker http://w1:9001 --worker http://w2:9002 --port 9000

- New runs go to the least-loaded worker. Their agents get that worker's own URLs (MCP, the API
  gateway), so agent traffic never passes through the coordinator.
- Every ``/envs/{id}/...`` request is routed to the worker that holds the run; branches and
  counterfactual runs stay with their parent.
- ``POST /envs/{id}/move {"to": url}`` moves a run between machines (export, import, delete), and
  ``POST /cluster/rebalance`` evens out the load.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any

import httpx
from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from . import __version__


class Cluster:
    def __init__(self, workers: list[str], token: str | None = None,
                 client: Callable[[str], httpx.Client] | None = None, timeout: float = 60.0):
        if not workers:
            raise ValueError("a cluster needs at least one worker")
        self.workers = [w.rstrip("/") for w in workers]
        self.token = token
        self._make = client or (lambda url: httpx.Client(base_url=url, timeout=timeout))
        self._clients: dict[str, httpx.Client] = {}
        self.placement: dict[str, str] = {}  # run id -> worker
        self._lock = threading.RLock()

    def client(self, worker: str) -> httpx.Client:
        with self._lock:
            if worker not in self._clients:
                self._clients[worker] = self._make(worker)
            return self._clients[worker]

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def request(self, worker: str, method: str, path: str, **kw: Any) -> httpx.Response:
        return self.client(worker).request(method, path, headers={**self._headers(), **kw.pop("headers", {})}, **kw)

    # -- load and placement ---------------------------------------------------------------------

    def health(self) -> dict[str, dict[str, Any]]:
        out = {}
        for w in self.workers:
            try:
                r = self.request(w, "GET", "/healthz")
                out[w] = {"ok": r.status_code == 200, **(r.json() if r.status_code == 200 else {})}
            except httpx.HTTPError as e:
                out[w] = {"ok": False, "error": type(e).__name__}
        return out

    def pick(self) -> str:
        health = self.health()
        live = [w for w in self.workers if health[w]["ok"]]
        if not live:
            raise RuntimeError("no worker is reachable")
        return min(live, key=lambda w: (health[w].get("envs", 0), self.workers.index(w)))

    def locate(self, run_id: str) -> str:
        with self._lock:
            if run_id in self.placement:
                return self.placement[run_id]
        for w in self.workers:  # a run created on a worker directly, or before a coordinator restart
            try:
                if self.request(w, "GET", f"/envs/{run_id}").status_code == 200:
                    with self._lock:
                        self.placement[run_id] = w
                    return w
            except httpx.HTTPError:
                continue
        raise KeyError(run_id)

    def place(self, run_id: str, worker: str) -> None:
        with self._lock:
            self.placement[run_id] = worker

    # -- moving runs --------------------------------------------------------------------------------

    def move(self, run_id: str, to: str) -> dict[str, Any]:
        to = to.rstrip("/")
        if to not in self.workers:
            raise ValueError(f"{to} is not a worker")
        src = self.locate(run_id)
        if src == to:
            return {"run": run_id, "from": src, "to": to, "moved": False}
        exported = self.request(src, "GET", f"/envs/{run_id}/export")
        if exported.status_code != 200:
            raise ValueError(f"can't export {run_id}: {exported.text[:200]}")
        imported = self.request(to, "POST", "/envs/import", json={"run": exported.json(), "id": run_id})
        if imported.status_code != 200:
            raise ValueError(f"can't import {run_id} on {to}: {imported.text[:200]}")
        self.request(src, "DELETE", f"/envs/{run_id}")
        self.place(run_id, to)
        return {"run": run_id, "from": src, "to": to, "moved": True, "env": imported.json()}

    def rebalance(self) -> list[dict[str, Any]]:
        """Move runs from the busiest workers to the idlest until no two differ by more than one."""
        runs: dict[str, list[str]] = {}
        for w in self.workers:
            r = self.request(w, "GET", "/envs")
            runs[w] = [e["id"] for e in r.json()["envs"]] if r.status_code == 200 else []
            for rid in runs[w]:
                self.place(rid, w)
        moves = []
        while True:
            busiest = max(self.workers, key=lambda w: len(runs[w]))
            idlest = min(self.workers, key=lambda w: len(runs[w]))
            if len(runs[busiest]) - len(runs[idlest]) <= 1:
                return moves
            rid = runs[busiest].pop()
            try:
                moves.append({k: v for k, v in self.move(rid, idlest).items() if k != "env"})
                runs[idlest].append(rid)
            except ValueError as e:  # e.g. a run with local-path components: leave it
                moves.append({"run": rid, "from": busiest, "to": idlest, "moved": False, "error": str(e)})
                runs[idlest].append(rid)


def create_coordinator(cluster: Cluster, token: str | None = None) -> FastAPI:
    app = FastAPI(title="toolsim coordinator", version=__version__)

    @app.middleware("http")
    async def guard(request: Request, call_next: Any) -> Response:
        if token and request.url.path != "/healthz":
            given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            if given != token:
                return JSONResponse({"detail": "missing or invalid token"}, status_code=401)
        return await call_next(request)

    def forward(worker: str, request_method: str, path: str, query: str, body: bytes,
                ctype: str | None) -> Response:
        r = cluster.request(worker, request_method, path + (f"?{query}" if query else ""), content=body or None,
                            headers={"Content-Type": ctype} if ctype else {})
        headers = {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "location")}
        return Response(r.content, status_code=r.status_code, headers=headers)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "version": __version__, "workers": len(cluster.workers)}

    @app.get("/cluster")
    def cluster_state() -> dict[str, Any]:
        return {"workers": cluster.health(), "placement": dict(cluster.placement)}

    @app.post("/cluster/rebalance")
    def rebalance() -> dict[str, Any]:
        return {"moves": cluster.rebalance()}

    @app.post("/envs")
    async def start(request: Request) -> Response:
        try:
            worker = cluster.pick()
        except RuntimeError as e:
            raise HTTPException(503, str(e)) from None
        r = forward(worker, "POST", "/envs", request.url.query, await request.body(),
                    request.headers.get("content-type"))
        if r.status_code == 200:
            cluster.place(json.loads(r.body)["id"], worker)
        return r

    @app.post("/envs/import")
    async def import_run(request: Request) -> Response:
        worker = cluster.pick()
        r = forward(worker, "POST", "/envs/import", "", await request.body(), request.headers.get("content-type"))
        if r.status_code == 200:
            cluster.place(json.loads(r.body)["id"], worker)
        return r

    @app.get("/envs")
    def list_envs() -> dict[str, Any]:
        out = []
        for w in cluster.workers:
            try:
                r = cluster.request(w, "GET", "/envs")
            except httpx.HTTPError:
                continue
            if r.status_code == 200:
                for e in r.json()["envs"]:
                    cluster.place(e["id"], w)
                    out.append({**e, "worker": w})
        return {"envs": out}

    @app.post("/envs/{run_id}/move")
    def move(run_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return cluster.move(run_id, str(body.get("to") or ""))
        except KeyError:
            raise HTTPException(404, f"no environment run {run_id}") from None
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    @app.api_route("/envs/{run_id}/{rest:path}", methods=["GET", "POST", "DELETE"])
    @app.api_route("/envs/{run_id}", methods=["GET", "DELETE"])
    async def route(run_id: str, request: Request, rest: str = "") -> Response:
        try:
            worker = cluster.locate(run_id)
        except KeyError:
            raise HTTPException(404, f"no environment run {run_id}") from None
        path = f"/envs/{run_id}" + (f"/{rest}" if rest else "")
        r = forward(worker, request.method, path, request.url.query, await request.body(),
                    request.headers.get("content-type"))
        if r.status_code == 200 and rest in ("branch", "fork", "counterfactual"):
            data = json.loads(r.body)
            for rid in [data.get("id"), *(x.get("run") for x in data.get("reports", [data]) if isinstance(x, dict))]:
                if rid:
                    cluster.place(rid, worker)  # derived runs live with their parent
        if request.method == "DELETE" and not rest and r.status_code == 200:
            cluster.placement.pop(run_id, None)
        return r

    return app
