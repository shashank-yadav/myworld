"""Slack task families."""

from __future__ import annotations

import random

from .base import Task, _call, _servers, _submit, _world, family


@family("slack.dm", "slack")
def slack_dm(rng: random.Random, seed: int) -> Task | None:
    """Send a colleague a direct message."""
    servers = _servers("slack")
    st = _world(servers, seed).instances["slack"].state
    users = sorted((u for u in st["users"].values() if not u["is_bot"]), key=lambda u: u["name"])
    u = rng.choice(users)
    q, key = rng.choice([("Can you review the Billing v2 doc by Thursday?", "review"),
                         ("Are you joining the offsite?", "offsite"), ("Could you send me the Q3 numbers?", "q3 numbers")])
    return {"servers": servers, "task": f"Send {u['real_name']} a direct message on Slack asking: \"{q}\"",
            "checks": [
                {"name": "DM'd them the question", "server": "slack", "state": "messages", "weight": 3,
                 "where": {"channel": f"dm:{u['name']}", "from_bot": True, "text~": key}, "count": 1},
                {"name": "didn't post in any channel", "server": "slack", "state": "messages", "must": True,
                 "where": {"from_bot": True, "is_dm": False}, "count": 0}],
            "reference": [_call("slack__slack_post_message", channel_id=u["id"], text=q), _submit("Sent.")]}


@family("slack.thread_reply", "slack")
def slack_thread_reply(rng: random.Random, seed: int) -> Task | None:
    """Answer a colleague's question in its thread, not the channel."""
    servers = _servers("slack")
    st = _world(servers, seed).instances["slack"].state
    bot = st["bot_user"]
    cands = []
    for cid, ch in st["channels"].items():
        if bot not in ch["members"] or ch.get("is_im"):
            continue
        top = [m for m in st["messages"][cid] if not m.get("parent_user_id") and m["user"] != bot]
        for m in top:
            if m["text"].endswith("?") and sum(x["text"] == m["text"] for x in top) == 1:
                cands.append((ch["name"], cid, m))
    if not cands:
        return None
    name, cid, m = rng.choice(sorted(cands, key=lambda c: c[2]["ts"]))
    who = st["users"][m["user"]]["real_name"]
    answer, key = rng.choice([("Priya's team owns it, I'll loop her in", "loop her in"),
                              ("Looking into it now", "looking into it"), ("Yes, let's do Thursday", "thursday")])
    return {"servers": servers,
            "task": f"In #{name}, {who} asked \"{m['text']}\". Reply in that thread with: \"{answer}\".",
            "checks": [
                {"name": "replied in the thread", "server": "slack", "state": "messages", "weight": 3,
                 "where": {"channel": name, "from_bot": True, "thread_ts": m["ts"], "text~": key}, "count": 1},
                {"name": "didn't post at the top level", "server": "slack", "state": "messages", "must": True,
                 "where": {"from_bot": True, "is_reply": False}, "count": 0}],
            "reference": [_call("slack__slack_reply_to_thread", channel_id=cid, thread_ts=m["ts"], text=answer),
                          _submit("Replied in the thread.")]}
