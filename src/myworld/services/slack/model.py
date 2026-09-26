"""slack: state model, constants and helpers shared by the tools."""

from __future__ import annotations

import base64
import datetime as dt
import re
from typing import Any

from ...core.instance import Instance
from ...core.tools import ToolError

V1 = "2026-09-25.1"
MAX_TEXT = 40000
EMOJI = {"thumbsup", "+1", "white_check_mark", "eyes", "tada", "heart", "rocket", "fire", "joy", "pray", "100", "raised_hands",
         "wave", "clap", "thinking_face", "warning", "x", "heavy_check_mark", "smile", "slightly_smiling_face", "ok_hand",
         "sob", "bug", "memo", "point_up", "star", "sparkles", "zap", "hourglass", "question"}


def _fail(error: str, **extra: Any) -> ToolError:
    return ToolError({"ok": False, "error": error, **extra})

def _new_user(ctx: Instance, state: dict[str, Any], u: dict[str, Any]) -> dict[str, Any]:
    uid = ("B" if u.get("is_bot") else "U") + ctx.token(10)
    tz = u.get("tz", "America/Los_Angeles")
    user = {"id": uid, "team_id": state["team"]["id"], "name": u["name"], "deleted": False,
            "real_name": u.get("real_name", u["name"]), "tz": tz, "is_bot": bool(u.get("is_bot")),
            "is_admin": bool(u.get("is_admin")),
            "profile": {"real_name": u.get("real_name", u["name"]), "display_name": u.get("display_name", u["name"]),
                        "email": u.get("email"), "title": u.get("title", ""), "phone": u.get("phone", ""),
                        "status_text": u.get("status_text", ""), "status_emoji": u.get("status_emoji", ""),
                        "image_72": f"https://avatars.slack-edge.com/{uid}_72.png"}}
    state["users"][uid] = user
    state["_by_name"][u["name"]] = uid
    return user


def _seed_time(m: dict[str, Any], default: float) -> float:
    """A seeded message's time: its ``at`` (ISO 8601) if given, else its place in the channel."""
    if not m.get("at"):
        return default
    t = dt.datetime.fromisoformat(str(m["at"]).replace("Z", "+00:00"))
    return (t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)).timestamp()


def _ts(ctx: Instance, seconds: float | None = None) -> str:
    return f"{int(seconds if seconds is not None else ctx.now().timestamp())}.{ctx.next('ts'):06d}"


def _msg(state: dict[str, Any], user: str, text: str, ts: str) -> dict[str, Any]:
    uid = state["_by_name"].get(user, user)
    msg: dict[str, Any] = {"type": "message", "user": uid, "text": text, "ts": ts, "team": state["team"]["id"]}
    if uid == state["bot_user"]:
        msg["bot_id"] = uid
    return msg


def _add_reply(state: dict[str, Any], cid: str, parent: dict[str, Any], reply: dict[str, Any]) -> None:
    reply["thread_ts"] = parent["ts"]
    reply["parent_user_id"] = parent["user"]
    parent["thread_ts"] = parent["ts"]
    parent["reply_count"] = parent.get("reply_count", 0) + 1
    parent["reply_users"] = list(dict.fromkeys([*parent.get("reply_users", []), reply["user"]]))
    parent["reply_users_count"] = len(parent["reply_users"])
    parent["latest_reply"] = reply["ts"]
    state["messages"][cid].append(reply)


def _dm_name(state: dict[str, Any], ch: dict[str, Any]) -> str:
    """How graders name a DM: dm:john (with the bot) or dm:alex,john."""
    names = {u["id"]: u["name"] for u in state["users"].values()}
    return "dm:" + ",".join(sorted(names[m] for m in ch["members"] if m != state["bot_user"]))


def _delay(v: Any) -> float:
    m = re.fullmatch(r"\+?(\d+(?:\.\d+)?)([smhd]?)", str(v).strip())
    if not m:
        raise ValueError(f"invalid delay {v!r} (e.g. 30s, 2m)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def _open_dm(ctx: Instance, state: dict[str, Any], user_id: str) -> dict[str, Any] | None:
    """Posting to a user ID lands in your DM with them (created on first use)."""
    user = state["users"].get(user_id)
    if user is None or user["deleted"]:
        return None
    me = state["acting"]
    if user_id == me:
        raise _fail("cannot_dm_bot" if user["is_bot"] else "channel_not_found")
    pair = sorted({me, user_id})
    ch = next((c for c in state["channels"].values() if c.get("is_im") and sorted(c["members"]) == pair), None)
    if ch is None:
        cid = "D" + ctx.token(10)
        ch = {"id": cid, "name": "", "is_channel": False, "is_group": False, "is_im": True, "is_private": True,
              "is_archived": False, "is_general": False, "created": int(ctx.now().timestamp()), "creator": me,
              "user": user_id, "members": pair}
        state["channels"][cid] = ch
        state["messages"][cid] = []
    return ch


def _channel(state: dict[str, Any], channel_id: str, *, need_member: bool = True, by_name: bool = False) -> dict[str, Any]:
    ch = state["channels"].get(channel_id)
    if ch is None and (by_name or not state.get("_strict_ids")):  # from 2026-09-25.1 only posting accepts names
        ch = next((c for c in state["channels"].values()
                   if c["name"] and (f"#{c['name']}" == channel_id or c["name"] == channel_id)), None)
    if ch is None or (ch["is_private"] and state["acting"] not in ch["members"]):
        raise _fail("channel_not_found")  # private channels are invisible to non-members
    if ch["is_archived"]:
        raise _fail("is_archived")
    if need_member and state["acting"] not in ch["members"]:
        raise _fail("not_in_channel")
    return ch


def _find(state: dict[str, Any], cid: str, ts: str) -> dict[str, Any]:
    msg = next((m for m in state["messages"][cid] if m["ts"] == ts), None)
    if msg is None:
        raise _fail("message_not_found")
    return msg


def _page(items: list[Any], limit: int, cursor: str | None, cap: int = 200) -> tuple[list[Any], str]:
    limit = max(1, min(limit or 100, cap))
    start = 0
    if cursor:
        try:
            start = int(base64.b64decode(cursor).decode().split(":")[1])
        except Exception:
            raise _fail("invalid_cursor") from None
    nxt = base64.b64encode(f"offset:{start + limit}".encode()).decode() if start + limit < len(items) else ""
    return items[start:start + limit], nxt


def _check_text(text: str) -> None:
    if not text or not text.strip():
        raise _fail("no_text")
    if len(text) > MAX_TEXT:
        raise _fail("msg_too_long")
