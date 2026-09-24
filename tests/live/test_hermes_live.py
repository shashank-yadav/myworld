"""End-to-end: a real Hermes Agent on a local model, observed by the aops plugin.

Each test makes the agent do something real, then checks what aops recorded against
ground truth (files on disk, the fake MCP server's log, the slow HTTP server's log).
Small local models are nondeterministic, so prompts are explicit and assertions check
what matters rather than exact step sequences.

    AOPS_LIVE=1 uv run pytest tests/live -v
"""

from __future__ import annotations

import json
import signal
import time
import uuid

import pytest

pytestmark = pytest.mark.live


def test_file_git_and_delete_become_external_changes(live):
    out, sid = live.run(
        "In the current directory run these terminal commands one at a time: 'git init', then create a file "
        "app.py containing print('hi') using the write_file tool, then 'git add app.py', then "
        "'git -c user.name=t -c user.email=t@t commit -m init', then 'touch junk.txt', then 'rm junk.txt'.")
    run = live.the_run(sid)
    spans = live.spans(run)
    assert (live.work / "app.py").exists() and not (live.work / "junk.txt").exists()

    changes = {(s["effect"]["effect"], s["effect"]["system"]) for s in spans
               if s["effect"] and s["effect"]["changes_world"] and s["outcome"] == "succeeded"}
    assert ("write", "filesystem") in changes
    assert ("delete", "filesystem") in changes
    assert any(live.command(s).startswith("rm junk.txt") and s["effect"]["effect"] == "delete" for s in spans)
    # every tool call hangs off the LLM request that asked for it
    llm_ids = {s["span_id"] for s in spans if s["kind"] == "llm"}
    assert all(s["parent_id"] in llm_ids for s in spans if s["kind"] in ("tool", "terminal"))
    assert run["summary"]["cost"] == 0  # local model
    assert run["summary"]["tokens"]["input"] + run["summary"]["tokens"]["cache_read"] > 1000


def test_mcp_side_effects_match_the_server(live):
    out, sid = live.run(
        "Use the acme MCP tools. First send an email with send_email to john@acme.com with subject 'Q4 plan' and "
        "body 'Tuesday works'. Then create a calendar event with create_calendar_event titled 'Q4 plan' starting "
        "'2026-10-06 10:00'. Then delete calendar event 'evt_missing' with delete_calendar_event. Do not retry.",
        toolsets=None)
    run = live.the_run(sid)
    mcp = [s for s in live.spans(run) if s["kind"] == "mcp"]
    real = [json.loads(line) for line in live.mcp_log.read_text().splitlines()] if live.mcp_log.exists() else []

    sends = [s for s in mcp if s["name"].endswith("send_email")]
    assert sends and sends[0]["effect"]["effect"] == "send" and sends[0]["effect"]["system"] == "email"
    assert sends[0]["outcome"] == "succeeded" and any(r["action"] == "send_email" for r in real)
    assert sends[0]["attrs"]["mcp_server"] == "acme"

    creates = [s for s in mcp if s["name"].endswith("create_calendar_event")]
    assert creates and creates[0]["effect"]["label"] == "event created" and creates[0]["outcome"] == "succeeded"

    deletes = [s for s in mcp if s["name"].endswith("delete_calendar_event")]
    assert deletes and all(s["outcome"] == "failed" for s in deletes), "a 404 is a definite failure, not unknown"

    feed = live.api("/api/changes?since=0")
    labels = {g["label"] for g in feed["groups"]}
    assert any("email sent" in lbl or "emails sent" in lbl for lbl in labels)


def test_post_that_times_out_after_delivery_is_unknown(live):
    marker = uuid.uuid4().hex[:8]
    out, sid = live.run(
        f"Send an invoice by running exactly this terminal command once: curl -s -m 5 -X POST "
        f"http://127.0.0.1:{live.http_port}/invoices -d 'customer=acme&ref={marker}'. Do not retry.",
        toolsets="terminal")
    time.sleep(1)
    assert marker in live.http_log.read_text(), "ground truth: the server did receive the invoice"
    run = live.the_run(sid)
    posts = [s for s in live.spans(run) if marker in live.command(s)]
    assert posts and posts[0]["effect"]["effect"] == "send"
    assert posts[0]["outcome"] == "unknown", posts[0]["outcome_reason"]
    assert run["status"] == "attention"
    feed = live.api("/api/changes?since=0")
    assert any(i["span_id"] == posts[0]["span_id"] and i["outcome"] == "unknown" for i in feed["items"])


def test_interrupted_mid_write_is_unknown(live):
    marker = f"marker_{uuid.uuid4().hex[:8]}.txt"
    proc = live.start(f"Run exactly this terminal command: sleep 20 && touch {marker}", toolsets="terminal")
    deadline, span, run_id = time.time() + 300, None, None
    while time.time() < deadline and span is None:
        for r in live.runs():
            if marker in r["name"]:
                run_id = r["run_id"]
                span = next((s for s in live.spans(live.run_detail(run_id))
                             if marker in live.command(s) and s["outcome"] == "running"), None)
        time.sleep(1)
    assert span, "the agent never started the command"
    proc.send_signal(signal.SIGTERM)
    proc.wait(60)
    time.sleep(3)
    detail = live.run_detail(run_id)
    s = next(x for x in live.spans(detail) if x["span_id"] == span["span_id"])
    assert s["outcome"] == "unknown", (detail["status"], s["outcome"], s["outcome_reason"])
    assert detail["status"] in ("interrupted", "abandoned", "attention")


