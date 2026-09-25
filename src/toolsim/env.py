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

Checks look at the final **state** (what's true in the world) or at the **calls** the agent made
(what it did). ``where`` matchers: plain values match exactly (lists: "contains all"), a key
ending in ``~`` matches a case-insensitive substring, dotted keys reach into nested objects and
lists. Bounds: ``count``, ``min``, ``max``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .core.instance import Instance
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
    base_dir: Path = Path(".")

    @classmethod
    def load(cls, path: str | Path) -> Environment:
        path = Path(path)
        return cls.from_dict(yaml.safe_load(path.read_text()), base_dir=path.parent)

    @classmethod
    def from_dict(cls, spec: dict[str, Any], base_dir: Path = Path(".")) -> Environment:
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
        return cls(name=spec["name"], task=spec.get("task", ""), servers={k: v or {} for k, v in servers.items()},
                   faults=spec.get("faults") or [], checks=spec.get("checks") or [],
                   description=spec.get("description", ""), rng_seed=spec.get("rng_seed", 0), base_dir=base_dir)

    def seed_for(self, server: str) -> dict[str, Any] | None:
        cfg = self.servers[server]
        if "seed" in cfg:
            return cfg["seed"]
        if "seed_file" in cfg:
            return yaml.safe_load((self.base_dir / cfg["seed_file"]).read_text())
        return None

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


def _describe(c: dict[str, Any]) -> str:
    what = f"state.{c['state']}" if "state" in c else f"calls to {c['calls']}"
    return f"{c['server']}: {what} where {c.get('where') or {}}"


def _collection(state: dict[str, Any], path: str) -> list[Any]:
    cur: Any = state
    for part in path.split("."):
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
        if substring:
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
