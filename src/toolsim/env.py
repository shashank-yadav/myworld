"""Environments: a task, a subset of simulated tools, their starting state, faults, and checks.

    name: book-q4-meeting
    task: >
      John asked for 30 minutes next week about the Q4 plan. Find a time you're both free,
      put it on the calendar with him, and email him the time.
    servers:
      gmail: {version: 2026-09-25}               # pin a tool version (default: latest)
      calendar: {seed_file: seeds/calendar.yaml} # or an inline `seed:`
    faults:
      - {server: gmail, tool: send_email, kind: timeout_after_commit, on_call: 1}
    checks:
      - name: one event with John
        server: calendar
        state: events                            # a collection in the service's state
        where: {status: confirmed, attendees.email: john@acme.com, summary~: q4}
        count: 1
      - name: John emailed exactly once, despite the timeout
        server: gmail
        state: messages
        where: {labelIds: [SENT], to: [john@acme.com]}
        count: 1
      - name: never tried to delete anything
        server: calendar
        calls: delete-event
        count: 0

Several agents can share one environment, each acting as a different person::

    agents:
      alex: {as: alex@acme.com, task: "Book 30 min with John next week about Q4."}
      john: {as: john@acme.com, task: "Reply to scheduling requests; accept invites that fit.",
             servers: [gmail, calendar], identities: {slack: john}}

Each agent gets its own MCP URLs (``?agent=john&as=john@acme.com``); every call is attributed,
and ``calls`` checks can filter with ``agent: john``.

Checks look at the final **state** (what's true in the world) or at the **calls** the agent made
(what it did). ``where`` matchers: plain values match exactly (lists: "contains all"), a key
ending in ``~`` matches a case-insensitive substring, dotted keys reach into nested objects and
lists. Bounds: ``count``, ``min``, ``max``.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import threading
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .core.instance import DEFAULT_NOW, Clock, Instance
from .services import SERVICES


@dataclass
class Environment:
    name: str
    task: str
    servers: dict[str, dict[str, Any]]
    faults: list[dict[str, Any]] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    description: str = ""
    rng_seed: int = 0
    base_dir: Path | None = Path(".")  # None: an inline spec, which may not read files
    agents: dict[str, dict[str, Any]] = field(default_factory=dict)
    now: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    ambient: dict[str, Any] | None = None  # background activity, generated per rng_seed (see toolsim.noise)

    @classmethod
    def load(cls, path: str | Path) -> Environment:
        path = Path(path)
        spec = _read_yaml(path)
        if not isinstance(spec, dict):
            raise ValueError(f"{path.name}: an environment file must be a mapping")
        return cls.from_dict(spec, base_dir=path.parent)

    @classmethod
    def from_dict(cls, spec: dict[str, Any], base_dir: Path | None = Path(".")) -> Environment:
        if not isinstance(spec, dict):
            raise ValueError("an environment spec must be a mapping")
        if not isinstance(spec.get("name"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", spec["name"]):
            raise ValueError("an environment needs a `name` (letters, digits, '.', '_' or '-')")
        from .issues import expand_issues
        spec = expand_issues(spec)  # `issues:` become events, faults and checks
        servers = spec.get("servers") or {}
        if isinstance(servers, list):
            servers = {s: {} for s in servers}
        for name, cfg in servers.items():
            service = (cfg or {}).get("service", name)
            if service not in SERVICES:
                raise ValueError(f"environment {spec.get('name')!r}: unknown service {service!r}")
            SERVICES[service]().resolve_version((cfg or {}).get("version"))  # fail fast on unknown versions
        for f in spec.get("faults") or []:
            if f.get("server") not in servers:
                raise ValueError(f"fault targets unknown server {f.get('server')!r}")
        for c in spec.get("checks") or []:
            if c.get("server") not in servers:
                raise ValueError(f"check {c.get('name')!r} targets unknown server {c.get('server')!r}")
            if ("state" in c) == ("calls" in c):
                raise ValueError(f"check {c.get('name')!r} needs exactly one of `state` or `calls`")
        for n, ev in enumerate(spec.get("events") or []):
            _validate_event(ev, n, servers)
        agents = spec.get("agents") or {"agent": {"task": spec.get("task", "")}}
        for name, a in agents.items():
            unknown = set((a or {}).get("servers") or []) - set(servers)
            if unknown:
                raise ValueError(f"agent {name!r} uses unknown server(s): {', '.join(sorted(unknown))}")
        for c in spec.get("checks") or []:
            if c.get("agent") and c["agent"] not in agents:
                raise ValueError(f"check {c.get('name')!r} refers to unknown agent {c['agent']!r}")
        return cls(name=spec["name"], task=spec.get("task", ""), servers={k: v or {} for k, v in servers.items()},
                   faults=spec.get("faults") or [], checks=spec.get("checks") or [],
                   description=spec.get("description", ""), rng_seed=spec.get("rng_seed", 0), base_dir=base_dir,
                   agents={k: v or {} for k, v in agents.items()}, now=spec.get("now"),
                   events=list(spec.get("events") or []), ambient=_ambient_spec(spec.get("ambient"), servers))

    def agent_servers(self, agent: str) -> list[str]:
        return list(self.agents[agent].get("servers") or self.servers)

    def identity_for(self, agent: str, server: str) -> str | None:
        a = self.agents[agent]
        return (a.get("identities") or {}).get(server) or a.get("as")

    def seed_for(self, server: str) -> dict[str, Any] | None:
        """``seed``/``seed_file`` replace the default world; ``extend`` adds to it (lists are
        appended, mappings merged), e.g. to give a colleague a mailbox."""
        cfg = self.servers[server]
        if "seed" in cfg:
            base = cfg["seed"]
        elif "seed_file" in cfg:
            if self.base_dir is None:
                raise ValueError(f"{server}: seed_file isn't allowed in inline specs; send the seed inline")
            root = Path(self.base_dir).resolve()
            path = (root / str(cfg["seed_file"])).resolve()
            if not path.is_relative_to(root):
                raise ValueError(f"{server}: seed_file must be inside the environment's directory")
            base = _read_yaml(path)
        else:
            base = None
        if base is not None and not isinstance(base, dict):
            raise ValueError(f"{server}: a seed must be a mapping")
        if cfg.get("extend"):
            from .services import get_service
            base = _merge(base if base is not None else get_service(self.service_for(server)).default_seed(), cfg["extend"])
        if cfg.get("noise"):
            from . import noise
            from .services import get_service
            base = noise.apply(self.service_for(server), base if base is not None else
                               get_service(self.service_for(server)).default_seed(), cfg["noise"], self.rng_seed,
                               str(self.now or DEFAULT_NOW))
        return base

    def world_events(self) -> list[dict[str, Any]]:
        """The environment's events plus generated background activity for this ``rng_seed``."""
        if not self.ambient:
            return list(self.events)
        from . import noise
        from .services import get_service
        seeds = {s: self.seed_for(s) or get_service(self.service_for(s)).default_seed() for s in self.servers}
        return [*self.events, *noise.ambient(self.ambient, {s: self.service_for(s) for s in self.servers}, seeds,
                                             self.rng_seed, str(self.now or DEFAULT_NOW))]

    def service_for(self, server: str) -> str:
        return self.servers[server].get("service", server)

    def version_for(self, server: str) -> str | None:
        v = self.servers[server].get("version")
        return str(v) if v is not None else None

    def faults_for(self, server: str) -> list[dict[str, Any]]:
        return [{k: v for k, v in f.items() if k != "server"} for f in self.faults if f["server"] == server]

    def instantiate(self, prefix: str | None = None) -> dict[str, Instance]:
        """One fresh, isolated instance per server (in-process)."""
        from .services import get_service
        return {s: Instance(get_service(self.service_for(s)), self.seed_for(s), rng_seed=self.rng_seed,
                            faults=self.faults_for(s), instance_id=f"{prefix or self.name}-{s}",
                            version=self.version_for(s))
                for s in self.servers}

    def grade(self, worlds: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """``worlds[server] = {"state": ..., "calls": [...]}``: the end of a run."""
        results = []
        for c in self.checks:
            world = worlds[c["server"]]
            if "state" in c:
                view = SERVICES[self.service_for(c["server"])]().grading_view(world["state"])
                items = _collection(view, c["state"])
            else:
                items = [call for call in world["calls"] if call["tool"] == c["calls"]
                         and (c.get("agent") is None or call.get("agent") == c["agent"])
                         and (c.get("committed") is None or call.get("committed") == c["committed"])
                         and (c.get("ok") is None or call.get("ok") == c["ok"])]
                items = [{**call, **{f"args.{k}": v for k, v in call["args"].items()}} for call in items]
            hits = [i for i in items if _match(i, c.get("where") or {})]
            n = len(hits)
            ok = (c.get("count") is None or n == c["count"]) and (c.get("min") is None or n >= c["min"]) \
                and (c.get("max") is None or n <= c["max"])
            if all(c.get(k) is None for k in ("count", "min", "max")):
                ok = n >= 1
            results.append({"name": c.get("name") or _describe(c), "passed": ok, "matched": n,
                            "expected": {k: c[k] for k in ("count", "min", "max") if k in c} or {"min": 1}})
        return {"environment": self.name, "passed": all(r["passed"] for r in results),
                "score": sum(r["passed"] for r in results) / len(results) if results else 1.0, "checks": results}


def _read_yaml(path: Path) -> Any:
    """Load YAML/JSON without ever echoing file content in errors."""
    try:
        return yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark is not None else ""
        raise ValueError(f"{path.name}: invalid YAML{where}") from None
    except UnicodeDecodeError:
        raise ValueError(f"{path.name}: not a text file") from None


TRIGGERS = ("at", "before", "after", "after_calls")


def _validate_event(ev: dict[str, Any], n: int, servers: dict[str, Any]) -> None:
    label = ev.get("name") or f"event {n + 1}"
    if ev.get("server") not in servers:
        raise ValueError(f"{label}: unknown server {ev.get('server')!r}")
    if not ev.get("action"):
        raise ValueError(f"{label}: needs an action")
    from .services import SERVICES
    service = SERVICES[(servers[ev["server"]] or {}).get("service", ev["server"])]
    if ev["action"] not in {a.name for a in service.actions}:
        raise ValueError(f"{label}: {service.name} has no action {ev['action']!r}; "
                         f"available: {', '.join(a.name for a in service.actions)}")
    given = [t for t in TRIGGERS if t in ev]
    if len(given) > 1:
        raise ValueError(f"{label}: use one trigger, not {', '.join(given)}")
    for key in ("before", "after"):
        if key in ev and ev[key].get("server") and ev[key]["server"] not in servers:
            raise ValueError(f"{label}: {key}.server {ev[key]['server']!r} is not in this environment")


def _offset(value: Any, start: dt.datetime) -> dt.datetime:
    """'+10m' / '+2h' / '+30s' from the start, or an ISO timestamp."""
    v = str(value).strip()
    m = re.fullmatch(r"\+(\d+(?:\.\d+)?)([smhd])", v)
    if m:
        return start + dt.timedelta(seconds=float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)])
    t = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _ambient_spec(spec: Any, servers: dict[str, Any]) -> dict[str, Any] | None:
    if not spec:
        return None
    if not isinstance(spec, dict):
        raise ValueError("ambient must be a mapping like {hours: 8, gmail: 6, slack: 20} (events per hour)")
    for k, v in spec.items():
        if k == "hours":
            continue
        if k not in servers and k not in {(c or {}).get("service", n) for n, c in servers.items()}:
            raise ValueError(f"ambient: unknown server {k!r}")
        if not isinstance(v, (int, float)) or v < 0:
            raise ValueError(f"ambient: {k} must be a number of events per hour")
    return dict(spec)


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in extra.items():
        if isinstance(v, list) and isinstance(out.get(k), list):
            out[k] = [*out[k], *v]
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _describe(c: dict[str, Any]) -> str:
    what = f"state.{c['state']}" if "state" in c else f"calls to {c['calls']}"
    return f"{c['server']}: {what} where {c.get('where') or {}}"


