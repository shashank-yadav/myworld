"""Realistic sample runs so the dashboard has something to show before an agent is connected."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from .store import EventStore


class _Run:
    def __init__(self, name: str, start: float, source: str = "hermes", **attrs: Any):
        self.run_id = f"demo-{uuid.uuid4().hex[:10]}"
        self.t = start
        self.events: list[dict[str, Any]] = []
        self.source = source
        self._llm: str | None = None
        self._e("run.start", kind="agent", name=name, attrs=attrs)

    def _e(self, type: str, **kw: Any) -> None:
        self.events.append({"id": str(uuid.uuid4()), "ts": self.t, "type": type, "run_id": self.run_id,
                            "source": self.source, **kw})

    def llm(self, model: str, inp: int, out: int, secs: float, cache_read: int = 0, parent: str | None = None,
            cost: float | None = None, error: str | None = None) -> str:
        sid = f"llm:{uuid.uuid4().hex[:10]}"
        self._e("span.start", span_id=sid, parent_span_id=parent, kind="llm", name=model,
                attrs={"model": model, "provider": "anthropic" if model.startswith("claude") else "openrouter",
                       "request": {"messages": f"<{inp} tokens of conversation>"}})
        self.t += secs
        if error:
            self._e("span.end", span_id=sid, kind="llm", attrs={"status": "error", "error": error, "status_code": 529})
        else:
            usage = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cache_read}
            attrs: dict[str, Any] = {"status": "ok", "usage": usage, "finish_reason": "tool_use"}
            if cost is not None:
                attrs["cost_usd"] = cost
            self._e("span.end", span_id=sid, kind="llm", attrs=attrs)
        self._llm = sid
        self.t += 0.05
        return sid

    def tool(self, name: str, args: dict[str, Any], result: Any = None, secs: float = 0.4, kind: str = "tool",
             status: str = "ok", error: str | None = None, parent: str | None = None, end: bool = True,
             **attrs: Any) -> str:
        sid = f"tool:{uuid.uuid4().hex[:10]}"
        self._e("span.start", span_id=sid, parent_span_id=parent or self._llm, kind=kind, name=name,
                attrs={"args": args, **attrs})
        self.t += secs
        if end:
            end_attrs: dict[str, Any] = {"status": status, "duration_ms": round(secs * 1000)}
            if result is not None:
                end_attrs["result"] = result if isinstance(result, str) else json.dumps(result)
            if error:
                end_attrs["error"] = error
            self._e("span.end", span_id=sid, kind=kind, attrs=end_attrs)
        self.t += 0.05
        return sid

    def point(self, kind: str, name: str, **attrs: Any) -> None:
        self._e("span.event", span_id=f"evt:{uuid.uuid4().hex[:8]}", parent_span_id=self._llm, kind=kind, name=name,
                attrs=attrs)

    def end(self, status: str = "ok", **attrs: Any) -> list[dict[str, Any]]:
        self._e("run.end", kind="agent", attrs={"status": status, **attrs})
        return self.events


def build(now: float | None = None) -> list[dict[str, Any]]:
    now = now or time.time()
    out: list[dict[str, Any]] = []
    opus, sonnet, haiku = "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"

    # 1. The canonical run: 18 calls, 2 external changes, 1 failure.
    r = _Run("Book meeting with John", now - 2.1 * 3600, agent="hermes/telegram", user="shashank",
             workflow="scheduling", model=opus, user_message="Book a 30 min meeting with John next week about the Q4 plan")
    r.llm(opus, 5200, 180, 2.1, cache_read=4000)
    r.tool("mcp_gmail_search_emails", {"query": "from:john@acme.com newer_than:30d"},
           {"messages": [{"id": "m1", "subject": "Q4 planning"}, {"id": "m2", "subject": "Re: offsite"}]}, 0.8,
           kind="mcp", mcp_server="gmail")
    r.llm(opus, 5400, 120, 1.6, cache_read=9000)
    r.tool("mcp_contacts_lookup", {"name": "John"}, {"email": "john@acme.com", "tz": "America/Los_Angeles"}, 0.3,
           kind="mcp", mcp_server="contacts")
    r.tool("mcp_gmail_read_email", {"id": "m1"}, {"from": "John Park <john@acme.com>", "body": "Can we find time next week?"},
           0.5, kind="mcp", mcp_server="gmail")
    r.tool("mcp_gmail_read_email", {"id": "m2"}, {"from": "John Park <john@acme.com>", "body": "Tue/Wed work best"},
           0.4, kind="mcp", mcp_server="gmail")
    r.llm(opus, 5800, 210, 2.4, cache_read=10000)
    r.tool("mcp_gcal_list_events", {"calendar": "primary", "range": "next_week"}, {"events": 14}, 0.7,
           kind="mcp", mcp_server="gcal")
    r.tool("mcp_gcal_get_freebusy", {"emails": ["john@acme.com"]}, None, 0.3, kind="mcp", mcp_server="gcal",
           status="error", error="403 Forbidden: no access to john@acme.com free/busy")
    r.llm(opus, 6900, 260, 2.8, cache_read=11500)
    r.tool("memory", {"action": "read", "key": "john_park"}, {"prefers": "mornings, Pacific time"}, 0.05, kind="memory")
    r.tool("mcp_gcal_find_slot", {"duration_min": 30, "days": ["Tue", "Wed"], "window": "09:00-12:00 PT"},
           {"slots": ["Tue 10:00", "Wed 09:30"]}, 0.6, kind="mcp", mcp_server="gcal")
    r.llm(opus, 7600, 340, 3.1, cache_read=12500)
    r.tool("mcp_gcal_create_event", {"title": "Q4 plan · Shashank / John", "start": "Tue 10:00 PT", "duration_min": 30,
                                     "attendees": ["john@acme.com"]}, {"id": "evt_8f2", "status": "confirmed"}, 0.9,
           kind="mcp", mcp_server="gcal")
    r.llm(opus, 7100, 420, 3.4, cache_read=13200)
    r.tool("mcp_gmail_send_email", {"to": "john@acme.com", "subject": "Tue 10am PT for Q4 plan",
                                    "body": "Hi John, I put 30 min on Tuesday at 10am PT. Invite is in your inbox."},
           {"id": "msg_77a", "labelIds": ["SENT"]}, 1.1, kind="mcp", mcp_server="gmail")
    r.tool("memory", {"action": "add", "content": "Met John re Q4 plan, Tue 10am PT"}, {"ok": True}, 0.05, kind="memory")
    r.llm(opus, 7600, 150, 1.3, cache_read=14000)
    out += r.end(assistant_response="Booked Tue 10:00 PT with John and emailed him the details.")

    # 2. The screen that matters: a send that timed out.
    r = _Run("Send weekly investor update", now - 1.4 * 3600, agent="openclaw", source="openclaw", user="shashank",
             workflow="investor-updates", model=sonnet)
    r.llm(sonnet, 7000, 200, 1.4)
    r.tool("read", {"path": "~/notes/investor-update-2026-09-24.md"}, "# Weekly update\nARR +12%...", 0.1)
    r.tool("memory_search", {"query": "investor list"}, {"hits": 1}, 0.2, kind="memory")
    r.llm(sonnet, 9100, 900, 6.2)
    r.tool("gmail_send_email", {"to": ["investors@list.acme.com"], "subject": "Acme weekly update: Sep 24",
                                "body": "Hi all, ARR grew 12% week over week..."}, None, 30.0,
           status="timeout", error="ReadTimeout: gmail.googleapis.com did not respond within 30s")
    r.llm(sonnet, 9500, 120, 1.1)
    out += r.end(status="ok", assistant_response="The send timed out. I did not retry, to avoid a duplicate.")

    # 3. GitHub triage with a created issue and labels.
    r = _Run("Triage new GitHub issues in acme/api", now - 3.3 * 3600, agent="hermes/cli", user="priya",
             workflow="triage", model=sonnet)
    r.llm(sonnet, 6000, 150, 1.2)
    r.tool("mcp_github_list_issues", {"repo": "acme/api", "state": "open", "since": "24h"}, {"count": 4}, 0.9,
           kind="mcp", mcp_server="github")
    r.llm(sonnet, 8800, 300, 2.0)
    r.tool("mcp_github_add_labels", {"issue": 812, "labels": ["bug", "p1"]}, {"ok": True}, 0.5, kind="mcp", mcp_server="github")
    r.tool("mcp_github_add_labels", {"issue": 813, "labels": ["question"]}, {"ok": True}, 0.5, kind="mcp", mcp_server="github")
    r.tool("mcp_github_create_comment", {"issue": 813, "body": "Thanks! This is covered in docs/auth.md."}, {"id": 99102},
           0.6, kind="mcp", mcp_server="github")
    r.llm(sonnet, 9900, 400, 2.6)
    r.tool("mcp_github_create_issue", {"repo": "acme/api", "title": "Rate limiter drops requests at exactly 100 rps",
                                       "body": "Split out from #812..."}, {"number": 816}, 0.8, kind="mcp", mcp_server="github")
    r.tool("mcp_slack_post_message", {"channel": "#api-oncall", "text": "Triage done: 1 P1 (#812), new #816"},
           {"ok": True, "ts": "1727.2"}, 0.4, kind="mcp", mcp_server="slack")
    r.llm(sonnet, 10300, 120, 0.9)
    out += r.end()

    # 4. A coding run with filesystem writes, a failed test, and a git push.
    r = _Run("Fix flaky retry test and push", now - 5.2 * 3600, agent="hermes/cli", user="shashank",
             workflow="coding", model=opus)
    r.llm(opus, 15000, 250, 2.2, cache_read=8000)
    r.tool("read_file", {"path": "tests/test_retry.py"}, "def test_backoff(): ...", 0.05)
    r.tool("terminal", {"command": "uv run pytest tests/test_retry.py -q"}, None, 4.1, kind="terminal",
           status="error", error="exit 1: 1 failed (test_backoff: assert 0.19 < 0.1)")
    r.llm(opus, 17500, 800, 5.0, cache_read=15000)
    r.tool("patch", {"path": "src/retry.py", "diff": "-    jitter = random.random()\n+    jitter = rng.random()"},
           {"success": True}, 0.1)
    r.tool("terminal", {"command": "uv run pytest -q"}, "142 passed in 8.2s", 9.0, kind="terminal")
    r.llm(opus, 18200, 180, 1.4, cache_read=17000)
    r.tool("terminal", {"command": "git commit -am 'Seed retry jitter in tests' && git push origin fix/flaky-retry"},
           "To github.com:acme/api.git\n * [new branch] fix/flaky-retry", 2.3, kind="terminal")
    r.llm(opus, 18500, 120, 1.0, cache_read=18000)
    out += r.end()

    # 5. Research with a subagent, browser actions, and an unpriced model with a reported cost.
    r = _Run("Research competitor pricing pages", now - 0.6 * 3600, agent="openclaw", source="openclaw",
             user="priya", workflow="research", model="openrouter/deepseek-v4")
    r.llm("openrouter/deepseek-v4", 5000, 300, 1.8, cost=0.0021)
    sub = r.tool("sessions_spawn", {"task": "Collect pricing tiers from 3 competitors"}, {"summary": "3 pages captured"},
                 0.1, kind="subagent")
    r.tool("browser", {"action": "navigate", "url": "https://competitor-a.com/pricing"}, "ok", 2.2, kind="browser", parent=sub)
    r.tool("browser", {"action": "snapshot"}, "<page snapshot>", 0.8, kind="browser", parent=sub)
    r.tool("browser", {"action": "navigate", "url": "https://competitor-b.com/pricing"}, "ok", 1.9, kind="browser", parent=sub)
    r.tool("browser", {"action": "click", "ref": "e12", "element": "Annual billing toggle"}, "ok", 0.4, kind="browser", parent=sub)
    r.tool("web_fetch", {"url": "https://competitor-c.com/pricing"}, "<html>", 1.2, parent=sub)
    r.llm("openrouter/deepseek-v4", 22000, 1400, 9.5, cost=0.0118)
    r.tool("write", {"path": "~/research/pricing-2026-09.md"}, {"bytes": 4812}, 0.05)
    out += r.end()

    # 6. File cleanup: deletes, one of which 404s.
    r = _Run("Clean up stale drafts in Drive", now - 7.5 * 3600, agent="hermes/cli", user="shashank",
             workflow="housekeeping", model=haiku)
    r.llm(haiku, 4000, 200, 0.8)
    r.tool("mcp_drive_search_files", {"query": "title contains 'DRAFT' and modifiedTime < '2026-06-01'"}, {"files": 3},
           0.7, kind="mcp", mcp_server="drive")
    r.llm(haiku, 5200, 160, 0.7)
    r.point("approval", "approval requested", command="delete 3 Drive files", status="ok")
    r.point("approval", "approval: once", choice="once", status="ok")
    r.tool("mcp_drive_delete_file", {"id": "1aB"}, {"ok": True}, 0.5, kind="mcp", mcp_server="drive")
    r.tool("mcp_drive_delete_file", {"id": "1aC"}, {"ok": True}, 0.4, kind="mcp", mcp_server="drive")
    r.tool("mcp_drive_delete_file", {"id": "1aD"}, None, 0.3, kind="mcp", mcp_server="drive",
           status="error", error="404 File not found: 1aD")
    r.llm(haiku, 5600, 90, 0.5)
    out += r.end()

    # 7. An in-flight run.
    r = _Run("Reply to customer escalation #4471", now - 40, agent="hermes/slack", user="support-bot",
             workflow="support", model=sonnet)
    r.llm(sonnet, 8000, 220, 1.5)
    r.tool("mcp_zendesk_get_ticket", {"id": 4471}, {"subject": "Charged twice"}, 0.6, kind="mcp", mcp_server="zendesk")
    r.llm(sonnet, 9400, 380, 2.2)
    r.tool("mcp_stripe_create_refund", {"charge": "ch_3P...", "amount": 4900}, None, 1.0, kind="mcp",
           mcp_server="stripe", end=False)
    out += r.events

    # 8. Yesterday: an abandoned run whose write never reported back.
    r = _Run("Update CRM with call notes", now - 26 * 3600, agent="openclaw", source="openclaw", user="shashank",
             workflow="crm", model=sonnet)
    r.llm(sonnet, 6000, 300, 1.8)
    r.tool("mcp_hubspot_update_contact", {"id": "c_19", "notes": "Interested in Teams plan"}, None, 1.2,
           kind="mcp", mcp_server="hubspot", end=False)
    out += r.events
    return out


def seed(store: EventStore) -> int:
    return store.append(build())[0]
