"""Gmail.

Tool names, parameters and response text follow the most widely used Gmail MCP server
(GongRzhe/Gmail-MCP-Server). The state model follows the Gmail API: messages with label IDs,
threads, system and user labels, drafts and filters.

Seed format::

    user: {email: alex@acme.com, name: Alex Rivera}
    labels: [Receipts, Clients]                      # user labels
    emails:
      - {from: "John Park <john@acme.com>", to: [alex@acme.com], subject: Q4 plan,
         body: "Can we meet next week?", date: "2026-09-19T16:02:00Z",
         labels: [INBOX, UNREAD], thread: q4,        # emails sharing a thread key share a threadId
         attachments: [{filename: plan.pdf, mimeType: application/pdf, size: 48213}]}
"""

from __future__ import annotations

import datetime as dt
import re
import shlex
from email.utils import format_datetime, parseaddr
from typing import Annotated, Any, Literal

from ..core.instance import Instance, Service
from ..core.tools import ToolError, tool

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


class Gmail(Service):
    name = "gmail"
    title = "Gmail"
    description = "Simulated Gmail mailbox. Behaves like the Gmail MCP server; nothing is really sent."

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
        user = seed.get("user") or {"email": "alex@acme.com", "name": "Alex Rivera"}
        state: dict[str, Any] = {"user": user, "labels": {}, "messages": {}, "drafts": {}, "filters": {},
                                 "downloads": [], "_threads": {}}
        for name in SYSTEM_LABELS:
            state["labels"][name] = {"id": name, "name": name, "type": "system"}
        for name in seed.get("labels", []):
            _new_label(ctx, state, name if isinstance(name, str) else name["name"])
        for e in sorted(seed.get("emails", []), key=lambda e: str(e.get("date") or "")):
            labels = []
            for lab in e.get("labels", ["INBOX"]):
                labels.append(lab if lab in state["labels"] else _label_by_name(state, lab)
                              or _new_label(ctx, state, lab)["id"])
            _store(ctx, state, sender=e.get("from", "unknown@example.com"), to=_list(e.get("to")),
                   cc=_list(e.get("cc")), bcc=_list(e.get("bcc")), subject=e.get("subject", ""),
                   body=e.get("body", ""), labels=labels, date=_parse_time(e.get("date"), ctx.now()),
                   thread_key=e.get("thread"), attachments=e.get("attachments") or [])
        return state

    def render(self, value: Any) -> str:
        if isinstance(value, dict) and isinstance(value.get("error"), dict):
            return f"Error: {value['error'].get('message')}"
        return super().render(value)

    def fault_error(self, fault: Any) -> tuple[Any, int]:
        if fault.kind == "rate_limit":
            return {"error": {"code": 429, "message": "User-rate limit exceeded. Retry after "
                              f"{fault.retry_after}s", "status": "RESOURCE_EXHAUSTED"}}, 429
        if fault.kind == "server_error":
            return {"error": {"code": 500, "message": "Backend Error", "status": "INTERNAL"}}, 500
        return super().fault_error(fault)


# -- state helpers -----------------------------------------------------------------------

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
           html_body: str | None = None) -> dict[str, Any]:
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
             "size": int(a.get("size", 1024))} for a in attachments or []]
    msg = {"id": mid, "threadId": thread_id, "labelIds": list(dict.fromkeys(labels)), "from": sender, "to": to,
           "cc": cc, "bcc": bcc, "subject": subject, "body": body, "htmlBody": html_body,
           "date": format_datetime(date), "internalDate": _ms(date), "snippet": " ".join(body.split())[:140],
           "attachments": atts, "messageId": f"<{ctx.hex(24)}@mail.gmail.com>"}
    state["messages"][mid] = msg
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


def _addr(s: str) -> str:
    return parseaddr(s)[1].lower() or s.lower()


# -- search -------------------------------------------------------------------------------

_UNITS = {"d": 1, "m": 30, "y": 365}


