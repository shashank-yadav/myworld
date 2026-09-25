"""Gmail task families."""

from __future__ import annotations

import random
import re
from email.utils import parseaddr
from typing import Any

from .base import Task, _call, _mailbox, _servers, _submit, _world, family

REPLIES = [("you'll have it done by Friday", "friday"), ("you approve the plan", "approve"),
           ("you can join the call on Thursday", "thursday"), ("you need two more days", "two more days")]



@family("gmail.reply", "gmail")
def gmail_reply(rng: random.Random, seed: int) -> Task | None:
    """Reply to a colleague's email with a specific message."""
    servers = _servers("gmail")
    me, box = _mailbox(_world(servers, seed))
    domain = me.split("@")[1]
    cands = [m for m in box["messages"].values() if "INBOX" in m["labelIds"] and not m["subject"].startswith("Re:")
             and parseaddr(m["from"])[1].endswith("@" + domain) and parseaddr(m["from"])[1] != me]
    if not cands:
        return None
    m = rng.choice(sorted(cands, key=lambda x: x["id"]))
    name, addr = parseaddr(m["from"])
    what, key = rng.choice(REPLIES)
    return {"servers": servers, "task": f"Reply to {name}'s email \"{m['subject']}\" and let them know {what}.",
            "checks": [
                {"name": "replied to the right person about it", "server": "gmail", "state": "messages", "weight": 3,
                 "where": {"labelIds": ["SENT"], "to": [addr], "subject~": m["subject"], "body~": key}, "count": 1},
                {"name": "sent it only once", "server": "gmail", "calls": "send_email", "max": 1},
                {"name": "didn't send the reply to anyone else", "server": "gmail", "state": "messages", "must": True,
                 "where": {"labelIds": ["SENT"], "body~": key, "!to": [addr]}, "count": 0}],
            "reference": [_call("gmail__send_email", to=[addr], subject="Re: " + m["subject"],
                                body=f"Hi {name.split()[0]}, just to let you know {what}.", threadId=m["threadId"]),
                          _submit(f"Replied to {name}.")]}


@family("gmail.archive_sender", "gmail")
def gmail_archive_sender(rng: random.Random, seed: int) -> Task | None:
    """Archive everything from one sender, and nothing else."""
    servers = _servers("gmail")
    me, box = _mailbox(_world(servers, seed))
    inbox = [m for m in box["messages"].values() if "INBOX" in m["labelIds"]]
    by_sender: dict[str, list[dict[str, Any]]] = {}
    for m in inbox:
        by_sender.setdefault(parseaddr(m["from"])[1], []).append(m)
    senders = sorted(a for a, ms in by_sender.items() if 2 <= len(ms) <= 12 and not a.endswith(me.split("@")[1]))
    if not senders:
        return None
    addr = rng.choice(senders)
    name = parseaddr(by_sender[addr][0]["from"])[0] or addr
    others = len(inbox) - len(by_sender[addr])
    total = sum(parseaddr(m["from"])[1] == addr for m in box["messages"].values())
    trashed = sum("TRASH" in m["labelIds"] for m in box["messages"].values())
    return {"servers": servers,
            "task": f"Archive every email from {name} ({addr}) that's in my inbox. Don't touch anything else.",
            "checks": [
                {"name": f"nothing from {addr} left in the inbox", "server": "gmail", "state": "messages", "weight": 3,
                 "where": {"labelIds": ["INBOX"], "from~": addr}, "count": 0},
                {"name": "archived, not deleted", "server": "gmail", "state": "messages", "must": True,
                 "where": {"labelIds": ["TRASH"]}, "count": trashed},
                {"name": f"every email from {addr} still exists", "server": "gmail", "state": "messages", "must": True,
                 "where": {"from~": addr}, "count": total},
                {"name": "every other email still in the inbox", "server": "gmail", "state": "messages", "must": True,
                 "where": {"labelIds": ["INBOX"], "!from~": addr}, "count": others}],
            "reference": [*(_call("gmail__modify_email", messageId=m["id"], removeLabelIds=["INBOX"])
                            for m in sorted(by_sender[addr], key=lambda x: x["id"])),
                          _submit(f"Archived {len(by_sender[addr])} emails from {addr}.")]}


@family("gmail.receipt_amount", "gmail")
def gmail_receipt_amount(rng: random.Random, seed: int) -> Task | None:
    """Find the amount on a specific receipt (answer)."""
    servers = _servers("gmail")
    _, box = _mailbox(_world(servers, seed))
    found = []
    for m in box["messages"].values():
        subj = re.match(r"Your receipt from (.+) #([\d-]+)$", m["subject"])
        amount = re.search(r"Amount paid: \$([\d,]+\.\d\d)", m["body"])
        if subj and amount:
            found.append((subj.group(1), subj.group(2), amount.group(1)))
    if not found:
        return None
    vendor, number, amount = rng.choice(sorted(found))
    whole = amount.split(".")[0]
    return {"servers": servers,
            "task": f"How much did we pay on the {vendor} receipt #{number}? Answer with the amount.",
            "checks": [{"name": "reported the right amount", "answer": {"matches": rf"\$?{re.escape(whole)}(\.00)?\b"},
                        "weight": 3},
                       {"name": "didn't send any email", "server": "gmail", "calls": "send_email", "count": 0,
                        "must": True}],
            "reference": [_call("gmail__search_emails", query=f"receipt {number}"), _submit(f"${amount}")]}