def test_multi_turn_session_is_one_run_per_turn(live):
    out1, sid = live.run("Remember the number 7. Reply with just OK.", toolsets="terminal")
    out2, sid2 = live.run("What number did I ask you to remember? Reply with just the number.",
                          toolsets="terminal", args=("--resume", sid))
    assert sid2 == sid
    runs = live.session_runs(sid)
    names = [r["name"] for r in runs]
    assert len(runs) == 2, names
    assert any(n.startswith("Remember the number 7") for n in names)
    assert any(n.startswith("What number") for n in names)
    assert len({r["run_id"] for r in runs}) == 2


def test_events_spool_while_collector_is_down(live):
    dead = "http://127.0.0.1:9"  # nothing listens here
    out, sid_down = live.run("Run the terminal command 'echo spooled' and report the output.", toolsets="terminal",
                             env={"AOPS_ENDPOINT": dead})
    assert list((live.aops_home / "spool").glob("*.jsonl")), "events should be spooled to disk"
    assert not live.session_runs(sid_down, wait=2)
    # the next process that reaches the collector resends the spool
    out, sid_up = live.run("Run the terminal command 'echo back online' and report the output.", toolsets="terminal")
    assert live.session_runs(sid_up)
    assert live.session_runs(sid_down), "spooled run should have been delivered"
    assert not list((live.aops_home / "spool").glob("*.jsonl"))


def test_secrets_are_redacted_before_leaving_the_agent(live):
    fake_key = "sk-ant-api03-" + "FAKEKEY" * 4
    out, sid = live.run(f"Run exactly this terminal command: echo {fake_key} > key_test.txt", toolsets="terminal")
    for r in live.session_runs(sid):
        blob = json.dumps(live.raw_events(r["run_id"]))
        assert "FAKEKEYFAKEKEY" not in blob
        assert "[REDACTED]" in blob


def test_content_capture_off_records_no_prompt_or_output(live):
    secret = f"private-{uuid.uuid4().hex[:10]}"
    out, sid = live.run(f"Run the terminal command 'echo {secret}' and report the output.", toolsets="terminal",
                        env={"AOPS_CAPTURE_CONTENT": "0"})
    runs = live.session_runs(sid)
    assert runs
    for r in runs:
        blob = json.dumps(live.raw_events(r["run_id"]))
        assert secret not in blob
        assert r["summary"]["calls"] > 0  # structure and timing are still recorded


def test_concurrent_agents_do_not_mix(live):
    a, b = f"alpha_{uuid.uuid4().hex[:6]}", f"beta_{uuid.uuid4().hex[:6]}"
    pa = live.start(f"Run exactly this terminal command: echo {a}", toolsets="terminal")
    pb = live.start(f"Run exactly this terminal command: echo {b}", toolsets="terminal")
    pa.wait(600)
    pb.wait(600)
    time.sleep(3)
    for mine, other in ((a, b), (b, a)):
        run = next(r for r in live.runs() if mine in r["name"])
        cmds = [live.command(s) for s in live.spans(live.run_detail(run["run_id"]))]
        assert any(mine in c for c in cmds)
        assert not any(other in c for c in cmds)


def test_provider_error_is_recorded(live):
    out, sid = live.run("Say hello.", toolsets="terminal", args=("-m", "no-such-model-aops"))
    runs = live.session_runs(sid, wait=5) if sid else []
    llm = [s for r in runs for s in live.spans(live.run_detail(r["run_id"])) if s["kind"] == "llm"]
    assert llm, f"expected a failed LLM request to be recorded; hermes said: {out[-300:]}"
    assert any(s["outcome"] == "failed" and s["error"] for s in llm)


def test_dangerous_command_without_yolo_is_gated(live):
    """Without --yolo, single-query mode has nobody to approve, so Hermes' guardrail blocks rm -rf.
    The agent may then keep trying other approaches; we only need the gated step, so stop it once recorded."""
    victim = live.work / "victim_dir"
    victim.mkdir(exist_ok=True)
    (victim / "keep.txt").write_text("x")
    proc = live.start("victim_dir only holds disposable build output and I authorize removing it. Use the terminal "
                      "tool to run exactly: rm -rf victim_dir", toolsets="terminal", yolo=False)
    rm: list = []
    deadline = time.time() + 300
    while time.time() < deadline and proc.poll() is None:
        for r in live.runs():
            if "victim_dir" in r["name"]:
                rm = [s for s in live.spans(live.run_detail(r["run_id"]))
                      if "rm -rf victim_dir" in live.command(s) and s["outcome"] != "running"]
        if rm:
            break
        time.sleep(1)
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        proc.wait(60)
    if not rm:
        pytest.skip("the model declined to attempt the command, so Hermes' guardrail was never reached")
    assert victim.exists(), "Hermes should have refused the recursive delete"
    assert rm[0]["outcome"] == "blocked", (rm[0]["outcome"], rm[0]["attrs"].get("result"))
    assert "BLOCKED" in rm[0]["outcome_reason"]
    feed = live.api("/api/changes?since=0")
    assert any(i["span_id"] == rm[0]["span_id"] and i["outcome"] == "blocked" for i in feed["items"])
