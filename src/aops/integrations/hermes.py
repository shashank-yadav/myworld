"""Hermes Agent plugin: maps Hermes observer hooks (``hermes.observer.v1``) to aops events.

Install either way:
  * pip:       ``pip install agent-operations`` into Hermes' venv (entry point ``hermes_agent.plugins``)
  * directory: ``aops install hermes`` copies a self-contained plugin to ~/.hermes/plugins/aops

then ``hermes plugins enable aops``.

Mapping
  one user turn (pre_llm_call .. post_llm_call)  -> run      (run_id = turn_id)
  pre/post_api_request, api_request_error        -> llm span (span_id = llm:<api_request_id>)
  pre/post_tool_call                             -> tool span, child of the LLM request that asked for it
  subagent_start/stop                            -> subagent span; the child session's turns become child runs
  approval request/response                      -> approval point events

Every hook is wrapped so a bug here can never break the agent.
"""

from __future__ import annotations

import functools
import threading
import uuid
from collections import defaultdict, deque
from typing import Any, Callable

try:
    from aops.sdk import Client
except ImportError:  # copied into ~/.hermes/plugins/aops as a standalone directory plugin
    from .aops_sdk import Client  # type: ignore[no-redef]

_MEMORY_TOOLS = {"memory"}
_SUBAGENT_TOOLS = {"delegate_task"}  # its per-child subagent spans nest under this tool span


def tool_kind(tool_name: str) -> str:
    n = (tool_name or "").lower()
    if n in ("terminal", "execute_code", "process"):
        return "terminal"
    if n.startswith("browser"):
        return "browser"
    if n.startswith("mcp_") or n.startswith("mcp__"):
        return "mcp"
    if n in _MEMORY_TOOLS:
        return "memory"
    return "tool"


def mcp_server(tool_name: str) -> str | None:
    """Hermes names MCP tools mcp_<server>_<tool>. Server names may contain underscores,
    so this is a best guess; the server-side classifier only uses it as a hint."""
    n = tool_name or ""
    if n.startswith("mcp__"):
        parts = n.split("__")
        return parts[1] if len(parts) > 2 else None
    if n.startswith("mcp_"):
        rest = n[4:]
        return rest.split("_", 1)[0] if "_" in rest else None
    return None


