"""Local collector: ingests events and serves the dashboard + query API.

Binds to 127.0.0.1 by default. Nothing leaves the machine.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from .sdk import read_jsonl
from .store import EventStore, default_home
from .trace import BREAKDOWN_DIMENSIONS, breakdown, build_run, build_runs, change_feed

DASHBOARD = Path(__file__).parent / "dashboard" / "index.html"


def start_of_today() -> float:
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def drain_spool(store: EventStore, spool_dir: Path | None = None) -> int:
    """Ingest events that integrations spooled to disk while the collector was down."""
    spool_dir = spool_dir or default_home() / "spool"
    n = 0
    for path in sorted(spool_dir.glob("*.jsonl")) if spool_dir.exists() else []:
        claimed = path.with_suffix(".draining")
        try:
            path.rename(claimed)
        except OSError:
            continue
        n += store.append(read_jsonl(claimed))[0]
        claimed.unlink(missing_ok=True)
    return n


def create_app(store: EventStore | None = None) -> FastAPI:
    store = store or EventStore()
    app = FastAPI(title="Agent Operations", version="0.1.0")
    app.state.store = store

    def load_runs(since: float | None, until: float | None = None, limit: int = 500):
        ids = store.recent_run_ids(since=since, until=until, limit=limit)
        return build_runs(store.run_events(ids))

    @app.post("/v1/events")
    def ingest(payload: Any = Body(...)) -> dict[str, Any]:
        events = payload.get("events") if isinstance(payload, dict) else payload
        if not isinstance(events, list):
            raise HTTPException(400, "expected {'events': [...]} or a JSON array")
        accepted, errors = store.append(events)
        return {"accepted": accepted, "rejected": len(errors), "errors": errors[:20]}

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "events": store.count(), "db": store.path}

    @app.get("/api/runs")
    def runs(since: float | None = None, limit: int = Query(100, le=1000), q: str | None = None,
             status: str | None = None) -> dict[str, Any]:
        rs = load_runs(since, limit=limit)
        out = []
        for r in rs:
            d = r.as_dict()
            if q and q.lower() not in f"{d['name']} {d['agent']} {d['run_id']}".lower():
                continue
            if status == "problems" and not (d["summary"]["failures"] or d["summary"]["unknown"]):
                continue
            out.append(d)
        return {"runs": out}

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str) -> dict[str, Any]:
        events = store.run_events([run_id])[run_id]
        if not events:
            raise HTTPException(404, "run not found")
        run = build_run(run_id, events)
        d = run.as_dict(tree=True)
        # child runs spawned by subagents in this run
        window = store.recent_run_ids(since=(run.start or 0) - 1, limit=500)
        children = [c for c in build_runs(store.run_events([i for i in window if i != run_id]))
                    if c.attrs.get("parent_run_id") == run_id]
        d["child_runs"] = [c.as_dict() for c in children]
        return d

    @app.get("/api/runs/{run_id}/events")
    def run_raw_events(run_id: str) -> dict[str, Any]:
        return {"events": store.run_events([run_id])[run_id]}

    @app.get("/api/changes")
    def changes(since: float | None = None, until: float | None = None) -> dict[str, Any]:
        since = start_of_today() if since is None else since
        return change_feed(load_runs(since, until, limit=5000), since, until)

    @app.get("/api/breakdown")
    def get_breakdown(by: str = "model", since: float | None = None) -> dict[str, Any]:
        if by not in BREAKDOWN_DIMENSIONS:
            raise HTTPException(400, f"by must be one of {', '.join(BREAKDOWN_DIMENSIONS)}")
        since = start_of_today() if since is None else since
        rows = breakdown(load_runs(since, limit=5000), by, since)
        return {"by": by, "since": since, "rows": rows}

    @app.get("/api/overview")
    def overview(since: float | None = None) -> dict[str, Any]:
        since = start_of_today() if since is None else since
        rs = load_runs(since, limit=5000)
        summaries = [r.summary() for r in rs]
        feed = change_feed(rs, since)
        return {
            "since": since, "now": time.time(), "runs": len(rs),
            "running": sum(r.status == "running" for r in rs),
            "cost": round(sum(s["cost"] for s in summaries), 4),
            "calls": sum(s["calls"] for s in summaries),
            "failures": sum(s["failures"] for s in summaries),
            "unknown": sum(s["unknown"] for s in summaries),
            "changes": feed["totals"],
        }

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(DASHBOARD, headers={"cache-control": "no-store"})

    return app
