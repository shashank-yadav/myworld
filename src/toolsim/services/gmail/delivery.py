"""gmail: delivery realism (bounces, auto-replies, quota)."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from ...core.instance import Instance
from ...core.tools import ToolError
from .model import _addr, _store
from .search import _matches

COMMON_DOMAINS = ["gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com"]


def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _bounce_reason(state: dict[str, Any], rcpt: str) -> str | None:
    """Why a recipient is undeliverable, or None. Company addresses must exist; domains one or two
    keystrokes away from a real one (acme.co, gmial.com) don't resolve."""
    rcpt = rcpt.lower()
    domain = rcpt.rsplit("@", 1)[-1]
    if domain in state["_domains"]:
        if rcpt not in state["_directory"]:
            return (f"Address not found\n\nYour message wasn't delivered to {rcpt} because the address couldn't "
                    "be found, or is unable to receive mail.")
        return None
    for real in state["_domains"] + COMMON_DOMAINS:
        if domain != real and _edit_distance(domain, real) <= 2:
            return (f"Address not found\n\nYour message wasn't delivered to {rcpt} because the domain {domain} "
                    "couldn't be found. Check for typos or unnecessary spaces and try again.")
    return None


def _check_quota(ctx: Instance, box: dict[str, Any], recipients: int) -> None:
    today = ctx.now().date()
    sent = sum(len(m["to"]) + len(m["cc"]) + len(m["bcc"]) for m in box["messages"].values() if "SENT" in m["labelIds"]
               and dt.datetime.fromtimestamp(int(m["internalDate"]) / 1000, dt.timezone.utc).date() == today)
    if sent + recipients > ctx.state["_daily_send_limit"]:
        raise ToolError({"error": {"code": 429, "message": "Daily user sending limit exceeded. Try again after "
                                   "the limit resets.", "status": "RESOURCE_EXHAUSTED"}}, status=429)


def _matches_rule(rule: dict[str, Any], msg: dict[str, Any], rcpt: str) -> bool:
    if _addr(rule.get("person", "")) != rcpt:
        return False
    w = rule.get("match") or {}
    if w.get("from") and w["from"].lower() not in msg["from"].lower():
        return False
    if w.get("subject~") and w["subject~"].lower() not in msg["subject"].lower():
        return False
    return not (w.get("body~") and w["body~"].lower() not in msg["body"].lower())


def _after_send(ctx: Instance, msg: dict[str, Any]) -> None:
    """What the world does after a send: bounces, out-of-office replies, colleagues answering."""
    state, sender = ctx.state, ctx.actor
    for rcpt in dict.fromkeys(_addr(a).lower() for a in [*msg["to"], *msg["cc"], *msg["bcc"]]):
        reason = _bounce_reason(state, rcpt)
        if reason:
            ctx.schedule(ctx.rng.randint(20, 120), "deliver_reply", {
                "to": sender, "sender": "Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
                "subject": "Delivery Status Notification (Failure)", "body": reason,
                "in_reply_to": msg["messageId"]}, reason=f"bounce for {rcpt}")
            continue
        for rule in state["_auto_replies"]:
            if not _matches_rule(rule, msg, rcpt):
                continue
            ooo = rule.get("out_of_office")
            key = f"{rcpt}->{sender}"
            if ooo and key in state["_ooo_sent"]:
                continue  # Gmail's vacation responder answers each sender once
            if ooo:
                state["_ooo_sent"].append(key)
            name = rule.get("name") or state["_names"].get(rcpt) or rcpt.split("@")[0].replace(".", " ").title()
            ctx.schedule(_delay(rule.get("delay", "+30s" if ooo else "+5m")), "deliver_reply", {
                "to": sender, "sender": f"{name} <{rcpt}>",
                "subject": ("Automatic reply: " if ooo else "Re: ") + msg["subject"].removeprefix("Re: "),
                "body": ooo or rule.get("reply", ""), "in_reply_to": msg["messageId"]},
                reason=f"{'out-of-office' if ooo else 'reply'} from {rcpt}")
            break


def _delay(v: Any) -> float:
    m = re.fullmatch(r"\+?(\d+(?:\.\d+)?)([smhd]?)", str(v).strip())
    if not m:
        raise ToolError({"error": {"code": 400, "message": f"invalid delay {v!r}"}})
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def _deliver(ctx: Instance, sent: dict[str, Any], sender_box: dict[str, Any]) -> list[str]:
    """Put a copy of a sent message in every recipient mailbox that exists in this world.
    The copy keeps the RFC 822 Message-ID, lands in the recipient's thread for the same
    conversation, and passes through the recipient's filters."""
    delivered = []
    sender = sender_box["user"]["email"].lower()
    refs = {m["messageId"] for m in sender_box["messages"].values() if m["threadId"] == sent["threadId"]}
    for rcpt in dict.fromkeys(_addr(a) for a in [*sent["to"], *sent["cc"], *sent["bcc"]]):
        box = ctx.state["mailboxes"].get(rcpt)
        if box is None or rcpt == sender:
            continue
        thread = next((m["threadId"] for m in box["messages"].values() if m["messageId"] in refs), None)
        copy_ = _store(ctx, box, sender=sent["from"], to=sent["to"], cc=sent["cc"], bcc=[], subject=sent["subject"],
                       body=sent["body"], html_body=sent.get("htmlBody"), labels=["INBOX", "UNREAD"], date=ctx.now(),
                       thread_id=thread, attachments=[{k: a[k] for k in ("filename", "mimeType", "size")}
                                                      for a in sent["attachments"]])
        copy_["messageId"] = sent["messageId"]
        _apply_filters(box, copy_, ctx)
        delivered.append(rcpt)
    return delivered


def _apply_filters(box: dict[str, Any], msg: dict[str, Any], ctx: Instance) -> None:
    for f in box["filters"].values():
        c = f["criteria"]
        terms = []
        for k in ("from", "to", "subject"):
            if c.get(k):
                terms.append(f'{k}:"{c[k]}"')
        if c.get("query"):
            terms.append(c["query"])
        if c.get("hasAttachment"):
            terms.append("has:attachment")
        if terms and _matches(box, msg, " ".join(terms), ctx.now()):
            a = f["action"]
            msg["labelIds"] = [l for l in dict.fromkeys(msg["labelIds"] + list(a.get("addLabelIds") or []))
                               if l not in (a.get("removeLabelIds") or [])]
