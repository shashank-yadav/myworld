"""Append-only event log on SQLite.

UPDATE and DELETE are rejected by triggers: the log is an audit trail, and everything
else (runs, traces, change feeds) is derived from it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .schema import InvalidEvent, normalize

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    id             TEXT NOT NULL UNIQUE,
    ts             REAL NOT NULL,
    received_at    REAL NOT NULL,
    type           TEXT NOT NULL,
    run_id         TEXT NOT NULL,
    span_id        TEXT,
    parent_span_id TEXT,
    kind           TEXT NOT NULL,
    name           TEXT NOT NULL,
    source         TEXT NOT NULL,
    attrs          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id, seq);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""


def default_home() -> Path:
    return Path(os.environ.get("AOPS_HOME") or Path.home() / ".aops")


class EventStore:
    def __init__(self, path: str | Path | None = None):
        if path is None:
            path = default_home() / "events.db"
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)

    def append(self, events: Iterable[dict[str, Any]], *, source: str | None = None) -> tuple[int, list[str]]:
        """Validate and append events. Returns (accepted, errors). Duplicate ids are skipped."""
        rows, errors = [], []
        now = time.time()
        for i, raw in enumerate(events):
            try:
                e = normalize(raw, source=source)
            except InvalidEvent as exc:
                errors.append(f"event {i}: {exc}")
                continue
            rows.append((
                e["id"], e["ts"], now, e["type"], e["run_id"], e["span_id"], e["parent_span_id"],
                e["kind"], e["name"], e["source"], json.dumps(e["attrs"], default=str),
            ))
        with self._lock, self._db:
            cur = self._db.executemany(
                "INSERT OR IGNORE INTO events (id, ts, received_at, type, run_id, span_id, parent_span_id,"
                " kind, name, source, attrs) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            accepted = cur.rowcount if cur.rowcount is not None else len(rows)
        return accepted, errors

    def run_events(self, run_ids: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
        ids = list(dict.fromkeys(run_ids))
        out: dict[str, list[dict[str, Any]]] = {r: [] for r in ids}
        for chunk in (ids[i:i + 500] for i in range(0, len(ids), 500)):
            marks = ",".join("?" * len(chunk))
            with self._lock:
                rows = self._db.execute(
                    f"SELECT * FROM events WHERE run_id IN ({marks}) ORDER BY seq", chunk
                ).fetchall()
            for r in rows:
                out[r["run_id"]].append(_row(r))
        return out

    def recent_run_ids(self, *, since: float | None = None, until: float | None = None,
                       limit: int = 200) -> list[str]:
        """Run ids with any activity in [since, until], most recently active first."""
        where, args = [], []
        if since is not None:
            where.append("ts >= ?")
            args.append(since)
        if until is not None:
            where.append("ts <= ?")
            args.append(until)
        sql = "SELECT run_id, MAX(ts) AS last FROM events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " GROUP BY run_id ORDER BY last DESC LIMIT ?"
        with self._lock:
            return [r["run_id"] for r in self._db.execute(sql, [*args, limit]).fetchall()]

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def close(self) -> None:
        self._db.close()


def _row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "seq": r["seq"], "id": r["id"], "ts": r["ts"], "type": r["type"], "run_id": r["run_id"],
        "span_id": r["span_id"], "parent_span_id": r["parent_span_id"], "kind": r["kind"],
        "name": r["name"], "source": r["source"], "attrs": json.loads(r["attrs"]),
    }
