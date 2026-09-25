"""Fault injection: make tool calls fail the way real services fail.

The kinds that matter most for agents are the ambiguous ones. ``timeout_after_commit`` performs
the action and *then* reports a timeout, which is the case where a naive retry creates a
duplicate. Faults are deterministic: they fire on specific call numbers, or with a probability
drawn from the instance's seeded RNG.

    {"tool": "send_email", "kind": "timeout_after_commit", "on_call": 1}
    {"tool": "*", "kind": "rate_limit", "probability": 0.1, "times": 3}
"""

from __future__ import annotations

import fnmatch
import random
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

FaultKind = Literal[
    "timeout",               # never reaches the service: nothing happens, caller sees a timeout
    "timeout_after_commit",  # the action happens, then the caller sees a timeout
    "server_error",          # 5xx before anything happens
    "rate_limit",            # 429 with retry-after
    "error",                 # a custom service error (payload given in the fault)
    "latency",               # succeeds, slowly
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
    retry_after: int = 30             # rate_limit
    payload: Any = None               # error: what the agent sees
    fired: int = field(default=0)
    seen: int = field(default=0)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown fault kind {self.kind!r}; expected one of {sorted(KINDS)}")

    def matches(self, tool: str) -> bool:
        return fnmatch.fnmatchcase(tool, self.tool)

    def should_fire(self, rng: random.Random) -> bool:
        """Call once per matching tool call."""
        self.seen += 1
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
