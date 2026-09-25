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


@family("jira.to_review", "jira")
def jira_to_review(rng: random.Random, seed: int) -> Task | None:
    """Move a To Do issue to In Review (through the workflow) with a comment."""
    servers = _servers("jira")
    st = _world(servers, seed).instances["jira"].state
    todo = sorted((i["key"] for i in st["issues"].values() if i["status"] == "To Do"),
                  key=lambda k: (k.split("-")[0], int(k.split("-")[1])))
    if not todo:
        return None
    key = rng.choice(todo)
    note = rng.choice(["Fix is up, ready for review", "Patched on staging, please review", "PR linked, needs a review"])
    return {"servers": servers,
            "task": f"Move {key} to In Review and add the comment \"{note}\".",
            "checks": [
                {"name": f"{key} is In Review", "server": "jira", "state": "issues", "weight": 2,
                 "where": {"key": key, "status": "In Review"}, "count": 1},
                {"name": "commented", "server": "jira", "state": "issues",
                 "where": {"key": key, "comments.body~": note}, "count": 1},
                {"name": "no other issue moved", "server": "jira", "calls": "jira_transition_issue", "must": True,
                 "where": {"!args.issue_key": key}, "count": 0}],
            "reference": [_call("jira__jira_get_transitions", issue_key=key),
                          _call("jira__jira_transition_issue", issue_key=key, transition_id="11"),
                          _call("jira__jira_transition_issue", issue_key=key, transition_id="21"),
                          _call("jira__jira_add_comment", issue_key=key, body=note), _submit("Done.")]}
