"""Run many isolated instances behind one HTTP server.

Agent-facing (per instance):
    POST /instances/{id}/mcp              MCP streamable HTTP endpoint (JSON responses)
         ?agent=NAME&as=IDENTITY          which agent is calling, and which user it acts as

Whole environments (several servers, several agents, one clock):
    POST   /envs                          {spec | file, id?, seed?} -> per-agent MCP configs
    POST   /envs/{id}/snapshot|restore|fork|reset   atomic across all servers
    GET    /envs/{id}/calls               one timeline of every agent's calls
    GET    /envs/{id}/timeline            calls and world events together
    POST   /envs/{id}/events              make something happen in the world now
    POST   /envs/{id}/advance             let virtual time pass (fires timed events)
    GET    /envs/{id}/grade               run the environment's checks (score, reward, violations)
    POST   /envs/{id}/submit              {answer} -> the agent's final answer; returns the grade

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

Operations:
    GET    /healthz                       liveness (never requires auth)

Security defaults (see HostConfig): binds to localhost, validates the Origin header (MCP's
DNS-rebinding guidance), optional bearer token, environment files only from allowed directories,
bounded instances/snapshots/request sizes/fault delays.
"""

from __future__ import annotations

import dataclasses

import hmac
import logging
import re
import secrets
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import __version__
from .core import mcp
from .core.faults import TransportFault
from .core.instance import Instance
from .env import Environment, EnvRun, parse_time
from .services import SERVICES, get_service

log = logging.getLogger("toolsim.host")

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


@dataclass
class HostConfig:
    token: str | None = None                  # require "Authorization: Bearer <token>" (or ?token=) when set
    allowed_origins: list[str] = field(default_factory=list)  # extra browser origins allowed (localhost always is)
    env_dirs: list[Path] = field(default_factory=list)  # POST /envs {"file"} may only load from these directories
    max_instances: int = 2000
    max_snapshots: int = 500                  # oldest are evicted first
    max_hang_s: float = 120.0                 # cap on real-time fault delays
    max_delay_s: float = 7 * 86400            # cap on virtual fault delays
    max_body_bytes: int = 10 * 1024 * 1024
    speed: float | None = None                # default clock: None = virtual (fast), 1 = real time, 60 = 60x
    gateway_port: int | None = None           # also run the HTTPS gateway (real Google/GitHub clients) on this port
    gateway_bind: str = "127.0.0.1"
    gateway_passthrough: bool = True          # tunnel other hosts to the internet (False: refuse them)
    ca_dir: Path | None = None                # where the gateway's CA lives (default ~/.toolsim/ca)


def _valid_id(value: str | None, what: str) -> str | None:
    if value is not None and not ID_RE.match(str(value)):
        raise ValueError(f"invalid {what} {value!r}: use 1-100 letters, digits, '.', '_' or '-'")
    return value


