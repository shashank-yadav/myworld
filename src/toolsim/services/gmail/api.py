"""gmail: the Gmail REST API (v1), as ``gog``, Google's client libraries and ``curl`` see it.

Messages come back in Gmail's own shapes: ``payload`` MIME trees with base64url bodies,
``format=minimal|metadata|full|raw``, thread and history ids, drafts, labels with counts,
send-as settings and filters. Sending takes a real RFC 2822 message (``raw``), so whatever a
client builds (HTML alternatives, attachments, In-Reply-To) is what lands in the world.
"""

from __future__ import annotations

import base64
import hashlib
import html
import quopri
import re
from email import message_from_bytes, policy
from email.utils import getaddresses
from typing import Any

from ...api import Request, Response, operation
from ...api.google import b64url, error, page, select, unb64url
from ...core.instance import Instance
from .delivery import _send
from .model import SYSTEM_LABELS, _addr, _label_by_name, _new_label, _store
from .search import _matches

HOSTS = ("gmail.googleapis.com", "www.googleapis.com")
BASE = "/gmail/v1/users/{userId}"
VISIBLE = {"INBOX": ("labelShow", "show"), "SENT": ("labelShow", "hide"), "DRAFT": ("labelShow", "hide"),
           "SPAM": ("labelHide", "hide"), "TRASH": ("labelHide", "hide"), "STARRED": ("labelShow", "hide"),
           "IMPORTANT": ("labelShow", "hide"), "UNREAD": ("labelShow", "hide")}


def op(op_id: str, method: str, path: str, **kw: Any):  # noqa: ANN201
    return operation(op_id, method, BASE + path, hosts=HOSTS, **kw)


# -- whose mailbox --------------------------------------------------------------------------------

def _box(ctx: Instance, req: Request) -> dict[str, Any]:
    uid = req.params.get("userId", "me")
    if uid != "me" and uid.lower() != ctx.actor:
        raise error(403, f"Delegation denied for {ctx.actor}", "forbidden")
    return ctx.state["mailboxes"][ctx.actor]


def _msg(box: dict[str, Any], mid: str) -> dict[str, Any]:
    m = box["messages"].get(mid)
    if m is None:
        raise error(404, "Requested entity was not found.", "notFound")
    return m


def _hid(box: dict[str, Any], mid: str | None = None) -> str:
    if mid is None:
        return str(box.get("_history", 1000))
    return str(box.get("_hist", {}).get(mid, 1000))


def _touch(box: dict[str, Any], *mids: str) -> None:
    """A change Gmail's history would record (labels, trash): bump the mailbox's historyId."""
    for mid in mids:
        box["_history"] = box.get("_history", 1000) + 1
        box.setdefault("_hist", {})[mid] = box["_history"]
        box.setdefault("_changes", []).append({"id": box["_history"], "message": mid})


# -- messages as MIME ------------------------------------------------------------------------------

def _boundary(seed: str, n: int = 0) -> str:
    return "000000000000" + hashlib.sha1(f"{seed}:{n}".encode()).hexdigest()[:16]


