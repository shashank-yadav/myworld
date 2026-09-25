"""Services and instances.

A :class:`Service` describes one fake tool: its state model, its tools and how it phrases
errors. An :class:`Instance` is one isolated, running copy of a service with its own state,
virtual clock, ID sequence, faults and call log. Instances never share anything, so any number
can run side by side (one per agent, per rollout, per test).

Instances are deterministic: the same seed data and ``rng_seed`` produce the same IDs,
timestamps and fault draws on every run. That's what makes runs comparable and forkable.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import random
import string
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, ClassVar

from .faults import Fault, parse_faults
from .tools import Tool, ToolError, validate_args

DEFAULT_NOW = "2026-09-21T16:00:00+00:00"  # Monday 09:00 in San Francisco


class Service:
    """Subclass per tool. Set ``name``, ``tools`` and implement ``initial_state``."""

    name: ClassVar[str] = ""
    title: ClassVar[str] = ""
    description: ClassVar[str] = ""
    tools: ClassVar[list[Tool]] = []
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
        return max(cls.versions)

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

    def render(self, value: Any) -> str:
        return value if isinstance(value, str) else json.dumps(value, indent=2, default=str)


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
                 version: str | None = None, clock: Clock | None = None):
        self.service = service
        self.version = service.resolve_version(version)
        self.id = instance_id or f"{service.name}-{uuid.uuid4().hex[:8]}"
        self.seed = copy.deepcopy(seed) if seed is not None else service.default_seed()
        self.rng_seed = rng_seed
        self.fault_specs = faults or []
        self.lock = threading.RLock()
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
            if not self._shared_clock:  # a shared clock is reset by its environment
                now = self.seed.get("now") or DEFAULT_NOW
                self.clock = dt.datetime.fromisoformat(str(now).replace("Z", "+00:00"))
                self._clock.seq = 0
            self.counters: dict[str, int] = {}
            self.calls: list[dict[str, Any]] = []
            self.faults = parse_faults(self.fault_specs)
            self.state = self.service.initial_state(copy.deepcopy(self.seed), self)

    def snapshot(self) -> dict[str, Any]:
        """Everything needed to resume from this exact point (fork a run here)."""
        with self.lock:
            return copy.deepcopy({
                "state": self.state, "clock": self.clock.isoformat(), "seq": self._clock.seq, "counters": self.counters,
                "rng": self.rng.getstate(), "calls": self.calls, "faults": [f.to_dict() for f in self.faults],
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
            self._actor = actor
            key = self.service.actor_key
            swapped = key is not None and actor is not None and self.state.get(key) != actor
            if swapped:
                default, self.state[key] = self.state[key], actor
            try:
                return self._call(name, args, agent)
            finally:
                if swapped:
                    self.state[key] = default
                self._actor = None

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

            fault = next((f for f in self.faults if f.matches(name) and f.should_fire(self.rng)), None)
            if fault is not None:
                record["fault"] = fault.kind
                if fault.hang_s:
                    time.sleep(fault.hang_s)
                if fault.kind not in ("timeout_after_commit", "latency"):
                    payload, _ = self.service.fault_error(fault)
                    return self._finish(record, CallResult(self.service.render(payload), True, payload))

            before = None if t.read_only else copy.deepcopy(self.state)
            try:
                value = t.fn(self, **validate_args(t, args))
                record["committed"] = not t.read_only
                result = CallResult(self.service.render(value), False, value)
            except ToolError as e:
                if before is not None:
                    self.state = before  # failed calls leave no partial writes
                result = CallResult(self.service.render(e.payload), True, e.payload)

            if fault is not None and fault.kind == "timeout_after_commit":
                payload, _ = self.service.fault_error(fault)
                result = CallResult(self.service.render(payload), True, payload)
            return self._finish(record, result)

    def _finish(self, record: dict[str, Any], result: CallResult) -> CallResult:
        record["ok"] = not result.is_error
        record["result"] = result.data if result.data is not None else result.text
        return result
