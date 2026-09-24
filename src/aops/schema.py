"""The wire format every integration speaks (``aops.v1``).

Everything an agent does becomes a flat, append-only stream of events. Runs, spans,
trace trees, change feeds and cost rollups are all derived from this stream on read,
so the log is the only source of truth and integrations stay trivially simple.

An event::

    {
      "id": "uuid",                 # idempotency key; duplicates are ignored
      "ts": 1727190000.123,         # unix seconds, when it happened in the agent
      "type": "span.start",         # run.start | run.end | span.start | span.end | span.event
      "run_id": "turn-42",          # one unit of agent work ("Book meeting with John")
      "span_id": "tool:call_9",     # one step inside the run (absent on run.* events)
      "parent_span_id": "llm:req_3",# causal parent step, or null for top level
      "kind": "tool",               # see KINDS
      "name": "gmail_send_email",
      "source": "hermes",           # which integration produced it
      "attrs": {...}                # free-form: args, result, model, usage, status, error, ...
    }

``span.start`` and ``span.end`` share a ``span_id`` and their attrs are merged. A span
that starts but never ends is meaningful: for anything with side effects it means the
outcome is unknown.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

SCHEMA_VERSION = "aops.v1"

EVENT_TYPES = frozenset({"run.start", "run.end", "span.start", "span.end", "span.event"})

KINDS = frozenset({
    "llm",        # a model/provider API request
    "tool",       # a generic tool call
    "mcp",        # a tool served by an MCP server
    "browser",    # browser automation
    "terminal",   # shell command
    "memory",     # agent memory read/write
    "subagent",   # delegated child agent
    "approval",   # human approval request/response
    "message",    # inbound/outbound chat message
    "agent",      # run-level lifecycle
})

# Span/run statuses integrations may report on *.end events.
STATUSES = frozenset({"ok", "error", "timeout", "blocked", "cancelled", "interrupted"})


class InvalidEvent(ValueError):
    pass


def normalize(raw: dict[str, Any], *, source: str | None = None) -> dict[str, Any]:
    """Validate one incoming event and fill defaults. Raises InvalidEvent."""
    if not isinstance(raw, dict):
        raise InvalidEvent("event must be an object")
    etype = raw.get("type")
    if etype not in EVENT_TYPES:
        raise InvalidEvent(f"unknown event type {etype!r}")
    run_id = raw.get("run_id")
    if not run_id or not isinstance(run_id, str):
        raise InvalidEvent("run_id is required")
    span_id = raw.get("span_id")
    if etype.startswith("span.") and not span_id:
        raise InvalidEvent(f"{etype} requires span_id")
    kind = raw.get("kind") or ("agent" if etype.startswith("run.") else "tool")
    if kind not in KINDS:
        kind = "tool"
    attrs = raw.get("attrs") or {}
    if not isinstance(attrs, dict):
        raise InvalidEvent("attrs must be an object")
    ts = raw.get("ts")
    try:
        ts = float(ts) if ts is not None else time.time()
    except (TypeError, ValueError):
        raise InvalidEvent("ts must be unix seconds") from None
    if ts > 1e11:  # tolerate milliseconds from JS integrations
        ts /= 1000.0
    return {
        "id": str(raw.get("id") or uuid.uuid4()),
        "ts": ts,
        "type": etype,
        "run_id": run_id,
        "span_id": span_id or None,
        "parent_span_id": raw.get("parent_span_id") or None,
        "kind": kind,
        "name": str(raw.get("name") or ""),
        "source": str(raw.get("source") or source or "unknown"),
        "attrs": attrs,
    }
