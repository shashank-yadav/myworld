"""Gmail from a Google Takeout mbox (Mail/All mail Including Spam and Trash.mbox).

Keeps what the simulator models: sender and recipients, subject, plain-text body, date,
labels (from X-Gmail-Labels), threads (from X-GM-THRID) and attachment metadata.
"""

from __future__ import annotations

import datetime as dt
import html
import mailbox
import re
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any

from .common import ImportOptions, iso

LABEL_MAP = {"inbox": "INBOX", "sent": "SENT", "draft": "DRAFT", "drafts": "DRAFT", "spam": "SPAM", "trash": "TRASH",
             "unread": "UNREAD", "starred": "STARRED", "important": "IMPORTANT", "opened": None, "archived": None,
             "category personal": "CATEGORY_PERSONAL", "category social": "CATEGORY_SOCIAL",
             "category promotions": "CATEGORY_PROMOTIONS", "category updates": "CATEGORY_UPDATES",
             "category forums": "CATEGORY_FORUMS", "chat": None}


def _h(v: Any) -> str:
    return str(make_header(decode_header(v))) if v else ""


def _body(msg: Any) -> tuple[str, list[dict[str, Any]]]:
    text, htm, atts = None, None, []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.is_multipart():
            continue
        disp = (part.get("Content-Disposition") or "").lower()
        ctype = part.get_content_type()
        if "attachment" in disp or part.get_filename():
            payload = part.get_payload(decode=True) or b""
            atts.append({"filename": _h(part.get_filename()) or "attachment", "mimeType": ctype, "size": len(payload)})
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if ctype == "text/plain" and text is None:
            text = content
        elif ctype == "text/html" and htm is None:
            htm = content
    if text is None and htm is not None:
        text = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style).*?</\1>", " ", htm)))
        text = re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()
    return (text or "").strip(), atts


def import_mbox(path: str | Path, opts: ImportOptions | None = None, *, owner: str | None = None,
                owner_name: str | None = None) -> dict[str, Any]:
    """``owner``: the mailbox owner's address (default: the most frequent sender of SENT mail)."""
    opts = opts or ImportOptions()
    box = mailbox.mbox(str(path), create=False)
    raw = []
    for msg in box:
        try:
            date = parsedate_to_datetime(msg["Date"]) if msg["Date"] else None
        except (TypeError, ValueError):
            date = None
        if date is None:
            continue
        if date.tzinfo is None:
            date = date.replace(tzinfo=dt.timezone.utc)
        labels = [l.strip().lower() for l in _h(msg["X-Gmail-Labels"]).split(",") if l.strip()]
        raw.append((date, msg, labels))
    if not raw:
        raise ValueError(f"no messages with dates found in {path}")
    if owner is None:
        senders: dict[str, int] = {}
        for _, msg, labels in raw:
            if "sent" in labels:
                for _, addr in getaddresses([_h(msg["From"])]):
                    senders[addr.lower()] = senders.get(addr.lower(), 0) + 1
        owner = max(senders, key=senders.get) if senders else getaddresses([_h(raw[0][1]["To"])])[0][1]
    opts.home = opts.home or owner.split("@")[-1]
    raw = opts.newest(raw, key=lambda r: r[0])
    opts.plan_shift([d for d, _, _ in raw])

    user_labels: set[str] = set()
    emails = []
    for date, msg, labels in raw:
        mapped = []
        for l in labels:
            if l in LABEL_MAP:
                if LABEL_MAP[l]:
                    mapped.append(LABEL_MAP[l])
            else:
                name = l.title() if l.islower() else l
                user_labels.add(name)
                mapped.append(name)
        if "SENT" not in mapped and "DRAFT" not in mapped and not any(x in mapped for x in ("SPAM", "TRASH")) \
                and "INBOX" not in mapped and not labels:
            mapped.append("INBOX")
        body, atts = _body(msg)
        e: dict[str, Any] = {
            "from": opts.address(_h(msg["From"])),
            "to": [opts.address(f"{n} <{a}>" if n else a) for n, a in getaddresses([_h(msg["To"])]) if a],
            "subject": opts.text(_h(msg["Subject"])) or "(no subject)",
            "body": opts.text(body) or "", "date": iso(opts.time(date)), "labels": mapped or ["INBOX"]}
        cc = [opts.address(a) for _, a in getaddresses([_h(msg["Cc"])]) if a]
        if cc:
            e["cc"] = cc
        if atts:
            e["attachments"] = atts
        thread = msg["X-GM-THRID"] or msg["Thread-Index"]
        if thread:
            e["thread"] = str(thread).strip()
        emails.append(e)
    email, name = opts.person(owner, owner_name)
    opts.save_map()
    return {"user": {"email": email, "name": name}, "labels": sorted(user_labels), "emails": emails}
