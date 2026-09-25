"""github: generated volume, distractors and background activity (see ``toolsim.noise``)."""

from __future__ import annotations

import copy
import random
from typing import Any

from ...noise import COMPONENTS, CUSTOMERS, REPLIES, _iso, _now, _past, _pick, _rng, people


def generate(seed: dict[str, Any], cfg: dict[str, Any], rng_seed: int, now: str) -> dict[str, Any]:
    seed = copy.deepcopy(seed)
    rng = _rng(rng_seed, "github")
    users = seed.setdefault("users", [])
    domain = next((u["email"].split("@")[1] for u in users if u.get("email")), "acme.com")
    crowd = people(rng_seed, domain, int(cfg.get("people", 10)), {u.get("name", "") for u in users})
    known = {u["login"] for u in users}
    users.extend({"login": c["login"], "name": c["name"], "email": c["email"], "noise": True}
                 for c in crowd if c["login"] not in known)
    logins = [c["login"] for c in crowd] + ["dependabot[bot]"]
    users.append({"login": "dependabot[bot]", "name": "dependabot[bot]"})
    repos = seed.get("repos") or []
    if not repos:
        return seed
    total = int(cfg.get("issues", 40))
    for idx, repo in enumerate(repos):
        n_issues = total * 2 // 3 if idx == 0 else total // (3 * max(1, len(repos) - 1))
        issues, pulls = repo.setdefault("issues", []), repo.setdefault("pulls", [])
        number = len(issues) + len(pulls) + max([x.get("number", 0) for x in issues + pulls] or [0])
        labels = set(repo.get("labels", [])) | {"bug", "enhancement", "question", "dependencies", "good first issue",
                                                "needs-triage", "docs"}
        repo["labels"] = sorted(labels)
        seeded_titles = [i["title"] for i in issues]
        for k in range(n_issues):
            comp = _pick(rng, COMPONENTS)
            kind = rng.choices(["bug", "feature", "question", "deps", "docs"], [40, 25, 10, 15, 10])[0]
            title, lab = {
                "bug": (f"{comp.capitalize()} {_pick(rng, ['returns 500 when payload is empty', 'times out for large accounts', 'double-sends webhooks', 'ignores the locale header', 'leaks memory under load'])}", ["bug"]),
                "feature": (f"Add {_pick(rng, ['pagination to', 'filtering to', 'retries to', 'audit log for', 'bulk export in'])} {comp}", ["enhancement"]),
                "question": (f"How do I configure {comp} for {_pick(rng, CUSTOMERS)}?", ["question"]),
                "deps": (f"Bump {_pick(rng, ['requests', 'urllib3', 'pydantic', 'fastapi', 'cryptography'])} from 1.{rng.randint(0, 9)}.{rng.randint(0, 9)} to 1.{rng.randint(10, 19)}.0", ["dependencies"]),
                "docs": (f"Document {comp} limits", ["docs"]),
            }[kind]
            if cfg.get("distractors", True) and seeded_titles and k < 2:
                title, lab = f"{_pick(rng, seeded_titles)} ({_pick(rng, ['again', 'follow-up', 'staging'])})", ["bug"]
            number += 1
            issues.append({"number": number, "noise": True, "title": title, "created": _iso(_past(rng, _now(now), 120)), "labels": lab + (["needs-triage"] if rng.random() < 0.3 else []),
                           "author": "dependabot[bot]" if kind == "deps" else _pick(rng, logins[:-1]),
                           "state": "closed" if rng.random() < 0.3 else "open",
                           "body": f"Seen on {_pick(rng, ['staging', 'production', 'local'])}. Steps to reproduce in the thread.",
                           "comments": [{"author": _pick(rng, logins[:-1]), "body": _pick(rng, REPLIES + ["Can't reproduce on main.", "PR incoming."])}
                                        for _ in range(rng.choices([0, 1, 2, 3], [40, 30, 20, 10])[0])]})
        if idx == 0:
            for k in range(int(cfg.get("pulls", 3))):
                branch = f"{_pick(rng, ['feat', 'fix', 'chore'])}/{_pick(rng, COMPONENTS).replace(' ', '-')}-{k}"
                repo.setdefault("branches", {})[branch] = {"files": {f"src/{branch.split('/')[1]}.py": f"# {branch}\n"},
                                                           "author": _pick(rng, logins[:-1]), "message": f"Work on {branch}"}
                number += 1
                comp = branch.split("/")[1].rsplit("-", 1)[0].replace("-", " ")
                title = {"feat": f"Add {comp} filters", "fix": f"Fix {comp} retry edge case", "chore": f"Refactor {comp} module"}
                pulls.append({"number": number, "title": title[branch.split("/")[0]],
                              "created": _iso(_past(rng, _now(now), 10)),
                              "head": branch, "base": repo.get("default_branch", "main"), "author": _pick(rng, logins[:-1]),
                              "draft": rng.random() < 0.25})
                repo.setdefault("statuses", {})[branch] = [{"context": "ci", "state": _pick(rng, ["success", "success", "failure", "pending"])}]
    return seed


def ambient(server: str, seed: dict[str, Any], times: list[int], rng: random.Random, rng_seed: int,
            now: str) -> list[dict[str, Any]]:
    """Background activity for one run: world events at the given offsets (seconds)."""
    events: list[dict[str, Any]] = []
    targets = [(r["name"], i["number"]) for r in seed.get("repos", []) for i in r.get("issues", [])
               if i.get("noise") and i.get("state", "open") == "open"]
    authors = [u["login"] for u in seed.get("users", []) if u.get("noise")]
    for t in times:
        if targets and authors:
            repo, n = _pick(rng, targets)
            events.append({"server": server, "action": "add_comment", "at": f"+{t}s", "name": "ambient comment",
                           "params": {"repo": repo, "number": n, "author": _pick(rng, authors),
                                      "body": _pick(rng, REPLIES + ["Still seeing this.", "Any update?"])}})
    return events
