import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from aops.sdk import Client, scrub


def test_scrub_redacts_and_bounds():
    out = scrub({"api_key": "x", "body": "use sk-ant-abcdefghijklmnopqrstuvwxyz", "n": 3, "big": "a" * 40_000})
    assert out["api_key"] == "[REDACTED]"
    assert "sk-ant" not in out["body"] and out["n"] == 3
    assert len(out["big"]) < 33_000
    assert scrub("prompt text", capture_content=False) == "<11 chars>"


def test_spools_when_collector_down_then_resends(tmp_path):
    received = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            received.extend(json.loads(self.rfile.read(int(self.headers["content-length"])))["events"])
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), H)
    port = server.server_address[1]
    server.server_close()  # collector "down"

    c = Client(source="t", endpoint=f"http://127.0.0.1:{port}", spool_dir=tmp_path, flush_interval=0.05)
    with c.run("hello") as run:
        with run.span("tool", "write_file", args={"path": "x"}) as s:
            s.set(result="ok")
    assert c.flush(5)
    spooled = [json.loads(l) for f in tmp_path.glob("*.jsonl") for l in f.read_text().splitlines()]
    assert [e["type"] for e in spooled] == ["run.start", "span.start", "span.end", "run.end"]

    server = HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        c.run_start("r2", "second")
        assert c.flush(5)
        assert {e["type"] for e in received} >= {"run.start", "span.start", "span.end", "run.end"}
        assert len(received) == 5 and not list(tmp_path.glob("*.jsonl"))
    finally:
        server.shutdown()
        c.close()


def test_span_exception_marks_error_and_reraises(tmp_path):
    c = Client(source="t", endpoint="http://127.0.0.1:9", spool_dir=tmp_path, flush_interval=0.05)
    try:
        with c.run("x") as run, run.span("tool", "send_email"):
            raise TimeoutError("slow")
    except TimeoutError:
        pass
    c.flush(5)
    events = [json.loads(l) for f in tmp_path.glob("*.jsonl") for l in f.read_text().splitlines()]
    assert [e["attrs"].get("status") for e in events if e["type"].endswith(".end")] == ["timeout", "error"]
    c.close()


def test_event_names_are_scrubbed(tmp_path):
    c = Client(source="t", endpoint="http://127.0.0.1:9", spool_dir=tmp_path, flush_interval=0.05)
    c.run_start("r", "echo sk-ant-api03-FAKEKEYFAKEKEYFAKEKEY")
    c.flush(5)
    c.close()
    events = [json.loads(l) for f in tmp_path.glob("*.jsonl") for l in f.read_text().splitlines()]
    assert events[0]["name"] == "echo [REDACTED]"