class Host:
    def __init__(self, config: HostConfig | None = None) -> None:
        self.config = config or HostConfig()
        self.instances: dict[str, Instance] = {}
        self.envs: dict[str, EnvRun] = {}
        self.snapshots: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.env_snapshots: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.RLock()
        self.gateway_secret = self.config.token or secrets.token_hex(16)  # signs agents' API tokens
        self.gateway: Any = None
        if self.config.gateway_port is not None:
            from .gateway import Gateway
            self.gateway = Gateway(self, bind=self.config.gateway_bind, port=self.config.gateway_port,
                                   ca_dir=self.config.ca_dir, passthrough=self.config.gateway_passthrough).start()

    def credentials(self, base_url: str, *, run: EnvRun | None = None, instance: Instance | None = None,
                    agent: str | None = None) -> dict[str, Any]:
        """What an agent's real clients need: tokens, and (with the gateway) proxy and CA settings."""
        from .api.http import mint
        kw = {"run": run.id} if run is not None else {"instance": instance.id if instance else None}
        google = mint(self.gateway_secret, agent=agent, **kw)
        github = mint(self.gateway_secret, agent=agent, kind="github", **kw)
        email = None
        insts = ({s: run.instances[s] for s in run.env.agent_servers(agent)} if run is not None and agent
                 else run.instances if run is not None else {instance.id: instance} if instance else {})
        for server, i in insts.items():
            if i.service.name in ("gmail", "calendar", "drive"):
                who = run.env.identity_for(agent, server) if run is not None and agent else None
                email = i.resolve_actor(who) if who else i.service.default_actor(i.state)
                break
        env = {"GH_TOKEN": github, "GITHUB_TOKEN": github, "GOG_ACCESS_TOKEN": google}
        if email:
            env["GOG_ACCOUNT"] = email
        out: dict[str, Any] = {
            "google_access_token": google, "github_token": github, "email": email,
            "direct_base_url": f"{base_url}/gw/{{host}}",
            "google_authorized_user": {"token": google, "refresh_token": google,
                                       "token_uri": "https://oauth2.googleapis.com/token",
                                       "client_id": "toolsim.apps.googleusercontent.com", "client_secret": "toolsim",
                                       "scopes": ["https://mail.google.com/", "https://www.googleapis.com/auth/calendar",
                                                  "https://www.googleapis.com/auth/drive",
                                                  "https://www.googleapis.com/auth/spreadsheets",
                                                  "https://www.googleapis.com/auth/documents",
                                                  "https://www.googleapis.com/auth/contacts"],
                                       "universe_domain": "googleapis.com", "account": email or ""},
            "env": env}
        if self.gateway is not None:
            out["env"] = {**self.gateway.client_env(), **env}
            out["ca_pem"] = self.gateway.ca.pem
        return out

    # -- registry (all mutations and listings under the lock) -----------------------------

    def _room_for(self, n: int) -> None:
        if len(self.instances) + n > self.config.max_instances:
            raise ValueError(f"instance limit reached ({self.config.max_instances}); delete some first")

    def start_env(self, env: Environment, run_id: str | None = None) -> EnvRun:
        self.check_faults(env.faults)
        if not env.time_set and self.config.speed is not None:
            env = dataclasses.replace(env, speed=self.config.speed)
        run = EnvRun(env, _valid_id(run_id, "environment run id") or f"{env.name}-{uuid.uuid4().hex[:6]}")
        return self._register(run)

    def _register(self, run: EnvRun) -> EnvRun:
        with self._lock:
            if run.id in self.envs or any(i.id in self.instances for i in run.instances.values()):
                raise ValueError(f"environment run {run.id} already exists")
            self._room_for(len(run.instances))
            self.envs[run.id] = run
            for inst in run.instances.values():
                self.instances[inst.id] = inst
        return run

    def delete_env(self, run_id: str) -> None:
        with self._lock:
            run = self.envs.pop(run_id)
            for inst in run.instances.values():
                self.instances.pop(inst.id, None)

    def create(self, service: str, seed: dict[str, Any] | None = None, *, rng_seed: int = 0,
               faults: list[dict[str, Any]] | None = None, instance_id: str | None = None,
               version: str | None = None, speed: float | None | str = "default") -> Instance:
        self.check_faults(faults)
        iid = _valid_id(instance_id, "instance id") or f"{service}-{uuid.uuid4().hex[:8]}"
        with self._lock:  # check before building: seeds can be large
            if iid in self.instances:
                raise ValueError(f"instance {iid} already exists")
            self._room_for(1)
        inst = Instance(get_service(service), seed, rng_seed=int(rng_seed), faults=faults, instance_id=iid,
                        version=version, speed=self.config.speed if speed == "default" else speed)
        with self._lock:
            if iid in self.instances:
                raise ValueError(f"instance {iid} already exists")
            self._room_for(1)
            self.instances[iid] = inst
        return inst

    def get(self, instance_id: str) -> Instance:
        inst = self.instances.get(instance_id)
        if inst is None:
            raise KeyError(instance_id)
        return inst

    def delete(self, instance_id: str) -> None:
        with self._lock:
            inst = self.instances.get(instance_id)
            if inst is not None and any(inst is i for r in self.envs.values() for i in r.instances.values()):
                raise ValueError(f"instance {instance_id} belongs to an environment run; delete the run instead")
            self.instances.pop(instance_id, None)

    def list_instances(self) -> list[Instance]:
        with self._lock:
            return list(self.instances.values())

    def list_envs(self) -> list[EnvRun]:
        with self._lock:
            return list(self.envs.values())

    def save_snapshot(self, store: OrderedDict[str, dict[str, Any]], prefix: str, entry: dict[str, Any]) -> str:
        sid = f"{prefix}-{uuid.uuid4().hex[:12]}"
        with self._lock:
            store[sid] = entry
            while len(store) > self.config.max_snapshots:
                store.popitem(last=False)
        return sid

    # -- validation --------------------------------------------------------------------------

    def check_faults(self, specs: Any) -> None:
        if specs is None:
            return
        if not isinstance(specs, list) or not all(isinstance(f, dict) for f in specs):
            raise ValueError("faults must be a list of objects")
        for f in specs:
            for key, cap in (("hang_s", self.config.max_hang_s), ("delay_s", self.config.max_delay_s)):
                try:
                    v = float(f.get(key) or 0)
                except (TypeError, ValueError):
                    raise ValueError(f"fault {key} must be a number") from None
                if v < 0 or v > cap:
                    raise ValueError(f"fault {key}={v} is outside 0..{cap}")

    def resolve_env_file(self, name: str) -> Path:
        """Only files inside the configured environment directories can be loaded over the API."""
        if not self.config.env_dirs:
            raise ValueError("loading environment files over the API is disabled; send the spec inline, "
                             "or start the host with --env-dir")
        for d in self.config.env_dirs:
            base = Path(d).resolve()
            candidate = (base / name).resolve()
            if candidate.is_relative_to(base) and candidate.is_file():
                return candidate
        raise ValueError(f"no environment file {name!r} in the allowed directories")