def _matches(state: dict[str, Any], msg: dict[str, Any], query: str, now: dt.datetime) -> bool:
    """A faithful subset of Gmail search: from/to/cc/subject/label/in/is/has/newer_than/older_than/
    after/before/filename, bare words and "phrases", OR, and -negation. Like Gmail, spam and trash
    are excluded unless the query asks for them."""
    try:
        tokens = shlex.split(query)
    except ValueError:
        tokens = query.split()
    if not any(t.lower() in ("in:spam", "in:trash", "in:anywhere", "label:spam", "label:trash") for t in tokens):
        if {"SPAM", "TRASH"} & set(msg["labelIds"]):
            return False
    clauses: list[list[str]] = []  # AND of ORs: "a OR b c" -> (a|b) & c
    pending_or = False
    for t in tokens:
        if t == "OR" and clauses:
            pending_or = True
        elif pending_or:
            clauses[-1].append(t)
            pending_or = False
        else:
            clauses.append([t])
    return all(any(_term(state, msg, a, now) for a in c) for c in clauses)


def _term(state: dict[str, Any], msg: dict[str, Any], term: str, now: dt.datetime) -> bool:
    if term.startswith("-") and len(term) > 1:
        return not _term(state, msg, term[1:], now)
    key, _, val = term.partition(":")
    key, v = key.lower(), val.lower().strip('"')
    labels = set(msg["labelIds"])
    sent = dt.datetime.fromtimestamp(int(msg["internalDate"]) / 1000, dt.timezone.utc)
    if not val:
        text = f"{msg['subject']} {msg['body']} {msg['from']} {' '.join(msg['to'])}".lower()
        return term.lower().strip('"') in text
    if key == "from":
        return v in msg["from"].lower()
    if key in ("to", "cc", "bcc"):
        return any(v in a.lower() for a in msg[key]) or (key == "to" and any(v in a.lower() for a in msg["cc"]))
    if key == "subject":
        return v in msg["subject"].lower()
    if key in ("in", "label"):
        if v == "anywhere":
            return True
        lid = v.upper() if v.upper() in state["labels"] else _label_by_name(state, v)
        return lid in labels
    if key == "is":
        return {"unread": "UNREAD" in labels, "read": "UNREAD" not in labels, "starred": "STARRED" in labels,
                "important": "IMPORTANT" in labels}.get(v, False)
    if key == "has":
        return v == "attachment" and bool(msg["attachments"])
    if key == "filename":
        return any(v in a["filename"].lower() for a in msg["attachments"])
    if key in ("newer_than", "older_than"):
        m = re.fullmatch(r"(\d+)([dmy])", v)
        if not m:
            return False
        cutoff = now - dt.timedelta(days=int(m.group(1)) * _UNITS[m.group(2)])
        return sent >= cutoff if key == "newer_than" else sent < cutoff
    if key in ("after", "before"):
        try:
            day = dt.datetime.strptime(v.replace("-", "/"), "%Y/%m/%d").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            return False
        return sent >= day if key == "after" else sent < day
    if key == "category":
        return f"CATEGORY_{v.upper()}" in labels
    return term.lower() in f"{msg['subject']} {msg['body']}".lower()


# -- tools -------------------------------------------------------------------------------

