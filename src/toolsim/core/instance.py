"""Services and instances.

A :class:`Service` describes one fake tool: its state model, its tools and how it phrases
errors. An :class:`Instance` is one isolated, running copy of a service with its own state,
virtual clock, ID sequence, faults and call log. Instances never share anything, so any number
can run side by side (one per agent, per rollout, per test).

Instances are deterministic: the same seed data and ``rng_seed`` produce the same IDs,
timestamps and fault draws on every run. That's what makes runs comparable and forkable.
"""

from __future__ import annotations

import contextlib
import copy
import logging
import pickle
import datetime as dt
import json
import random
import string
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, ClassVar, Iterator

from .faults import Fault, TransportFault, parse_faults
from .tools import Action, Tool, ToolError, validate_args, version_key

log = logging.getLogger("toolsim")

DEFAULT_NOW = "2026-09-21T16:00:00+00:00"  # Monday 09:00 in San Francisco
_REASONS = {404: "Not Found", 429: "Too Many Requests", 500: "Internal Server Error", 502: "Bad Gateway",
            503: "Service Unavailable", 504: "Gateway Timeout"}


class Service:
    """Subclass per tool. Set ``name``, ``tools`` and implement ``initial_state``."""

    name: ClassVar[str] = ""
    title: ClassVar[str] = ""
    description: ClassVar[str] = ""
    tools: ClassVar[list[Tool]] = []
    actions: ClassVar[list[Action]] = []  # world events environments can trigger (never agent-visible)
    # Date-based versions, oldest first: {"2026-09-25": "what changed"}. A version is a frozen
    # contract: tool definitions and behavior. Any change ships as a new date; old dates keep
    # working (gate changes on ``ctx.version`` or ``@tool(since=..., until=...)``).
    versions: ClassVar[dict[str, str]] = {}
    # How closely the interface is known to match the real MCP server:
    #   "documented": tool names and parameters taken from the real server's published reference
    #   "preview":    tool names are real; some parameters or response shapes are inferred
    fidelity: ClassVar[str] = "documented"

    @classmethod
    def latest_version(cls) -> str:
        return max(cls.versions, key=version_key)

    def resolve_version(self, version: str | None) -> str:
        if version in (None, "", "latest"):
            return self.latest_version()
        v = str(version)
        if v not in self.versions:
            raise ValueError(f"{self.name} has no version {v!r}; available: {', '.join(self.versions)}")
        return v

    def initial_state(self, seed: dict[str, Any], ctx: Instance) -> dict[str, Any]:
        raise NotImplementedError

    # -- multiple agents -----------------------------------------------------------------
    # An instance is a whole workspace (a company's mail system, a Slack workspace, a GitHub
    # org). Each agent acts as one person in it. Services map an identity (usually an email)
    # to their own user key, and tools read ``ctx.actor`` instead of a fixed user.

    # Services whose state already names "the current user" (``state["me"]``, ``state["viewer"]``...)
    # set this: during a call made as someone else, that entry holds the acting user.
    actor_key: ClassVar[str | None] = None

    def default_actor(self, state: dict[str, Any]) -> str:
        """Who calls act as when the agent doesn't say."""
        return state.get(self.actor_key, "") if self.actor_key else ""

    def resolve_actor(self, state: dict[str, Any], identity: str) -> str:
        """Map an identity (email, login, name) to this service's user key. Raise ValueError if unknown."""
        raise ValueError(f"{self.name} does not support acting as other users")

    def probe(self, ctx: Instance) -> None:
        """A fixed script of representative calls on the default world. Its results are frozen per
        version (see toolsim.versions), so any behavior change shows up as a changed fingerprint."""

    def default_seed(self) -> dict[str, Any]:
        """A small, realistic world used when an environment doesn't provide one."""
        return {}

    # How this service phrases failures. Override to match the real API's error shapes.
    def fault_error(self, fault: Fault) -> tuple[Any, int]:
        return {
            "timeout": ("Error: request timed out after 30000ms (ETIMEDOUT)", 504),
            "timeout_after_commit": ("Error: request timed out after 30000ms (ETIMEDOUT)", 504),
            "server_error": ({"error": {"code": 500, "message": "Internal error encountered."}}, 500),
            "rate_limit": ({"error": {"code": 429, "message": "Rate limit exceeded. Retry after "
                                                                f"{fault.retry_after}s."}}, 429),
            "error": (fault.payload or {"error": "error"}, 400),
        }[fault.kind]

    def grading_view(self, state: dict[str, Any]) -> dict[str, Any]:
        """State as checks see it. Services add derived facts agents never see (e.g. which channel)."""
        return state

    def error_shape(self, status: int, message: str) -> Any:
        """This service's native error body for an HTTP status (used by reliability faults)."""
        return {"error": {"code": status, "message": message}}

    def render(self, value: Any) -> str:
        return value if isinstance(value, str) else json.dumps(value, indent=2, default=str)


