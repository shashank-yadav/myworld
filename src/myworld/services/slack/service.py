"""The slack service: identity, versions, seed format, state, errors and the version probe."""

from __future__ import annotations

from typing import Any

from ...core.instance import Instance, Service
from .model import V1, _add_reply, _delay, _dm_name, _msg, _new_user, _seed_time, _ts


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