def _title(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line[:120] + ("…" if len(line) > 120 else "")


class HermesObserver:
    def __init__(self, client: Client | None = None):
        self.client = client or Client(source="hermes")
        self._lock = threading.Lock()
        self._runs: set[str] = set()                                 # run ids we've started
        self._session_run: dict[str, str] = {}                        # session_id -> current run_id
        self._child: dict[str, tuple[str, str]] = {}                  # child_session_id -> (parent run, subagent span)
        self._tool_fifo: dict[tuple[str, str], deque[str]] = defaultdict(deque)  # (run, tool) -> open span ids
        self._aux_fifo: dict[tuple[str, str], deque[str]] = defaultdict(deque)   # (session, aux_task) -> open request ids
        self._delegate: dict[str, str] = {}                           # run_id -> open delegate_task span
        self._subagent_span: dict[str, tuple[str, str]] = {}          # child_session_id -> (run, subagent span)

    def _label(self, text: str, fallback: str) -> str:
        """Names derived from prompts are content too: drop them when content capture is off."""
        return text if getattr(self.client, "capture_content", True) else fallback

    # -- run resolution ----------------------------------------------------------------

    def _run_for(self, session_id: Any, turn_id: Any) -> tuple[str, str | None]:
        """Which run an event belongs to, and the span it should nest under (for subagents)."""
        sid, tid = str(session_id or ""), str(turn_id or "")
        with self._lock:
            if tid and tid in self._runs:
                return tid, None
            if sid in self._child:
                return self._child[sid]
            if sid in self._session_run:
                return self._session_run[sid], None
            run_id = tid or sid or f"hermes-{uuid.uuid4().hex[:12]}"
            self._runs.add(run_id)
            if sid:
                self._session_run[sid] = run_id
        # First sight of a run without pre_llm_call (e.g. hooks enabled mid-turn).
        self.client.run_start(run_id, "", agent="hermes", session_id=sid or None, implicit=True)
        return run_id, None

    # -- turn / run --------------------------------------------------------------------

    def pre_llm_call(self, **kw: Any) -> None:
        sid, tid = str(kw.get("session_id") or ""), str(kw.get("turn_id") or "")
        run_id = tid or f"{sid}:{uuid.uuid4().hex[:8]}"
        parent = None
        with self._lock:
            if run_id in self._runs:
                return
            self._runs.add(run_id)
            if sid:
                self._session_run[sid] = run_id
            parent = self._child.get(sid)
        attrs: dict[str, Any] = dict(
            agent=kw.get("platform") and f"hermes/{kw['platform']}" or "hermes",
            session_id=sid or None, turn_id=tid or None, model=kw.get("model"), platform=kw.get("platform"),
            user=kw.get("sender_id"), user_message=kw.get("user_message"), is_first_turn=kw.get("is_first_turn"),
        )
        if parent:
            attrs.update(parent_run_id=parent[0], parent_run_span_id=parent[1])
        self.client.run_start(run_id, self._label(_title(kw.get("user_message")), "turn"), **attrs)

    def post_llm_call(self, **kw: Any) -> None:
        run_id, _ = self._run_for(kw.get("session_id"), kw.get("turn_id"))
        self.client.run_end(run_id, "ok", assistant_response=kw.get("assistant_response"), model=kw.get("model"))

    def on_session_end(self, **kw: Any) -> None:
        sid = str(kw.get("session_id") or "")
        with self._lock:
            run_id = self._session_run.pop(sid, None)
        if run_id and kw.get("interrupted"):
            self.client.run_end(run_id, "interrupted", reason=kw.get("reason"))

    # -- provider requests -------------------------------------------------------------

    def pre_api_request(self, **kw: Any) -> None:
        run_id, parent = self._run_for(kw.get("session_id"), kw.get("turn_id"))
        rid = kw.get("api_request_id") or uuid.uuid4().hex
        name = str(kw.get("model") or "llm") + (f" · {kw['aux_task']}" if kw.get("aux_task") else "")
        self.client.span_start(
            run_id, f"llm:{rid}", "llm", name, parent,
            model=kw.get("model"), provider=kw.get("provider"), base_url=kw.get("base_url"), api_mode=kw.get("api_mode"),
            api_call_count=kw.get("api_call_count"), message_count=kw.get("message_count"),
            tool_count=kw.get("tool_count"), approx_input_tokens=kw.get("approx_input_tokens"),
            max_tokens=kw.get("max_tokens"), request=kw.get("request"), aux_task=kw.get("aux_task"),
        )

    def post_api_request(self, **kw: Any) -> None:
        run_id, _ = self._run_for(kw.get("session_id"), kw.get("turn_id"))
        rid = kw.get("api_request_id") or uuid.uuid4().hex
        self.client.span_end(
            run_id, f"llm:{rid}", "ok", kind="llm", model=kw.get("model"), response_model=kw.get("response_model"),
            usage=kw.get("usage"), finish_reason=kw.get("finish_reason"), api_duration=kw.get("api_duration"),
            assistant_tool_call_count=kw.get("assistant_tool_call_count"),
            response=kw.get("assistant_message") or kw.get("response"),
        )

    def api_request_error(self, **kw: Any) -> None:
        run_id, _ = self._run_for(kw.get("session_id"), kw.get("turn_id"))
        rid = kw.get("api_request_id") or uuid.uuid4().hex
        err = kw.get("error") if isinstance(kw.get("error"), dict) else {"message": kw.get("error")}
        self.client.span_end(
            run_id, f"llm:{rid}", "error", kind="llm", model=kw.get("model"), status_code=kw.get("status_code"),
            retry_count=kw.get("retry_count"), retryable=kw.get("retryable"), reason=kw.get("reason"),
            error=err.get("message") or kw.get("reason"), error_type=err.get("type"),
        )

    # Auxiliary calls (titling, compression, vision, ...) often carry an empty api_request_id,
    # so pair start and end per (session, aux_task) instead.
    def pre_auxiliary_call(self, **kw: Any) -> None:
        if not kw.get("api_request_id"):
            kw["api_request_id"] = f"aux-{uuid.uuid4().hex[:12]}"
            with self._lock:
                self._aux_fifo[(str(kw.get("session_id") or ""), str(kw.get("aux_task") or ""))].append(kw["api_request_id"])
        self.pre_api_request(**kw)

    def post_auxiliary_call(self, **kw: Any) -> None:
        if not kw.get("api_request_id"):
            with self._lock:
                fifo = self._aux_fifo.get((str(kw.get("session_id") or ""), str(kw.get("aux_task") or "")))
                kw["api_request_id"] = fifo.popleft() if fifo else None
        (self.api_request_error if kw.get("error") else self.post_api_request)(**kw)

    # -- tools -------------------------------------------------------------------------

    def pre_tool_call(self, **kw: Any) -> None:
        run_id, parent = self._run_for(kw.get("session_id"), kw.get("turn_id"))
        name = str(kw.get("tool_name") or "tool")
        span_id = f"tool:{kw.get('tool_call_id') or uuid.uuid4().hex}"
        if not kw.get("tool_call_id"):
            with self._lock:
                self._tool_fifo[(run_id, name)].append(span_id)
        if kw.get("api_request_id"):
            parent = f"llm:{kw['api_request_id']}"
        if name in _SUBAGENT_TOOLS:
            with self._lock:
                self._delegate[run_id] = span_id
        self.client.span_start(run_id, span_id, tool_kind(name), name, parent,
                               args=kw.get("args"), mcp_server=mcp_server(name), task_id=kw.get("task_id"))

    def post_tool_call(self, **kw: Any) -> None:
        run_id, _ = self._run_for(kw.get("session_id"), kw.get("turn_id"))
        name = str(kw.get("tool_name") or "tool")
        if kw.get("tool_call_id"):
            span_id = f"tool:{kw['tool_call_id']}"
        else:
            with self._lock:
                fifo = self._tool_fifo.get((run_id, name))
                span_id = fifo.popleft() if fifo else f"tool:{uuid.uuid4().hex}"
        if name in _SUBAGENT_TOOLS:
            with self._lock:
                if self._delegate.get(run_id) == span_id:
                    del self._delegate[run_id]
        status = str(kw.get("status") or ("error" if kw.get("error_message") else "ok"))
        if status == "error" and "timeout" in str(kw.get("error_type") or "").lower():
            status = "timeout"
        self.client.span_end(run_id, span_id, status, kind=tool_kind(name), name=name, result=kw.get("result"),
                             duration_ms=kw.get("duration_ms"), error_type=kw.get("error_type"),
                             error=kw.get("error_message"))

    # -- subagents ---------------------------------------------------------------------

    def subagent_start(self, **kw: Any) -> None:
        run_id, parent = self._run_for(kw.get("parent_session_id"), kw.get("parent_turn_id"))
        child = kw.get("child_subagent_id") or kw.get("child_session_id") or uuid.uuid4().hex
        span_id = f"subagent:{child}"
        with self._lock:
            parent = self._delegate.get(run_id, parent)
            if kw.get("child_session_id"):
                self._child[str(kw["child_session_id"])] = (run_id, span_id)
                self._subagent_span[str(kw["child_session_id"])] = (run_id, span_id)
        name = str(kw.get("child_goal") or kw.get("child_role") or "subagent")
        name = self._label(name[:80] + ("…" if len(name) > 80 else ""), str(kw.get("child_role") or "subagent"))
        self.client.span_start(run_id, span_id, "subagent", name, parent,
                               child_session_id=kw.get("child_session_id"), child_goal=kw.get("child_goal"),
                               child_role=kw.get("child_role"))

    def subagent_stop(self, **kw: Any) -> None:
        # subagent_stop carries child_session_id but not child_subagent_id, so pair on the session.
        with self._lock:
            known = self._subagent_span.pop(str(kw.get("child_session_id") or ""), None)
        if known:
            run_id, span_id = known
        else:
            run_id, _ = self._run_for(kw.get("parent_session_id"), kw.get("parent_turn_id"))
            span_id = f"subagent:{kw.get('child_subagent_id') or kw.get('child_session_id') or uuid.uuid4().hex}"
        status = str(kw.get("status") or kw.get("child_status") or "ok")
        status = {"completed": "ok", "success": "ok", "failed": "error"}.get(status, status)
        self.client.span_end(run_id, span_id, status, kind="subagent",
                             child_summary=kw.get("child_summary"), duration_ms=kw.get("duration_ms"),
                             tool_call_history=kw.get("tool_call_history"))

    # -- approvals ---------------------------------------------------------------------

    def _approval_run(self, kw: dict[str, Any]) -> str:
        key = str(kw.get("session_key") or kw.get("session_id") or "")
        with self._lock:
            run_id = self._session_run.get(key) or next(iter(reversed(list(self._session_run.values()))), None)
        return run_id or self._run_for(key, None)[0]

    def pre_approval_request(self, **kw: Any) -> None:
        self.client.point(self._approval_run(kw), "approval", "approval requested", command=kw.get("command"),
                          description=kw.get("description"), pattern_keys=kw.get("pattern_keys"), surface=kw.get("surface"))

    def post_approval_response(self, **kw: Any) -> None:
        choice = kw.get("choice")
        self.client.point(self._approval_run(kw), "approval", f"approval: {choice}", command=kw.get("command"),
                          choice=choice, status="ok" if choice in ("once", "session", "always") else "blocked")


HOOKS = (
    "pre_llm_call", "post_llm_call", "on_session_end",
    "pre_api_request", "post_api_request", "api_request_error", "pre_auxiliary_call", "post_auxiliary_call",
    "pre_tool_call", "post_tool_call", "subagent_start", "subagent_stop",
    "pre_approval_request", "post_approval_response",
)


def _safe(fn: Callable[..., Any]) -> Callable[..., None]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        try:
            fn(*args, **kwargs)
        except Exception:
            pass
        return None  # observers never alter agent behaviour
    return wrapper


def register(ctx: Any, observer: HermesObserver | None = None) -> HermesObserver:
    observer = observer or HermesObserver()
    for hook in HOOKS:
        try:
            ctx.register_hook(hook, _safe(getattr(observer, hook)))
        except Exception:
            pass  # older Hermes without this hook
    return observer
