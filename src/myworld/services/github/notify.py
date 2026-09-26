"""github: email notifications (2026-09-25.3), as GitHub sends them to a thread's participants (its
author, assignees, commenters, reviewers and anyone @mentioned), never to whoever acted."""

from __future__ import annotations

import re
from typing import Any

from ...core.instance import Instance

FROM = "notifications@github.com"
MENTION = re.compile(r"(?<![\w/])@([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))")
REVIEWED = {"APPROVED": "approved these changes.", "CHANGES_REQUESTED": "requested changes on this pull request.",
            "COMMENTED": "reviewed this pull request."}


def _login(u: Any) -> str | None:
    return u.get("login") if isinstance(u, dict) else u


def notifications(ctx: Instance, before: dict[str, Any], after: dict[str, Any], by: str | None) -> None:
    users = after["users"]
    name = lambda login: users.get(login, {}).get("name") or login or "Someone"  # noqa: E731
    for full, r in after["repos"].items():
        old = before["repos"].get(full) or {"issues": {}, "comments": {}, "pulls": {}, "reviews": {}}
        for n, i in r["issues"].items():
            was = old["issues"].get(n)
            comments, reviews = r["comments"].get(n, []), r["reviews"].get(n, [])
            events: list[tuple[str, str, set[str], str]] = []  # (actor, text, recipients, reason)

            def mentioned(text: str | None) -> set[str]:
                return {m for m in MENTION.findall(text or "") if m in users}

            participants = {i["user"], *i["assignees"], *(c["user"] for c in comments),  # mentioning subscribes too
                            *(_login(rv["user"]) for rv in reviews), *mentioned(i["body"]),
                            *(m for c in comments for m in mentioned(c["body"]))}

            if was is None:
                events.append((i["user"], f"{i['body'] or ''}", set(i["assignees"]) | mentioned(i["body"]), "assign"))
            else:
                new = set(i["assignees"]) - set(was["assignees"])
                if new:
                    events.append((by, f"{name(by)} assigned you.", new, "assign"))
                if i["state"] != was["state"]:
                    pr = r["pulls"].get(n)
                    merged = pr and pr["merged"] and not old["pulls"].get(n, {}).get("merged")
                    text = (f"Merged #{n} into {pr['base']}." if merged else
                            f"Closed #{n}{' as ' + i['state_reason'].replace('_', ' ') if i.get('state_reason') else ''}."
                            if i["state"] == "closed" else f"Reopened #{n}.")
                    events.append((by, text, participants, "state_change"))
            seen = {c["id"] for c in old["comments"].get(n, [])}
            for c in comments:
                if c["id"] not in seen:
                    events.append((c["user"], c["body"], participants | mentioned(c["body"]), "comment"))
            seen = {rv["id"] for rv in old["reviews"].get(n, [])}
            for rv in reviews:
                if rv["id"] not in seen:
                    who = _login(rv["user"])
                    events.append((who, f"@{who} {REVIEWED[rv['state']]}" + (f"\n\n{rv['body']}" if rv["body"] else ""),
                                   participants | mentioned(rv["body"]), "comment"))
            kind = "PR" if i["is_pull"] else "Issue"
            url = f"https://github.com/{full}/{'pull' if i['is_pull'] else 'issues'}/{n}"
            for actor, text, recipients, reason in events:
                for login in sorted(recipients - {actor, None}):
                    email = users.get(login, {}).get("email")
                    if not email or users[login].get("type", "User") != "User":
                        continue
                    why = ("you were assigned." if reason == "assign" and login in i["assignees"] else
                           "you were mentioned." if f"@{login}" in text else
                           "you authored the thread." if login == i["user"] else "you are subscribed to this thread.")
                    ctx.notify(email, f"{name(actor)} <{FROM}>",
                               f"{'' if was is None and reason == 'assign' else 'Re: '}[{full}] {i['title']} ({kind} #{n})",
                               f"{text}\n\n—\nReply to this email directly or view it on GitHub:\n{url}\n"
                               f"You are receiving this because {why}", ["CATEGORY_UPDATES"])