def _filler(att: dict[str, Any]) -> bytes:
    """Stand-in bytes for a seeded attachment (right type and size; the content is never read)."""
    head = {"application/pdf": b"%PDF-1.4\n", "image/png": b"\x89PNG\r\n\x1a\n", "text/csv": b"id,amount\n"}.get(
        att.get("mimeType", ""), b"")
    size = max(int(att.get("size", 1024)), len(head))
    body = hashlib.sha256(att["attachmentId"].encode()).digest()
    return (head + body * (size // len(body) + 1))[:size]


def _att_bytes(att: dict[str, Any]) -> bytes:
    return unb64url(att["data"]) if att.get("data") else _filler(att)


def _text_part(text: str, subtype: str) -> dict[str, Any]:
    data = text.encode()
    cte = "7bit" if data.isascii() and all(len(line) < 990 for line in text.splitlines()) else "quoted-printable"
    return {"mimeType": f"text/{subtype}", "filename": "",
            "headers": [("Content-Type", f'text/{subtype}; charset="UTF-8"'), ("Content-Transfer-Encoding", cte)],
            "data": data}


def _tree(box: dict[str, Any], m: dict[str, Any]) -> dict[str, Any]:
    """The message's MIME structure (the same tree renders ``payload`` and ``raw``)."""
    owner = box["user"]["email"]
    outgoing = "SENT" in m["labelIds"] or "DRAFT" in m["labelIds"]
    headers: list[tuple[str, str]] = []
    if not outgoing:
        headers += [("Delivered-To", owner),
                    ("Received", f"by 2002:a05:6a10:{int(m['internalDate']) % 65536:x} with SMTP id "
                                 f"{m['id'][:12]}; {m['date']}")]
    headers += [("MIME-Version", "1.0"), ("Date", m["date"]), ("Message-ID", m["messageId"]),
                ("Subject", m["subject"]), ("From", m["from"])]
    if m["to"]:
        headers.append(("To", ", ".join(m["to"])))
    if m["cc"]:
        headers.append(("Cc", ", ".join(m["cc"])))
    if m["bcc"] and outgoing:
        headers.append(("Bcc", ", ".join(m["bcc"])))
    extra = m.get("headers") or {}
    if not extra and "SENT" in m["labelIds"]:
        prev = _previous(box, m)
        if prev:
            extra = {"In-Reply-To": prev["messageId"], "References": prev["messageId"]}
    for k in ("In-Reply-To", "References"):
        if extra.get(k):
            headers.append((k, extra[k]))
    body: dict[str, Any] = _text_part(m["body"], "plain")
    if m.get("htmlBody"):
        body = {"mimeType": "multipart/alternative", "boundary": _boundary(m["id"], 1),
                "parts": [body, _text_part(m["htmlBody"], "html")]}
    if m["attachments"]:
        parts = [body]
        for a in m["attachments"]:
            parts.append({"mimeType": a["mimeType"], "filename": a["filename"], "attachment": a,
                          "headers": [("Content-Type", f'{a["mimeType"]}; name="{a["filename"]}"'),
                                      ("Content-Disposition", f'attachment; filename="{a["filename"]}"'),
                                      ("Content-Transfer-Encoding", "base64"),
                                      ("X-Attachment-Id", "f_" + a["attachmentId"][6:16].lower())]})
        body = {"mimeType": "multipart/mixed", "boundary": _boundary(m["id"], 0), "parts": parts}
    root = dict(body)
    own = root.get("headers") or []
    if root["mimeType"].startswith("multipart/"):
        own = [("Content-Type", f'{root["mimeType"]}; boundary="{root["boundary"]}"')]
    root["headers"] = headers + own
    return root


def _previous(box: dict[str, Any], m: dict[str, Any]) -> dict[str, Any] | None:
    earlier = [x for x in box["messages"].values() if x["threadId"] == m["threadId"]
               and int(x["internalDate"]) < int(m["internalDate"])]
    return max(earlier, key=lambda x: int(x["internalDate"])) if earlier else None


def _payload(node: dict[str, Any], part_id: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"partId": part_id, "mimeType": node["mimeType"], "filename": node.get("filename", ""),
                           "headers": [{"name": k, "value": v} for k, v in node.get("headers", [])]}
    if node["mimeType"].startswith("multipart/"):
        if part_id:  # nested multiparts carry only their Content-Type
            out["headers"] = [{"name": "Content-Type",
                               "value": f'{node["mimeType"]}; boundary="{node["boundary"]}"'}]
        out["body"] = {"size": 0}
        out["parts"] = [_payload(p, f"{part_id}.{i}" if part_id else str(i)) for i, p in enumerate(node["parts"])]
    elif node.get("attachment"):
        a = node["attachment"]
        out["body"] = {"attachmentId": a["attachmentId"], "size": int(a["size"])}
    else:
        out["body"] = {"size": len(node["data"]), "data": b64url(node["data"])}
    return out


def _raw(node: dict[str, Any]) -> bytes:
    lines = [f"{k}: {v}" for k, v in node.get("headers", [])]
    if node["mimeType"].startswith("multipart/"):
        if not any(k == "Content-Type" for k, _ in node.get("headers", [])):
            lines.append(f'Content-Type: {node["mimeType"]}; boundary="{node["boundary"]}"')
        out = "\r\n".join(lines).encode() + b"\r\n\r\n"
        for p in node["parts"]:
            out += f"--{node['boundary']}\r\n".encode() + _raw(p) + b"\r\n"
        return out + f"--{node['boundary']}--".encode()
    if node.get("attachment"):
        data = _att_bytes(node["attachment"])
        enc = base64.encodebytes(data).replace(b"\n", b"\r\n")
        return "\r\n".join(lines).encode() + b"\r\n\r\n" + enc
    data = node["data"]
    if dict(node.get("headers", [])).get("Content-Transfer-Encoding") == "quoted-printable":
        data = quopri.encodestring(data).replace(b"\n", b"\r\n")
    return "\r\n".join(lines).encode() + b"\r\n\r\n" + data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def _snippet(m: dict[str, Any]) -> str:
    return html.escape(m["snippet"], quote=True).replace("&#x27;", "&#39;")


def _message(box: dict[str, Any], m: dict[str, Any], fmt: str = "full", headers: list[str] | None = None
             ) -> dict[str, Any]:
    tree = _tree(box, m)
    raw = _raw(tree)
    out: dict[str, Any] = {"id": m["id"], "threadId": m["threadId"], "labelIds": list(m["labelIds"]),
                           "snippet": _snippet(m), "sizeEstimate": len(raw), "historyId": _hid(box, m["id"]),
                           "internalDate": m["internalDate"]}
    if fmt == "raw":
        out["raw"] = b64url(raw)
    elif fmt == "metadata":
        want = {h.lower() for h in headers or []}
        hs = [{"name": k, "value": v} for k, v in tree["headers"] if not want or k.lower() in want]
        out["payload"] = {"mimeType": tree["mimeType"], "headers": hs}
    elif fmt == "full":
        out["payload"] = _payload(tree)
    return out


def _format(req: Request) -> str:
    fmt = (req.arg("format") or "full").lower()
    if fmt not in ("minimal", "full", "raw", "metadata"):
        raise error(400, f"Invalid value at 'format' ({fmt})", location="format")
    return fmt


# -- listing and search ----------------------------------------------------------------------------

def _visible(ctx: Instance, box: dict[str, Any]) -> list[dict[str, Any]]:
    """What search can see: the index may lag behind new mail (set_search_lag)."""
    lag = box.get("_index_lag", 0)
    if not lag:
        return list(box["messages"].values())
    cutoff = int((ctx.now().timestamp() - lag) * 1000)
    since = box.get("_index_lag_since", 0)
    return [m for m in box["messages"].values() if int(m["internalDate"]) <= since or int(m["internalDate"]) <= cutoff]


def _hits(ctx: Instance, box: dict[str, Any], req: Request) -> list[dict[str, Any]]:
    q = (req.arg("q") or "").strip()
    labels = req.args("labelIds")
    spam_trash = req.bool_arg("includeSpamTrash")
    pool = _visible(ctx, box) if q else list(box["messages"].values())
    out = []
    for m in pool:
        if labels and not set(labels) <= set(m["labelIds"]):
            continue
        if not spam_trash and {"SPAM", "TRASH"} & set(m["labelIds"]) and not (set(labels) & {"SPAM", "TRASH"}):
            if not q or not re.search(r"\b(in|label):(spam|trash|anywhere)\b", q, re.I):
                continue
        if q and not _matches(box, m, q + (" in:anywhere" if spam_trash else ""), ctx.now()):
            continue
        out.append(m)
    out.sort(key=lambda m: (int(m["internalDate"]), m["id"]), reverse=True)
    return out


@op("gmail.users.messages.list", "GET", "/messages", read_only=True)
def messages_list(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    hits = _hits(ctx, box, req)
    chunk, token = page(hits, req, default=100, maximum=500)
    out: dict[str, Any] = {}
    if chunk:
        out["messages"] = [{"id": m["id"], "threadId": m["threadId"]} for m in chunk]
    if token:
        out["nextPageToken"] = token
    out["resultSizeEstimate"] = len(hits)
    return select(out, req.arg("fields"))


@op("gmail.users.messages.get", "GET", "/messages/{id}", read_only=True)
def messages_get(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    return select(_message(box, _msg(box, req.params["id"]), _format(req), req.args("metadataHeaders")),
                  req.arg("fields"))


def _threads(box: dict[str, Any], msgs: list[dict[str, Any]]) -> list[str]:
    seen: dict[str, int] = {}
    for m in msgs:
        seen[m["threadId"]] = max(seen.get(m["threadId"], 0), int(m["internalDate"]))
    return sorted(seen, key=lambda t: seen[t], reverse=True)


def _thread_msgs(box: dict[str, Any], tid: str) -> list[dict[str, Any]]:
    return sorted((m for m in box["messages"].values() if m["threadId"] == tid), key=lambda m: int(m["internalDate"]))


@op("gmail.users.threads.list", "GET", "/threads", read_only=True)
def threads_list(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    tids = _threads(box, _hits(ctx, box, req))
    chunk, token = page(tids, req, default=100, maximum=500)
    out: dict[str, Any] = {}
    if chunk:
        out["threads"] = []
        for tid in chunk:
            last = _thread_msgs(box, tid)[-1]
            out["threads"].append({"id": tid, "snippet": _snippet(last), "historyId": _hid(box, last["id"])})
    if token:
        out["nextPageToken"] = token
    out["resultSizeEstimate"] = len(tids)
    return select(out, req.arg("fields"))


@op("gmail.users.threads.get", "GET", "/threads/{id}", read_only=True)
def threads_get(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    msgs = _thread_msgs(box, req.params["id"])
    if not msgs:
        raise error(404, "Requested entity was not found.", "notFound")
    fmt = _format(req)
    items = [_message(box, m, fmt, req.args("metadataHeaders")) for m in msgs]
    return select({"id": req.params["id"], "historyId": max((i["historyId"] for i in items), key=int),
                   "messages": items}, req.arg("fields"))


# -- changing messages -----------------------------------------------------------------------------

def _label_ids(box: dict[str, Any], ids: list[str] | None) -> list[str]:
    for lid in ids or []:
        if lid not in box["labels"]:
            raise error(400, f"Invalid label: {lid}", "invalidArgument")
    return list(ids or [])


def _relabel(box: dict[str, Any], m: dict[str, Any], add: list[str], remove: list[str]) -> None:
    if "DRAFT" in add or "DRAFT" in remove or "SENT" in add:
        raise error(400, "Invalid label: DRAFT" if "DRAFT" in add + remove else "Invalid label: SENT", "invalidArgument")
    before = list(m["labelIds"])
    m["labelIds"] = [lid for lid in dict.fromkeys(m["labelIds"] + add) if lid not in remove]
    if m["labelIds"] != before:
        _touch(box, m["id"])


def _brief(box: dict[str, Any], m: dict[str, Any]) -> dict[str, Any]:
    return {"id": m["id"], "threadId": m["threadId"], "labelIds": list(m["labelIds"])}


@op("gmail.users.messages.modify", "POST", "/messages/{id}/modify")
def messages_modify(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    m = _msg(box, req.params["id"])
    b = req.json()
    _relabel(box, m, _label_ids(box, b.get("addLabelIds")), _label_ids(box, b.get("removeLabelIds")))
    return _brief(box, m)


@op("gmail.users.messages.batchModify", "POST", "/messages/batchModify")
def messages_batch_modify(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    b = req.json()
    ids = b.get("ids") or []
    if len(ids) > 1000:
        raise error(400, "Too many ids specified. Maximum 1000.", "invalidArgument")
    add, remove = _label_ids(box, b.get("addLabelIds")), _label_ids(box, b.get("removeLabelIds"))
    for mid in ids:
        if mid in box["messages"]:  # unknown ids are skipped, like Gmail
            _relabel(box, box["messages"][mid], add, remove)
    return Response(204)


@op("gmail.users.messages.batchDelete", "POST", "/messages/batchDelete", destructive=True)
def messages_batch_delete(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    for mid in req.json().get("ids") or []:
        box["messages"].pop(mid, None)
    return Response(204)


def _trash(box: dict[str, Any], m: dict[str, Any]) -> None:
    """Trash keeps the other labels (views hide trashed mail), so untrash puts everything back."""
    m["labelIds"] = [lid for lid in m["labelIds"] if lid != "SPAM"] + ["TRASH"]
    _touch(box, m["id"])


def _untrash(box: dict[str, Any], m: dict[str, Any]) -> None:
    m["labelIds"] = [lid for lid in m["labelIds"] if lid != "TRASH"]
    _touch(box, m["id"])


@op("gmail.users.messages.trash", "POST", "/messages/{id}/trash")
def messages_trash(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    m = _msg(box, req.params["id"])
    _trash(box, m)
    return _brief(box, m)


@op("gmail.users.messages.untrash", "POST", "/messages/{id}/untrash")
def messages_untrash(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    m = _msg(box, req.params["id"])
    _untrash(box, m)
    return _brief(box, m)


@op("gmail.users.messages.delete", "DELETE", "/messages/{id}", destructive=True)
def messages_delete(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    _msg(box, req.params["id"])
    del box["messages"][req.params["id"]]
    return Response(204)


@op("gmail.users.threads.modify", "POST", "/threads/{id}/modify")
def threads_modify(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    msgs = _thread_msgs(box, req.params["id"])
    if not msgs:
        raise error(404, "Requested entity was not found.", "notFound")
    b = req.json()
    add, remove = _label_ids(box, b.get("addLabelIds")), _label_ids(box, b.get("removeLabelIds"))
    for m in msgs:
        _relabel(box, m, add, remove)
    return {"id": req.params["id"], "messages": [_brief(box, m) for m in msgs]}


@op("gmail.users.threads.trash", "POST", "/threads/{id}/trash")
def threads_trash(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    msgs = _thread_msgs(box, req.params["id"])
    if not msgs:
        raise error(404, "Requested entity was not found.", "notFound")
    for m in msgs:
        _trash(box, m)
    return {"id": req.params["id"], "messages": [_brief(box, m) for m in msgs]}


@op("gmail.users.threads.untrash", "POST", "/threads/{id}/untrash")
def threads_untrash(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    msgs = _thread_msgs(box, req.params["id"])
    if not msgs:
        raise error(404, "Requested entity was not found.", "notFound")
    for m in msgs:
        _untrash(box, m)
    return {"id": req.params["id"], "messages": [_brief(box, m) for m in msgs]}


@op("gmail.users.threads.delete", "DELETE", "/threads/{id}", destructive=True)
def threads_delete(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    msgs = _thread_msgs(box, req.params["id"])
    if not msgs:
        raise error(404, "Requested entity was not found.", "notFound")
    for m in msgs:
        del box["messages"][m["id"]]
    return Response(204)


@op("gmail.users.messages.attachments.get", "GET", "/messages/{messageId}/attachments/{id}", read_only=True)
def attachments_get(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    m = _msg(box, req.params["messageId"])
    a = next((a for a in m["attachments"] if a["attachmentId"] == req.params["id"]), None)
    if a is None:
        raise error(400, "Invalid attachment token", "invalidArgument")
    data = _att_bytes(a)
    return {"attachmentId": a["attachmentId"], "size": len(data), "data": b64url(data)}


# -- sending ---------------------------------------------------------------------------------------

def _parse_raw(req: Request, raw: bytes | None) -> dict[str, Any]:
    if not raw:
        raise error(400, "'raw' RFC822 payload message string or uploading message via /upload/* URL required",
                    "invalidArgument")
    em = message_from_bytes(raw, policy=policy.default)
    text = html_body = None
    attachments = []
    for part in em.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        if part.get_content_disposition() == "attachment" or (part.get_filename() and ctype not in ("text/plain", "text/html")):
            data = part.get_payload(decode=True) or b""
            attachments.append({"filename": part.get_filename() or "attachment", "mimeType": ctype,
                                "size": len(data), "data": b64url(data)})
        elif ctype == "text/plain" and text is None:
            text = part.get_content()
        elif ctype == "text/html" and html_body is None:
            html_body = part.get_content()
    if text is None:
        text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", html_body or ""))).strip()
    addrs = lambda h: [f"{n} <{a}>" if n else a for n, a in getaddresses(em.get_all(h, []))]  # noqa: E731
    headers = {k: str(em[k]) for k in ("In-Reply-To", "References") if em[k]}
    return {"from": str(em["From"]) if em["From"] else None, "to": addrs("To"), "cc": addrs("Cc"),
            "bcc": addrs("Bcc"), "subject": str(em["Subject"] or ""), "body": text.rstrip("\n") if text else "",
            "html_body": html_body, "attachments": attachments, "headers": headers}


def _request_raw(req: Request, draft: bool = False) -> tuple[bytes | None, str | None]:
    if isinstance(req.body, (bytes, bytearray)):  # /upload/... with message/rfc822 media
        return bytes(req.body), None
    b = req.json()
    if "__media__" in b:  # uploadType=multipart: metadata, then the message itself
        meta = b.get("message") if draft else b
        return base64.b64decode(b["__media__"]), (meta or {}).get("threadId") or b.get("threadId")
    msg = b.get("message") if draft else b
    msg = msg or {}
    return (unb64url(msg["raw"]) if msg.get("raw") else None), msg.get("threadId")


def _norm_subject(s: str) -> str:
    return re.sub(r"^((re|fwd?|aw|wg)\s*:\s*)+", "", s.strip(), flags=re.I).lower()


def _thread_for(box: dict[str, Any], thread_id: str | None, subject: str) -> str | None:
    """Gmail only threads a sent message when the client names the thread and the subject matches."""
    if not thread_id:
        return None
    msgs = _thread_msgs(box, thread_id)
    if not msgs:
        raise error(404, "Requested entity was not found.", "notFound")
    return thread_id if _norm_subject(msgs[0]["subject"]) == _norm_subject(subject) else None


def _sender(box: dict[str, Any], given: str | None) -> str:
    user = box["user"]
    if given and _addr(given) == user["email"].lower():
        return given  # a display name of your own choosing is fine
    return f"{user['name']} <{user['email']}>"  # anything else is replaced by the account's address


def _send_raw(ctx: Instance, box: dict[str, Any], raw: bytes | None, thread_id: str | None) -> dict[str, Any]:
    p = _parse_raw(Request("POST", "", {}, {}), raw)
    if not (p["to"] or p["cc"] or p["bcc"]):
        raise error(400, "Recipient address required", "invalidArgument")
    return _send(ctx, box, to=p["to"], cc=p["cc"], bcc=p["bcc"], subject=p["subject"], body=p["body"],
                 html_body=p["html_body"], attachments=p["attachments"],
                 thread_id=_thread_for(box, thread_id, p["subject"]), sender=_sender(box, p["from"]),
                 headers=p["headers"])


@op("gmail.users.messages.send", "POST", "/messages/send")
def messages_send(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    raw, thread_id = _request_raw(req)
    return _brief(box, _send_raw(ctx, box, raw, thread_id or req.arg("threadId")))


operation("gmail.users.messages.send", "POST", "/upload/gmail/v1/users/{userId}/messages/send", hosts=HOSTS)(messages_send)


@op("gmail.users.messages.insert", "POST", "/messages")
def messages_insert(ctx: Instance, req: Request) -> Any:
    """Put a message straight into the mailbox (no sending)."""
    box = _box(ctx, req)
    raw, thread_id = _request_raw(req)
    p = _parse_raw(req, raw)
    labels = req.json().get("labelIds") or ["INBOX"]
    m = _store(ctx, box, sender=p["from"] or box["user"]["email"], to=p["to"], cc=p["cc"], bcc=p["bcc"],
               subject=p["subject"], body=p["body"], html_body=p["html_body"], labels=_label_ids(box, labels),
               date=ctx.now(), thread_id=_thread_for(box, thread_id, p["subject"]), attachments=p["attachments"],
               headers=p["headers"])
    return _brief(box, m)


# -- drafts ----------------------------------------------------------------------------------------

def _draft(box: dict[str, Any], did: str) -> dict[str, Any]:
    d = box["drafts"].get(did)
    if d is None or d["messageId"] not in box["messages"]:
        raise error(404, "Requested entity was not found.", "notFound")
    return d


def _draft_json(box: dict[str, Any], d: dict[str, Any], fmt: str = "minimal") -> dict[str, Any]:
    m = box["messages"][d["messageId"]]
    return {"id": d["id"], "message": _message(box, m, fmt) if fmt != "brief" else _brief(box, m)}


def _store_draft(ctx: Instance, box: dict[str, Any], raw: bytes | None, thread_id: str | None) -> dict[str, Any]:
    p = _parse_raw(Request("POST", "", {}, {}), raw)
    return _store(ctx, box, sender=_sender(box, p["from"]), to=p["to"], cc=p["cc"], bcc=p["bcc"],
                  subject=p["subject"], body=p["body"], html_body=p["html_body"], labels=["DRAFT"], date=ctx.now(),
                  thread_id=_thread_for(box, thread_id, p["subject"]), attachments=p["attachments"],
                  headers=p["headers"])


@op("gmail.users.drafts.create", "POST", "/drafts")
def drafts_create(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    raw, thread_id = _request_raw(req, draft=True)
    m = _store_draft(ctx, box, raw, thread_id)
    did = "r" + str(int(hashlib.sha1(m["id"].encode()).hexdigest(), 16))[:19]
    box["drafts"][did] = {"id": did, "messageId": m["id"]}
    return _draft_json(box, box["drafts"][did], "brief")


operation("gmail.users.drafts.create", "POST", "/upload/gmail/v1/users/{userId}/drafts", hosts=HOSTS)(drafts_create)


@op("gmail.users.drafts.list", "GET", "/drafts", read_only=True)
def drafts_list(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    ds = [d for d in box["drafts"].values() if d["messageId"] in box["messages"]]
    q = req.arg("q")
    if q:
        ds = [d for d in ds if _matches(box, box["messages"][d["messageId"]], q, ctx.now())]
    ds.sort(key=lambda d: int(box["messages"][d["messageId"]]["internalDate"]), reverse=True)
    chunk, token = page(ds, req, default=100, maximum=500)
    out: dict[str, Any] = {}
    if chunk:
        out["drafts"] = [{"id": d["id"], "message": {"id": d["messageId"],
                                                     "threadId": box["messages"][d["messageId"]]["threadId"]}}
                         for d in chunk]
    if token:
        out["nextPageToken"] = token
    out["resultSizeEstimate"] = len(ds)
    return out


@op("gmail.users.drafts.get", "GET", "/drafts/{id}", read_only=True)
def drafts_get(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    return _draft_json(box, _draft(box, req.params["id"]), _format(req))


@op("gmail.users.drafts.update", "PUT", "/drafts/{id}")
def drafts_update(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    d = _draft(box, req.params["id"])
    raw, thread_id = _request_raw(req, draft=True)
    old = box["messages"].pop(d["messageId"])
    m = _store_draft(ctx, box, raw, thread_id or old["threadId"])
    d["messageId"] = m["id"]
    return _draft_json(box, d, "brief")


operation("gmail.users.drafts.update", "PUT", "/upload/gmail/v1/users/{userId}/drafts/{id}", hosts=HOSTS)(drafts_update)


@op("gmail.users.drafts.delete", "DELETE", "/drafts/{id}", destructive=True)
def drafts_delete(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    d = _draft(box, req.params["id"])
    box["messages"].pop(d["messageId"], None)
    del box["drafts"][d["id"]]
    return Response(204)


@op("gmail.users.drafts.send", "POST", "/drafts/send")
def drafts_send(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    b = req.json()
    if not b.get("id"):
        raise error(400, "Invalid draft", "invalidArgument")
    d = _draft(box, b["id"])
    m = box["messages"][d["messageId"]]
    if (b.get("message") or {}).get("raw"):  # send with changes
        raw, thread_id = _request_raw(req, draft=True)
    else:
        others = [x for x in _thread_msgs(box, m["threadId"]) if x["id"] != m["id"]]
        raw, thread_id = _raw(_tree(box, m)), (m["threadId"] if others else None)
    box["messages"].pop(d["messageId"])
    del box["drafts"][d["id"]]
    return _brief(box, _send_raw(ctx, box, raw, thread_id))


operation("gmail.users.drafts.send", "POST", "/upload/gmail/v1/users/{userId}/drafts/send", hosts=HOSTS)(drafts_send)


# -- labels ----------------------------------------------------------------------------------------

def _label_json(box: dict[str, Any], lab: dict[str, Any], counts: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"id": lab["id"], "name": lab["name"]}
    if lab["type"] == "user":
        out["messageListVisibility"] = lab.get("messageListVisibility", "show")
        out["labelListVisibility"] = lab.get("labelListVisibility", "labelShow")
    elif lab["id"] in VISIBLE:
        out["labelListVisibility"], out["messageListVisibility"] = VISIBLE[lab["id"]]
    out["type"] = lab["type"]
    if lab.get("color"):
        out["color"] = lab["color"]
    if counts:
        msgs = [m for m in box["messages"].values() if lab["id"] in m["labelIds"]]
        unread = [m for m in msgs if "UNREAD" in m["labelIds"]]
        out.update(messagesTotal=len(msgs), messagesUnread=len(unread), threadsTotal=len({m["threadId"] for m in msgs}),
                   threadsUnread=len({m["threadId"] for m in unread}))
    return out


def _label(box: dict[str, Any], lid: str) -> dict[str, Any]:
    lab = box["labels"].get(lid)
    if lab is None:
        raise error(404, "Requested entity was not found.", "notFound")
    return lab


@op("gmail.users.labels.list", "GET", "/labels", read_only=True)
def labels_list(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    system = [_label_json(box, box["labels"][n]) for n in ["CHAT", *SYSTEM_LABELS] if n in box["labels"]]
    if "CHAT" not in box["labels"]:
        system.insert(0, {"id": "CHAT", "name": "CHAT", "messageListVisibility": "hide",
                          "labelListVisibility": "labelHide", "type": "system"})
    user = [_label_json(box, lab) for lab in box["labels"].values() if lab["type"] == "user"]
    return {"labels": system + user}


@op("gmail.users.labels.get", "GET", "/labels/{id}", read_only=True)
def labels_get(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    return _label_json(box, _label(box, req.params["id"]), counts=True)


def _check_name(box: dict[str, Any], name: str, lid: str | None = None) -> None:
    if not name or not name.strip():
        raise error(400, "Invalid label name", "invalidArgument")
    if name.upper() in SYSTEM_LABELS or name.upper() == "CHAT":
        raise error(400, "Invalid label name", "invalidArgument")
    other = _label_by_name(box, name)
    if other and other != lid:
        raise error(409, "Label name exists or conflicts", "conflict", status="ALREADY_EXISTS")


@op("gmail.users.labels.create", "POST", "/labels")
def labels_create(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    b = req.json()
    _check_name(box, b.get("name", ""))
    lab = _new_label(ctx, box, b["name"], b.get("messageListVisibility", "show"), b.get("labelListVisibility", "labelShow"))
    if b.get("color"):
        lab["color"] = b["color"]
    return _label_json(box, lab)


def _update_label(ctx: Instance, req: Request, patch: bool) -> Any:
    box = _box(ctx, req)
    lab = _label(box, req.params["id"])
    if lab["type"] == "system":
        raise error(400, "Invalid label name" if req.json().get("name") else "Invalid update request",
                    "invalidArgument")
    b = req.json()
    if "name" in b or not patch:
        _check_name(box, b.get("name", ""), lab["id"])
        lab["name"] = b["name"]
    for k in ("messageListVisibility", "labelListVisibility", "color"):
        if k in b:
            lab[k] = b[k]
    return _label_json(box, lab)


@op("gmail.users.labels.patch", "PATCH", "/labels/{id}")
def labels_patch(ctx: Instance, req: Request) -> Any:
    return _update_label(ctx, req, True)


@op("gmail.users.labels.update", "PUT", "/labels/{id}")
def labels_update(ctx: Instance, req: Request) -> Any:
    return _update_label(ctx, req, False)


@op("gmail.users.labels.delete", "DELETE", "/labels/{id}", destructive=True)
def labels_delete(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    lab = _label(box, req.params["id"])
    if lab["type"] == "system":
        raise error(400, "Invalid delete request", "invalidArgument")
    del box["labels"][lab["id"]]
    for m in box["messages"].values():
        if lab["id"] in m["labelIds"]:
            m["labelIds"].remove(lab["id"])
    return Response(204)


# -- profile, history, settings --------------------------------------------------------------------

@op("gmail.users.getProfile", "GET", "/profile", read_only=True)
def get_profile(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    msgs = [m for m in box["messages"].values() if "DRAFT" not in m["labelIds"]]
    return {"emailAddress": box["user"]["email"], "messagesTotal": len(msgs),
            "threadsTotal": len({m["threadId"] for m in msgs}), "historyId": _hid(box)}


@op("gmail.users.history.list", "GET", "/history", read_only=True)
def history_list(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    start = req.arg("startHistoryId")
    if not start:
        raise error(400, "Missing required parameter: startHistoryId", location="startHistoryId")
    start_id = int(start)
    if start_id < 1000:
        raise error(404, "Requested entity was not found.", "notFound")
    hist = box.get("_hist", {})
    changed = {c["message"]: c["id"] for c in box.get("_changes", []) if c["id"] > start_id}
    records = []
    for mid, hid in sorted(hist.items(), key=lambda x: x[1]):
        if hid <= start_id or mid not in box["messages"]:
            continue
        m = box["messages"][mid]
        rec: dict[str, Any] = {"id": str(hid), "messages": [{"id": mid, "threadId": m["threadId"]}]}
        if mid not in changed or changed[mid] != hid:
            rec["messagesAdded"] = [{"message": _brief(box, m)}]
        else:
            rec["labelsAdded" if "TRASH" in m["labelIds"] else "labelsRemoved"] = [
                {"message": _brief(box, m), "labelIds": list(m["labelIds"])}]
        records.append(rec)
    kinds = {{"messageAdded": "messagesAdded", "messageDeleted": "messagesDeleted", "labelAdded": "labelsAdded",
              "labelRemoved": "labelsRemoved"}.get(k, k) for k in req.args("historyTypes")}
    if kinds:
        records = [r for r in records if kinds & (set(r) - {"id", "messages"})]
    chunk, token = page(records, req, default=100, maximum=500)
    out: dict[str, Any] = {}
    if chunk:
        out["history"] = chunk
    if token:
        out["nextPageToken"] = token
    out["historyId"] = _hid(box)
    return out


def _send_as(box: dict[str, Any]) -> dict[str, Any]:
    u = box["user"]
    return {"sendAsEmail": u["email"], "displayName": u.get("name", ""), "replyToAddress": "",
            "signature": u.get("signature", ""), "isPrimary": True, "isDefault": True}


@op("gmail.users.settings.sendAs.list", "GET", "/settings/sendAs", read_only=True)
def send_as_list(ctx: Instance, req: Request) -> Any:
    return {"sendAs": [_send_as(_box(ctx, req))]}


@op("gmail.users.settings.sendAs.get", "GET", "/settings/sendAs/{sendAsEmail}", read_only=True)
def send_as_get(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    if req.params["sendAsEmail"].lower() != box["user"]["email"].lower():
        raise error(404, "Requested entity was not found.", "notFound")
    return _send_as(box)


@op("gmail.users.settings.getVacation", "GET", "/settings/vacation", read_only=True)
def get_vacation(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    rule = next((r for r in ctx.state.get("_auto_replies", []) if r.get("out_of_office")
                 and _addr(r.get("from", r.get("who", ""))) == box["user"]["email"].lower()), None)
    if not rule:
        return {"enableAutoReply": False}
    return {"enableAutoReply": True, "responseBodyPlainText": rule["out_of_office"], "restrictToContacts": False,
            "restrictToDomain": False}


def _filter_json(f: dict[str, Any]) -> dict[str, Any]:
    return {"id": f["id"], "criteria": dict(f.get("criteria") or {}), "action": dict(f.get("action") or {})}


@op("gmail.users.settings.filters.list", "GET", "/settings/filters", read_only=True)
def filters_list(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    fs = [_filter_json(f) for f in box["filters"].values()]
    return {"filter": fs} if fs else {}


@op("gmail.users.settings.filters.get", "GET", "/settings/filters/{id}", read_only=True)
def filters_get(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    f = box["filters"].get(req.params["id"])
    if f is None:
        raise error(404, "Requested entity was not found.", "notFound")
    return _filter_json(f)


@op("gmail.users.settings.filters.create", "POST", "/settings/filters")
def filters_create(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    b = req.json()
    criteria, act = b.get("criteria") or {}, b.get("action") or {}
    if not any(criteria.values()):
        raise error(400, "Filter doesn't have any criteria", "invalidArgument")
    if not any(act.values()):
        raise error(400, "Filter doesn't have any actions", "invalidArgument")
    _label_ids(box, act.get("addLabelIds"))
    _label_ids(box, act.get("removeLabelIds"))
    fid = "ANe1Bm" + ctx.token(18, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
    box["filters"][fid] = {"id": fid, "criteria": criteria, "action": act}
    return _filter_json(box["filters"][fid])


@op("gmail.users.settings.filters.delete", "DELETE", "/settings/filters/{id}", destructive=True)
def filters_delete(ctx: Instance, req: Request) -> Any:
    box = _box(ctx, req)
    if box["filters"].pop(req.params["id"], None) is None:
        raise error(404, "Requested entity was not found.", "notFound")
    return Response(204)



from . import people  # noqa: E402,F401  (Google Contacts: the People API)
