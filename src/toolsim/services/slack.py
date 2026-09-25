"""Slack.

Tools, parameters and responses follow the reference Slack MCP server
(modelcontextprotocol/servers, src/slack), which returns raw Slack Web API JSON. The server
acts as a bot user, so Slack's membership rules apply: the bot can only read and post in
channels it has joined (``not_in_channel`` otherwise), a very common agent failure.

Seed format::

    team: {name: Acme, domain: acme}
    bot: {name: acme-assistant}
    users:
      - {name: john, real_name: John Park, email: john@acme.com, title: Engineering Manager, tz: America/Los_Angeles}
    channels:
      - name: general
        members: [bot, john, alex]          # user names; "bot" is the MCP server's bot user
        topic: Company-wide announcements
        messages:
          - {user: john, text: "Deploy is done", reactions: {tada: [alex]},
             replies: [{user: alex, text: "Nice!"}]}
    responders:                               # colleagues who answer (2026-09-25.1)
      - {user: john, when: {mention: true}, reply: "Looking now", delay: 2m}
      - {user: priya, when: {dm: true, "text~": deploy}, reply: "It's out", times: 2}

From 2026-09-25.1, like the real API: posting to a user ID (U...) opens a DM (a D... channel);
only slack_post_message accepts channel names, every other tool needs the channel ID
(``channel_not_found`` otherwise); and responders reply a while later. A responder's ``when`` can
have ``channel`` (name), ``dm``, ``mention`` (a real ``<@U123>`` mention: a plain "@john"
notifies nobody) and ``text~``; with no ``when`` it answers DMs and mentions. It replies in the
thread (``thread: false`` for top level; DMs reply top level) up to ``times`` times (default 1).
"""

from __future__ import annotations

import base64
import datetime as dt
import re
from typing import Annotated, Any

from ..core.instance import Instance, Service
from ..core.tools import ToolError, action, tool

V1 = "2026-09-25.1"
MAX_TEXT = 40000
EMOJI = {"thumbsup", "+1", "white_check_mark", "eyes", "tada", "heart", "rocket", "fire", "joy", "pray", "100", "raised_hands",
         "wave", "clap", "thinking_face", "warning", "x", "heavy_check_mark", "smile", "slightly_smiling_face", "ok_hand",
         "sob", "bug", "memo", "point_up", "star", "sparkles", "zap", "hourglass", "question"}


def _fail(error: str, **extra: Any) -> ToolError:
    return ToolError({"ok": False, "error": error, **extra})


