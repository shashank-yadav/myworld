"""gmail: world actions (triggered by environments, never by agents)."""

from __future__ import annotations

from typing import Any

from ...core.instance import Instance
from ...core.tools import ToolError, action
from .delivery import _apply_filters
from .model import _addr, _store


def _box(ctx: Instance, email: str) -> dict[str, Any]:
    box = ctx.state["mailboxes"].get(_addr(email))
    if box is None:
        raise ToolError({"error": {"code": 404, "message": f"no mailbox {email}"}})
    return box


@action("deliver_email")
def act_deliver_email(ctx: Instance, sender: str, subject: str, body: str, to: str | None = None,
                      cc: list[str] | None = None, attachments: list[dict] | None = None,
                      thread_subject: str | None = None, labels: list[str] | None = None) -> str:
    """An email arrives from outside (or from a colleague without an agent). Filters apply.
    ``to`` defaults to the world's default mailbox; ``thread_subject`` threads it with an existing conversation;
    ``labels`` adds category labels such as CATEGORY_PROMOTIONS."""
    to = to or ctx.state["default"]
    box = _box(ctx, to)
    thread = None
    if thread_subject:
        norm = thread_subject.lower().removeprefix("re: ")
        thread = next((m["threadId"] for m in sorted(box["messages"].values(), key=lambda m: int(m["internalDate"]))
                       if m["subject"].lower().removeprefix("re: ") == norm), None)
    m = _store(ctx, box, sender=sender, to=[to], cc=cc or [], bcc=[], subject=subject, body=body,
               labels=["INBOX", "UNREAD", *(labels or [])], date=ctx.now(), thread_id=thread,
               attachments=attachments or [])
    _apply_filters(box, m, ctx)
    return m["id"]


@action("set_search_lag")
def act_set_search_lag(ctx: Instance, seconds: int, mailbox: str | None = None) -> None:
    """Eventual consistency: new messages take ``seconds`` to become searchable (real Gmail lags too).
    An agent that "verifies" a send by searching may see nothing and send again."""
    for email in [mailbox] if mailbox else list(ctx.state["mailboxes"]):
        box = _box(ctx, email)
        box["_index_lag"] = int(seconds)
        box["_index_lag_since"] = int(ctx.now().timestamp() * 1000)  # mail already there stays searchable


@action("deliver_reply")
def act_deliver_reply(ctx: Instance, to: str, sender: str, subject: str, body: str, in_reply_to: str | None = None) -> str:
    """A reply arrives, threaded with the message it answers (bounces and auto-replies use this)."""
    box = _box(ctx, to)
    thread = next((m["threadId"] for m in box["messages"].values() if in_reply_to and m["messageId"] == in_reply_to), None)
    m = _store(ctx, box, sender=sender, to=[to], cc=[], bcc=[], subject=subject, body=body,
               labels=["INBOX", "UNREAD"], date=ctx.now(), thread_id=thread)
    _apply_filters(box, m, ctx)
    if sender.lower().split("<")[-1].rstrip(">") not in ctx.state["_directory"]:
        ctx.state["_directory"].append(_addr(sender).lower())
    return m["id"]
