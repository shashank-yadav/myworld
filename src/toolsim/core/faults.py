"""Fault injection: make tool calls fail the way real services fail.

The kinds that matter most for agents are the ambiguous ones. ``timeout_after_commit`` performs
the action and *then* reports a timeout, which is the case where a naive retry creates a
duplicate. Faults are deterministic: they fire on specific call numbers, or with a probability
drawn from the instance's seeded RNG.

    {"tool": "send_email", "kind": "timeout_after_commit", "on_call": 1}
    {"tool": "*", "kind": "rate_limit", "probability": 0.1, "times": 3}
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import random
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

FaultKind = Literal[
    "timeout",               # never reaches the service: nothing happens, caller sees a timeout
    "timeout_after_commit",  # the action happens, then the caller sees a timeout
    "server_error",          # 5xx before anything happens (``status``: 500 default, or 502/503/504)
    "unavailable",           # 503 Service Unavailable (use with a window for an outage)
    "bad_gateway",           # 502 Bad Gateway
    "not_found",             # 404 for something that exists (a flaky backend or replica lag)
    "rate_limit",            # 429 with retry-after
    "error",                 # a custom service error (payload given in the fault)
    "latency",               # succeeds, slowly (``hang_s`` real seconds, ``delay_s`` virtual seconds)
    "transport_error",       # the transport itself fails: an HTTP 502/503 page instead of an MCP reply
    "truncated",             # the action runs but the response is cut off mid-way
    "duplicate_commit",      # a retrying proxy executes the write twice; the caller sees one success
]
KINDS = set(FaultKind.__args__)  # type: ignore[attr-defined]


@dataclass
class Fault:
    kind: str
    tool: str = "*"                   # glob over tool names
    on_call: int | None = None        # fire on the Nth matching call (1-based)
    probability: float | None = None  # or fire randomly (seeded)
    times: int | None = 1             # how many times it may fire (None = unlimited)
    hang_s: float = 0.0               # sleep this long before answering (real client timeouts)
    delay_s: float = 0.0              # virtual time that passes during the call (world events may fire)
    retry_after: int = 30             # rate_limit
    status: int | None = None         # HTTP status for server_error / transport_error
    payload: Any = None               # error: what the agent sees
    from_call: int | None = None      # active window over matching calls (1-based, inclusive)...
    until_call: int | None = None
    start: str | None = None          # ...or over virtual time: "+10m" / "+25m" from the start, or ISO
    end: str | None = None
    fired: int = field(default=0)
    seen: int = field(default=0)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown fault kind {self.kind!r}; expected one of {sorted(KINDS)}")

    def matches(self, tool: str) -> bool:
        return fnmatch.fnmatchcase(tool, self.tool)

    def should_fire(self, rng: random.Random, now: dt.datetime | None = None, start: dt.datetime | None = None) -> bool:
        """Call once per matching tool call."""
        self.seen += 1
        windowed = any(v is not None for v in (self.from_call, self.until_call, self.start, self.end))
        if windowed:  # an outage window: every matching call inside it fails
            if self.from_call is not None and self.seen < self.from_call:
                return False
            if self.until_call is not None and self.seen > self.until_call:
                return False
            if now is not None and start is not None:
                if self.start is not None and now < _when(self.start, start):
                    return False
                if self.end is not None and now >= _when(self.end, start):
                    return False
            if self.probability is not None and rng.random() >= self.probability:
                return False
            self.fired += 1
            return True
        if self.times is not None and self.fired >= self.times:
            return False
        if self.on_call is not None:
            hit = self.seen == self.on_call
        elif self.probability is not None:
            hit = rng.random() < self.probability
        else:
            hit = True
        if hit:
            self.fired += 1
        return hit

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_faults(specs: list[dict[str, Any]] | None) -> list[Fault]:
    return [Fault(**{k: v for k, v in s.items() if k not in ("fired", "seen", "server")}) for s in specs or []]


def _when(value: str, start: dt.datetime) -> dt.datetime:
    m = re.fullmatch(r"\+(\d+(?:\.\d+)?)([smhd])", str(value).strip())
    if m:
        return start + dt.timedelta(seconds=float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)])
    t = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


class TransportFault(Exception):
    """The transport failed: no MCP response at all (an HTTP error page, a dropped connection)."""

    def __init__(self, status: int, body: str):
        super().__init__(body)
        self.status = status
        self.body = body
