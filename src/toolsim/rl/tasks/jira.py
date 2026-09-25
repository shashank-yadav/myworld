"""Jira task families."""

from __future__ import annotations

import random

from .base import Task, _call, _servers, _submit, _world, family


@family("jira.reassign", "jira")
def jira_reassign(rng: random.Random, seed: int) -> Task | None:
    """Reassign someone's unfinished issues in one project."""
    servers = _servers("jira")
    st = _world(servers, seed).instances["jira"].state
    issues = list(st["issues"].values())
    pairs = []
    for uid in sorted(st["users"]):
        for proj in sorted(st["projects"]):
            open_ = [i for i in issues if i["assignee"] == uid and i["project"] == proj and i["status"] != "Done"]
            if 2 <= len(open_) <= 6:
                pairs.append((uid, proj))
    if not pairs:
        return None
    uid, proj = rng.choice(pairs)
    other = rng.choice(sorted(u for u in st["users"] if u != uid))
    targets = sorted((i for i in issues if i["assignee"] == uid and i["project"] == proj and i["status"] != "Done"),
                     key=lambda i: int(i["key"].split("-")[1]))
    who, to = st["users"][uid], st["users"][other]
    done = sum(i["assignee"] == uid and i["status"] == "Done" for i in issues)
    elsewhere = sum(i["assignee"] == uid and i["project"] != proj and i["status"] != "Done" for i in issues)
    return {"servers": servers,
            "task": f"{who['display_name']} is going on leave. Reassign all of their unfinished issues in the {proj} "
                    f"project to {to['display_name']}. Leave their Done issues and other projects alone.",
            "checks": [*({"name": f"{i['key']} reassigned", "server": "jira", "state": "issues",
                          "where": {"key": i["key"], "assignee": other}, "count": 1} for i in targets),
                       {"name": "Done issues untouched", "server": "jira", "state": "issues", "must": True,
                        "where": {"assignee": uid, "status": "Done"}, "count": done},
                       {"name": "other projects untouched", "server": "jira", "state": "issues", "must": True,
                        "where": {"assignee": uid, "!project": proj, "!status": "Done"}, "count": elsewhere}],
            "reference": [*(_call("jira__jira_assign_issue", issue_key=i["key"], assignee=to["email"] or other)
                            for i in targets), _submit(f"Reassigned {len(targets)} issues.")]}