@tool("send_email", destructive=False)
def send_email(ctx: Instance,
               to: Annotated[list[str], "List of recipient email addresses"],
               subject: Annotated[str, "Email subject"],
               body: Annotated[str, "Email body content (used for text/plain or when htmlBody not provided)"],
               cc: Annotated[list[str] | None, "List of CC recipients"] = None,
               bcc: Annotated[list[str] | None, "List of BCC recipients"] = None,
               mimeType: Annotated[Literal["text/plain", "text/html", "multipart/alternative"] | None, "Email content type"] = None,
               htmlBody: Annotated[str | None, "HTML version of the email body"] = None,
               attachments: Annotated[list[str] | None, "List of file paths to attach"] = None,
               threadId: Annotated[str | None, "Thread ID to reply to"] = None,
               inReplyTo: Annotated[str | None, "Message ID being replied to"] = None) -> str:
    """Sends a new email"""
    s = ctx.state
    if not to:
        raise _invalid("Recipient address required")
    for a in [*to, *(cc or []), *(bcc or [])]:
        if "@" not in _addr(a):
            raise _invalid(f"Invalid To header: {a}")
    if threadId and not any(m["threadId"] == threadId for m in s["messages"].values()):
        raise _not_found()
    user = s["user"]
    msg = _store(ctx, s, sender=f"{user['name']} <{user['email']}>", to=to, cc=cc or [], bcc=bcc or [],
                 subject=subject, body=body, html_body=htmlBody, labels=["SENT"], date=ctx.now(), thread_id=threadId,
                 attachments=[{"filename": p.rsplit("/", 1)[-1]} for p in attachments or []])
    # mail to yourself lands in your inbox too
    if user["email"].lower() in {_addr(a) for a in [*to, *(cc or [])]}:
        msg["labelIds"] += ["INBOX", "UNREAD"]
    return f"Email sent successfully with ID: {msg['id']}"


@tool("draft_email")
def draft_email(ctx: Instance,
                to: Annotated[list[str], "List of recipient email addresses"],
                subject: Annotated[str, "Email subject"],
                body: Annotated[str, "Email body content"],
                cc: Annotated[list[str] | None, "List of CC recipients"] = None,
                bcc: Annotated[list[str] | None, "List of BCC recipients"] = None,
                mimeType: Annotated[Literal["text/plain", "text/html", "multipart/alternative"] | None, "Email content type"] = None,
                htmlBody: Annotated[str | None, "HTML version of the email body"] = None,
                attachments: Annotated[list[str] | None, "List of file paths to attach"] = None,
                threadId: Annotated[str | None, "Thread ID to reply to"] = None,
                inReplyTo: Annotated[str | None, "Message ID being replied to"] = None) -> str:
    """Draft a new email"""
    s, user = ctx.state, ctx.state["user"]
    msg = _store(ctx, s, sender=f"{user['name']} <{user['email']}>", to=to, cc=cc or [], bcc=bcc or [],
                 subject=subject, body=body, html_body=htmlBody, labels=["DRAFT"], date=ctx.now(), thread_id=threadId)
    did = "r" + "".join(ctx.rng.choice("0123456789") for _ in range(19))
    s["drafts"][did] = {"id": did, "messageId": msg["id"]}
    return f"Email draft created successfully with ID: {did}"


@tool("read_email", read_only=True)
def read_email(ctx: Instance, messageId: Annotated[str, "ID of the email message to retrieve"]) -> str:
    """Retrieves the content of a specific email"""
    m = _get(ctx.state, messageId)
    out = (f"Thread ID: {m['threadId']}\nSubject: {m['subject']}\nFrom: {m['from']}\nTo: {', '.join(m['to'])}\n"
           + (f"Cc: {', '.join(m['cc'])}\n" if m["cc"] else "") + f"Date: {m['date']}\n\n{m['body']}")
    if m["attachments"]:
        out += f"\n\nAttachments ({len(m['attachments'])}):\n" + "\n".join(
            f"- {a['filename']} ({a['mimeType']}, {a['size'] // 1024} KB, ID: {a['attachmentId']})" for a in m["attachments"])
    return out


@tool("download_attachment")
def download_attachment(ctx: Instance,
                        messageId: Annotated[str, "ID of the email message containing the attachment"],
                        attachmentId: Annotated[str, "ID of the attachment to download"],
                        filename: Annotated[str | None, "Filename to save the attachment as"] = None,
                        savePath: Annotated[str | None, "Directory path to save the attachment"] = None) -> str:
    """Downloads an email attachment to a specified location"""
    m = _get(ctx.state, messageId)
    att = next((a for a in m["attachments"] if a["attachmentId"] == attachmentId), None)
    if att is None:
        raise _not_found()
    path = f"{(savePath or '.').rstrip('/')}/{filename or att['filename']}"
    ctx.state["downloads"].append({"messageId": messageId, "attachmentId": attachmentId, "path": path})
    return (f"Attachment downloaded successfully:\nFile: {filename or att['filename']}\nSize: {att['size']} bytes\n"
            f"Saved to: {path}")


