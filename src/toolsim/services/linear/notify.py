"""linear: email notifications (2026-09-25.2) from notifications@linear.app: assignments, comments,
@mentions and status changes reach the issue's creator, assignee and commenters, never whoever acted."""

from __future__ import annotations

import re
from typing import Any

from ...core.instance import Instance

FROM = "notifications@linear.app"
MENTION = re.compile(r"(?<![\w/])@([\w.-]+)")


def _tell(lines: dict[str, list[str]], uids: set[str | None], line: str, actor: str | None, users: dict[str, Any] | None = None) -> None:
    """Queue ``line`` for everyone in ``uids`` but the actor (one email per person per change)."""
    for uid in sorted(u for u in uids if u):
        if uid != actor and (users is None or uid in users):
            lines.setdefault(uid, []).append(line)


def notifications(ctx: Instance, before: dict[str, Any], after: dict[str, Any], by: str | None) -> None:
    users = after["users"]
    name = lambda uid: users[uid]["name"] if uid in users else "Someone"  # noqa: E731
    handles = {u["displayName"].lower(): uid for uid, u in users.items()}
    for iid, i in after["issues"].items():
        was = before["issues"].get(iid)
        comments = after["comments"].get(iid, [])
        people = {i["creatorId"], i["assigneeId"], *(c["userId"] for c in comments)}
        lines: dict[str, list[str]] = {}


        def mentioned(text: str) -> set[str]:
            return {handles[m.lower().rstrip(".")] for m in MENTION.findall(text or "") if m.lower().rstrip(".") in handles}

        if was is None:
            _tell(lines, {i["assigneeId"]}, f"{name(by)} assigned {i['identifier']} to you.", by, users)
            _tell(lines, mentioned(i.get("description") or ""), f"{name(by)} mentioned you in {i['identifier']}.", by, users)
        else:
            if i["assigneeId"] != was["assigneeId"]:
                _tell(lines, {i["assigneeId"]}, f"{name(by)} assigned {i['identifier']} to you.", by, users)
            if i["stateId"] != was["stateId"]:
                _tell(lines, {i["creatorId"], i["assigneeId"]},
                      f"{name(by)} changed the status to {after['states'][i['stateId']]['name']}.", by, users)
        seen = {c["id"] for c in before["comments"].get(iid, [])}
        for c in comments:
            if c["id"] not in seen:
                _tell(lines, people | mentioned(c["body"]), f"{name(c['userId'])} commented:\n\n{c['body']}", c["userId"], users)
        for uid, what in lines.items():
            ctx.notify(users[uid]["email"], f"Linear <{FROM}>", f"{i['identifier']} {i['title']}",
                       "\n\n".join(what) + f"\n\nView issue: {i['url']}", ["CATEGORY_UPDATES"])