def create_app(host: Host | None = None, config: HostConfig | None = None) -> FastAPI:
    host = host or Host(config)
    cfg = host.config
    app = FastAPI(title="toolsim", version=__version__)
    app.state.host = host

    # -- cross-cutting: size limit, origin check, auth, error shape ---------------------------

    @app.middleware("http")
    async def guard(request: Request, call_next: Any) -> Response:
        if request.url.path == "/healthz":
            return await call_next(request)
        if request.url.path.startswith("/gw/"):  # the agents' API traffic carries its own signed tokens
            return await call_next(request)
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > cfg.max_body_bytes:
            return JSONResponse({"detail": f"request body over {cfg.max_body_bytes} bytes"}, status_code=413)
        origin = request.headers.get("origin")
        if origin and not _origin_ok(origin, cfg.allowed_origins):
            return JSONResponse({"detail": f"origin {origin} not allowed"}, status_code=403)
        if cfg.token:
            given = request.headers.get("authorization", "").removeprefix("Bearer ").strip() \
                or request.query_params.get("token", "")
            if not hmac.compare_digest(given.encode(), cfg.token.encode()):
                return JSONResponse({"detail": "missing or invalid token"}, status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})
        return await call_next(request)

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception) -> JSONResponse:
        ref = uuid.uuid4().hex[:10]
        log.exception("unhandled error %s on %s %s", ref, request.method, request.url.path)
        return JSONResponse({"detail": "internal error", "ref": ref}, status_code=500)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "version": __version__, "instances": len(host.instances), "envs": len(host.envs)}

    def inst(instance_id: str) -> Instance:
        try:
            return host.get(instance_id)
        except KeyError:
            raise HTTPException(404, f"no instance {instance_id}") from None

    def bad_request(e: Exception) -> HTTPException:
        return HTTPException(400, str(e) if isinstance(e, ValueError) else f"invalid request: {type(e).__name__}: {e}")

    # -- agent-facing MCP ----------------------------------------------------------------

    @app.post("/instances/{instance_id}/mcp")
    async def mcp_endpoint(instance_id: str, request: Request) -> Response:
        i = inst(instance_id)
        body = await request.body()
        if len(body) > cfg.max_body_bytes:
            return JSONResponse({"detail": "request body too large"}, status_code=413)
        try:
            import json
            msg = json.loads(body)
        except ValueError:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                                status_code=400)
        # multi-agent: ?agent=john&as=john@acme.com (or X-Toolsim-Agent / X-Toolsim-As headers)
        agent = request.query_params.get("agent") or request.headers.get("x-toolsim-agent")
        as_ = request.query_params.get("as") or request.headers.get("x-toolsim-as")
        if as_:
            try:
                i.resolve_actor(as_)
            except ValueError as e:
                return JSONResponse({"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                                     "error": {"code": -32001, "message": str(e)}}, status_code=403)
        try:  # tool calls can block (latency faults, big worlds): keep them off the event loop
            resp = await run_in_threadpool(mcp.handle, i, msg, agent=agent, as_=as_)
        except TransportFault as e:  # what a real proxy in front of a dead service returns
            return Response(e.body, status_code=e.status, media_type="text/html")
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

    # -- whole environments (multi-agent) --------------------------------------------------

    def env_run(run_id: str) -> EnvRun:
        run = host.envs.get(run_id)
        if run is None:
            raise HTTPException(404, f"no environment run {run_id}")
        return run

    def describe_env(request: Request, run: EnvRun) -> dict[str, Any]:
        base = str(request.base_url).rstrip("/")
        run.tick()
        return {"id": run.id, "environment": run.env.name, "now": run.clock.now.isoformat(), "calls": run.clock.seq,
                "time": {"mode": "realtime", "speed": run.clock.speed} if run.clock.realtime else {"mode": "virtual"},
                "servers": {s: {"instance": i.id, "service": i.service.name, "version": i.version}
                            for s, i in run.instances.items()},
                "agents": run.agent_configs(base),
                "credentials": {(a or "default"): host.credentials(base, run=run, agent=a)
                                for a in (run.env.agents or [None])}}

    # -- real APIs (Google Workspace, GitHub), without the HTTPS gateway ------------------------

    @app.api_route("/gw/{target}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
    async def gateway_direct(target: str, path: str, request: Request) -> Response:
        from .api.http import handle
        body = await request.body()
        result = await run_in_threadpool(handle, host, target, request.method, "/" + path,
                                         request.url.query, dict(request.headers), body)
        return Response(result.body, status_code=result.status, headers=result.headers)

    @app.post("/envs")
    def start_env(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """{"spec": {...environment...}} or {"file": "name.yaml"} (from --env-dir); optional "id", and
        "seed" to randomize this episode's world (noise, ambient activity, IDs), and "time"
        ("virtual", "realtime" or {"speed": N}) to override the clock mode."""
        try:
            if body.get("file"):
                env = Environment.load(host.resolve_env_file(str(body["file"])))
            elif isinstance(body.get("spec"), dict):
                env = Environment.from_dict(body["spec"], base_dir=None)  # inline specs can't read files
            else:
                raise ValueError('send {"spec": {...}} or {"file": "name.yaml"}')
            if body.get("seed") is not None:
                if not isinstance(body["seed"], int) or isinstance(body["seed"], bool):
                    raise ValueError("seed must be an integer")
                env = dataclasses.replace(env, rng_seed=body["seed"])
            if "time" in body:
                env = dataclasses.replace(env, speed=parse_time(body["time"]), time_set=True)
            run = host.start_env(env, body.get("id"))
        except (KeyError, ValueError, TypeError, OSError) as e:
            raise bad_request(e) from None
        return describe_env(request, run)

    @app.get("/envs")
    def list_envs(request: Request) -> dict[str, Any]:
        return {"envs": [describe_env(request, r) for r in host.list_envs()]}

    @app.get("/envs/{run_id}")
    def get_env(request: Request, run_id: str) -> dict[str, Any]:
        return describe_env(request, env_run(run_id))

    @app.delete("/envs/{run_id}")
    def delete_env(run_id: str) -> dict[str, Any]:
        env_run(run_id)
        host.delete_env(run_id)
        return {"deleted": run_id}

    @app.post("/envs/{run_id}/snapshot")
    def env_snapshot(run_id: str) -> dict[str, Any]:
        """Atomic across every server in the environment."""
        run = env_run(run_id)
        sid = host.save_snapshot(host.env_snapshots, "envsnap", {
            "env": run.env.name, "servers": sorted(run.instances), "data": run.snapshot()})
        return {"snapshot_id": sid}

    @app.post("/envs/{run_id}/restore")
    def env_restore(run_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        run = env_run(run_id)
        snap = host.env_snapshots.get(str(body.get("snapshot_id", "")))
        if snap is None:
            raise HTTPException(404, "no such snapshot (it may have been evicted)")
        if snap["env"] != run.env.name or snap["servers"] != sorted(run.instances):
            raise HTTPException(400, f"snapshot is of environment {snap['env']!r}, not {run.env.name!r}")
        run.restore(snap["data"])
        return {"restored": run_id}

    @app.post("/envs/{run_id}/fork")
    def env_fork(request: Request, run_id: str, body: dict[str, Any] | None = Body(None)) -> dict[str, Any]:
        """An independent copy of the whole world at this instant, with its own agent URLs."""
        src = env_run(run_id)
        try:
            new_id = _valid_id((body or {}).get("id"), "environment run id") or f"{src.env.name}-{uuid.uuid4().hex[:6]}"
            with host._lock:
                if new_id in host.envs:
                    raise ValueError(f"environment run {new_id} already exists")
                host._room_for(len(src.instances))
            run = host._register(src.fork(new_id))
        except ValueError as e:
            raise bad_request(e) from None
        return describe_env(request, run)

    @app.post("/envs/{run_id}/reset")
    def env_reset(run_id: str) -> dict[str, Any]:
        env_run(run_id).reset()
        return {"reset": run_id}

    @app.get("/envs/{run_id}/calls")
    def env_calls(run_id: str) -> dict[str, Any]:
        """One timeline: every agent's calls across every server, in order."""
        run = env_run(run_id)
        with run.lock:
            run.tick()
            return {"calls": run.calls()}

    @app.get("/envs/{run_id}/timeline")
    def env_timeline(run_id: str) -> dict[str, Any]:
        """Agent calls and world events together, in order."""
        run = env_run(run_id)
        with run.lock:
            run.tick()
            return {"timeline": run.timeline()}

    @app.post("/envs/{run_id}/advance")
    def env_advance(run_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """{"seconds": 600}: let time pass; timed events that become due fire."""
        run = env_run(run_id)
        try:
            seconds = float(body["seconds"])
            if not 0 <= seconds <= cfg.max_delay_s:
                raise ValueError(f"seconds must be within 0..{cfg.max_delay_s}")
            run.advance(seconds)
        except (KeyError, ValueError, TypeError) as e:
            raise bad_request(e) from None
        return {"now": run.clock.now.isoformat()}

    @app.post("/envs/{run_id}/events")
    def env_inject(run_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """{"server", "action", "params", "as"?}: make something happen in the world right now."""
        run = env_run(run_id)
        try:
            if body.get("params") is not None and not isinstance(body["params"], dict):
                raise ValueError("params must be an object")
            result = run.inject(body["server"], body["action"], body.get("params"), body.get("as"))
        except (KeyError, ValueError, TypeError) as e:
            raise bad_request(e) from None
        return {"ok": True, "result": result}

    @app.get("/envs/{run_id}/grade")
    def env_grade(run_id: str) -> dict[str, Any]:
        run = env_run(run_id)
        run.tick()
        return run.grade()

    @app.post("/envs/{run_id}/submit")
    def env_submit(run_id: str, body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        """The agent's final answer; returns the grade (with ``reward``) for the finished episode."""
        run = env_run(run_id)
        answer = body.get("answer")
        if not isinstance(answer, str) or len(answer) > 100_000:
            raise bad_request(ValueError("answer must be a string (at most 100k characters)"))
        with run.lock:
            run.answer = answer
        return run.grade()

    # -- single instances -----------------------------------------------------------------------

    @app.get("/services")
    def services() -> dict[str, Any]:
        out = {}
        for name, cls in SERVICES.items():
            s = cls()
            out[name] = {"title": s.title, "latest": s.latest_version(), "versions": s.versions, "fidelity": s.fidelity,
                         "tools": [t.name for t in s.tools if t.in_version(s.latest_version())],
                         "actions": [a.name for a in s.actions]}
        return out

    @app.post("/instances")
    def create(request: Request, spec: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            if spec.get("seed") is not None and not isinstance(spec["seed"], dict):
                raise ValueError("seed must be an object")
            i = host.create(spec["service"], spec.get("seed"), rng_seed=spec.get("rng_seed", 0),
                            faults=spec.get("faults"), instance_id=spec.get("id"), version=spec.get("version"),
                            speed=parse_time(spec["time"]) if "time" in spec else "default")
        except (KeyError, ValueError, TypeError) as e:
            raise bad_request(e) from None
        return _describe(request, i)

    @app.get("/instances")
    def list_instances(request: Request) -> dict[str, Any]:
        return {"instances": [_describe(request, i) for i in host.list_instances()]}

    @app.delete("/instances/{instance_id}")
    def delete(instance_id: str) -> dict[str, Any]:
        inst(instance_id)
        try:
            host.delete(instance_id)
        except ValueError as e:
            raise bad_request(e) from None
        return {"deleted": instance_id}

    @app.get("/instances/{instance_id}/state")
    def state(instance_id: str) -> dict[str, Any]:
        i = inst(instance_id)
        with i.lock:
            i.tick()
            return {"id": i.id, "service": i.service.name, "now": i.now().isoformat(), "state": i.state}

    @app.get("/instances/{instance_id}/calls")
    def calls(instance_id: str) -> dict[str, Any]:
        i = inst(instance_id)
        with i.lock:
            return {"calls": list(i.calls)}

    @app.post("/instances/{instance_id}/reset")
    def reset(instance_id: str) -> dict[str, Any]:
        inst(instance_id).reset()
        return {"reset": instance_id}

    @app.post("/instances/{instance_id}/snapshot")
    def snapshot(instance_id: str) -> dict[str, Any]:
        i = inst(instance_id)
        sid = host.save_snapshot(host.snapshots, "snap", {"service": i.service.name, "version": i.version,
                                                          "data": i.snapshot()})
        return {"snapshot_id": sid}

    @app.post("/instances/{instance_id}/restore")
    def restore(instance_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        i = inst(instance_id)
        snap = host.snapshots.get(str(body.get("snapshot_id", "")))
        if snap is None:
            raise HTTPException(404, "no such snapshot (it may have been evicted)")
        if (snap["service"], snap["version"]) != (i.service.name, i.version):
            raise HTTPException(400, f"snapshot is of {snap['service']}@{snap['version']}, not {i.service.name}@{i.version}")
        i.restore(snap["data"])
        return {"restored": instance_id}

    @app.post("/instances/{instance_id}/fork")
    def fork(instance_id: str, request: Request) -> dict[str, Any]:
        src = inst(instance_id)
        try:
            clone = host.create(src.service.name, src.seed, rng_seed=src.rng_seed, faults=src.fault_specs,
                                version=src.version)
        except ValueError as e:
            raise bad_request(e) from None
        clone.restore(src.snapshot())
        return _describe(request, clone)

    @app.put("/instances/{instance_id}/faults")
    def faults(instance_id: str, specs: list[dict[str, Any]] = Body(...)) -> dict[str, Any]:
        i = inst(instance_id)
        try:
            host.check_faults(specs)
            i.set_faults(specs)
        except (TypeError, ValueError) as e:
            raise bad_request(e) from None
        return {"faults": specs}

    return app


def _origin_ok(origin: str, allowed: list[str]) -> bool:
    """Browsers send Origin; accept localhost and explicitly allowed origins (MCP DNS-rebinding guidance)."""
    if origin in allowed:
        return True
    host = urlsplit(origin).hostname or ""
    return host in LOCAL_HOSTS


def _describe(request: Request, i: Instance) -> dict[str, Any]:
    base = str(request.base_url).rstrip("/")
    return {"id": i.id, "service": i.service.name, "version": i.version, "mcp_url": f"{base}/instances/{i.id}/mcp",
            "calls": len(i.calls), "now": i.now().isoformat()}