@tool("search_emails", read_only=True)
def search_emails(ctx: Instance,
                  query: Annotated[str, "Gmail search query (e.g., 'from:example@gmail.com')"],
                  maxResults: Annotated[int | None, "Maximum number of results to return"] = None) -> str:
    """Searches for emails using Gmail search syntax"""
    s = ctx.state
    hits = [m for m in s["messages"].values() if _matches(s, m, query, ctx.now())]
    hits.sort(key=lambda m: int(m["internalDate"]), reverse=True)
    hits = hits[: maxResults or 10]
    return "\n".join(f"ID: {m['id']}\nSubject: {m['subject']}\nFrom: {m['from']}\nDate: {m['date']}\n" for m in hits)


@tool("modify_email", idempotent=True)
def modify_email(ctx: Instance,
                 messageId: Annotated[str, "ID of the email message to modify"],
                 labelIds: Annotated[list[str] | None, "List of label IDs to apply"] = None,
                 addLabelIds: Annotated[list[str] | None, "List of label IDs to add to the message"] = None,
                 removeLabelIds: Annotated[list[str] | None, "List of label IDs to remove from the message"] = None) -> str:
    """Modifies email labels (move to different folders)"""
    s = ctx.state
    m = _get(s, messageId)
    add = _check_labels(s, (addLabelIds or []) + (labelIds or []))
    remove = _check_labels(s, removeLabelIds)
    m["labelIds"] = [l for l in dict.fromkeys(m["labelIds"] + add) if l not in remove]
    return f"Email {messageId} labels updated successfully"


@tool("delete_email", destructive=True, idempotent=True)
def delete_email(ctx: Instance, messageId: Annotated[str, "ID of the email message to delete"]) -> str:
    """Permanently deletes an email"""
    _get(ctx.state, messageId)
    del ctx.state["messages"][messageId]
    return f"Email {messageId} deleted successfully"


@tool("list_email_labels", read_only=True)
def list_email_labels(ctx: Instance) -> str:
    """Retrieves all available Gmail labels"""
    labels = list(ctx.state["labels"].values())
    system = [l for l in labels if l["type"] == "system"]
    user = [l for l in labels if l["type"] == "user"]
    fmt = lambda l: f"ID: {l['id']}\nName: {l['name']}\n"  # noqa: E731
    return (f"Found {len(labels)} labels ({len(system)} system, {len(user)} user):\n\n"
            "System Labels:\n" + "\n".join(map(fmt, system)) + "\nUser Labels:\n" + "\n".join(map(fmt, user)))


@tool("batch_modify_emails", idempotent=True)
def batch_modify_emails(ctx: Instance,
                        messageIds: Annotated[list[str], "List of message IDs to modify"],
                        addLabelIds: Annotated[list[str] | None, "List of label IDs to add to all messages"] = None,
                        removeLabelIds: Annotated[list[str] | None, "List of label IDs to remove from all messages"] = None,
                        batchSize: Annotated[int | None, "Number of messages to process in each batch (default: 50)"] = 50) -> str:
    """Modifies labels for multiple emails in batches"""
    s = ctx.state
    add, remove = _check_labels(s, addLabelIds), _check_labels(s, removeLabelIds)
    ok, failed = [], []
    for mid in messageIds:
        m = s["messages"].get(mid)
        if m is None:
            failed.append(mid)
            continue
        m["labelIds"] = [l for l in dict.fromkeys(m["labelIds"] + add) if l not in remove]
        ok.append(mid)
    out = f"Batch label modification complete.\nSuccessfully processed: {len(ok)} messages\n"
    if failed:
        out += f"Failed to process: {len(failed)} messages\n\nFailed message IDs:\n" + "\n".join(
            f"- {m[:16]}... (Requested entity was not found.)" for m in failed)
    return out


