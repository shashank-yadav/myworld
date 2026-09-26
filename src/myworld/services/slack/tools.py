"""slack: agent-facing tools."""

from __future__ import annotations

from typing import Annotated, Any

from ...core.instance import Instance
from ...core.tools import tool
from .model import EMOJI, _add_reply, _channel, _check_text, _fail, _find, _msg, _open_dm, _page, _ts


@tool("slack_list_channels", read_only=True)
def slack_list_channels(ctx: Instance,
                        limit: Annotated[int | None, "Maximum number of channels to return (default 100, max 200)"] = 100,
                        cursor: Annotated[str | None, "Pagination cursor for next page of results"] = None) -> dict[str, Any]:
    """List public or pre-defined channels in the workspace with pagination"""
    s = ctx.state
    chans = [{k: v for k, v in c.items() if k != "members"} | {"num_members": len(c["members"]),
                                                               "is_member": s["acting"] in c["members"]}
             for c in s["channels"].values() if not c["is_private"] and not c["is_archived"] and not c.get("is_im")]
    page, nxt = _page(chans, limit or 100, cursor)
    return {"ok": True, "channels": page, "response_metadata": {"next_cursor": nxt}}


@tool("slack_post_message")
def slack_post_message(ctx: Instance,
                       channel_id: Annotated[str, "The ID of the channel to post to"],
                       text: Annotated[str, "The message text to post"]) -> dict[str, Any]:
    """Post a new message to a Slack channel"""
    s = ctx.state
    dm = _open_dm(ctx, s, channel_id) if s.get("_strict_ids") and channel_id[:1] in "UWB" else None
    ch = dm or _channel(s, channel_id, by_name=True)
    _check_text(text)
    msg = _msg(s, s["acting"], text, _ts(ctx))
    s["messages"][ch["id"]].append(msg)
    _responders(ctx, ch, msg)
    return {"ok": True, "channel": ch["id"], "ts": msg["ts"], "message": msg}


@tool("slack_reply_to_thread")
def slack_reply_to_thread(ctx: Instance,
                          channel_id: Annotated[str, "The ID of the channel containing the thread"],
                          thread_ts: Annotated[str, "The timestamp of the parent message in the format '1234567890.123456'. Timestamps in the format without the period can be converted by adding the period such that 6 numbers come after it."],
                          text: Annotated[str, "The reply text"]) -> dict[str, Any]:
    """Reply to a specific message thread in Slack"""
    s = ctx.state
    ch = _channel(s, channel_id)
    _check_text(text)
    parent = next((m for m in s["messages"][ch["id"]] if m["ts"] == thread_ts), None)
    if parent is None:
        raise _fail("thread_not_found")
    if parent.get("parent_user_id"):  # replying to a reply threads under the original parent
        parent = _find(s, ch["id"], parent["thread_ts"])
    reply = _msg(s, s["acting"], text, _ts(ctx))
    _add_reply(s, ch["id"], parent, reply)
    _responders(ctx, ch, reply)
    return {"ok": True, "channel": ch["id"], "ts": reply["ts"], "message": reply}


@tool("slack_add_reaction")
def slack_add_reaction(ctx: Instance,
                       channel_id: Annotated[str, "The ID of the channel containing the message"],
                       timestamp: Annotated[str, "The timestamp of the message to react to"],
                       reaction: Annotated[str, "The name of the emoji reaction (without ::)"]) -> dict[str, Any]:
    """Add a reaction emoji to a message"""
    s = ctx.state
    ch = _channel(s, channel_id)
    name = reaction.strip(":")
    if name not in EMOJI:
        raise _fail("invalid_name")
    msg = _find(s, ch["id"], timestamp)
    r = next((r for r in msg.setdefault("reactions", []) if r["name"] == name), None)
    if r and s["acting"] in r["users"]:
        raise _fail("already_reacted")
    if r is None:
        r = {"name": name, "users": [], "count": 0}
        msg["reactions"].append(r)
    r["users"].append(s["acting"])
    r["count"] += 1
    return {"ok": True}


