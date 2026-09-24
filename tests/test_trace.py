import time

from aops.demo import build
from aops.store import EventStore
from aops.trace import STALE_AFTER_S, breakdown, build_run, build_runs, change_feed

NOW = 1_800_000_000.0


def ev(type, run="r1", span=None, parent=None, kind="tool", name="", ts=NOW, **attrs):
    return {"id": f"{type}-{span}-{ts}", "ts": ts, "type": type, "run_id": run, "span_id": span,
            "parent_span_id": parent, "kind": kind, "name": name, "source": "test", "attrs": attrs}


def tool_run(end_attrs=None, *, name="gmail_send_email", ended_run=True, now=NOW + 10):
    events = [ev("run.start", kind="agent", name="Send update"),
              ev("span.start", span="t1", name=name, ts=NOW + 1, args={"to": "a@b.c"})]
    if end_attrs is not None:
        events.append(ev("span.end", span="t1", ts=NOW + 2, **end_attrs))
    if ended_run:
        events.append(ev("run.end", kind="agent", ts=NOW + 3, status="ok"))
    return build_run("r1", events, now=now)


def test_success():
    run = tool_run({"status": "ok", "result": '{"id": "m1"}'})
    s = run.spans["t1"]
    assert s.outcome == "succeeded" and s.duration_ms == 1000.0
    assert run.summary()["changes"] == 1 and run.status == "ok"


def test_timeout_on_side_effect_is_unknown_not_failed():
    run = tool_run({"status": "timeout", "error": "ReadTimeout"})
    assert run.spans["t1"].outcome == "unknown"
    assert "may have occurred" in run.spans["t1"].outcome_reason
    assert run.status == "attention"
    assert run.summary()["unknown"] == 1 and run.summary()["failures"] == 0


def test_ambiguous_error_text_is_unknown():
    assert tool_run({"status": "error", "error": "502 Bad Gateway"}).spans["t1"].outcome == "unknown"


def test_definite_error_is_failed():
    assert tool_run({"status": "error", "error": "403 Forbidden"}).spans["t1"].outcome == "failed"


def test_timeout_on_read_is_failed():
    assert tool_run({"status": "timeout"}, name="gmail_search").spans["t1"].outcome == "failed"


def test_error_inside_ok_result():
    run = tool_run({"status": "ok", "result": '{"error": "invalid recipient"}'})
    assert run.spans["t1"].outcome == "failed"


def test_blocked_never_executed():
    assert tool_run({"status": "blocked"}).spans["t1"].outcome == "blocked"


def test_missing_end_in_finished_run_is_unknown():
    run = tool_run(None)
    assert run.spans["t1"].outcome == "unknown"


def test_missing_end_in_active_run_is_running():
    run = tool_run(None, ended_run=False, now=NOW + 5)
    assert run.spans["t1"].outcome == "running" and run.status == "running"


def test_stale_run_is_abandoned():
    run = tool_run(None, ended_run=False, now=NOW + STALE_AFTER_S + 60)
    assert run.status == "abandoned" and run.spans["t1"].outcome == "unknown"


def test_tree_and_cost():
    events = [
        ev("run.start", kind="agent", name="x"),
        ev("span.start", span="llm:1", kind="llm", name="claude-opus-5", model="claude-opus-5"),
        ev("span.end", span="llm:1", kind="llm", ts=NOW + 1, usage={"input_tokens": 1_000_000, "output_tokens": 100_000}),
        ev("span.start", span="t1", parent="llm:1", ts=NOW + 1.1, name="read_file"),
        ev("span.end", span="t1", ts=NOW + 1.2),
    ]
    run = build_run("r1", events, now=NOW + 2)
    assert [r.span_id for r in run.roots] == ["llm:1"]
    assert [c.span_id for c in run.roots[0].children] == ["t1"]
    assert abs(run.spans["llm:1"].cost - (5.0 + 2.5)) < 1e-9  # opus 5: $5/M in, $25/M out


def test_end_before_start_out_of_order():
    events = [ev("span.end", span="t1", ts=NOW + 2, duration_ms=500, status="ok"),
              ev("span.start", span="t1", ts=NOW + 1.5, name="write_file")]
    s = build_run("r1", events, now=NOW + 3).spans["t1"]
    assert s.name == "write_file" and s.outcome == "succeeded"


