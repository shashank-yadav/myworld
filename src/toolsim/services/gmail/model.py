"""gmail: state model, constants and helpers shared by the tools."""

from __future__ import annotations

import datetime as dt
import functools
from email.utils import format_datetime, parseaddr
from typing import Any

from ...core.instance import Instance
from ...core.tools import ToolError

V1 = "2026-09-25.1"
V2 = "2026-09-25.2"
V3 = "2026-09-25.3"

SYSTEM_LABELS = ["INBOX", "SENT", "DRAFT", "SPAM", "TRASH", "UNREAD", "STARRED", "IMPORTANT",
                 "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS", "CATEGORY_UPDATES", "CATEGORY_FORUMS"]


def _not_found() -> ToolError:
    return ToolError({"error": {"code": 404, "message": "Requested entity was not found.", "status": "NOT_FOUND"}},
                     status=404)


def _invalid(message: str) -> ToolError:
    return ToolError({"error": {"code": 400, "message": message, "status": "INVALID_ARGUMENT"}}, status=400)


def _ms(t: dt.datetime) -> str:
    return str(int(t.timestamp() * 1000))


def _parse_time(v: Any, default: dt.datetime) -> dt.datetime:
    if not v:
        return default
    t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)

def _new_mailbox(ctx: Instance, seed: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    box: dict[str, Any] = {"user": user, "labels": {}, "messages": {}, "drafts": {}, "filters": {},
                           "downloads": [], "_threads": {}}
    for name in SYSTEM_LABELS:
        box["labels"][name] = {"id": name, "name": name, "type": "system"}
    if seed.get("contacts") is not None:  # saved Google Contacts (see people.py)
        box["_seed_contacts"] = list(seed["contacts"])
    for name in seed.get("labels", []):
        _new_label(ctx, box, name if isinstance(name, str) else name["name"])
    for e in sorted(seed.get("emails", []), key=lambda e: str(e.get("date") or "")):
        labels = []
        for lab in e.get("labels", ["INBOX"]):
            labels.append(lab if lab in box["labels"] else _label_by_name(box, lab) or _new_label(ctx, box, lab)["id"])
        _store(ctx, box, sender=e.get("from", "unknown@example.com"), to=_list(e.get("to")), cc=_list(e.get("cc")),
               bcc=_list(e.get("bcc")), subject=e.get("subject", ""), body=e.get("body", ""), labels=labels,
               date=_parse_time(e.get("date"), ctx.now()), thread_key=e.get("thread"),
               attachments=e.get("attachments") or [])
    return box


def _mb(ctx: Instance) -> dict[str, Any]:
    """The mailbox of whoever this call acts as."""
    return ctx.state["mailboxes"][ctx.actor]


def _list(v: Any) -> list[str]:
    if not v:
        return []
    return [v] if isinstance(v, str) else list(v)


def _label_by_name(state: dict[str, Any], name: str) -> str | None:
    return next((l["id"] for l in state["labels"].values() if l["name"].lower() == name.lower()), None)


def _new_label(ctx: Instance, state: dict[str, Any], name: str, message_vis: str = "show",
               label_vis: str = "labelShow") -> dict[str, Any]:
    label = {"id": f"Label_{ctx.next('label')}", "name": name, "type": "user",
             "messageListVisibility": message_vis, "labelListVisibility": label_vis}
    state["labels"][label["id"]] = label
    return label


def _store(ctx: Instance, state: dict[str, Any], *, sender: str, to: list[str], cc: list[str], bcc: list[str],
           subject: str, body: str, labels: list[str], date: dt.datetime, thread_key: str | None = None,
           thread_id: str | None = None, attachments: list[dict[str, Any]] | None = None,
           html_body: str | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
    mid = ctx.hex(16)
    if thread_id is None:
        if thread_key and thread_key in state["_threads"]:
            thread_id = state["_threads"][thread_key]
        else:
            thread_id = mid
            if thread_key:
                state["_threads"][thread_key] = thread_id
    atts = [{"attachmentId": f"ANGjdJ{ctx.token(20, 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-')}",
             "filename": a.get("filename", "file"), "mimeType": a.get("mimeType", "application/octet-stream"),
             "size": int(a.get("size", 1024)), **({"data": a["data"]} if a.get("data") else {})}
            for a in attachments or []]
    msg = {"id": mid, "threadId": thread_id, "labelIds": list(dict.fromkeys(labels)), "from": sender, "to": to,
           "cc": cc, "bcc": bcc, "subject": subject, "body": body, "htmlBody": html_body,
           "date": format_datetime(date), "internalDate": _ms(date), "snippet": " ".join(body.split())[:140],
           "attachments": atts, "messageId": f"<{ctx.hex(24)}@mail.gmail.com>"}
    if headers:  # In-Reply-To / References, as sent through the REST API
        msg["headers"] = dict(headers)
    state["messages"][mid] = msg
    state["_history"] = state.get("_history", 1000) + 1  # Gmail historyId: this mailbox changed
    state.setdefault("_hist", {})[mid] = state["_history"]
    return msg


def _get(state: dict[str, Any], message_id: str) -> dict[str, Any]:
    msg = state["messages"].get(message_id)
    if msg is None:
        raise _not_found()
    return msg


def _check_labels(state: dict[str, Any], ids: list[str] | None) -> list[str]:
    ids = ids or []
    for lid in ids:
        if lid not in state["labels"]:
            raise _invalid(f"Invalid label: {lid}")
    return ids


@functools.lru_cache(maxsize=65536)
def _addr(s: str) -> str:
    return parseaddr(s)[1].lower() or s.lower()
