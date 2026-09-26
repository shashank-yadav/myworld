"""jira: email notifications (2026-09-25.3), as Jira Cloud's default notification scheme sends them:
to the assignee, reporter, watchers and earlier commenters, never to whoever made the change."""

from __future__ import annotations

from typing import Any

from ...core.instance import Instance
from .model import SITE

FROM = "jira@" + SITE.removeprefix("https://")


def _tell(lines: dict[str, list[str]], uids: set[str | None], line: str, actor: str | None) -> None:
    """Queue ``line`` for everyone in ``uids`` but the actor (one email per person per change)."""
    for uid in sorted(u for u in uids if u):
        if uid != actor:
            lines.setdefault(uid, []).append(line)


def notifications(ctx: Instance, before: dict[str, Any], after: dict[str, Any], by: str | None) -> None:
    users = after["users"]
    name = lambda uid: users.get(uid, {}).get("display_name", uid) if uid else "Someone"  # noqa: E731
    for key, i in after["issues"].items():
        old = before["issues"].get(key)
        lines: dict[str, list[str]] = {}  # recipient account id -> what happened, in order


        people = {i["assignee"], i["reporter"], *i.get("watchers", []), *(c["author"] for c in i["comments"])}
        if old is None:
            if i["assignee"]:
                _tell(lines, {i["assignee"]}, f"{name(by)} created {key} and assigned it to you.", by)
        else:
            if i["assignee"] != old["assignee"]:
                _tell(lines, {i["assignee"]}, f"{name(by)} assigned {key} to you.", by)
                _tell(lines, {old["assignee"], i["reporter"], *i.get("watchers", [])} - {i["assignee"]},
                     f"{name(by)} changed the assignee to {name(i['assignee']) if i['assignee'] else 'Unassigned'}.", by)
            if i["status"] != old["status"]:
                _tell(lines, people, f"{name(by)} changed the status to {i['status']}.", by)
        seen = {c["id"] for c in (old or {}).get("comments", [])}
        for c in i["comments"]:
            if c["id"] not in seen:
                _tell(lines, people, f"{name(c['author'])} commented on {key}:\n\n{c['body']}", c["author"])
        for uid, what in lines.items():
            email = users.get(uid, {}).get("email")
            if email:
                sender = f"{name(by)} (Jira) <{FROM}>" if by else f"Jira <{FROM}>"
                ctx.notify(email, sender, f"[JIRA] ({key}) {i['summary']}",
                           "\n\n".join(what) + f"\n\nView issue: {SITE}/browse/{key}", ["CATEGORY_UPDATES"])