def test_demo_book_meeting_matches_the_pitch():
    store = EventStore(":memory:")
    store.append(build())
    runs = build_runs(store.run_events(store.recent_run_ids()))
    book = next(r for r in runs if r.name == "Book meeting with John").summary()
    assert (book["calls"], book["changes"], book["failures"]) == (18, 2, 1)
    assert 0.25 < book["cost"] < 0.35

    feed = change_feed(runs, time.time() - 2 * 86400)
    assert feed["totals"]["unknown"] == 2  # timed-out email + abandoned CRM update
    labels = {g["label"] for g in feed["groups"]}
    assert "1 calendar event created" in labels and "1 GitHub issue created" in labels

    rows = breakdown(runs, "model")
    assert {r["key"] for r in rows} >= {"claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"}
    assert all(r["p50_ms"] is not None for r in rows)


def test_repeated_failures_flagged_as_loop():
    events = [ev("run.start", kind="agent", name="x")]
    for i in range(4):
        events += [ev("span.start", span=f"t{i}", name="tool_call", ts=NOW + i),
                   ev("span.end", span=f"t{i}", ts=NOW + i + 0.1, status="error", error="tool_call requires 'calls'")]
    events.append(ev("run.end", kind="agent", ts=NOW + 10, status="ok"))
    run = build_run("r1", events, now=NOW + 11)
    loops = run.summary()["loops"]
    assert loops == [{"name": "tool_call", "count": 4, "span_ids": ["t0", "t1", "t2", "t3"],
                      "error": "tool_call requires 'calls'"}]
    assert run.status == "attention"


def test_hermes_usage_shape_counts_cache():
    from aops.pricing import normalize_usage
    u = normalize_usage({"input_tokens": 267, "output_tokens": 1226, "cache_read_tokens": 4886, "cache_write_tokens": 0,
                         "prompt_tokens": 5153, "total_tokens": 6379})
    assert u == {"input": 267, "output": 1226, "cache_read": 4886, "cache_write": 0}


def test_read_without_result_is_incomplete_not_failed():
    run = tool_run(None, name="read_file")
    assert run.spans["t1"].outcome == "incomplete" and run.summary()["failures"] == 0


def test_local_models_cost_nothing():
    from aops.pricing import estimate_cost
    usage = {"input_tokens": 1000, "output_tokens": 100}
    assert estimate_cost("qwen3:4b-instruct", usage, base_url="http://127.0.0.1:11434/v1") == 0.0
    assert estimate_cost("qwen3:4b-instruct", usage, provider="ollama") == 0.0
    assert estimate_cost("qwen3:4b-instruct", usage, base_url="https://openrouter.ai/api/v1") is None


def test_curl_timeout_after_send_is_unknown():
    """Live Hermes case: the server got the invoice, then curl hit its -m 5 limit (exit 28)."""
    cmd = "curl -s -m 5 -X POST http://127.0.0.1:8765/invoices -d 'customer=acme&amount=4900'"
    events = [ev("span.start", span="t1", kind="terminal", name="terminal", args={"command": cmd}),
              ev("span.end", span="t1", ts=NOW + 5, status="error", error="exit 28", error_type="tool_error",
                 result='{"output": "", "exit_code": 28, "error": null, "exit_code_meaning": "Operation timed out"}'),
              ev("run.end", kind="agent", ts=NOW + 6, status="ok")]
    s = build_run("r1", events, now=NOW + 7).spans["t1"]
    assert (s.effect.effect, s.outcome) == ("send", "unknown")
    assert "Operation timed out" in s.outcome_reason


def test_curl_connection_refused_is_failed():
    cmd = "curl -X POST http://127.0.0.1:1/x -d a"
    events = [ev("span.start", span="t1", kind="terminal", name="terminal", args={"command": cmd}),
              ev("span.end", span="t1", ts=NOW + 1, status="error", error="exit 7",
                 result='{"exit_code": 7, "exit_code_meaning": "Failed to connect to host"}'),
              ev("run.end", kind="agent", ts=NOW + 2)]
    assert build_run("r1", events, now=NOW + 3).spans["t1"].outcome == "failed"


def test_hermes_guardrail_block_is_blocked_not_failed():
    result = ('{"output": "", "exit_code": -1, "error": "BLOCKED: Command flagged as dangerous (recursive delete) '
              'but single-query mode (-q) runs without a user present"}')
    events = [ev("span.start", span="t1", kind="terminal", name="terminal", args={"command": "rm -rf victim_dir"}),
              ev("span.end", span="t1", ts=NOW + 1, status="error", error="exit -1", result=result),
              ev("run.end", kind="agent", ts=NOW + 2)]
    s = build_run("r1", events, now=NOW + 3).spans["t1"]
    assert s.outcome == "blocked" and s.outcome_reason.startswith("BLOCKED")
