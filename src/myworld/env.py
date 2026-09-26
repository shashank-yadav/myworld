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

Checks look at the final **state** (what's true in the world), at the **calls** the agent made
(what it did), or at its final **answer**. For RL, ``weight`` sets a check's share of the reward
(partial credit) and ``must: true`` makes it a hard constraint: if it fails, the reward is 0::

      - {name: never emailed the attacker, server: gmail, state: messages, where: {...}, count: 0, must: true}
      - name: reported the merge commit
        answer: {contains: {server: github, state: "repos[acme/api].pulls", where: {number: 4}, field: merge_commit_sha}}
        weight: 2

``answer`` takes ``contains`` (text, or a value looked up in the final state) or ``matches`` (a regex),
plus guards against answer stuffing: ``max_len`` (characters) and ``not`` (regexes that must not
match, e.g. the distractor values). ``calls: "*"`` matches calls to any tool, so
``{calls: "*", where: {committed: true}, count: 0}`` says "changed nothing". ``where`` matchers: plain values match exactly (lists: "contains all"), a key
ending in ``~`` matches a case-insensitive substring, dotted keys reach into nested objects and
lists; ``!key`` negates a matcher and ``key~re`` matches a regular expression. Bounds: ``count``,
``min``, ``max``.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
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
    ambient: dict[str, Any] | None = None  # background activity, generated per rng_seed (see myworld.noise)
    speed: float | None = None  # None: virtual time (fast, deterministic); 1: real time; 60: a minute per second
    time_set: bool = False      # whether the spec chose a clock (otherwise a host may apply its default)
    components: dict[str, dict[str, Any]] = field(default_factory=dict)  # directories, databases, remote (myworld.world)
    graders: list[dict[str, Any]] = field(default_factory=list)  # plug-in graders (myworld.graders), e.g. a benchmark's own
    history: list[dict[str, Any]] = field(default_factory=list)  # journal entries already done: a run starts after them

    def to_dict(self, materialize: bool = False) -> dict[str, Any]:
        """The spec for this environment (``from_dict`` builds it back). ``materialize`` writes
        every server's seed inline, so the spec needs no files and regenerates no noise."""
        servers = {}
        for s, cfg in self.servers.items():
            cfg = dict(cfg)
            if materialize:
                seed = self.seed_for(s)
                for k in ("seed_file", "extend", "noise"):
                    cfg.pop(k, None)
                if seed is not None:
                    cfg["seed"] = seed
            servers[s] = cfg
        out: dict[str, Any] = {"name": self.name, "task": self.task, "description": self.description,
                               "servers": servers, "faults": list(self.faults), "checks": list(self.checks),
                               "rng_seed": self.rng_seed, "agents": dict(self.agents), "events": list(self.events)}
        for key, value in (("now", self.now), ("ambient", self.ambient)):
            if value is not None:
                out[key] = value
        if self.time_set or self.speed is not None:
            out["time"] = "virtual" if self.speed is None else {"speed": self.speed}
        if self.components:
            out["components"] = dict(self.components)
        if self.graders:
            out["graders"] = list(self.graders)
        if self.history:
            out["history"] = list(self.history)
        return out

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
        components = spec.get("components") or {}
        if not isinstance(components, dict):
            raise ValueError("components must be a mapping of name -> {type: directory|sqlite|remote, ...}")
        for name, comp in components.items():
            if name in servers:
                raise ValueError(f"component {name!r} has the same name as a server")
            if (comp or {}).get("type") not in ("directory", "sqlite", "remote", "command", "docker"):
                raise ValueError(f"component {name!r}: type must be directory, sqlite, remote, command or docker")
        targets = {**servers, **components}
        for name, cfg in servers.items():
            service = (cfg or {}).get("service", name)
            if service not in SERVICES:
                raise ValueError(f"environment {spec.get('name')!r}: unknown service {service!r}")
            SERVICES[service]().resolve_version((cfg or {}).get("version"))  # fail fast on unknown versions
        for f in spec.get("faults") or []:
            if f.get("server") not in servers:
                raise ValueError(f"fault targets unknown server {f.get('server')!r}")
        for c in spec.get("checks") or []:
            if sum(k in c for k in ("state", "calls", "answer")) != 1:
                raise ValueError(f"check {c.get('name')!r} needs exactly one of `state`, `calls` or `answer`")
            if "answer" in c:
                a = c["answer"]
                if not isinstance(a, dict) or not ({"contains", "matches"} & set(a)):
                    raise ValueError(f"check {c.get('name')!r}: answer needs `contains` or `matches`")
                ref = a.get("contains")
                if isinstance(ref, dict) and ref.get("server") not in servers:
                    raise ValueError(f"check {c.get('name')!r} looks up unknown server {ref.get('server')!r}")
            elif c.get("server") not in targets:
                raise ValueError(f"check {c.get('name')!r} targets unknown server {c.get('server')!r}")
            elif c["server"] in components and "calls" in c:
                raise ValueError(f"check {c.get('name')!r}: components have state, not calls")
            if c.get("weight") is not None and (not isinstance(c["weight"], (int, float)) or c["weight"] < 0):
                raise ValueError(f"check {c.get('name')!r}: weight must be a non-negative number")
        for n, ev in enumerate(spec.get("events") or []):
            _validate_event(ev, n, servers, components)
        history = spec.get("history") or []
        if not isinstance(history, list) or not all(isinstance(e, dict) and e.get("kind") for e in history):
            raise ValueError("history must be a list of journal entries (as a run's journal records them)")
        from .graders import grader
        for g in spec.get("graders") or []:
            if not isinstance(g, dict) or not g.get("use"):
                raise ValueError("each grader needs `use: <name>`")
            grader(g["use"])  # fail fast on unknown graders
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
                   events=list(spec.get("events") or []), ambient=_ambient_spec(spec.get("ambient"), servers),
                   speed=parse_time(spec.get("time")), time_set="time" in spec,
                   components={k: dict(v or {}) for k, v in components.items()},
                   graders=[dict(g) for g in spec.get("graders") or []],
                   history=[dict(e) for e in history])

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

    def grade(self, worlds: dict[str, dict[str, Any]], answer: str | None = None) -> dict[str, Any]:
        """``worlds[server] = {"state": ..., "calls": [...]}``: the end of a run. ``answer`` is the
        agent's final answer, if any. ``score`` is the weighted share of checks passed; ``reward``
        is the score, or 0 if a ``must`` check failed."""
        results = []
        for c in self.checks:
            if "answer" in c:
                ok, detail = self._answer_ok(c["answer"], worlds, answer)
                results.append({"name": c.get("name") or "answer", "passed": ok, "matched": int(ok),
                                "expected": detail, "weight": float(c.get("weight", 1)), "must": bool(c.get("must"))})
                continue
            world = worlds[c["server"]]
            if "state" in c:
                view = world["view"] if "view" in world else \
                    SERVICES[self.service_for(c["server"])]().grading_view(world["state"])
                items = _collection(view, c["state"])
            else:
                items = [call for call in world["calls"] if c["calls"] in ("*", call["tool"])
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
                            "expected": {k: c[k] for k in ("count", "min", "max") if k in c} or {"min": 1},
                            "weight": float(c.get("weight", 1)), "must": bool(c.get("must"))})
        native = list(results)
        graded = []
        if self.graders:
            from .graders import grader
            for g in self.graders:
                out = grader(g["use"])(self, g, worlds, answer)
                graded.append(out)
                results += [{"weight": 1.0, "must": False, **r} for r in out.get("checks", [])]
        total = sum(r["weight"] for r in results)
        score = sum(r["weight"] for r in results if r["passed"]) / total if total else 1.0
        if graded and all(r["must"] for r in native) and all("score" in g for g in graded):
            # a benchmark's own score (e.g. partial credit); the environment's checks are only constraints
            score = sum(g["score"] for g in graded) / len(graded)
        passed = all(r["passed"] for r in results) and all(g.get("passed", True) for g in graded)
        violated = [r["name"] for r in results if r["must"] and not r["passed"]]
        out = {"environment": self.name, "passed": passed, "score": score,
               "reward": 0.0 if violated else score, "violations": violated, "checks": results}
        for g in graded:
            out.update(g.get("extra") or {})
        return out

    def _answer_ok(self, spec: dict[str, Any], worlds: dict[str, dict[str, Any]],
                   answer: str | None) -> tuple[bool, dict[str, Any]]:
        text = answer or ""
        if spec.get("max_len") is not None and len(text) > int(spec["max_len"]):
            return False, {"max_len": spec["max_len"], "got": len(text)}
        for bad in spec.get("not") or []:
            if re.search(str(bad), text, re.I | re.S):
                return False, {"not": bad}
        if "matches" in spec:
            return bool(re.search(str(spec["matches"]), text, re.I | re.S)), {"matches": spec["matches"]}
        want = spec["contains"]
        if isinstance(want, dict):  # a value from the final state, e.g. the merge commit SHA
            w = worlds[want["server"]]
            view = w["view"] if "view" in w else SERVICES[self.service_for(want["server"])]().grading_view(w["state"])
            hits = [i for i in _collection(view, want["state"]) if _match(i, want.get("where") or {})]
            values = [v for i in hits for v in _values(i, want["field"])] if want.get("field") else hits
            values = [str(v) for v in values if v not in (None, "")]
            ok = bool(values) and all(v.lower() in text.lower() for v in values[:1])
            return ok, {"contains": values[:1] or "(nothing in the final state)"}
        return str(want).lower() in text.lower(), {"contains": want}


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


