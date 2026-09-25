"""The gmail service: identity, versions, seed format, state, errors and the version probe."""

from __future__ import annotations

from email.utils import parseaddr
from typing import Any

from ...core.instance import Instance, Service
from .model import V1, _addr, _list, _mb, _new_mailbox


class Gmail(Service):
    name = "gmail"
    title = "Gmail"
    description = "Simulated Gmail mailbox. Behaves like the Gmail MCP server; nothing is really sent."

    versions = {"2026-09-25": "Initial release: 19 tools modeled on GongRzhe/Gmail-MCP-Server.",
                V1: "Bounces for unknown/typo'd recipients, out-of-office and colleague auto-replies, daily send quota."}

    def probe(self, ctx: Instance) -> None:
        c = ctx.call
        ids = list(_mb(ctx)["messages"])
        c("search_emails", {"query": "is:unread"})
        c("search_emails", {"query": "from:john OR has:attachment newer_than:7d"})
        c("read_email", {"messageId": ids[0]})
        c("read_email", {"messageId": "missing"})
        c("send_email", {"to": ["john@acme.com"], "subject": "Re: Q4 planning", "body": "Tue 10am?"})
        c("draft_email", {"to": ["priya@acme.com"], "subject": "Draft", "body": "WIP"})
        c("modify_email", {"messageId": ids[0], "removeLabelIds": ["UNREAD"], "addLabelIds": ["STARRED"]})
        c("list_email_labels", {})
        c("create_label", {"name": "Travel"})
        c("get_or_create_label", {"name": "clients"})
        c("create_filter_from_template", {"template": "fromSender", "parameters": {"senderEmail": "x@y.com", "archive": True}})
        c("list_filters", {})
        c("delete_email", {"messageId": ids[-1]})
        c("search_emails", {"query": "in:sent"})
        if ctx.at_least(V1):
            c("send_email", {"to": ["jhon@acme.com"], "subject": "Typo", "body": "Hi"})
            c("send_email", {"to": ["priya@acme.co"], "subject": "Typo domain", "body": "Hi"})
            ctx.advance(600)
            c("search_emails", {"query": "from:mailer-daemon"})

    def default_seed(self) -> dict[str, Any]:
        return {
            "user": {"email": "alex@acme.com", "name": "Alex Rivera"},
            "labels": ["Receipts", "Clients"],
            "emails": [
                {"from": "John Park <john@acme.com>", "to": ["alex@acme.com"], "subject": "Q4 planning",
                 "body": "Hi Alex, can we find 30 minutes next week to go over the Q4 plan? Tue or Wed works best.",
                 "date": "2026-09-19T16:02:00Z", "labels": ["INBOX", "UNREAD", "IMPORTANT"], "thread": "q4"},
                {"from": "Stripe <receipts@stripe.com>", "to": ["alex@acme.com"], "subject": "Your receipt from Acme Cloud",
                 "body": "Amount paid: $49.00. Receipt #1847-2213.", "date": "2026-09-18T08:15:00Z",
                 "labels": ["INBOX", "CATEGORY_UPDATES"],
                 "attachments": [{"filename": "receipt-1847-2213.pdf", "mimeType": "application/pdf", "size": 38211}]},
                {"from": "Priya Shah <priya@acme.com>", "to": ["alex@acme.com"], "cc": ["john@acme.com"],
                 "subject": "Re: Launch checklist", "body": "Updated the checklist, two items left.",
                 "date": "2026-09-20T10:40:00Z", "labels": ["INBOX"], "thread": "launch"},
                {"from": "Alex Rivera <alex@acme.com>", "to": ["priya@acme.com"], "subject": "Launch checklist",
                 "body": "Here's the draft checklist for the launch.", "date": "2026-09-17T09:00:00Z",
                 "labels": ["SENT"], "thread": "launch"},
                {"from": "Deals Daily <no-reply@dealsdaily.biz>", "to": ["alex@acme.com"],
                 "subject": "70% off everything today only", "body": "Click here for deals.",
                 "date": "2026-09-20T06:00:00Z", "labels": ["INBOX", "UNREAD", "CATEGORY_PROMOTIONS"]},
            ],
        }

    def initial_state(self, seed: dict[str, Any], ctx: Instance) -> dict[str, Any]:
        """One mailbox per person. The seed's top-level user/labels/emails is the default mailbox;
        ``mailboxes: [{user, labels, emails}, ...]`` adds colleagues whose agents can join."""
        primary = {k: seed[k] for k in ("user", "labels", "emails") if k in seed}
        state: dict[str, Any] = {"mailboxes": {}}
        for box in [primary, *seed.get("mailboxes", [])]:
            user = box.get("user") or {"email": "alex@acme.com", "name": "Alex Rivera"}
            state["mailboxes"][user["email"].lower()] = _new_mailbox(ctx, box, user)
        state["default"] = next(iter(state["mailboxes"]))
        # who exists: every mailbox owner, everyone in the seeded mail, and an explicit directory
        known = set(state["mailboxes"]) | {_addr(a).lower() for a in seed.get("directory", [])}
        names = {e: b["user"].get("name", e) for e, b in state["mailboxes"].items()}
        for box in [primary, *seed.get("mailboxes", [])]:
            for e in box.get("emails", []):
                for a in [e.get("from", ""), *_list(e.get("to")), *_list(e.get("cc"))]:
                    if a:
                        known.add(_addr(a).lower())
                        name = parseaddr(a)[0]
                        if name:
                            names.setdefault(_addr(a).lower(), name)
        state["_directory"] = sorted(known)
        state["_names"] = names
        state["_domains"] = sorted({a.split("@")[1] for a in state["mailboxes"]} | set(seed.get("domains", [])))
        state["_auto_replies"] = list(seed.get("auto_replies", []))
        state["_daily_send_limit"] = int(seed.get("daily_send_limit", 2000))
        state["_ooo_sent"] = []
        return state

    def default_actor(self, state: dict[str, Any]) -> str:
        return state["default"]

    def resolve_actor(self, state: dict[str, Any], identity: str) -> str:
        email = _addr(identity)
        if email not in state["mailboxes"]:
            raise ValueError(f"no mailbox for {identity} in this environment")
        return email

    def grading_view(self, state: dict[str, Any]) -> dict[str, Any]:
        """The default mailbox at the top level (``state: messages``), every mailbox under ``mailboxes``."""
        return {**state["mailboxes"][state["default"]], "mailboxes": state["mailboxes"]}

    def render(self, value: Any) -> str:
        if isinstance(value, dict) and isinstance(value.get("error"), dict):
            return f"Error: {value['error'].get('message')}"
        return super().render(value)

    def error_shape(self, status: int, message: str) -> Any:
        codes = {404: "NOT_FOUND", 429: "RESOURCE_EXHAUSTED", 500: "INTERNAL", 502: "UNAVAILABLE", 503: "UNAVAILABLE",
                 504: "DEADLINE_EXCEEDED"}
        return {"error": {"code": status, "message": message, "status": codes.get(status, "UNKNOWN")}}

    def fault_error(self, fault: Any) -> tuple[Any, int]:
        if fault.kind == "rate_limit":
            return {"error": {"code": 429, "message": "User-rate limit exceeded. Retry after "
                              f"{fault.retry_after}s", "status": "RESOURCE_EXHAUSTED"}}, 429
        if fault.kind == "server_error":
            return {"error": {"code": 500, "message": "Backend Error", "status": "INTERNAL"}}, 500
        return super().fault_error(fault)
