"""Derive runs, span trees, outcomes, change feeds and rollups from the event log."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from .classify import Effect, classify, is_ambiguous_error, is_ambiguous_exit
from .pricing import estimate_cost, normalize_usage

# A span with no end event, in a run that has gone quiet for this long, is abandoned.
STALE_AFTER_S = 15 * 60
# This many consecutive failures of the same tool is flagged as a loop.
LOOP_MIN_REPEATS = 3

# outcome values
SUCCEEDED, FAILED, UNKNOWN, BLOCKED, RUNNING = "succeeded", "failed", "unknown", "blocked", "running"
# never reported a result, but had no external side effect to worry about
INCOMPLETE = "incomplete"


@dataclass
class Span:
    span_id: str
    run_id: str
    kind: str
    name: str
    parent_id: str | None = None
    start: float | None = None
    end: float | None = None
    status: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    children: list[Span] = field(default_factory=list)
    effect: Effect | None = None
    outcome: str = RUNNING
    outcome_reason: str = ""
    cost: float | None = None
    usage: dict[str, int] = field(default_factory=dict)
    point: bool = False

    @property
    def duration_ms(self) -> float | None:
        if self.start is None or self.end is None:
            return None
        return round((self.end - self.start) * 1000, 1)

    @property
    def error(self) -> Any:
        return self.attrs.get("error") or self.attrs.get("error_message")

    def as_dict(self, *, children: bool = True) -> dict[str, Any]:
        d = {
            "span_id": self.span_id, "run_id": self.run_id, "parent_id": self.parent_id,
            "kind": self.kind, "name": self.name, "start": self.start, "end": self.end,
            "duration_ms": self.duration_ms, "status": self.status, "outcome": self.outcome,
            "outcome_reason": self.outcome_reason, "effect": self.effect.as_dict() if self.effect else None,
            "cost": self.cost, "usage": self.usage, "model": self.attrs.get("model"),
            "error": self.error, "attrs": self.attrs, "point": self.point,
        }
        if children:
            d["children"] = [c.as_dict() for c in self.children]
        return d


@dataclass
class Run:
    run_id: str
    name: str = ""
    source: str = ""
    start: float | None = None
    end: float | None = None
    last_ts: float = 0.0
    status: str = "running"
    attrs: dict[str, Any] = field(default_factory=dict)
    spans: dict[str, Span] = field(default_factory=dict)
    roots: list[Span] = field(default_factory=list)
    event_count: int = 0

    @property
    def agent(self) -> str:
        return str(self.attrs.get("agent") or self.source or "unknown")

    def summary(self) -> dict[str, Any]:
        spans = list(self.spans.values())
        calls = [s for s in spans if not s.point]
        changes = [s for s in calls if s.effect and s.effect.changes_world]
        cost = sum(s.cost or 0 for s in calls)
        return {
            "calls": len(calls),
            "llm_calls": sum(s.kind == "llm" for s in calls),
            "tool_calls": sum(s.kind != "llm" for s in calls),
            "changes": sum(s.outcome == SUCCEEDED for s in changes),
            "changes_attempted": len(changes),
            "failures": sum(s.outcome == FAILED for s in calls),
            "unknown": sum(s.outcome == UNKNOWN for s in calls),
            "cost": round(cost, 6),
            "unpriced_llm_calls": sum(s.kind == "llm" and s.cost is None for s in calls),
            "tokens": {k: sum(s.usage.get(k, 0) for s in calls) for k in ("input", "output", "cache_read", "cache_write")},
            "duration_ms": round(((self.end or self.last_ts) - self.start) * 1000, 1) if self.start else None,
            "loops": self.loops(),
        }

    def loops(self, min_repeats: int = LOOP_MIN_REPEATS) -> list[dict[str, Any]]:
        """Tools that failed several times in a row: the agent is stuck retrying the same thing."""
        steps = sorted((s for s in self.spans.values() if s.kind != "llm" and not s.point), key=lambda s: s.start or 0)
        found, streak = [], []
        for s in steps + [None]:
            if s is not None and s.outcome == FAILED and (not streak or streak[-1].name == s.name):
                streak.append(s)
                continue
            if len(streak) >= min_repeats:
                found.append({"name": streak[0].name, "count": len(streak), "span_ids": [x.span_id for x in streak],
                              "error": str(streak[-1].error or "")[:200]})
            streak = [s] if s is not None and s.outcome == FAILED else []
        return found

    def as_dict(self, *, tree: bool = False) -> dict[str, Any]:
        d = {
            "run_id": self.run_id, "name": self.name or "(untitled run)", "source": self.source,
            "agent": self.agent, "user": self.attrs.get("user"), "workflow": self.attrs.get("workflow"),
            "session_id": self.attrs.get("session_id"), "model": self.attrs.get("model"),
            "parent_run_id": self.attrs.get("parent_run_id"), "parent_run_span_id": self.attrs.get("parent_run_span_id"),
            "start": self.start, "end": self.end, "last_ts": self.last_ts, "status": self.status,
            "summary": self.summary(), "event_count": self.event_count,
        }
        if tree:
            d["attrs"] = self.attrs
            d["roots"] = [s.as_dict() for s in self.roots]
        return d


def build_run(run_id: str, events: Iterable[dict[str, Any]], *, now: float | None = None) -> Run:
    now = now or time.time()
    run = Run(run_id=run_id)
    ended_explicitly = False
    for e in events:
        run.event_count += 1
        ts = e["ts"]
        run.last_ts = max(run.last_ts, ts)
        run.start = ts if run.start is None else min(run.start, ts)
        run.source = run.source or e["source"]
        attrs = e["attrs"]
        et = e["type"]
        if et == "run.start":
            run.start = ts
            run.name = e["name"] or run.name
            run.attrs.update(attrs)
        elif et == "run.end":
            run.end = ts
            ended_explicitly = True
            run.status = attrs.get("status") or "ok"
            run.attrs.update({k: v for k, v in attrs.items() if k != "status"})
            run.name = run.name or e["name"]
        else:
            sid = e["span_id"]
            span = run.spans.get(sid)
            if span is None:
                span = run.spans[sid] = Span(span_id=sid, run_id=run_id, kind=e["kind"], name=e["name"])
            span.parent_id = span.parent_id or e["parent_span_id"]
            span.name = span.name or e["name"]
            if e["kind"] != "tool" or span.kind == "tool":
                span.kind = e["kind"]
            span.attrs.update(attrs)
            if et == "span.start":
                span.start = ts
            elif et == "span.end":
                span.end = ts
                span.status = attrs.get("status") or "ok"
                if span.start is None:
                    dur = attrs.get("duration_ms")
                    span.start = ts - float(dur) / 1000 if isinstance(dur, (int, float)) else ts
            else:  # span.event: a point in time, complete on arrival
                span.point = True
                span.start = span.end = ts
                span.status = attrs.get("status") or "ok"

    finished = ended_explicitly or (now - run.last_ts) > STALE_AFTER_S
    if not ended_explicitly and finished:
        run.status = "abandoned"
    for span in run.spans.values():
        _finalize_span(span, finished)

    for span in sorted(run.spans.values(), key=lambda s: (s.start or 0)):
        parent = run.spans.get(span.parent_id) if span.parent_id else None
        if parent is not None and parent is not span:
            parent.children.append(span)
        else:
            run.roots.append(span)

    if run.status == "ok" and (any(s.outcome == UNKNOWN for s in run.spans.values()) or run.loops()):
        run.status = "attention"
    return run


def _finalize_span(span: Span, run_finished: bool) -> None:
    a = span.attrs
    span.effect = classify(span.kind, span.name, a)
    if span.kind == "llm":
        span.usage = normalize_usage(a.get("usage"))
        span.cost = estimate_cost(a.get("response_model") or a.get("model"), a.get("usage"), a.get("cost_usd"),
                                  base_url=a.get("base_url"), provider=a.get("provider"))
    elif isinstance(a.get("cost_usd"), (int, float)):
        span.cost = float(a["cost_usd"])
    span.outcome, span.outcome_reason = _outcome(span, run_finished)


def _result_error(result: Any) -> Any:
    """Tools often report failure inside an 'ok' result, e.g. '{"error": "..."}'."""
    if isinstance(result, str) and result.lstrip().startswith("{"):
        try:
            result = json.loads(result)
        except ValueError:
            return None
    if isinstance(result, dict) and result.get("success") is not False and not result.get("error"):
        return None
    if isinstance(result, dict):
        return result.get("error") or "tool reported success: false"
    return None


def _outcome(span: Span, run_finished: bool) -> tuple[str, str]:
    a = span.attrs
    if isinstance(a.get("outcome"), str):  # explicit from the integration
        return a["outcome"], a.get("outcome_reason", "reported by integration")
    side_effect = bool(span.effect and span.effect.effect in ("write", "delete", "send", "execute"))
    if span.end is None:
        if not run_finished:
            return RUNNING, ""
        if side_effect:
            return UNKNOWN, "Started but never reported a result. The external side effect may have occurred."
        return INCOMPLETE, "Never reported a result (the agent may have exited first)."
    status = span.status or "ok"
    err = span.error or (_result_error(a.get("result")) if status == "ok" else None)
    detail = _result_detail(a.get("result"))
    if status == "blocked" or detail.get("blocked"):
        return BLOCKED, str(_result_error(a.get("result")) or err or "Blocked before execution.")
    if status in ("ok",) and not err:
        return SUCCEEDED, ""
    if detail.get("meaning"):
        err = f"{err}: {detail['meaning']}" if err else detail["meaning"]
    command = str((a.get("args") or {}).get("command") or "") if isinstance(a.get("args"), dict) else ""
    if (status in ("timeout", "cancelled", "interrupted")
            or is_ambiguous_error(err, a.get("error_type"), a.get("status_code"))
            or is_ambiguous_exit(command, detail.get("exit_code"))):
        if side_effect:
            return UNKNOWN, f"{status if status != 'error' else 'Error'}: {err or 'no response'}. External side effect may have occurred."
        return FAILED, str(err or status)
    return FAILED, str(err or status)


def _result_detail(result: Any) -> dict[str, Any]:
    """Exit code and its meaning from a structured tool result (e.g. Hermes' terminal tool)."""
    if isinstance(result, str) and result.lstrip().startswith("{"):
        try:
            result = json.loads(result)
        except ValueError:
            return {}
    if not isinstance(result, dict):
        return {}
    err = str(result.get("error") or "")
    return {"exit_code": result.get("exit_code", result.get("returncode")),
            "meaning": result.get("exit_code_meaning") or result.get("stderr_summary"),
            # Hermes' guardrails refuse before executing: '{"exit_code": -1, "error": "BLOCKED: ..."}'
            "blocked": err.startswith("BLOCKED") or result.get("status") == "blocked"}


# --- views across runs ----------------------------------------------------------------

def build_runs(events_by_run: dict[str, list[dict[str, Any]]], *, now: float | None = None) -> list[Run]:
    runs = [build_run(rid, evs, now=now) for rid, evs in events_by_run.items() if evs]
    runs.sort(key=lambda r: r.start or 0, reverse=True)
    return runs


def change_feed(runs: list[Run], since: float, until: float | None = None) -> dict[str, Any]:
    """Every step that changed (or may have changed) the outside world."""
    items = []
    for run in runs:
        for s in run.spans.values():
            if not (s.effect and s.effect.changes_world) or s.point:
                continue
            if s.start is None or s.start < since or (until is not None and s.start > until):
                continue
            items.append((run, s))
    items.sort(key=lambda rs: rs[1].start or 0, reverse=True)

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for run, s in items:
        e = s.effect
        key = (e.system, e.label())
        g = groups.setdefault(key, {"system": e.system, "object": e.object, "verb": e.verb, "effect": e.effect,
                                    SUCCEEDED: 0, FAILED: 0, UNKNOWN: 0, BLOCKED: 0, RUNNING: 0})
        g[s.outcome] = g.get(s.outcome, 0) + 1
    group_list = sorted(groups.values(), key=lambda g: -(g[SUCCEEDED] + g[UNKNOWN] + g[FAILED]))
    for g in group_list:
        g["label"] = _count_label(g[SUCCEEDED], g["object"], g["verb"], g["system"])

    totals = defaultdict(int)
    for _, s in items:
        totals[s.outcome] += 1
    return {
        "since": since, "until": until,
        "totals": {"changes": totals[SUCCEEDED], "failed": totals[FAILED], "unknown": totals[UNKNOWN],
                   "blocked": totals[BLOCKED], "running": totals[RUNNING]},
        "groups": group_list,
        "items": [{
            "run_id": run.run_id, "run_name": run.name or "(untitled run)", "agent": run.agent,
            **s.as_dict(children=False),
        } for run, s in items],
    }


_PLURAL = {"pull request": "pull requests", "repository": "repositories", "http request": "HTTP requests",
           "http resource": "HTTP resources", "memory": "memory entries", "dependency": "dependencies"}
_SYSTEM_NAMES = {"github": "GitHub", "gitlab": "GitLab", "hubspot": "HubSpot", "payments": "payment", "drive": "Drive",
                 "infrastructure": "infra", "database": "database", "registry": "registry", "git": "git"}


def _count_label(n: int, obj: str | None, verb: str | None, system: str) -> str:
    obj = obj or "action"
    noun = obj if n == 1 else _PLURAL.get(obj, obj + "s")
    if obj == "event" and system == "calendar":
        noun = "calendar " + noun
    elif system not in ("email", "filesystem", "calendar", "shell", "tool", "web", "memory") and system not in obj and obj not in system:
        noun = f"{_SYSTEM_NAMES.get(system, system.capitalize())} {noun}"
    return f"{n} {noun} {verb or 'changed'}"


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return round(v[min(len(v) - 1, int(q * (len(v) - 1) + 0.5))], 1)


BREAKDOWN_DIMENSIONS = ("agent", "model", "tool", "mcp", "workflow", "user")


def breakdown(runs: list[Run], by: str, since: float | None = None) -> list[dict[str, Any]]:
    """Spend and latency grouped by one dimension."""
    rows: dict[str, dict[str, Any]] = {}
    durations: dict[str, list[float]] = defaultdict(list)

    def add(key: str | None, span: Span) -> None:
        key = key or "(none)"
        r = rows.setdefault(key, {"key": key, "calls": 0, "errors": 0, "cost": 0.0, "input_tokens": 0,
                                  "output_tokens": 0, "runs": set()})
        r["calls"] += 1
        r["errors"] += span.outcome in (FAILED, UNKNOWN)
        r["cost"] += span.cost or 0.0
        r["input_tokens"] += span.usage.get("input", 0) + span.usage.get("cache_read", 0) + span.usage.get("cache_write", 0)
        r["output_tokens"] += span.usage.get("output", 0)
        r["runs"].add(span.run_id)
        if span.duration_ms is not None:
            durations[key].append(span.duration_ms)

    for run in runs:
        for s in run.spans.values():
            if s.point or (since is not None and (s.start or 0) < since):
                continue
            if by == "model":
                if s.kind == "llm":
                    add(s.attrs.get("response_model") or s.attrs.get("model"), s)
            elif by == "tool":
                if s.kind != "llm":
                    add(s.name, s)
            elif by == "mcp":
                if s.kind == "mcp" or s.attrs.get("mcp_server"):
                    add(s.attrs.get("mcp_server") or s.name.split("__")[1] if "__" in s.name else s.attrs.get("mcp_server"), s)
            elif by == "agent":
                add(run.agent, s)
            elif by in ("workflow", "user"):
                add(run.attrs.get(by), s)
            else:
                raise ValueError(f"unknown dimension {by!r}")

    out = []
    for key, r in rows.items():
        d = durations[key]
        out.append({**r, "runs": len(r["runs"]), "cost": round(r["cost"], 6),
                    "p50_ms": _pct(d, 0.5), "p95_ms": _pct(d, 0.95), "total_ms": round(sum(d), 1)})
    out.sort(key=lambda r: (-r["cost"], -r["total_ms"]))
    return out
