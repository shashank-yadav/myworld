from aops.integrations.hermes import HermesObserver, mcp_server, register, tool_kind
from aops.store import EventStore
from aops.trace import build_run


class FakeClient:
    """Records what the observer emits, in aops wire format."""
    def __init__(self):
        self.events = []
        self.n = 0

    def emit(self, type, run_id, **kw):
        self.n += 1
        attrs = {k: v for k, v in kw.items() if k not in ("span_id", "parent_span_id", "kind", "name") and v is not None}
        self.events.append({"id": str(self.n), "ts": 1000.0 + self.n, "type": type, "run_id": run_id,
                            "span_id": kw.get("span_id"), "parent_span_id": kw.get("parent_span_id"),
                            "kind": kw.get("kind"), "name": kw.get("name", ""), "source": "hermes", "attrs": attrs})

    def run_start(self, run_id, name="", **a): self.emit("run.start", run_id, kind="agent", name=name, **a)
    def run_end(self, run_id, status="ok", **a): self.emit("run.end", run_id, kind="agent", status=status, **a)
    def span_start(self, run_id, span_id, kind, name, parent_span_id=None, **a):
        self.emit("span.start", run_id, span_id=span_id, parent_span_id=parent_span_id, kind=kind, name=name, **a)
    def span_end(self, run_id, span_id, status="ok", kind=None, name="", **a):
        self.emit("span.end", run_id, span_id=span_id, kind=kind, name=name, status=status, **a)
    def point(self, run_id, kind, name, parent_span_id=None, **a):
        self.emit("span.event", run_id, span_id=f"evt:{self.n}", parent_span_id=parent_span_id, kind=kind, name=name, **a)


class FakeCtx:
    def __init__(self):
        self.hooks = {}

    def register_hook(self, name, fn):
        if name == "pre_auxiliary_call":
            raise ValueError("unknown hook in this Hermes version")
        self.hooks[name] = fn


def test_full_turn_becomes_one_run():
    fc, ctx = FakeClient(), FakeCtx()
    register(ctx, HermesObserver(fc))
    h = ctx.hooks
    ids = dict(session_id="s1", turn_id="t1")
    h["pre_llm_call"](user_message="Book meeting with John\nthanks", model="claude-opus-5", platform="telegram",
                      sender_id="u1", conversation_history=[], is_first_turn=True, **ids)
    h["pre_api_request"](api_request_id="a1", model="claude-opus-5", provider="anthropic", **ids)
    h["post_api_request"](api_request_id="a1", model="claude-opus-5", usage={"input_tokens": 1000, "output_tokens": 50}, **ids)
    h["pre_tool_call"](tool_name="mcp_gcal_create_event", args={"title": "x"}, tool_call_id="c1", api_request_id="a1", **ids)
    h["post_tool_call"](tool_name="mcp_gcal_create_event", tool_call_id="c1", result='{"id": 1}', status="ok", duration_ms=800, **ids)
    h["pre_tool_call"](tool_name="mcp_gmail_send_email", args={"to": "j"}, tool_call_id="c2", api_request_id="a1", **ids)
    h["post_tool_call"](tool_name="mcp_gmail_send_email", tool_call_id="c2", status="error", error_type="TimeoutError",
                        error_message="timed out after 30s", **ids)
    h["post_llm_call"](assistant_response="done", **ids)

    store = EventStore(":memory:")
    accepted, errors = store.append(fc.events)
    assert errors == [] and accepted == len(fc.events)
    run = build_run("t1", store.run_events(["t1"])["t1"], now=2000)
    assert run.name == "Book meeting with John" and run.agent == "hermes/telegram"
    llm = run.roots[0]
    assert llm.kind == "llm" and llm.cost is not None
    kids = {c.name: c for c in llm.children}
    assert kids["mcp_gcal_create_event"].outcome == "succeeded"
    assert kids["mcp_gmail_send_email"].outcome == "unknown"
    assert kids["mcp_gmail_send_email"].kind == "mcp"
    assert run.summary() | {} and run.summary()["changes"] == 1 and run.status == "attention"


def test_subagent_child_run_links_to_parent():
    fc = FakeClient()
    o = HermesObserver(fc)
    o.pre_llm_call(session_id="p", turn_id="pt", user_message="research")
    o.subagent_start(parent_session_id="p", parent_turn_id="pt", child_session_id="c", child_subagent_id="sa1",
                     child_role="researcher", child_goal="find prices")
    o.pre_llm_call(session_id="c", turn_id="ct", user_message="find prices")
    child_start = [e for e in fc.events if e["type"] == "run.start" and e["run_id"] == "ct"][0]
    assert child_start["attrs"]["parent_run_id"] == "pt"
    assert child_start["attrs"]["parent_run_span_id"] == "subagent:sa1"
    # real Hermes: subagent_stop has child_session_id and child_status, but no child_subagent_id
    o.subagent_stop(parent_session_id="p", parent_turn_id="pt", child_session_id="c", child_status="completed",
                    child_summary="ok")
    end = fc.events[-1]
    assert end["span_id"] == "subagent:sa1" and end["attrs"]["status"] == "ok"


def test_subagent_nests_under_delegate_task():
    fc = FakeClient()
    o = HermesObserver(fc)
    o.pre_llm_call(session_id="p", turn_id="pt", user_message="x")
    o.pre_tool_call(tool_name="delegate_task", tool_call_id="d1", session_id="p", turn_id="pt", args={"tasks": []})
    o.subagent_start(parent_session_id="p", parent_turn_id="pt", child_session_id="c", child_subagent_id="sa1",
                     child_goal="count lines")
    start = [e for e in fc.events if e["span_id"] == "subagent:sa1"][0]
    assert start["parent_span_id"] == "tool:d1" and start["name"] == "count lines"
    assert [e for e in fc.events if e["span_id"] == "tool:d1"][0]["kind"] == "tool"


def test_tool_without_call_id_pairs_fifo_and_hooks_never_raise():
    fc, ctx = FakeClient(), FakeCtx()
    register(ctx, HermesObserver(fc))
    h = ctx.hooks
    h["pre_tool_call"](tool_name="terminal", args={"command": "ls"}, session_id="s", turn_id="t")
    h["post_tool_call"](tool_name="terminal", status="ok", session_id="s", turn_id="t")
    starts = [e for e in fc.events if e["type"] == "span.start"]
    ends = [e for e in fc.events if e["type"] == "span.end"]
    assert starts[0]["span_id"] == ends[0]["span_id"] and starts[0]["kind"] == "terminal"
    assert h["pre_tool_call"](tool_name=object()) is None  # garbage in, no exception, no behaviour change


def test_helpers():
    assert tool_kind("browser_click") == "browser" and tool_kind("memory") == "memory"
    assert tool_kind("delegate_task") == "tool" and tool_kind("mcp_github_create_issue") == "mcp"
    assert mcp_server("mcp_github_create_issue") == "github"


def test_aux_calls_with_empty_request_id_pair_up():
    fc = FakeClient()
    o = HermesObserver(fc)
    o.pre_llm_call(session_id="s", turn_id="t", user_message="hi")
    o.pre_auxiliary_call(session_id="s", turn_id="t", api_request_id="", aux_task="title_generation", model="m")
    o.post_auxiliary_call(session_id="s", turn_id="t", api_request_id="", aux_task="title_generation", model="m",
                          usage={"input_tokens": 5})
    start, end = fc.events[-2], fc.events[-1]
    assert start["span_id"] == end["span_id"] and start["name"] == "m · title_generation"
