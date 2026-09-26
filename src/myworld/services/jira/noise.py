"""jira: generated volume, distractors and background activity (see ``myworld.noise``)."""

from __future__ import annotations

import copy
import datetime as dt
import random
from typing import Any

from ...noise import COMPONENTS, CUSTOMERS, REPLIES, VENDORS, _now, _pick, _rng, people


def generate(seed: dict[str, Any], cfg: dict[str, Any], rng_seed: int, now: str) -> dict[str, Any]:
    seed = copy.deepcopy(seed)
    rng, t_now = _rng(rng_seed, "jira"), _now(now)
    me = seed.get("user") or {"account_id": "alex", "email": "alex@acme.com"}
    domain = (me.get("email") or "alex@acme.com").split("@")[1]
    users = seed.setdefault("users", [])
    have = {u["account_id"] for u in users} | {me["account_id"]}
    crowd = [c for c in people(rng_seed, domain, int(cfg.get("people", 10))) if c["handle"] not in have]
    users.extend({"account_id": c["handle"], "display_name": c["name"], "email": c["email"], "noise": True} for c in crowd)
    assignees = [None, me["account_id"], *(u["account_id"] for u in users)]
    projects = seed.get("projects") or []
    total = int(cfg.get("issues", 60))
    for p in projects:
        issues = p.setdefault("issues", [])
        keyed = any(i.get("key") for i in issues)
        n = max([int(str(i["key"]).rsplit("-", 1)[1]) for i in issues if i.get("key")] or [len(issues)])
        seeded = [i["summary"] for i in issues]
        for k in range(total // max(1, len(projects))):
            comp = _pick(rng, COMPONENTS)
            summary = _pick(rng, [f"{comp.capitalize()} alerts are noisy", f"Migrate {comp} to the new cluster",
                                  f"{_pick(rng, CUSTOMERS)} reports slow {comp}", f"Write runbook for {comp}",
                                  f"Spike: evaluate {_pick(rng, VENDORS)}", f"Clean up unused {comp} flags",
                                  f"{comp.capitalize()} error rate above SLO"])
            if cfg.get("distractors", True) and seeded and k < 2:
                summary = f"{_pick(rng, ['Follow-up', 'Investigate', 'Old'])}: {_pick(rng, seeded)}"
            n += 1
            created = t_now - dt.timedelta(days=rng.uniform(1, 90))
            issue = {"noise": True, "summary": summary, "type": rng.choices(["Task", "Bug", "Story"], [45, 35, 20])[0],
                     "status": rng.choices(["To Do", "In Progress", "In Review", "Done"], [40, 20, 10, 30])[0],
                     "priority": rng.choices(["Highest", "High", "Medium", "Low", "Lowest"], [5, 20, 50, 20, 5])[0],
                     "assignee": _pick(rng, assignees), "labels": rng.sample(["backend", "infra", "tech-debt", "customer"], k=rng.randint(0, 2)),
                     "created": created.strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
                     "comments": [{"author": _pick(rng, assignees[1:]), "body": _pick(rng, REPLIES)} for _ in range(rng.randint(0, 2))]}
            if keyed:
                issue["key"] = f"{p['key']}-{n}"
            issues.append(issue)
    return seed


def ambient(server: str, seed: dict[str, Any], times: list[int], rng: random.Random, rng_seed: int,
            now: str) -> list[dict[str, Any]]:
    """Background activity for one run: world events at the given offsets (seconds)."""
    events: list[dict[str, Any]] = []
    targets = [i.get("key") or f"{p['key']}-{n}" for p in seed.get("projects", [])  # unkeyed: numbered in order
               for n, i in enumerate(p.get("issues", []), 1) if i.get("noise")]
    authors = [u["account_id"] for u in seed.get("users", []) if u.get("noise")]
    for t in times:
        if targets and authors:
            events.append({"server": server, "action": "add_comment", "at": f"+{t}s", "name": "ambient comment",
                           "params": {"issue_key": _pick(rng, targets), "author": _pick(rng, authors),
                                      "body": _pick(rng, REPLIES + ["Bumping priority, customer asked again."])}})
    return events
