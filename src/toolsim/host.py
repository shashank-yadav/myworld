"""Run many isolated instances behind one HTTP server.

Agent-facing (per instance):
    POST /instances/{id}/mcp              MCP streamable HTTP endpoint (JSON responses)

Control plane (for test harnesses, RL loops and graders; agents never see it):
    GET    /services                      available services and their tools
    POST   /instances                     {service, version?, seed?, rng_seed?, faults?, id?} -> {id, mcp_url}
    GET    /instances                     list
    DELETE /instances/{id}
    GET    /instances/{id}/state          full internal state (for graders)
    GET    /instances/{id}/calls          every tool call the agent made, with outcome and fault
    POST   /instances/{id}/reset          back to the seed
    POST   /instances/{id}/snapshot       -> {snapshot_id}
    POST   /instances/{id}/restore        {snapshot_id}
    PUT    /instances/{id}/faults         [fault, ...]
    POST   /instances/{id}/fork           -> a new independent instance from the current state
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .core import mcp
from .core.instance import Instance
from .services import SERVICES, get_service


class Host:
    def __init__(self) -> None:
        self.instances: dict[str, Instance] = {}
        self.snapshots: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def create(self, service: str, seed: dict[str, Any] | None = None, *, rng_seed: int = 0,
               faults: list[dict[str, Any]] | None = None, instance_id: str | None = None,
               version: str | None = None) -> Instance:
        inst = Instance(get_service(service), seed, rng_seed=rng_seed, faults=faults,
                        instance_id=instance_id or f"{service}-{uuid.uuid4().hex[:8]}", version=version)
        with self._lock:
            if inst.id in self.instances:
                raise ValueError(f"instance {inst.id} already exists")
            self.instances[inst.id] = inst
        return inst

    def get(self, instance_id: str) -> Instance:
        inst = self.instances.get(instance_id)
        if inst is None:
            raise KeyError(instance_id)
        return inst

    def delete(self, instance_id: str) -> None:
        with self._lock:
            self.instances.pop(instance_id, None)


def create_app(host: Host | None = None) -> FastAPI:
    host = host or Host()
    app = FastAPI(title="toolsim", version="0.1.0")
    app.state.host = host

    def inst(instance_id: str) -> Instance:
        try:
            return host.get(instance_id)
        except KeyError:
            raise HTTPException(404, f"no instance {instance_id}") from None

    # -- agent-facing MCP ----------------------------------------------------------------

    @app.post("/instances/{instance_id}/mcp")
    async def mcp_endpoint(instance_id: str, request: Request) -> Response:
        i = inst(instance_id)
        try:
            msg = await request.json()
        except ValueError:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                                status_code=400)
        resp = mcp.handle(i, msg)
        if resp is None:
            return Response(status_code=202)
        headers = {}
        if isinstance(msg, dict) and msg.get("method") == "initialize":
            headers["Mcp-Session-Id"] = uuid.uuid4().hex
        return JSONResponse(resp, headers=headers)

    @app.get("/instances/{instance_id}/mcp")
    def mcp_get(instance_id: str) -> Response:
        inst(instance_id)
        return Response(status_code=405, headers={"Allow": "POST"})  # no server-initiated stream

    @app.delete("/instances/{instance_id}/mcp")
    def mcp_end_session(instance_id: str) -> Response:
        inst(instance_id)
        return Response(status_code=200)

    # -- control plane -------------------------------------------------------------------

    @app.get("/services")
    def services() -> dict[str, Any]:
        out = {}
        for name, cls in SERVICES.items():
            s = cls()
            out[name] = {"title": s.title, "latest": s.latest_version(), "versions": s.versions,
                         "tools": [t.name for t in s.tools if t.in_version(s.latest_version())]}
        return out

    @app.post("/instances")
    def create(request: Request, spec: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            i = host.create(spec["service"], spec.get("seed"), rng_seed=spec.get("rng_seed", 0),
                            faults=spec.get("faults"), instance_id=spec.get("id"), version=spec.get("version"))
        except (KeyError, ValueError) as e:
            raise HTTPException(400, str(e)) from None
        return _describe(request, i)

    @app.get("/instances")
    def list_instances(request: Request) -> dict[str, Any]:
        return {"instances": [_describe(request, i) for i in host.instances.values()]}

    @app.delete("/instances/{instance_id}")
    def delete(instance_id: str) -> dict[str, Any]:
        inst(instance_id)
        host.delete(instance_id)
        return {"deleted": instance_id}

    @app.get("/instances/{instance_id}/state")
    def state(instance_id: str) -> dict[str, Any]:
        i = inst(instance_id)
        with i.lock:
            return {"id": i.id, "service": i.service.name, "now": i.now().isoformat(), "state": i.state}

    @app.get("/instances/{instance_id}/calls")
    def calls(instance_id: str) -> dict[str, Any]:
        return {"calls": inst(instance_id).calls}

    @app.post("/instances/{instance_id}/reset")
    def reset(instance_id: str) -> dict[str, Any]:
        inst(instance_id).reset()
        return {"reset": instance_id}

    @app.post("/instances/{instance_id}/snapshot")
    def snapshot(instance_id: str) -> dict[str, Any]:
        sid = f"snap-{uuid.uuid4().hex[:10]}"
        host.snapshots[sid] = {"instance": instance_id, "data": inst(instance_id).snapshot()}
        return {"snapshot_id": sid}

    @app.post("/instances/{instance_id}/restore")
    def restore(instance_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        snap = host.snapshots.get(body.get("snapshot_id", ""))
        if snap is None:
            raise HTTPException(404, "no such snapshot")
        i = inst(instance_id)
        if snap["instance"] != instance_id and host.get(snap["instance"]).service.name != i.service.name:
            raise HTTPException(400, "snapshot belongs to a different service")
        i.restore(snap["data"])
        return {"restored": instance_id}

    @app.post("/instances/{instance_id}/fork")
    def fork(instance_id: str, request: Request) -> dict[str, Any]:
        src = inst(instance_id)
        clone = host.create(src.service.name, src.seed, rng_seed=src.rng_seed, faults=src.fault_specs,
                            version=src.version)
        clone.restore(src.snapshot())
        return _describe(request, clone)

    @app.put("/instances/{instance_id}/faults")
    def faults(instance_id: str, specs: list[dict[str, Any]] = Body(...)) -> dict[str, Any]:
        try:
            inst(instance_id).set_faults(specs)
        except (TypeError, ValueError) as e:
            raise HTTPException(400, str(e)) from None
        return {"faults": specs}

    return app


def _describe(request: Request, i: Instance) -> dict[str, Any]:
    base = str(request.base_url).rstrip("/")
    return {"id": i.id, "service": i.service.name, "version": i.version, "mcp_url": f"{base}/instances/{i.id}/mcp",
            "calls": len(i.calls), "now": i.now().isoformat()}