def _validate_event(ev: dict[str, Any], n: int, servers: dict[str, Any],
                    components: dict[str, Any] | None = None) -> None:
    label = ev.get("name") or f"event {n + 1}"
    if "component" in ev:  # a mutation of a directory, database or remote component
        if ev["component"] not in (components or {}):
            raise ValueError(f"{label}: unknown component {ev['component']!r}")
        if not ev.get("mutate"):
            raise ValueError(f"{label}: a component event needs `mutate` (the operation) and `params`")
        return
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


def parse_time(value: Any) -> float | None:
    """The clock speed for ``time:``: ``virtual`` (default: fast and deterministic) -> None,
    ``realtime`` -> 1.0, ``{speed: 60}`` or ``60`` -> 60x real time."""
    if value in (None, "virtual", "fast", "superfast"):
        return None
    if value in ("realtime", "real"):
        return 1.0
    if isinstance(value, dict):
        if value.get("mode", "realtime") == "virtual":
            return None
        if value.get("mode", "realtime") != "realtime":
            raise ValueError("time.mode must be 'virtual' or 'realtime'")
        value = value.get("speed", 1)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 100_000:
        raise ValueError("time must be 'virtual', 'realtime', or a speed between 0 and 100000 (1 = real time)")
    return float(value)


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
        if key.startswith("!"):  # must NOT match
            if _match(obj, {key[1:]: want}):
                return False
            continue
        if key.endswith("~re"):  # regular expression, case-insensitive
            vals = _values(obj, key[:-3])
            flat = [x for v in vals for x in (v if isinstance(v, list) else [v])]
            if not any(isinstance(x, str) and re.search(str(want), x, re.I) for x in flat):
                return False
            continue
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


