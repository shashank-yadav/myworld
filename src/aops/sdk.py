"""Stdlib-only client that ships events to the local collector.

Designed to live inside someone else's agent process, so it:
  * never raises into the host (every public method swallows its own errors),
  * never blocks the agent loop (events are queued and posted by a daemon thread),
  * never loses events when the collector is down (they spool to disk and are resent).

Use it directly::

    from aops.sdk import Client
    aops = Client(source="my-agent")
    with aops.run("Book meeting with John", agent="assistant") as run:
        with run.span("tool", "calendar_create_event", args={...}) as step:
            step.set(result=create_event(...))
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import re
import threading
import time
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DEFAULT_ENDPOINT = "http://127.0.0.1:4319"
MAX_FIELD_CHARS = 32_000

_SECRETS = re.compile(
    r"(sk-(?:ant-|proj-)?[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[abposr]-[A-Za-z0-9\-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{30,}|"
    r"(?i:bearer)\s+[A-Za-z0-9._\-]{20,}|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})"
)
_SECRET_KEYS = re.compile(r"(?i)^(api[_-]?key|token|secret|password|passwd|authorization|cookie|access[_-]?token|refresh[_-]?token|client[_-]?secret)$")


def scrub(value: Any, *, capture_content: bool = True, _depth: int = 0) -> Any:
    """Redact obvious secrets and bound payload size before anything leaves the process."""
    if _depth > 12:
        return "…"
    if isinstance(value, str):
        if not capture_content:
            return f"<{len(value)} chars>"
        v = _SECRETS.sub("[REDACTED]", value)
        return v if len(v) <= MAX_FIELD_CHARS else v[:MAX_FIELD_CHARS] + f"… [+{len(v) - MAX_FIELD_CHARS} chars]"
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if _SECRET_KEYS.match(str(k)) else scrub(v, capture_content=capture_content, _depth=_depth + 1)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        items = [scrub(v, capture_content=capture_content, _depth=_depth + 1) for v in list(value)[:500]]
        return items + ([f"… [+{len(value) - 500} items]"] if len(value) > 500 else [])
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return scrub(str(value), capture_content=capture_content, _depth=_depth + 1)


# Fields that carry prompt/tool content. With AOPS_CAPTURE_CONTENT=0 they are reduced to sizes.
CONTENT_FIELDS = {"args", "result", "request", "response", "user_message", "assistant_response",
                  "assistant_message", "prompt", "output", "input", "messages", "child_goal", "child_summary"}


def _home() -> Path:
    return Path(os.environ.get("AOPS_HOME") or Path.home() / ".aops")


class Client:
    def __init__(self, *, source: str = "sdk", endpoint: str | None = None, spool_dir: str | Path | None = None,
                 flush_interval: float = 0.5, max_batch: int = 200, capture_content: bool | None = None,
                 enabled: bool | None = None):
        self.source = source
        self.endpoint = (endpoint or os.environ.get("AOPS_ENDPOINT") or DEFAULT_ENDPOINT).rstrip("/")
        self.spool_dir = Path(spool_dir) if spool_dir else _home() / "spool"
        self.flush_interval = flush_interval
        self.max_batch = max_batch
        self.capture_content = (os.environ.get("AOPS_CAPTURE_CONTENT", "1") != "0") if capture_content is None else capture_content
        self.enabled = (os.environ.get("AOPS_DISABLED", "0") != "1") if enabled is None else enabled
        self._q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=100_000)
        self._stop = threading.Event()
        self._flushed = threading.Condition()
        self._pending = 0
        self._thread: threading.Thread | None = None
        if self.enabled:
            self._thread = threading.Thread(target=self._loop, name="aops-shipper", daemon=True)
            self._thread.start()
            atexit.register(self.close)

    # -- low level ---------------------------------------------------------------------

    def emit(self, type: str, run_id: str, *, span_id: str | None = None, parent_span_id: str | None = None,
             kind: str | None = None, name: str = "", ts: float | None = None, **attrs: Any) -> None:
        if not self.enabled:
            return
        try:
            clean = {k: (scrub(v, capture_content=self.capture_content) if k in CONTENT_FIELDS else scrub(v))
                     for k, v in attrs.items() if v is not None}
            event = {"id": str(uuid.uuid4()), "ts": ts or time.time(), "type": type, "run_id": run_id,
                     "span_id": span_id, "parent_span_id": parent_span_id, "kind": kind, "name": scrub(name),
                     "source": self.source, "attrs": clean}
            with self._flushed:
                self._pending += 1
            self._q.put_nowait(event)
        except Exception:
            pass

    def run_start(self, run_id: str, name: str = "", **attrs: Any) -> None:
        self.emit("run.start", run_id, kind="agent", name=name, **attrs)

    def run_end(self, run_id: str, status: str = "ok", **attrs: Any) -> None:
        self.emit("run.end", run_id, kind="agent", status=status, **attrs)

    def span_start(self, run_id: str, span_id: str, kind: str, name: str, parent_span_id: str | None = None, **attrs: Any) -> None:
        self.emit("span.start", run_id, span_id=span_id, parent_span_id=parent_span_id, kind=kind, name=name, **attrs)

    def span_end(self, run_id: str, span_id: str, status: str = "ok", kind: str | None = None, name: str = "", **attrs: Any) -> None:
        self.emit("span.end", run_id, span_id=span_id, kind=kind, name=name, status=status, **attrs)

    def point(self, run_id: str, kind: str, name: str, parent_span_id: str | None = None, **attrs: Any) -> None:
        self.emit("span.event", run_id, span_id=f"evt:{uuid.uuid4().hex[:12]}", parent_span_id=parent_span_id,
                  kind=kind, name=name, **attrs)

    # -- high level --------------------------------------------------------------------

    @contextmanager
    def run(self, name: str, run_id: str | None = None, **attrs: Any) -> Iterator[RunHandle]:
        handle = RunHandle(self, run_id or f"run-{uuid.uuid4().hex[:16]}")
        self.run_start(handle.run_id, name, **attrs)
        try:
            yield handle
        except BaseException as exc:
            self.run_end(handle.run_id, "error", error=f"{type(exc).__name__}: {exc}")
            raise
        self.run_end(handle.run_id, "ok", **handle.end_attrs)

    # -- shipping ----------------------------------------------------------------------

    def flush(self, timeout: float = 5.0) -> bool:
        """Block until queued events are posted or spooled. Returns False on timeout."""
        if not self.enabled:
            return True
        deadline = time.time() + timeout
        with self._flushed:
            while self._pending > 0:
                left = deadline - time.time()
                if left <= 0:
                    return False
                self._flushed.wait(left)
        return True

    def close(self) -> None:
        if self._thread and not self._stop.is_set():
            self.flush(timeout=3.0)
            self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            batch = self._take()
            if not batch:
                continue
            ok = self._post(batch)
            if ok:
                self._resend_spool()
            else:
                self._spool(batch)
            with self._flushed:
                self._pending -= len(batch)
                self._flushed.notify_all()

    def _take(self) -> list[dict[str, Any]]:
        try:
            first = self._q.get(timeout=self.flush_interval)
        except queue.Empty:
            return []
        batch = [first]
        while len(batch) < self.max_batch:
            try:
                batch.append(self._q.get_nowait())
            except queue.Empty:
                break
        return batch

    def _post(self, batch: list[dict[str, Any]]) -> bool:
        try:
            body = json.dumps({"events": batch}, default=str).encode()
            req = urllib.request.Request(f"{self.endpoint}/v1/events", data=body, method="POST",
                                         headers={"content-type": "application/json"})
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                return 200 <= resp.status < 300
        except Exception:
            return False

    def _spool(self, batch: list[dict[str, Any]]) -> None:
        try:
            self.spool_dir.mkdir(parents=True, exist_ok=True)
            with open(self.spool_dir / f"{os.getpid()}.jsonl", "a") as f:
                for e in batch:
                    f.write(json.dumps(e, default=str) + "\n")
        except Exception:
            pass

    def _resend_spool(self) -> None:
        for path in sorted(self.spool_dir.glob("*.jsonl")) if self.spool_dir.exists() else []:
            claimed = path.with_suffix(f".sending-{os.getpid()}")
            try:
                path.rename(claimed)  # atomic claim, so two processes never resend the same file
            except OSError:
                continue
            events = read_jsonl(claimed)
            if all(self._post(events[i:i + self.max_batch]) for i in range(0, len(events), self.max_batch)):
                claimed.unlink(missing_ok=True)
            else:
                claimed.rename(path)
                return


class RunHandle:
    def __init__(self, client: Client, run_id: str):
        self.client = client
        self.run_id = run_id
        self.end_attrs: dict[str, Any] = {}

    def set(self, **attrs: Any) -> None:
        self.end_attrs.update(attrs)

    @contextmanager
    def span(self, kind: str, name: str, parent: SpanHandle | None = None, **attrs: Any) -> Iterator[SpanHandle]:
        h = SpanHandle(self, f"{kind}:{uuid.uuid4().hex[:16]}")
        self.client.span_start(self.run_id, h.span_id, kind, name, parent.span_id if parent else None, **attrs)
        try:
            yield h
        except TimeoutError as exc:
            self.client.span_end(self.run_id, h.span_id, "timeout", error=str(exc) or "timeout", **h.attrs)
            raise
        except BaseException as exc:
            self.client.span_end(self.run_id, h.span_id, "error", error=f"{type(exc).__name__}: {exc}", **h.attrs)
            raise
        self.client.span_end(self.run_id, h.span_id, h.attrs.pop("status", "ok"), **h.attrs)


class SpanHandle:
    def __init__(self, run: RunHandle, span_id: str):
        self.run = run
        self.span_id = span_id
        self.attrs: dict[str, Any] = {}

    def set(self, **attrs: Any) -> None:
        self.attrs.update(attrs)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    out = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
    except OSError:
        pass
    return out
