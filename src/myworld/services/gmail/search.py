"""gmail: search."""

from __future__ import annotations

import datetime as dt
import re
import shlex
from typing import Any

from .model import _label_by_name

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


