import sqlite3

import pytest
from fastapi.testclient import TestClient

from aops.server import create_app, drain_spool
from aops.store import EventStore


def e(i, **kw):
    return {"id": f"e{i}", "type": "span.start", "run_id": "r1", "span_id": f"s{i}", "kind": "tool", "name": "x", **kw}


def test_append_only_and_idempotent(tmp_path):
    store = EventStore(tmp_path / "ev.db")
    assert store.append([e(1), e(2)]) == (2, [])
    assert store.append([e(1)])[0] == 0  # duplicate id ignored
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._db.execute("UPDATE events SET name = 'y'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._db.execute("DELETE FROM events")


def test_invalid_events_rejected_individually(tmp_path):
    store = EventStore(tmp_path / "ev.db")
    accepted, errors = store.append([e(1), {"type": "nope", "run_id": "r"}, {"type": "span.end", "run_id": "r"}])
    assert accepted == 1 and len(errors) == 2


def test_millisecond_timestamps_normalized(tmp_path):
    store = EventStore(tmp_path / "ev.db")
    store.append([e(1, ts=1_727_000_000_123)])
    assert store.run_events(["r1"])["r1"][0]["ts"] == pytest.approx(1_727_000_000.123)


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(EventStore(tmp_path / "ev.db")))


def test_api_roundtrip(client):
    events = [
        {"type": "run.start", "run_id": "r1", "name": "Send update", "ts": 100.0},
        {"type": "span.start", "run_id": "r1", "span_id": "t1", "kind": "mcp", "name": "gmail_send_email", "ts": 101.0},
        {"type": "span.end", "run_id": "r1", "span_id": "t1", "ts": 131.0, "attrs": {"status": "timeout"}},
        {"type": "run.end", "run_id": "r1", "ts": 132.0, "attrs": {"status": "ok"}},
    ]
    r = client.post("/v1/events", json={"events": events})
    assert r.json()["accepted"] == 4

    runs = client.get("/api/runs").json()["runs"]
    assert runs[0]["name"] == "Send update" and runs[0]["summary"]["unknown"] == 1

    detail = client.get("/api/runs/r1").json()
    assert detail["roots"][0]["outcome"] == "unknown"

    changes = client.get("/api/changes?since=0").json()
    assert changes["totals"]["unknown"] == 1 and changes["items"][0]["name"] == "gmail_send_email"

    assert client.get("/api/breakdown?by=tool&since=0").json()["rows"][0]["key"] == "gmail_send_email"
    assert client.get("/api/breakdown?by=nope").status_code == 400
    assert client.get("/api/runs/missing").status_code == 404
    assert "Agent Operations" in client.get("/").text


def test_drain_spool(tmp_path):
    store = EventStore(tmp_path / "ev.db")
    spool = tmp_path / "spool"
    spool.mkdir()
    (spool / "123.jsonl").write_text('{"id":"a","type":"run.start","run_id":"r"}\nnot json\n')
    assert drain_spool(store, spool) == 1
    assert not list(spool.iterdir())