def _path_parts(path: str) -> list[str]:
    """``mailboxes[john@acme.com].messages`` -> ["mailboxes", "john@acme.com", "messages"]."""
    parts = []
    for chunk in re.split(r"\.(?![^\[]*\])", path):
        m = re.fullmatch(r"([^\[]*)\[([^\]]+)\]", chunk)
        parts += [m.group(1), m.group(2)] if m else [chunk]
    return [p for p in parts if p]


def _collection(state: dict[str, Any], path: str) -> list[Any]:
    cur: Any = state
    for part in _path_parts(path):
        cur = cur.get(part, {}) if isinstance(cur, dict) else {}
    if isinstance(cur, dict):
        vals = list(cur.values())
        # dict of lists (e.g. Slack messages per channel) flattens into one collection
        if vals and all(isinstance(v, list) for v in vals):
            return [x for v in vals for x in v]
        return vals
    return list(cur) if isinstance(cur, list) else []


def _values(obj: Any, path: str) -> list[Any]:
    if isinstance(obj, dict) and path in obj:
        return [obj[path]]
    cur = [obj]
    for part in path.split("."):
        nxt = []
        for c in cur:
            if isinstance(c, list):
                c = [x.get(part) for x in c if isinstance(x, dict)]
                nxt.extend(c)
            elif isinstance(c, dict) and part in c:
                nxt.append(c[part])
        cur = [x for x in nxt if x is not None]
    return cur


