"""gmail: Gmail's search language, as the Gmail API and the web UI understand it (2026-09-25.3).

    from:(alex OR "john park") subject:(q4 plan) -in:trash {has:attachment larger:5M} after:2026/9/1

Grouping with ( ), OR / | / {a b} (OR binds tighter than the implied AND), -negation, quoted
phrases, operators with grouped values, whole-word matching for bare words (``plan`` doesn't
find "planning"), sizes (``larger:5M``), and dates interpreted in Pacific time, as Gmail does.
Spam and trash are left out unless the query asks for them.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any
from zoneinfo import ZoneInfo

from .model import _addr

PACIFIC = ZoneInfo("America/Los_Angeles")
_UNITS = {"d": 1, "m": 30, "y": 365}
_SIZE = {"": 1, "k": 1024, "m": 1024 * 1024}
_TOKEN = re.compile(r'\s*(?:(-?\()|(\))|(-?\{)|(\})|(-?"[^"]*")|(-?[^\s(){}"]+(?:"[^"]*")?(?:\([^)]*\))?))')


def _tokens(query: str) -> list[str]:
    out, pos = [], 0
    while pos < len(query):
        m = _TOKEN.match(query, pos)
        if not m or m.end() == pos:
            break
        pos = m.end()
        out.append(next(g for g in m.groups() if g is not None))
    return out


def parse(query: str) -> Any:
    toks = _tokens(query)
    i = 0

    def peek() -> str | None:
        return toks[i] if i < len(toks) else None

    def expr(stop: str | None = None) -> Any:
        nonlocal i
        ands: list[Any] = []
        while peek() is not None and peek() != stop:
            node = term()
            while peek() in ("OR", "|"):
                i += 1
                if peek() is None or peek() == stop:
                    break
                node = ("or", node, term())
            ands.append(node)
        return ("and", *ands) if len(ands) != 1 else ands[0]

    def term() -> Any:
        nonlocal i
        t = toks[i]
        i += 1
        neg = t.startswith("-") and len(t) > 1
        body = t[1:] if neg else t
        if body == "(":
            node = expr(")")
            i += 1 if peek() == ")" else 0
        elif body == "{":
            items = []
            while peek() is not None and peek() != "}":
                items.append(term())
            i += 1 if peek() == "}" else 0
            node = ("or", *items) if items else ("true",)
        elif body.startswith('"'):
            node = ("phrase", body.strip('"'))
        elif ":" in body and not body.startswith(":") and re.match(r"^[A-Za-z_][A-Za-z0-9_]*:", body):
            key, _, val = body.partition(":")
            if val.startswith("(") and val.endswith(")"):
                node = ("op", key.lower(), parse(val[1:-1]))
            else:
                node = ("op", key.lower(), val.strip('"'))
        elif body == "AROUND":
            node = ("true",)  # proximity: treated as "both words appear"
            if peek() and peek().isdigit():
                i += 1
        else:
            node = ("word", body.lstrip("+"))
        return ("not", node) if neg else node

    return expr()


def _words(text: str) -> set[str]:
    return set(re.findall(r"[\w'.@+-]+", text.lower())) | set(re.findall(r"\w+", text.lower()))


def _has_word(text: str, w: str) -> bool:
    w = w.lower()
    if not re.fullmatch(r"\w+", w):
        return w in text.lower()  # addresses, domains, dotted terms: substring
    return w in _words(text)


def _size(msg: dict[str, Any]) -> int:
    return len(msg["body"].encode()) + len((msg.get("htmlBody") or "").encode()) + \
        sum(int(a.get("size", 0)) for a in msg["attachments"]) + 600


def _date_arg(v: str) -> dt.datetime | None:
    if v.isdigit() and len(v) >= 9:  # epoch seconds
        return dt.datetime.fromtimestamp(int(v), dt.timezone.utc)
    m = re.fullmatch(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", v)
    if not m:
        return None
    return dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=PACIFIC)


def matches(box: dict[str, Any], msg: dict[str, Any], query: str, now: dt.datetime, tree: Any = None) -> bool:
    tree = tree if tree is not None else parse(query)
    if not re.search(r"\b(in|label):(spam|trash|anywhere)\b", query.lower()):
        if {"SPAM", "TRASH"} & set(msg["labelIds"]):
            return False
    return _eval(box, msg, tree, now)


def _eval(box: dict[str, Any], msg: dict[str, Any], node: Any, now: dt.datetime) -> bool:
    kind = node[0]
    if kind == "and":
        return all(_eval(box, msg, n, now) for n in node[1:])
    if kind == "or":
        return any(_eval(box, msg, n, now) for n in node[1:])
    if kind == "not":
        return not _eval(box, msg, node[1], now)
    if kind == "true":
        return True
    everything = " ".join([msg["subject"], msg["body"], msg["from"], *msg["to"], *msg["cc"],
                           *(a["filename"] for a in msg["attachments"])])
    if kind == "word":
        return _has_word(everything, node[1])
    if kind == "phrase":
        p = node[1].lower()
        return bool(re.search(r"(?<!\w)" + re.escape(p) + r"(?!\w)", f"{msg['subject']}\n{msg['body']}".lower()))
    _, key, val = node
    if isinstance(val, tuple):  # from:(a OR b), subject:(two words)
        return _grouped(box, msg, key, val, now)
    return _operator(box, msg, key, val, now)


def _grouped(box: dict[str, Any], msg: dict[str, Any], key: str, node: Any, now: dt.datetime) -> bool:
    kind = node[0]
    if kind == "and":
        return all(_grouped(box, msg, key, n, now) for n in node[1:])
    if kind == "or":
        return any(_grouped(box, msg, key, n, now) for n in node[1:])
    if kind == "not":
        return not _grouped(box, msg, key, node[1], now)
    if kind in ("word", "phrase"):
        return _operator(box, msg, key, node[1], now)
    return _eval(box, msg, node, now)


def _addresses(msg: dict[str, Any], key: str) -> list[str]:
    if key == "from":
        return [msg["from"]]
    if key == "to":
        return list(msg["to"]) + list(msg["cc"])
    if key == "deliveredto":
        return list(msg["to"]) + list(msg["cc"]) + list(msg["bcc"])
    return list(msg[key])


def _operator(box: dict[str, Any], msg: dict[str, Any], key: str, val: str, now: dt.datetime) -> bool:
    v = val.lower()
    labels = set(msg["labelIds"])
    sent = dt.datetime.fromtimestamp(int(msg["internalDate"]) / 1000, dt.timezone.utc)
    me = box["user"]["email"].lower()
    if key in ("from", "to", "cc", "bcc", "deliveredto"):
        who = me if v == "me" else v
        return any(who in a.lower() or who == _addr(a) for a in _addresses(msg, key))
    if key == "subject":
        return _has_word(msg["subject"], v) if re.fullmatch(r"\w+", v) else v in msg["subject"].lower()
    if key in ("in", "label"):
        if v == "anywhere":
            return True
        system = {"inbox": "INBOX", "sent": "SENT", "drafts": "DRAFT", "draft": "DRAFT", "trash": "TRASH",
                  "spam": "SPAM", "starred": "STARRED", "important": "IMPORTANT", "unread": "UNREAD",
                  "chats": "CHAT", "snoozed": "SNOOZED"}
        if v in system:
            return system[v] in labels
        for lab in box["labels"].values():
            name = lab["name"].lower()
            if v in (name, name.replace(" ", "-"), name.replace("/", "-"), name.replace("/", "-").replace(" ", "-")):
                return lab["id"] in labels
        return False
    if key == "is":
        return {"unread": "UNREAD" in labels, "read": "UNREAD" not in labels, "starred": "STARRED" in labels,
                "important": "IMPORTANT" in labels, "snoozed": False, "muted": False, "chat": False}.get(v, False)
    if key == "has":
        user_labels = {lab["id"] for lab in box["labels"].values() if lab["type"] == "user"}
        names = " ".join(a["filename"].lower() for a in msg["attachments"])
        body = msg["body"].lower()
        return {"attachment": bool(msg["attachments"]), "userlabels": bool(labels & user_labels),
                "nouserlabels": not labels & user_labels, "yellow-star": "STARRED" in labels,
                "drive": "drive.google.com" in body or "docs.google.com" in body,
                "document": "docs.google.com/document" in body, "spreadsheet": "docs.google.com/spreadsheets" in body,
                "presentation": "docs.google.com/presentation" in body, "youtube": "youtube.com" in body,
                "pdf": ".pdf" in names}.get(v, False)
    if key == "filename":
        return any(v == a["filename"].lower() or a["filename"].lower().endswith("." + v) or v in a["filename"].lower()
                   for a in msg["attachments"])
    if key == "category":
        cats = {"social": "CATEGORY_SOCIAL", "promotions": "CATEGORY_PROMOTIONS", "updates": "CATEGORY_UPDATES",
                "forums": "CATEGORY_FORUMS", "personal": "CATEGORY_PERSONAL"}
        if v == "primary":
            return not labels & {"CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS", "CATEGORY_UPDATES", "CATEGORY_FORUMS"}
        return cats.get(v) in labels
    if key in ("larger", "smaller", "size"):
        m = re.fullmatch(r"(\d+(?:\.\d+)?)([km]?)b?", v)
        if not m:
            return False
        n = float(m.group(1)) * _SIZE[m.group(2)]
        size = _size(msg)
        return size > n if key == "larger" else size < n if key == "smaller" else size >= n
    if key in ("newer_than", "older_than"):
        m = re.fullmatch(r"(\d+)([dmy])", v)
        if not m:
            return False
        cutoff = now - dt.timedelta(days=int(m.group(1)) * _UNITS[m.group(2)])
        return sent >= cutoff if key == "newer_than" else sent < cutoff
    if key in ("after", "before", "newer", "older"):
        day = _date_arg(v)
        if day is None:
            return False
        return sent >= day if key in ("after", "newer") else sent < day
    if key == "rfc822msgid":
        return msg["messageId"].strip("<>").lower() == v.strip("<>")
    if key == "list":
        return v in msg["from"].lower()
    return _has_word(f"{msg['subject']} {msg['body']}", f"{key}:{val}")