class Slack(Service):
    name = "slack"
    title = "Slack"
    description = "Simulated Slack workspace. Behaves like the Slack MCP server; nothing is really posted."

    versions = {"2026-09-25": "Initial release: 8 tools modeled on the reference Slack MCP server.",
                V1: "DMs by posting to a user ID, channel IDs required outside slack_post_message, "
                    "colleagues who reply (responders)."}

    def probe(self, ctx: Instance) -> None:
        c = ctx.call
        chans = {ch["name"]: ch["id"] for ch in ctx.state["channels"].values()}
        c("slack_list_channels", {"limit": 2})
        c("slack_get_users", {})
        h = c("slack_get_channel_history", {"channel_id": chans["general"]})
        parent = next(m for m in h.data["messages"] if m.get("reply_count"))
        c("slack_get_thread_replies", {"channel_id": chans["general"], "thread_ts": parent["ts"]})
        c("slack_reply_to_thread", {"channel_id": chans["general"], "thread_ts": parent["ts"], "text": "On it"})
        c("slack_add_reaction", {"channel_id": chans["general"], "timestamp": parent["ts"], "reaction": "eyes"})
        c("slack_add_reaction", {"channel_id": chans["general"], "timestamp": parent["ts"], "reaction": "eyes"})
        c("slack_post_message", {"channel_id": chans["api-oncall"], "text": "Rolled back"})
        c("slack_post_message", {"channel_id": chans["random"], "text": "hi"})
        c("slack_post_message", {"channel_id": chans["leadership"], "text": "hi"})
        c("slack_get_user_profile", {"user_id": next(iter(ctx.state["users"]))})
        if ctx.at_least(V1):
            john = next(u["id"] for u in ctx.state["users"].values() if u["name"] == "john")
            dm = c("slack_post_message", {"channel_id": john, "text": "Are you around?"}).data["channel"]
            c("slack_get_channel_history", {"channel_id": dm})
            c("slack_get_channel_history", {"channel_id": "general"})
            c("slack_post_message", {"channel_id": "#general", "text": "by name"})
            c("slack_list_channels", {})

    def default_seed(self) -> dict[str, Any]:
        return {
            "team": {"name": "Acme", "domain": "acme"},
            "bot": {"name": "acme-assistant"},
            "users": [
                {"name": "alex", "real_name": "Alex Rivera", "email": "alex@acme.com", "title": "Head of Ops"},
                {"name": "john", "real_name": "John Park", "email": "john@acme.com", "title": "Engineering Manager"},
                {"name": "priya", "real_name": "Priya Shah", "email": "priya@acme.com", "title": "Staff Engineer"},
                {"name": "sam", "real_name": "Sam Lee", "email": "sam@acme.com", "title": "Support Lead", "tz": "Europe/London"},
            ],
            "channels": [
                {"name": "general", "topic": "Company-wide announcements", "members": ["bot", "alex", "john", "priya", "sam"],
                 "messages": [
                     {"user": "john", "text": "Reminder: Q4 planning docs due Friday."},
                     {"user": "priya", "text": "v2.3 is deployed to production :rocket:", "reactions": {"tada": ["alex", "sam"]},
                      "replies": [{"user": "alex", "text": "Great work!"}, {"user": "sam", "text": "Support is briefed."}]},
                 ]},
                {"name": "api-oncall", "topic": "Pages and incidents for the API", "members": ["bot", "john", "priya"],
                 "messages": [{"user": "priya", "text": "Error rate on /v1/charges is up to 2.1%, looking."}]},
                {"name": "random", "members": ["alex", "john", "priya", "sam"],  # the bot is NOT a member
                 "messages": [{"user": "sam", "text": "Anyone up for lunch?"}]},
                {"name": "leadership", "private": True, "members": ["alex", "john"],
                 "messages": [{"user": "alex", "text": "Headcount plan draft attached."}]},
            ],
        }

    def initial_state(self, seed: dict[str, Any], ctx: Instance) -> dict[str, Any]:
        team = seed.get("team") or {"name": "Acme", "domain": "acme"}
        state: dict[str, Any] = {"team": {"id": "T" + ctx.token(10), **team}, "users": {}, "channels": {},
                                 "messages": {}, "_by_name": {}}
        bot = seed.get("bot") or {"name": "assistant"}
        state["bot_user"] = _new_user(ctx, state, {"name": bot["name"], "real_name": bot.get("real_name", bot["name"]),
                                                   "is_bot": True})["id"]
        state["_by_name"]["bot"] = state["bot_user"]
        state["acting"] = state["bot_user"]  # who the current call acts as (see Service.actor_key)
        for u in seed.get("users", []):
            _new_user(ctx, state, u)
        if ctx.at_least(V1):
            state["_strict_ids"] = True
            state["_responders"] = []
            for r in seed.get("responders", []):
                if r.get("user") not in state["_by_name"]:
                    raise ValueError(f"responder {r.get('user')!r} is not a user in this workspace")
                state["_responders"].append({"user": state["_by_name"][r["user"]], "when": dict(r.get("when") or {}),
                                             "reply": r["reply"], "delay": _delay(r.get("delay", "1m")),
                                             "thread": r.get("thread", True), "times": int(r.get("times", 1)), "fired": 0})
        base = int(ctx.now().timestamp()) - 3 * 86400
        for c in seed.get("channels", []):
            cid = ("G" if c.get("private") else "C") + ctx.token(10)
            members = [state["_by_name"][m] for m in c.get("members", []) if m in state["_by_name"]]
            creator = members[0] if members else state["bot_user"]
            state["channels"][cid] = {
                "id": cid, "name": c["name"], "is_channel": not c.get("private"), "is_group": False, "is_im": False,
                "is_private": bool(c.get("private")), "is_archived": bool(c.get("archived")), "is_general": c["name"] == "general",
                "created": base, "creator": creator, "members": members,
                "topic": {"value": c.get("topic", ""), "creator": creator, "last_set": base},
                "purpose": {"value": c.get("purpose", ""), "creator": creator, "last_set": base},
            }
            state["messages"][cid] = []
            for i, m in enumerate(c.get("messages", [])):
                ts = _ts(ctx, _seed_time(m, base + 3600 * (i + 1)))
                msg = _msg(state, m["user"], m["text"], ts)
                for emoji, users in (m.get("reactions") or {}).items():
                    msg.setdefault("reactions", []).append({"name": emoji, "users": [state["_by_name"][u] for u in users],
                                                            "count": len(users)})
                state["messages"][cid].append(msg)
                for j, r in enumerate(m.get("replies", [])):
                    at = _seed_time(r, float(ts.split(".")[0]) + 60 * (j + 1))
                    _add_reply(state, cid, msg, _msg(state, r["user"], r["text"], _ts(ctx, at)))
        return state

    actor_key = "acting"  # agents act as the bot by default, or as any workspace member

    def resolve_actor(self, state: dict[str, Any], identity: str) -> str:
        q = identity.strip().lower().lstrip("@")
        if q in ("bot", state["bot_user"].lower()):
            return state["bot_user"]
        for u in state["users"].values():
            if q in (u["id"].lower(), u["name"].lower(), (u["profile"].get("email") or "").lower(), u["real_name"].lower()):
                return u["id"]
        raise ValueError(f"no Slack user {identity} in this workspace")

    def grading_view(self, state: dict[str, Any]) -> dict[str, Any]:
        """Messages annotated with channel name and author, so checks can say "posted in #api-oncall"."""
        names = {u["id"]: u["name"] for u in state["users"].values()}
        msgs = [{**m, "channel": ch["name"] or _dm_name(state, ch), "channel_id": cid, "is_dm": bool(ch.get("is_im")), "user_name": names.get(m["user"]),
                 "from_bot": m["user"] == state["bot_user"], "is_reply": bool(m.get("parent_user_id"))}
                for cid, ch in state["channels"].items() for m in state["messages"].get(cid, [])]
        return {**state, "messages": msgs,
                "channels": {c["name"] or _dm_name(state, c): c for c in state["channels"].values()}}

    def error_shape(self, status: int, message: str) -> Any:
        return {"ok": False, "error": {404: "not_found", 500: "internal_error", 502: "fatal_error",
                                       503: "service_unavailable", 504: "request_timeout"}.get(status, "fatal_error")}

    def fault_error(self, fault: Any) -> tuple[Any, int]:
        return {
            "rate_limit": ({"ok": False, "error": "ratelimited", "retry_after": fault.retry_after}, 429),
            "server_error": ({"ok": False, "error": "internal_error"}, 500),
            "timeout": ({"ok": False, "error": "request_timeout"}, 504),
            "timeout_after_commit": ({"ok": False, "error": "request_timeout"}, 504),
        }.get(fault.kind) or super().fault_error(fault)


# -- helpers -----------------------------------------------------------------------------

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


# -- tools -------------------------------------------------------------------------------

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


Slack.tools = [slack_list_channels, slack_post_message, slack_reply_to_thread, slack_add_reaction,
               slack_get_channel_history, slack_get_thread_replies, slack_get_users, slack_get_user_profile]


# -- world actions (triggered by environments, never by agents) --------------------------

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


Slack.actions = [act_post_message, act_set_member, act_archive_channel]