# -- anchoring a world to the wall clock ----------------------------------------------------------

WALLCLOCK = "wallclock"
_ISO = re.compile(r"(?<![\d-])(\d{4})-(\d{2})-(\d{2})(?=$|[T ]\d{2}:\d{2}|[^\d])")
_COMPACT = re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(T\d{6}Z?)")
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December"]
_SPOKEN = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)([a-z]*)(\.?) (\d{1,2})(?:(,? )(\d{4}))?\b")


def _shift_text(text: str, days: int) -> str:
    """Move every date written in ``text`` by ``days``: ISO dates, RRULE/EXDATE stamps, "Sep 22"."""
    def iso(m: re.Match[str]) -> str:
        try:
            d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))) + dt.timedelta(days=days)
        except ValueError:
            return m.group(0)
        return d.isoformat()

    def compact(m: re.Match[str]) -> str:
        try:
            d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))) + dt.timedelta(days=days)
        except ValueError:
            return m.group(0)
        return d.strftime("%Y%m%d") + m.group(4)

    def spoken(m: re.Match[str]) -> str:
        abbr, rest, dot, day, sep, year = m.groups()
        month = next(i for i, name in enumerate(_MONTHS, 1) if name.lower().startswith(abbr.lower()[:3]))
        base_year = int(year) if year else 2026
        try:
            d = dt.date(base_year, month, int(day)) + dt.timedelta(days=days)
        except ValueError:
            return m.group(0)
        name = _MONTHS[d.month - 1]
        shown = name if rest else ("Sept" if abbr == "Sept" and d.month == 9 else name[:3])
        return f"{shown}{dot} {d.day}" + (f"{sep}{d.year}" if year else "")

    return _SPOKEN.sub(spoken, _COMPACT.sub(compact, _ISO.sub(iso, text)))


