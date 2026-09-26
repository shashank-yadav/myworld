"""slack: world actions (triggered by environments, never by agents)."""

from __future__ import annotations

from typing import Any

from ...core.instance import Instance
from ...core.tools import action
from .model import _add_reply, _fail, _find, _msg, _ts


def _chan_by_name(state: dict[str, Any], name: str) -> dict[str, Any]:
    ch = next((c for c in state["channels"].values() if c["id"] == name or (c["name"] and c["name"] == name.lstrip("#"))), None)
    if ch is None:
        raise _fail("channel_not_found")
    return ch


def _user_id(state: dict[str, Any], who: str) -> str:
    q = who.lower().lstrip("@")
    for u in state["users"].values():
        if q in (u["id"].lower(), u["name"].lower(), (u["profile"].get("email") or "").lower()):
            return u["id"]
    raise _fail("user_not_found")


@action("post_message")
def act_post_message(ctx: Instance, channel: str, user: str, text: str, thread_of: str | None = None,
                     thread_ts: str | None = None) -> str:
    """A colleague posts (optionally replying in the thread whose parent contains ``thread_of``, or
    under the message ``thread_ts``)."""
    s = ctx.state
    ch = _chan_by_name(s, channel)
    msg = _msg(s, _user_id(s, user), text, _ts(ctx))
    if thread_ts:
        _add_reply(s, ch["id"], _find(s, ch["id"], thread_ts), msg)
    elif thread_of:
        parent = next((m for m in s["messages"][ch["id"]] if not m.get("parent_user_id") and thread_of.lower()
                       in m["text"].lower()), None)
        if parent is None:
            raise _fail("thread_not_found")
        _add_reply(s, ch["id"], parent, msg)
    else:
        s["messages"][ch["id"]].append(msg)
    return msg["ts"]


@action("set_member")
def act_set_member(ctx: Instance, channel: str, user: str, member: bool = True) -> None:
    """Someone joins or is removed from a channel (use user 'bot' for the agent's bot)."""
    s = ctx.state
    ch = _chan_by_name(s, channel)
    uid = s["bot_user"] if user == "bot" else _user_id(s, user)
    if member and uid not in ch["members"]:
        ch["members"].append(uid)
    if not member and uid in ch["members"]:
        ch["members"].remove(uid)


@action("archive_channel")
def act_archive_channel(ctx: Instance, channel: str) -> None:
    """A channel is archived; posting to it fails with is_archived."""
    _chan_by_name(ctx.state, channel)["is_archived"] = True