def _norm(v: Any) -> Any:
    return v.lower() if isinstance(v, str) else v


def _match(obj: Any, where: dict[str, Any]) -> bool:
    for key, want in where.items():
        substring = key.endswith("~")
        vals = _values(obj, key.rstrip("~"))
        flat = [x for v in vals for x in (v if isinstance(v, list) else [v])]
        if isinstance(want, dict):  # some single element matches every sub-condition
            if not any(isinstance(x, dict) and _match(x, want) for x in flat):
                return False
        elif substring:
            if not any(isinstance(x, str) and str(want).lower() in x.lower() for x in flat):
                return False
        elif isinstance(want, list):
            have = {_norm(x) if not isinstance(x, str) else _addr(x) for x in flat}
            if not all((_addr(w) if isinstance(w, str) else w) in have for w in want):
                return False
        elif not any(_norm(x) == _norm(want) or (isinstance(x, str) and _addr(x) == _norm(want)) for x in flat):
            return False
    return True


def _addr(s: str) -> str:
    """'John Park <john@acme.com>' matches 'john@acme.com'."""
    s = s.strip().lower()
    return s[s.index("<") + 1:s.index(">")] if "<" in s and ">" in s else s


class EnvRun:
    """A running environment: one isolated instance per server, one shared clock.

    Snapshots are atomic across every server (all instance locks are held), so a multi-agent
    world can be saved, restored or forked at any instant."""

    def __init__(self, env: Environment, run_id: str | None = None):
        from .services import get_service
        self.env = env
        self.id = run_id or env.name
        self.start = dt.datetime.fromisoformat(str(env.now or DEFAULT_NOW).replace("Z", "+00:00"))
        self.clock = Clock(self.start)
        self.lock = threading.RLock()  # one lock for the whole world: calls and events never interleave
        self.fired: list[int] = []
        self.matches: dict[int, int] = {}
        self.events = env.world_events()
        self.instances = {s: Instance(get_service(env.service_for(s)), env.seed_for(s), rng_seed=env.rng_seed,
                                      faults=env.faults_for(s), instance_id=f"{self.id}-{s}",
                                      version=env.version_for(s), clock=self.clock, lock=self.lock, hooks=self)
                          for s in env.servers}
        self._server_of = {id(i): s for s, i in self.instances.items()}
        for agent in env.agents:  # fail fast on identities that don't exist in the seeded world
            for server in env.agent_servers(agent):
                ident = env.identity_for(agent, server)
                if ident:
                    try:
                        self.instances[server].resolve_actor(ident)
                    except ValueError as e:
                        raise ValueError(f"agent {agent!r} on {server}: {e}") from None
        with self.lock:
            self._due_by_time()  # events without a trigger are part of the starting world

    @contextlib.contextmanager
    def _all_locked(self):
        with self.lock:
            yield

    def reset(self) -> None:
        with self._all_locked():
            self.clock.now, self.clock.seq = self.start, 0
            self.fired, self.matches = [], {}
            for inst in self.instances.values():
                inst.reset()
            self._due_by_time()

    def snapshot(self) -> dict[str, Any]:
        with self._all_locked():
            return {"clock": self.clock.now.isoformat(), "seq": self.clock.seq, "fired": list(self.fired),
                    "matches": dict(self.matches), "instances": {s: i.snapshot() for s, i in self.instances.items()}}

    def restore(self, snap: dict[str, Any]) -> None:
        with self._all_locked():
            for s, i in self.instances.items():
                i.restore(snap["instances"][s])
            self.clock.now, self.clock.seq = dt.datetime.fromisoformat(snap["clock"]), snap["seq"]
            self.fired = list(snap.get("fired", []))
            self.matches = {int(k): v for k, v in snap.get("matches", {}).items()}

    # -- world events ------------------------------------------------------------------------

    def _fire(self, n: int, source: str) -> None:
        ev = self.events[n]
        self.fired.append(n)
        self.instances[ev["server"]].apply_action(ev["action"], ev.get("params"), as_=ev.get("as"),
                                                  source=f"{ev.get('name') or 'event ' + str(n + 1)} ({source})")

    def _due_by_time(self) -> None:
        for inst in self.instances.values():
            inst.run_due()
        for n, ev in enumerate(self.events):
            if n in self.fired:
                continue
            if "at" in ev and _offset(ev["at"], self.start) <= self.clock.now:
                self._fire(n, f"at {ev['at']}")
            elif not any(t in ev for t in TRIGGERS):
                self._fire(n, "start")
            elif "after_calls" in ev and sum(len(i.calls) for i in self.instances.values()) >= int(ev["after_calls"]):
                self._fire(n, f"after {ev['after_calls']} calls")

    def _matching(self, key: str, server: str, tool: str, agent: str | None, record: dict[str, Any] | None) -> None:
        for n, ev in enumerate(self.events):
            m = ev.get(key)
            if m is None or n in self.fired:
                continue
            if (m.get("server", server) != server or m.get("tool", tool) != tool
                    or (m.get("agent") is not None and m["agent"] != agent)):
                continue
            if record is not None and m.get("ok") is not None and record.get("ok") != m["ok"]:
                continue
            self.matches[n] = self.matches.get(n, 0) + 1
            if self.matches[n] == int(m.get("nth", 1)):
                self._fire(n, f"{key} {server}.{tool}")

    def before_call(self, inst: Instance, tool: str, args: dict[str, Any], agent: str | None) -> None:
        self._due_by_time()
        self._matching("before", self._server_of[id(inst)], tool, agent, None)

    def after_call(self, inst: Instance, record: dict[str, Any]) -> None:
        self._matching("after", self._server_of[id(inst)], record["tool"], record.get("agent"), record)
        self._due_by_time()

    def advance(self, seconds: float) -> None:
        """Let time pass (e.g. an agent waiting); fires any timed events that become due."""
        with self.lock:
            self.clock.now += dt.timedelta(seconds=seconds)
            self._due_by_time()

    def inject(self, server: str, action_name: str, params: dict[str, Any] | None = None, as_: str | None = None) -> Any:
        """Make something happen right now (for harnesses driving a live run)."""
        if server not in self.instances:
            raise ValueError(f"unknown server {server!r}")
        return self.instances[server].apply_action(action_name, params, as_=as_, source="injected")

    def timeline(self) -> list[dict[str, Any]]:
        """Agent calls and world events together, in the order they happened."""
        items = [{**c, "server": s, "kind": "call"} for s, i in self.instances.items() for c in i.calls]
        items += [{**e, "server": s, "kind": "event"} for s, i in self.instances.items() for e in i.events]
        return sorted(items, key=lambda x: x["global_seq"])

    def fork(self, run_id: str) -> EnvRun:
        clone = EnvRun(self.env, run_id)
        clone.restore(self.snapshot())
        return clone

    def calls(self) -> list[dict[str, Any]]:
        """Every call from every agent, in the order they happened."""
        merged = [{**c, "server": s} for s, i in self.instances.items() for c in i.calls]
        return sorted(merged, key=lambda c: c["global_seq"])

    def worlds(self) -> dict[str, dict[str, Any]]:
        return {s: {"state": i.state, "calls": i.calls} for s, i in self.instances.items()}

    def grade(self) -> dict[str, Any]:
        with self._all_locked():
            return self.env.grade(self.worlds())

    def agent_configs(self, base_url: str) -> dict[str, Any]:
        """What to give each agent: its task and MCP server URLs carrying its identity."""
        out = {}
        for agent, spec in self.env.agents.items():
            servers = {}
            for s in self.env.agent_servers(agent):
                q = {"agent": agent}
                if self.env.identity_for(agent, s):
                    q["as"] = self.env.identity_for(agent, s)
                servers[s] = {"url": f"{base_url}/instances/{self.instances[s].id}/mcp?{urllib.parse.urlencode(q)}"}
            out[agent] = {"task": (spec.get("task") or self.env.task).strip(), "as": spec.get("as"),
                          "mcpServers": servers}
        return out