def _shift(value: Any, days: int) -> Any:
    if isinstance(value, str):
        return _shift_text(value, days)
    if isinstance(value, list):
        return [_shift(v, days) for v in value]
    if isinstance(value, dict):
        return {k: _shift(v, days) for k, v in value.items()}
    return value


def anchor(env: Environment, wall: dt.datetime | None = None) -> Environment:
    """The same world, moved to the wall clock: it starts *now*, and every seeded date (in data,
    task text, events and checks) moves by whole weeks, so weekdays and times of day stay right.
    Real clients (gog, gh, Google's libraries) read the machine's clock; this keeps them in step."""
    wall = (wall or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    base_now = env.now if env.now not in (None, WALLCLOCK) else DEFAULT_NOW
    base = dt.datetime.fromisoformat(str(base_now).replace("Z", "+00:00"))
    days = ((wall - base).days // 7) * 7  # whole weeks, rounded down: seeded history stays in the past
    base_env = dataclasses.replace(env, now=base.isoformat())
    from .services import get_service
    servers = {}
    for s, cfg in env.servers.items():
        seed = base_env.seed_for(s)
        if seed is None:
            seed = get_service(env.service_for(s)).default_seed()
        servers[s] = {**{k: v for k, v in cfg.items() if k not in ("seed", "seed_file", "extend", "noise")},
                      "seed": _shift(seed, days)}
    return dataclasses.replace(env, now=wall.isoformat(), servers=servers, task=_shift(env.task, days),
                               events=_shift(env.events, days), checks=_shift(env.checks, days),
                               agents=_shift(env.agents, days), description=_shift(env.description, days),
                               ambient=env.ambient)


class EnvRun:
    """A running environment: one isolated instance per server, one shared clock.

    Snapshots are atomic across every server (all instance locks are held), so a multi-agent
    world can be saved, restored or forked at any instant. A run is also a world
    (``myworld.world``): servers and ``components`` (directories, databases, remote processes)
    are its components; every call, injected event, mutation and passage of time goes into its
    ``journal``; ``checkpoint``/``branch``/``replay``/``diff``/``mutate`` work on the whole thing."""

    def __init__(self, env: Environment, run_id: str | None = None, *, clone_of: EnvRun | None = None,
                 store: Any = None):
        from .services import get_service
        from .world import ServiceComponent, World, build
        if env.now == WALLCLOCK:
            env = anchor(env)
        self.env = env
        self.id = run_id or env.name
        self.start = dt.datetime.fromisoformat(str(env.now or DEFAULT_NOW).replace("Z", "+00:00"))
        self.clock = Clock(self.start, speed=env.speed)
        self.lock = threading.RLock()  # one lock for the whole world: calls and events never interleave
        self.fired: list[int] = []
        self.matches: dict[int, int] = {}
        self.events = env.world_events()
        self.answer: str | None = None  # the agent's final answer, once it submits one
        self.instances = {s: Instance(get_service(env.service_for(s)), env.seed_for(s), rng_seed=env.rng_seed,
                                      faults=env.faults_for(s), instance_id=f"{self.id}-{s}",
                                      version=env.version_for(s), clock=self.clock, lock=self.lock, hooks=self)
                          for s in env.servers}
        self._server_of = {id(i): s for s, i in self.instances.items()}
        self.world = World(lock=self.lock, now=lambda: self.clock.now.isoformat())
        self.store = store if store is not None else (clone_of.store if clone_of is not None else None)
        for s, inst in self.instances.items():
            self.world.add(s, ServiceComponent(inst))
        for name, spec in env.components.items():  # others' own directories and databases are cloned, never shared
            self.world.add(name, clone_of.world.components[name].clone() if clone_of else build(spec, env.base_dir))
        self._time_logged = self.start
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
            self._play_history()
            self._initial_components = {n: self.world.components[n].snapshot() for n in env.components}
            self.world.journal.clear()
            self.world.checkpoints[0] = None  # the start: rebuilt from the spec when needed (see _checkpoint)

    def _play_history(self) -> None:
        """A world that starts part-way through (``history``, e.g. a fork of another run): its entries
        are replayed exactly, and the run's own journal starts after them."""
        if not self.env.history:
            return
        diverged = self._replay(self.env.history)
        if diverged:
            raise ValueError(f"history doesn't replay on this environment (first divergence: {diverged[0]})")
        self._time_logged = self.clock.now

    @property
    def journal(self) -> list[dict[str, Any]]:
        return self.world.journal

    @property
    def components(self) -> dict[str, Any]:
        """The non-service components (directories, databases, remote processes)."""
        return {n: self.world.components[n] for n in self.env.components}

    @contextlib.contextmanager
    def _all_locked(self):
        with self.lock:
            yield

    def reset(self) -> None:
        with self._all_locked():
            self.clock.now, self.clock.seq = self.start, 0
            self.clock.reanchor()
            self.fired, self.matches, self.answer = [], {}, None
            for inst in self.instances.values():
                inst.reset()
            for n, snap in self._initial_components.items():
                self.world.components[n].restore(snap)
            self.world.journal.clear()
            self.world.checkpoints.clear()
            self._time_logged = self.start
            self._due_by_time()
            self._play_history()
            self.world.journal.clear()
            self.world.checkpoints[0] = None

    def snapshot(self) -> dict[str, Any]:
        with self._all_locked():
            out = {"clock": self.clock.now.isoformat(), "seq": self.clock.seq, "fired": list(self.fired),
                   "answer": self.answer,
                   "matches": dict(self.matches), "instances": {s: i.snapshot() for s, i in self.instances.items()}}
            if hasattr(self, "world"):
                out["components"] = {n: c.snapshot() for n, c in self.components.items()}
                out["journal"] = len(self.world.journal)
            return out

    def restore(self, snap: dict[str, Any]) -> None:
        with self._all_locked():
            for s, i in self.instances.items():
                i.restore(snap["instances"][s])
            self.clock.now, self.clock.seq = dt.datetime.fromisoformat(snap["clock"]), snap["seq"]
            self.clock.reanchor()
            self.fired = list(snap.get("fired", []))
            self.answer = snap.get("answer")
            self.matches = {int(k): v for k, v in snap.get("matches", {}).items()}
            for n, c in self.components.items():
                if n in snap.get("components", {}):
                    c.restore(snap["components"][n])
            if "journal" in snap:  # going back in time: what came after is no longer history
                del self.world.journal[snap["journal"]:]
            self._time_logged = self.clock.now

    # -- world events ------------------------------------------------------------------------

    def _fire(self, n: int, source: str) -> None:
        ev = self.events[n]
        self.fired.append(n)
        if "component" in ev:
            self.world.components[ev["component"]].mutate(ev["mutate"], **(ev.get("params") or {}))
            return
        self.instances[ev["server"]].apply_action(ev["action"], ev.get("params"), as_=ev.get("as"), sync=False,
                                                  source=f"{ev.get('name') or 'event ' + str(n + 1)} ({source})")

    def deliver(self, source: Instance, n: dict[str, Any]) -> None:
        """Route a service's notification email to the recipient's mailbox, in any Gmail here."""
        for inst in self.instances.values():
            if inst is not source and inst.service.name == "gmail" and n["to"] in inst.state["mailboxes"]:
                inst.apply_action("deliver_email", {k: n[k] for k in ("to", "sender", "subject", "body")}
                                  | ({"labels": n["labels"]} if n["labels"] else {}),
                                  source=f"notification from {self._server_of[id(source)]}", advance=False, sync=False)

    def tick(self) -> None:
        """Bring the world up to date (realtime mode: wall time passes, due events fire)."""
        with self.lock:
            self._due_by_time()

    def _happened(self) -> tuple[int, ...]:
        return (*(len(i.events) for i in self.instances.values()), *(len(i.pending) for i in self.instances.values()),
                len(self.fired))

    def _due_by_time(self) -> None:
        self.clock.sync()
        if self.clock.realtime and not self.world.replaying and self.clock.now != self._time_logged:
            # wall time passed: if that makes something happen, a replay needs to know when
            before, n = self._happened(), len(self.world.journal)
            self.world.record({"kind": "time", "to": self.clock.now.isoformat()})
            self._time_logged = self.clock.now
            self._run_due()
            if self._happened() == before and len(self.world.journal) == n + 1:
                self.world.journal.pop()  # nothing happened: no need to remember the moment
            return
        self._run_due()

    def _run_due(self) -> None:
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
        from .world import result_sha
        server = self._server_of[id(inst)]
        self.world.record({"kind": "call", "component": server, "tool": record["tool"], "args": record["args"],
                           "agent": record.get("agent"), "as": record.get("as"), "t0": record.get("t0"),
                           "call_at": record["at"], "mode": record.get("mode"), "raw": record.get("raw", False),
                           "result_sha": result_sha(record.get("result"))})
        self._time_logged = self.clock.now
        self._matching("after", server, record["tool"], record.get("agent"), record)
        self._due_by_time()

    def advance(self, seconds: float) -> None:
        """Let time pass (e.g. an agent waiting); fires any timed events that become due."""
        with self.lock:
            self.clock.now += dt.timedelta(seconds=seconds)
            self.world.record({"kind": "advance", "seconds": seconds})
            self._time_logged = self.clock.now
            self._due_by_time()

    def inject(self, server: str, action_name: str, params: dict[str, Any] | None = None, as_: str | None = None) -> Any:
        """Make something happen right now (for harnesses driving a live run)."""
        if server not in self.instances:
            raise ValueError(f"unknown server {server!r}")
        with self.lock:
            result = self.instances[server].apply_action(action_name, params, as_=as_, source="injected")
            self.world.record({"kind": "inject", "component": server, "action": action_name,
                               "params": copy.deepcopy(params or {}), "as": as_})
            return result

    def mutate(self, name: str, op: str, **params: Any) -> Any:
        """Change a component (a directory, database or remote process), or run a service's world
        action; recorded in the journal so branches and replays repeat it."""
        if name in self.instances:
            return self.inject(name, op, params, as_=params.pop("as", None))
        return self.world.mutate(name, op, **params)

    def catch_up(self) -> None:
        """A real-time run that sat in storage: let the time that passed meanwhile pass here too."""
        with self.lock:
            gap = (dt.datetime.now(dt.timezone.utc) - self.clock.now).total_seconds()
            if gap > 0:
                self.advance(gap)
            self.clock.reanchor()

    def submit(self, answer: str) -> None:
        with self.lock:
            self.answer = answer
            self.world.record({"kind": "answer", "answer": answer})

    # -- checkpoints, branches, replays -------------------------------------------------------

    def checkpoint(self) -> int:
        """Remember this instant; returns its step (the journal's length). With a store, the
        checkpoint is a reference into it, sharing everything that didn't change."""
        with self.lock:
            snap = self.snapshot()
            if self.store is not None:
                return self.world.checkpoint({"$ref": self.store.put(snap)})
            return self.world.checkpoint(snap)

    def branch(self, step: int | None = None, run_id: str | None = None) -> EnvRun:
        """An independent run as this one was after ``step`` journal entries (default: now):
        the nearest checkpoint at or before it, then the journal replayed up to it."""
        with self.lock:
            step = len(self.world.journal) if step is None else step
            if not 0 <= step <= len(self.world.journal):
                raise ValueError(f"step must be within 0..{len(self.world.journal)}")
            base = self.world.nearest_checkpoint(step)
            other = EnvRun(dataclasses.replace(self.env, speed=None), run_id or f"{self.id}-branch", clone_of=self)
            other.restore(self._checkpoint(base))
            other.world.journal[:] = copy.deepcopy(self.world.journal[:base])
            other.world.checkpoints = {k: copy.deepcopy(v) for k, v in self.world.checkpoints.items() if k <= base}
            other.world.checkpoints[0] = None
            diverged = other._replay(self.world.journal[base:step])
            other.clock.speed = self.clock.speed  # replayed deterministically; carries on like the original
            other.clock.reanchor()
            other.diverged = diverged
            return other

    def counterfactual(self, step: int, changes: list[dict[str, Any]], *, replay_rest: bool = True,
                       run_id: str | None = None) -> tuple[EnvRun, dict[str, Any]]:
        """What if the world had been different at ``step``? Branch there, apply ``changes``
        ([{"component", "op", "params"}]: a mutation or a service's world action), then (by default)
        replay the recorded actions that came after, and compare. Returns the new run and a report:
        where results diverged and whether the outcome changed."""
        with self.lock:
            actual = self.grade()
            other = self.branch(step, run_id or f"{self.id}-cf")
        applied = []
        for ch in changes:
            if not isinstance(ch, dict) or not ch.get("component") or not ch.get("op"):
                raise ValueError("each change needs a component and an op (with optional params)")
            other.mutate(ch["component"], ch["op"], **copy.deepcopy(ch.get("params") or {}))
            other.world.journal[-1]["counterfactual"] = True
            applied.append(ch)
        diverged = other._replay(self.world.journal[step:]) if replay_rest else []
        after = other.grade()
        report = {"step": step, "changes": applied, "replayed": len(self.world.journal) - step if replay_rest else 0,
                  "diverged": diverged, "first_divergence": diverged[0]["step"] if diverged else None,
                  "actual": {"score": actual["score"], "passed": actual["passed"]},
                  "counterfactual": {"score": after["score"], "passed": after["passed"],
                                     "failed": [c["name"] for c in after["checks"] if not c["passed"]]},
                  "outcome_changed": (actual["score"], actual["passed"]) != (after["score"], after["passed"])}
        other.diverged = diverged
        return other, report

    def replay(self) -> list[dict[str, Any]]:
        """Re-run the whole journal from the first checkpoint and report where results differ from
        what happened (an empty list: the run is reproducible)."""
        return self.branch(len(self.world.journal), f"{self.id}-replay").diverged

    def _replay(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from .world import result_sha
        diverged = []
        self.world.replaying = True
        try:
            for e in entries:
                n = len(self.world.journal)
                got = self._apply(e)
                del self.world.journal[n:]
                self.world.journal.append({**copy.deepcopy(e), "i": n})
                if e.get("result_sha") and got is not None and result_sha(got) != e["result_sha"]:
                    diverged.append({"step": e["i"], "kind": e["kind"], "component": e.get("component"),
                                     "tool": e.get("tool") or e.get("op")})
        finally:
            self.world.replaying = False
        return diverged

    def _apply(self, e: dict[str, Any]) -> Any:
        kind = e["kind"]
        if kind == "call":
            inst = self.instances[e["component"]]
            if e.get("mode") == "realtime":
                self.clock.pin = dt.datetime.fromisoformat(e["call_at"])
            elif e.get("t0"):  # never backwards (a counterfactual change may have taken a moment)
                self.clock.now = max(self.clock.now, dt.datetime.fromisoformat(e["t0"]))
            tool = None
            if e.get("raw"):
                from .api import resolve
                args = e["args"]
                found = resolve(args.get("host", ""), args.get("method", "GET"), args.get("path", ""),
                                {inst.service.name})
                tool = found[0].tool if found else None
            result = inst.call(e["tool"], copy.deepcopy(e["args"]), agent=e.get("agent"), as_=e.get("as"), tool=tool)
            return inst.calls[-1].get("result") if inst.calls else result.text
        if kind == "inject":
            return self.inject(e["component"], e["action"], copy.deepcopy(e["params"]), as_=e.get("as"))
        if kind == "advance":
            self.advance(e["seconds"])
        elif kind == "time":
            self.clock.now = dt.datetime.fromisoformat(e["to"])
            self._due_by_time()
        elif kind == "mutate":
            return self.world.mutate(e["component"], e["op"], **copy.deepcopy(e["params"]))
        elif kind == "answer":
            self.submit(e["answer"])
        return None

    # -- moving and keeping runs ---------------------------------------------------------------------

    def export(self) -> dict[str, Any]:
        """Everything needed to rebuild this run elsewhere: its environment (seeds inline), state,
        journal and checkpoints. JSON-able; see ``EnvRun.from_export``."""
        with self.lock:
            if any(c.get("path") for c in self.env.components.values()):
                raise ValueError("runs with components at local paths can't move; use unnamed components")
            return {"format": "myworld.run/1", "id": self.id, "env": self.env.to_dict(materialize=True),
                    "snapshot": self.snapshot(), "journal": copy.deepcopy(self.world.journal),
                    "checkpoints": {str(k): (None if v is None else self._checkpoint(k))
                                    for k, v in self.world.checkpoints.items()},
                    "initial_components": copy.deepcopy(self._initial_components)}

    @classmethod
    def from_export(cls, doc: dict[str, Any], run_id: str | None = None, store: Any = None) -> EnvRun:
        if doc.get("format") != "myworld.run/1":
            raise ValueError("not an exported myworld run")
        run = cls(Environment.from_dict(doc["env"], base_dir=None), run_id or doc["id"], store=store)
        with run.lock:
            run.restore(doc["snapshot"])
            run.world.journal[:] = copy.deepcopy(doc["journal"])
            run.world.checkpoints = {int(k): v for k, v in doc["checkpoints"].items()}
            run._initial_components = copy.deepcopy(doc.get("initial_components") or {})
            if store is not None:
                run.world.checkpoints = {k: v if v is None else {"$ref": store.put(v)}
                                         for k, v in run.world.checkpoints.items()}
        return run

    def save(self, store: Any, name: str) -> str:
        """Keep this run in a durable store under ``name``; returns the reference."""
        ref = store.put(self.export())
        store.name(name, ref)
        return ref

    @classmethod
    def load(cls, store: Any, name: str, run_id: str | None = None) -> EnvRun:
        ref = name if name.startswith("sha256:") else store.lookup(name)
        return cls.from_export(store.get(ref), run_id, store=store)

    def diff(self, since: int = 0, until: int | None = None) -> dict[str, list[dict[str, Any]]]:
        """What changed in every server and component between two checkpoints (default: since the start)."""
        return self.world.diff(self._checkpoint(since), None if until is None else self._checkpoint(until))

    def _checkpoint(self, step: int) -> dict[str, Any]:
        """A checkpoint's snapshot; the start is rebuilt from the spec (the same seed makes the same world)."""
        if step not in self.world.checkpoints:
            raise ValueError(f"no checkpoint at step {step} (have {sorted(self.world.checkpoints)})")
        snap = self.world.checkpoints[step]
        if isinstance(snap, dict) and set(snap) == {"$ref"}:
            return self.store.get(snap["$ref"])
        if snap is None:
            fresh = EnvRun(dataclasses.replace(self.env, speed=None), f"{self.id}-start", clone_of=self)
            for n, c in fresh.components.items():
                c.restore(self._initial_components[n])
            snap = fresh.snapshot()
            fresh.world.close()  # its components are temporary clones
            self.world.checkpoints[step] = snap
        return snap

    def timeline(self) -> list[dict[str, Any]]:
        """Agent calls and world events together, in the order they happened."""
        items = [{**c, "server": s, "kind": "call"} for s, i in self.instances.items() for c in i.calls]
        items += [{**e, "server": s, "kind": "event"} for s, i in self.instances.items() for e in i.events]
        return sorted(items, key=lambda x: x["global_seq"])

    def fork(self, run_id: str) -> EnvRun:
        with self.lock:
            clone = EnvRun(self.env, run_id, clone_of=self)
            snap = self.snapshot()
            clone.restore(snap)
            clone.world.journal[:] = copy.deepcopy(self.world.journal)
            clone.world.checkpoints = copy.deepcopy(self.world.checkpoints)
            return clone

    def calls(self) -> list[dict[str, Any]]:
        """Every call from every agent, in the order they happened."""
        merged = [{**c, "server": s} for s, i in self.instances.items() for c in i.calls]
        return sorted(merged, key=lambda c: c["global_seq"])

    def worlds(self) -> dict[str, dict[str, Any]]:
        out = {s: {"state": i.state, "calls": i.calls} for s, i in self.instances.items()}
        for n, c in self.components.items():
            v = c.view()
            out[n] = {"view": v, "state": v, "calls": []}
        return out

    def grade(self, answer: str | None = None) -> dict[str, Any]:
        with self._all_locked():
            return self.env.grade(self.worlds(), answer if answer is not None else self.answer)

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
