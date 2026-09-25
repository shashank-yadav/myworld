"""linear: generated volume, distractors and background activity (see ``toolsim.noise``)."""

from __future__ import annotations

import copy
import random
from typing import Any

from ...noise import COMPONENTS, CUSTOMERS, REPLIES, VENDORS, _pick, _rng, people


def generate(seed: dict[str, Any], cfg: dict[str, Any], rng_seed: int, now: str) -> dict[str, Any]:
    seed = copy.deepcopy(seed)
    rng = _rng(rng_seed, "linear")
    me = seed.get("viewer") or {"name": "Alex Rivera", "email": "alex@acme.com"}
    domain = me["email"].split("@")[1]
    users = seed.setdefault("users", [])
    have = {u["email"].lower() for u in users} | {me["email"].lower()}
    crowd = [c for c in people(rng_seed, domain, int(cfg.get("people", 10))) if c["email"] not in have]
    users.extend({"name": c["name"], "email": c["email"], "noise": True} for c in crowd)
    assignees = [None, me["email"], *(u["email"] for u in users)]
    teams = seed.get("teams") or []
    total = int(cfg.get("issues", 40))
    for t in teams:
        issues = t.setdefault("issues", [])
        seeded = [i["title"] for i in issues]
        labels = t.get("labels") or []
        for k in range(total // max(1, len(teams))):
            comp = _pick(rng, COMPONENTS)
            title = _pick(rng, [f"{comp.capitalize()} alerts are noisy", f"Migrate {comp} to the new cluster",
                                f"{_pick(rng, CUSTOMERS)} reports slow {comp}", f"Write runbook for {comp}",
                                f"Spike: evaluate {_pick(rng, VENDORS)}", f"Clean up unused {comp} flags",
                                f"{comp.capitalize()} error rate above SLO"])
            if cfg.get("distractors", True) and seeded and k < 2:
                title = f"{_pick(rng, ['Follow-up', 'Investigate', 'Old'])}: {_pick(rng, seeded)}"
            issue = {"noise": True, "title": title,
                     "state": rng.choices(["Backlog", "Todo", "In Progress", "In Review", "Done", "Canceled"],
                                          [25, 25, 15, 5, 25, 5])[0],
                     "priority": rng.choices([0, 1, 2, 3, 4], [20, 5, 20, 40, 15])[0],
                     "labels": rng.sample(labels, k=min(len(labels), rng.randint(0, 1))),
                     "comments": [{"author": _pick(rng, assignees[1:]), "body": _pick(rng, REPLIES)}
                                  for _ in range(rng.randint(0, 2))]}
            who = _pick(rng, assignees)
            if who:
                issue["assignee"] = who
            issues.append(issue)
    return seed


def ambient(server: str, seed: dict[str, Any], times: list[int], rng: random.Random, rng_seed: int,
            now: str) -> list[dict[str, Any]]:
    """Background activity: generated colleagues comment on generated issues."""
    targets = [f"{t['key']}-{n}" for t in seed.get("teams", [])  # identifiers are numbered in seed order
               for n, i in enumerate(t.get("issues", []), 1) if i.get("noise")]
    authors = [u["email"] for u in seed.get("users", []) if u.get("noise")]
    if not targets or not authors:
        return []
    return [{"server": server, "action": "add_comment", "at": f"+{t}s", "name": "ambient comment",
             "params": {"issue": _pick(rng, targets), "author": _pick(rng, authors),
                        "body": _pick(rng, REPLIES + ["Bumping this, a customer asked again."])}} for t in times]