@tool("slack_get_channel_history", read_only=True)
def slack_get_channel_history(ctx: Instance,
                              channel_id: Annotated[str, "The ID of the channel"],
                              limit: Annotated[int | None, "Number of messages to retrieve (default 10)"] = 10) -> dict[str, Any]:
    """Get recent messages from a channel"""
    s = ctx.state
    ch = _channel(s, channel_id)
    top = [m for m in s["messages"][ch["id"]] if not m.get("parent_user_id")]
    top.sort(key=lambda m: m["ts"], reverse=True)
    n = max(1, min(limit or 10, 1000))
    return {"ok": True, "messages": top[:n], "has_more": len(top) > n, "pin_count": 0}


@tool("slack_get_thread_replies", read_only=True)
def slack_get_thread_replies(ctx: Instance,
                             channel_id: Annotated[str, "The ID of the channel containing the thread"],
                             thread_ts: Annotated[str, "The timestamp of the parent message in the format '1234567890.123456'. Timestamps in the format without the period can be converted by adding the period such that 6 numbers come after it."]) -> dict[str, Any]:
    """Get all replies in a message thread"""
    s = ctx.state
    ch = _channel(s, channel_id)
    parent = next((m for m in s["messages"][ch["id"]] if m["ts"] == thread_ts), None)
    if parent is None:
        raise _fail("thread_not_found")
    replies = sorted((m for m in s["messages"][ch["id"]] if m.get("thread_ts") == thread_ts and m is not parent),
                     key=lambda m: m["ts"])
    return {"ok": True, "messages": [parent, *replies], "has_more": False}


@tool("slack_get_users", read_only=True)
def slack_get_users(ctx: Instance,
                    cursor: Annotated[str | None, "Pagination cursor for next page of results"] = None,
                    limit: Annotated[int | None, "Maximum number of users to return (default 100, max 200)"] = 100) -> dict[str, Any]:
    """Get a list of all users in the workspace with their basic profile information"""
    page, nxt = _page(list(ctx.state["users"].values()), limit or 100, cursor)
    return {"ok": True, "members": page, "response_metadata": {"next_cursor": nxt}}


@tool("slack_get_user_profile", read_only=True)
def slack_get_user_profile(ctx: Instance, user_id: Annotated[str, "The ID of the user"]) -> dict[str, Any]:
    """Get detailed profile information for a specific user"""
    u = ctx.state["users"].get(user_id)
    if u is None:
        raise _fail("user_not_found")
    return {"ok": True, "profile": u["profile"]}


def _responders(ctx: Instance, ch: dict[str, Any], msg: dict[str, Any]) -> None:
    """Colleagues who answer what the agent posts, a little later (2026-09-25.1)."""
    s = ctx.state
    for r in s.get("_responders") or []:
        uid, w = r["user"], r["when"]
        if uid == s["acting"] or uid not in ch["members"] or r["fired"] >= r["times"]:
            continue
        is_dm, mentioned = bool(ch.get("is_im")), f"<@{uid}>" in msg["text"]
        if not w:
            hit = is_dm or mentioned
        else:
            hit = ((not w.get("channel") or w["channel"].lstrip("#") == ch["name"])
                   and (not w.get("dm") or is_dm) and (not w.get("mention") or mentioned)
                   and (not w.get("text~") or w["text~"].lower() in msg["text"].lower()))
        if not hit:
            continue
        r["fired"] += 1
        thread = msg.get("thread_ts") or (msg["ts"] if r["thread"] and not is_dm else None)
        ctx.schedule(r["delay"], "post_message", {"channel": ch["id"], "user": uid, "text": r["reply"],
                                                  "thread_ts": thread}, reason=f"{s['users'][uid]['name']} replies")