def _clone(value: Any) -> Any:
    """A deep copy of JSON-like state, ~3x faster than copy.deepcopy (rollback runs on every write)."""
    try:
        return pickle.loads(pickle.dumps(value, pickle.HIGHEST_PROTOCOL))  # noqa: S301 (our own bytes, never external)
    except Exception:
        return copy.deepcopy(value)


class Clock:
    """Virtual time plus a global call counter. Instances in one environment share a clock, so
    timestamps line up across services and calls from every agent form one ordered timeline."""

    def __init__(self, now: dt.datetime):
        self.now = now
        self.seq = 0


@dataclass
class CallResult:
    text: str
    is_error: bool
    data: Any = None


class Instance:
    def __init__(self, service: Service, seed: dict[str, Any] | None = None, *, rng_seed: int = 0,
                 faults: list[dict[str, Any]] | None = None, instance_id: str | None = None,
                 version: str | None = None, clock: Clock | None = None,
                 lock: threading.RLock | None = None, hooks: Any = None):
        self.service = service
        self.version = service.resolve_version(version)
        self.id = instance_id or f"{service.name}-{uuid.uuid4().hex[:8]}"
        self.seed = copy.deepcopy(seed) if seed is not None else service.default_seed()
        self.rng_seed = rng_seed
        self.fault_specs = faults or []
        self.lock = lock or threading.RLock()  # an environment shares one lock across its instances
        self.hooks = hooks  # before_call(inst, tool, args, agent) / after_call(inst, record), set by EnvRun
        self.tools = {t.name: t for t in service.tools if t.in_version(self.version)}
        self._actor: str | None = None
        self._shared_clock = clock is not None
        self._clock = clock or Clock(dt.datetime.fromisoformat(str(self.seed.get("now") or DEFAULT_NOW).replace("Z", "+00:00")))
        self.reset()

    @property
    def clock(self) -> dt.datetime:
        return self._clock.now

    @clock.setter
    def clock(self, value: dt.datetime) -> None:
        self._clock.now = value

    # -- lifecycle ---------------------------------------------------------------------

    def reset(self) -> None:
        """Back to the seed: same state, same clock, same future IDs."""
        with self.lock:
            self.rng = random.Random(self.rng_seed)
            self.start_time = dt.datetime.fromisoformat(str(self.seed.get("now") or DEFAULT_NOW).replace("Z", "+00:00"))
            if not self._shared_clock:  # a shared clock is reset by its environment
                now = self.seed.get("now") or DEFAULT_NOW
                self.clock = dt.datetime.fromisoformat(str(now).replace("Z", "+00:00"))
                self._clock.seq = 0
            self.counters: dict[str, int] = {}
            self.calls: list[dict[str, Any]] = []
            self.events: list[dict[str, Any]] = []  # world events applied to this instance
            self.pending: list[dict[str, Any]] = []  # scheduled world actions (see schedule())
            self.faults = parse_faults(self.fault_specs)
            try:
                self.state = self.service.initial_state(copy.deepcopy(self.seed), self)
            except ToolError as e:
                raise ValueError(f"invalid seed for {self.service.name}: {self.service.render(e.payload)}") from None
            except (KeyError, TypeError, AttributeError, IndexError, ValueError) as e:
                raise ValueError(f"invalid seed for {self.service.name}: {type(e).__name__}: {e}") from None

    def snapshot(self) -> dict[str, Any]:
        """Everything needed to resume from this exact point (fork a run here)."""
        with self.lock:
            return copy.deepcopy({
                "state": self.state, "clock": self.clock.isoformat(), "seq": self._clock.seq, "counters": self.counters,
                "rng": self.rng.getstate(), "calls": self.calls, "events": self.events, "pending": self.pending,
                "faults": [f.to_dict() for f in self.faults],
            })

    def restore(self, snap: dict[str, Any]) -> None:
        with self.lock:
            snap = copy.deepcopy(snap)
            self.state = snap["state"]
            self.clock = dt.datetime.fromisoformat(snap["clock"])
            self._clock.seq = snap.get("seq", self._clock.seq)
            self.counters = snap["counters"]
            rng = snap["rng"]
            self.rng.setstate((rng[0], tuple(rng[1]), rng[2]) if isinstance(rng, list) else rng)
            self.calls = snap["calls"]
            self.events = snap.get("events", [])
            self.pending = snap.get("pending", [])
            self.faults = [Fault(**f) for f in snap["faults"]]

    def set_faults(self, specs: list[dict[str, Any]]) -> None:
        with self.lock:
            self.fault_specs = specs
            self.faults = parse_faults(specs)

    # -- helpers for tool implementations ----------------------------------------------

    @property
    def actor(self) -> str:
        """The user the current call acts as."""
        return self._actor or self.service.default_actor(self.state)

    def resolve_actor(self, identity: str | None) -> str | None:
        if not identity:
            return None
        return self.service.resolve_actor(self.state, identity)

    def now(self) -> dt.datetime:
        return self.clock

    def at_least(self, version: str) -> bool:
        """Gate behavior introduced in ``version`` so older versions keep behaving as frozen."""
        return version_key(self.version) >= version_key(version)

    def schedule(self, delay_s: float, action_name: str, params: dict[str, Any] | None = None,
                 as_: str | None = None, reason: str = "") -> None:
        """Make a world action happen later (a bounce, a colleague's reply, CI finishing). Runs when
        virtual time passes the due time: on the next call to any instance of the environment, or when
        time is advanced."""
        due = self.clock + dt.timedelta(seconds=delay_s)
        self.pending.append({"due": due.isoformat(), "action": action_name, "params": copy.deepcopy(params or {}),
                             "as": as_, "reason": reason, "n": self.next("_pending")})

    def run_due(self) -> int:
        """Apply scheduled actions that are due, in due-time order. Returns how many ran."""
        ran = 0
        while True:
            due = [p for p in self.pending if dt.datetime.fromisoformat(p["due"]) <= self.clock]
            if not due:
                return ran
            item = min(due, key=lambda p: (p["due"], p["n"]))
            self.pending.remove(item)
            now = self.clock
            self.clock = dt.datetime.fromisoformat(item["due"])  # it happened when it was due, not now
            try:
                self.apply_action(item["action"], item["params"], as_=item["as"],
                                  source=f"scheduled: {item['reason'] or item['action']}", advance=False)
            except ValueError:
                log.warning("scheduled %s.%s failed", self.service.name, item["action"], exc_info=True)
            finally:
                self.clock = max(now, self.clock)
            ran += 1

    def advance(self, seconds: float) -> None:
        self.clock += dt.timedelta(seconds=seconds)

    def next(self, name: str, start: int = 1) -> int:
        self.counters[name] = self.counters.get(name, start - 1) + 1
        return self.counters[name]

    def hex(self, n: int) -> str:
        return "".join(self.rng.choice("0123456789abcdef") for _ in range(n))

    def token(self, n: int, alphabet: str = string.ascii_uppercase + string.digits) -> str:
        return "".join(self.rng.choice(alphabet) for _ in range(n))

    # -- the agent-facing entry point ----------------------------------------------------

    def list_tools(self) -> list[dict[str, Any]]:
        return [t.mcp_definition() for t in self.tools.values()]

    @contextlib.contextmanager
    def _acting(self, actor: str | None) -> Iterator[None]:
        self._actor = actor
        key = self.service.actor_key
        swapped = key is not None and actor is not None and self.state.get(key) != actor
        if swapped:
            default, self.state[key] = self.state[key], actor
        try:
            yield
        finally:
            if swapped:
                self.state[key] = default
            self._actor = None

    def call(self, name: str, args: dict[str, Any] | None, *, agent: str | None = None,
             as_: str | None = None) -> CallResult:
        """One tool call. ``agent`` names the calling agent (for attribution); ``as_`` is the
        identity it acts as (resolved per service; default: the service's default user)."""
        args = args or {}
        with self.lock:
            self.advance(self.rng.randint(1, 3))  # a call takes a moment of (virtual) time
            try:
                actor = self.resolve_actor(as_)
            except ValueError as e:
                return CallResult(f"Error: {e}", True)
            if self.hooks:
                self.hooks.before_call(self, name, args, agent)
            else:
                self.run_due()
            with self._acting(actor):
                result = self._call(name, args, agent)
            if self.hooks and self.calls:
                self.hooks.after_call(self, self.calls[-1])
            return result

    def apply_action(self, name: str, params: dict[str, Any] | None = None, *, as_: str | None = None,
                     source: str = "event", advance: bool = True) -> Any:
        """Make the world change (not an agent call). Raises ValueError on bad actions or params."""
        act = next((a for a in self.service.actions if a.name == name), None)
        if act is None:
            raise ValueError(f"{self.service.name} has no action {name!r}; "
                             f"available: {', '.join(a.name for a in self.service.actions) or 'none'}")
        with self.lock:
            if advance:
                self.advance(1)  # world events take a moment too, and so are strictly ordered in time
            actor = self.resolve_actor(as_)
            with self._acting(actor):
                try:
                    result = act.fn(self, **(params or {}))
                except ToolError as e:
                    raise ValueError(f"{self.service.name}.{name}: {self.service.render(e.payload)}") from None
                except TypeError as e:
                    raise ValueError(f"{self.service.name}.{name}: {e}") from None
            self._clock.seq += 1
            self.events.append({"global_seq": self._clock.seq, "at": self.clock.isoformat(), "event": name,
                                "params": copy.deepcopy(params or {}), "as": actor, "source": source})
            return result

    def _call(self, name: str, args: dict[str, Any], agent: str | None) -> CallResult:
        with self.lock:
            self._clock.seq += 1
            record: dict[str, Any] = {"seq": len(self.calls) + 1, "global_seq": self._clock.seq,
                                      "at": self.clock.isoformat(), "tool": name,
                                      "args": copy.deepcopy(args), "committed": False, "fault": None,
                                      "agent": agent, "actor": self.actor}
            self.calls.append(record)
            t = self.tools.get(name)
            if t is None:
                return self._finish(record, CallResult(f"Unknown tool: {name}", True))

            fault = next((f for f in self.faults if f.matches(name) and f.should_fire(self.rng, self.clock, self.start_time)),
                         None)
            if fault is not None:
                record["fault"] = fault.kind
                if fault.hang_s:
                    time.sleep(fault.hang_s)
                if fault.delay_s:
                    self.advance(fault.delay_s)
                if fault.kind == "transport_error":
                    status = fault.status or 502
                    record["ok"], record["result"] = False, f"HTTP {status}"
                    raise TransportFault(status, f"<html><head><title>{status} {_REASONS.get(status, 'Error')}</title>"
                                                 f"</head><body><h1>{status} {_REASONS.get(status, 'Error')}</h1></body></html>")
                if fault.kind not in ("timeout_after_commit", "latency", "truncated", "duplicate_commit"):
                    payload = self._fault_payload(fault)
                    return self._finish(record, CallResult(self.service.render(payload), True, payload))

            before = None if t.read_only else _clone(self.state)
            try:
                value = t.fn(self, **validate_args(t, args))
                if fault is not None and fault.kind == "duplicate_commit" and not t.read_only:
                    t.fn(self, **validate_args(t, args))  # the proxy retried; the caller never knows
                record["committed"] = not t.read_only
                result = CallResult(self.service.render(value), False, value)
                if fault is not None and fault.kind == "truncated":
                    cut = max(1, int(len(result.text) * 0.6))
                    result = CallResult(result.text[:cut], False, result.text[:cut])
            except ToolError as e:
                if before is not None:
                    self.state = before  # failed calls leave no partial writes
                result = CallResult(self.service.render(e.payload), True, e.payload)
            except Exception as e:  # a bug or an input shape the fake didn't anticipate: fail like a real 500
                if before is not None:
                    self.state = before
                log.exception("tool %s.%s failed on args %r", self.service.name, name, args)
                payload = self.service.error_shape(500, f"Internal error while handling {name}: {type(e).__name__}")
                record["internal_error"] = f"{type(e).__name__}: {e}"
                result = CallResult(self.service.render(payload), True, payload)

            if fault is not None and fault.kind == "timeout_after_commit":
                payload = self._fault_payload(fault)
                result = CallResult(self.service.render(payload), True, payload)
            return self._finish(record, result)

    def _fault_payload(self, fault: Fault) -> Any:
        simple = {"not_found": (404, "Not Found"), "unavailable": (503, "Service Unavailable"),
                  "bad_gateway": (502, "Bad Gateway")}
        if fault.kind in simple:
            status, msg = simple[fault.kind]
            return self.service.error_shape(status, msg)
        if fault.kind == "server_error" and fault.status not in (None, 500):
            return self.service.error_shape(fault.status, _REASONS.get(fault.status, "Server Error"))
        return self.service.fault_error(fault)[0]

    def _finish(self, record: dict[str, Any], result: CallResult) -> CallResult:
        record["ok"] = not result.is_error
        record["result"] = result.data if result.data is not None else result.text
        return result