@tool("batch_delete_emails", destructive=True)
def batch_delete_emails(ctx: Instance,
                        messageIds: Annotated[list[str], "List of message IDs to delete"],
                        batchSize: Annotated[int | None, "Number of messages to process in each batch (default: 50)"] = 50) -> str:
    """Permanently deletes multiple emails in batches"""
    s = ctx.state
    ok = [m for m in messageIds if s["messages"].pop(m, None) is not None]
    failed = [m for m in messageIds if m not in ok]
    out = f"Batch delete operation complete.\nSuccessfully deleted: {len(ok)} messages\n"
    if failed:
        out += f"Failed to delete: {len(failed)} messages\n\nFailed message IDs:\n" + "\n".join(
            f"- {m[:16]}... (Requested entity was not found.)" for m in failed)
    return out


def _label_text(prefix: str, l: dict[str, Any]) -> str:
    return f"{prefix}\nID: {l['id']}\nName: {l['name']}\nType: {l['type']}"


@tool("create_label")
def create_label(ctx: Instance,
                 name: Annotated[str, "Name for the new label"],
                 messageListVisibility: Annotated[Literal["show", "hide"] | None, "Whether to show or hide the label in the message list"] = None,
                 labelListVisibility: Annotated[Literal["labelShow", "labelShowIfUnread", "labelHide"] | None, "Visibility of the label in the label list"] = None) -> str:
    """Creates a new Gmail label"""
    s = ctx.state
    if _label_by_name(s, name):
        raise ToolError({"error": {"code": 409, "message": "Label name exists or conflicts", "status": "ALREADY_EXISTS"}},
                        status=409)
    return _label_text("Label created successfully:", _new_label(ctx, s, name, messageListVisibility or "show",
                                                                 labelListVisibility or "labelShow"))


@tool("update_label", idempotent=True)
def update_label(ctx: Instance,
                 id: Annotated[str, "ID of the label to update"],
                 name: Annotated[str | None, "New name for the label"] = None,
                 messageListVisibility: Annotated[Literal["show", "hide"] | None, "Whether to show or hide the label in the message list"] = None,
                 labelListVisibility: Annotated[Literal["labelShow", "labelShowIfUnread", "labelHide"] | None, "Visibility of the label in the label list"] = None) -> str:
    """Updates an existing Gmail label"""
    l = ctx.state["labels"].get(id)
    if l is None:
        raise _not_found()
    if l["type"] == "system":
        raise _invalid("Invalid update request: system labels can't be modified")
    for k, v in (("name", name), ("messageListVisibility", messageListVisibility),
                 ("labelListVisibility", labelListVisibility)):
        if v is not None:
            l[k] = v
    return _label_text("Label updated successfully:", l)


@tool("delete_label", destructive=True, idempotent=True)
def delete_label(ctx: Instance, id: Annotated[str, "ID of the label to delete"]) -> str:
    """Deletes a Gmail label"""
    s = ctx.state
    l = s["labels"].get(id)
    if l is None:
        raise _not_found()
    if l["type"] == "system":
        raise _invalid("Invalid delete request: system labels can't be deleted")
    del s["labels"][id]
    for m in s["messages"].values():
        m["labelIds"] = [x for x in m["labelIds"] if x != id]
    return f'Label "{l["name"]}" (ID: {id}) deleted successfully.'


@tool("get_or_create_label", idempotent=True)
def get_or_create_label(ctx: Instance,
                        name: Annotated[str, "Name of the label to get or create"],
                        messageListVisibility: Annotated[Literal["show", "hide"] | None, "Whether to show or hide the label in the message list"] = None,
                        labelListVisibility: Annotated[Literal["labelShow", "labelShowIfUnread", "labelHide"] | None, "Visibility of the label in the label list"] = None) -> str:
    """Gets an existing label by name or creates it if it doesn't exist"""
    s = ctx.state
    existing = _label_by_name(s, name)
    if existing:
        return _label_text("Successfully found existing label:", s["labels"][existing])
    return _label_text("Successfully created new label:", _new_label(ctx, s, name, messageListVisibility or "show",
                                                                     labelListVisibility or "labelShow"))


def _filter_text(f: dict[str, Any]) -> str:
    crit = ", ".join(f"{k}: {v}" for k, v in f["criteria"].items())
    act = ", ".join(f"{k}: {v}" for k, v in f["action"].items())
    return f"ID: {f['id']}\nCriteria: {crit}\nActions: {act}\n"


@tool("create_filter")
def create_filter(ctx: Instance,
                  criteria: Annotated[dict, "Criteria for matching emails (from, to, subject, query, negatedQuery, hasAttachment, size, sizeComparison)"],
                  action: Annotated[dict, "Actions to perform on matching emails (addLabelIds, removeLabelIds, forward)"]) -> str:
    """Creates a new Gmail filter with custom criteria and actions"""
    s = ctx.state
    if not criteria:
        raise _invalid("Filter criteria must not be empty")
    _check_labels(s, action.get("addLabelIds"))
    _check_labels(s, action.get("removeLabelIds"))
    fid = "ANe1Bm" + ctx.token(20, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
    s["filters"][fid] = {"id": fid, "criteria": dict(criteria), "action": dict(action)}
    return "Filter created successfully:\n" + _filter_text(s["filters"][fid])


@tool("list_filters", read_only=True)
def list_filters(ctx: Instance) -> str:
    """Retrieves all Gmail filters"""
    fs = list(ctx.state["filters"].values())
    if not fs:
        return "No filters found."
    return f"Found {len(fs)} filters:\n\n" + "\n".join(_filter_text(f) for f in fs)


@tool("get_filter", read_only=True)
def get_filter(ctx: Instance, filterId: Annotated[str, "ID of the filter to retrieve"]) -> str:
    """Gets details of a specific Gmail filter"""
    f = ctx.state["filters"].get(filterId)
    if f is None:
        raise _not_found()
    return "Filter details:\n" + _filter_text(f)


@tool("delete_filter", destructive=True, idempotent=True)
def delete_filter(ctx: Instance, filterId: Annotated[str, "ID of the filter to delete"]) -> str:
    """Deletes a Gmail filter"""
    if ctx.state["filters"].pop(filterId, None) is None:
        raise _not_found()
    return f"Filter {filterId} deleted successfully."


@tool("create_filter_from_template")
def create_filter_from_template(ctx: Instance,
                                template: Annotated[Literal["fromSender", "withSubject", "withAttachments", "largeEmails", "containingText", "mailingList"], "Pre-defined filter template to use"],
                                parameters: Annotated[dict, "Template-specific parameters (senderEmail, subjectText, searchText, listIdentifier, sizeInBytes, labelIds, archive, markAsRead)"]) -> str:
    """Creates a filter using a pre-defined template for common scenarios"""
    p = parameters
    criteria = {
        "fromSender": lambda: {"from": p.get("senderEmail")},
        "withSubject": lambda: {"subject": p.get("subjectText")},
        "withAttachments": lambda: {"hasAttachment": True},
        "largeEmails": lambda: {"size": p.get("sizeInBytes"), "sizeComparison": "larger"},
        "containingText": lambda: {"query": p.get("searchText")},
        "mailingList": lambda: {"query": f"list:{p.get('listIdentifier')}"},
    }[template]()
    if any(v is None for v in criteria.values()):
        raise _invalid(f"Missing required parameter for template {template}")
    action: dict[str, Any] = {}
    if p.get("labelIds"):
        action["addLabelIds"] = p["labelIds"]
    remove = (["INBOX"] if p.get("archive") else []) + (["UNREAD"] if p.get("markAsRead") else [])
    if remove:
        action["removeLabelIds"] = remove
    return create_filter.fn(ctx, criteria=criteria, action=action)


Gmail.tools = [send_email, draft_email, read_email, download_attachment, search_emails, modify_email, delete_email,
               list_email_labels, batch_modify_emails, batch_delete_emails, create_label, update_label, delete_label,
               get_or_create_label, create_filter, list_filters, get_filter, delete_filter, create_filter_from_template]
