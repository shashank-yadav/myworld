"""Github task families."""

from __future__ import annotations

import random
from typing import Any

from .base import REPO, Task, _call, _servers, _submit, _world, family


@family("github.label", "github")
def github_label(rng: random.Random, seed: int) -> Task | None:
    """Add a label to every open issue matching a word, keeping existing labels."""
    servers = _servers("github")
    repo = _world(servers, seed).instances["github"].state["repos"]["acme/api"]
    issues = [i for i in repo["issues"].values() if not i["is_pull"] and i["state"] == "open"]
    words = sorted({w for i in issues for w in ("billing", "auth", "webhooks", "search", "dashboard", "export",
                                               "notifications", "invoices", "sdk") if w in i["title"].lower()})
    options = [w for w in words if 2 <= sum(w in i["title"].lower() for i in issues) <= 5]
    if not options:
        return None
    word = rng.choice(options)
    targets = sorted((i for i in issues if word in i["title"].lower()), key=lambda i: i["number"])
    label = f"area/{word}"
    return {"servers": servers,
            "task": f"In acme/api, add the label \"{label}\" to every open issue whose title mentions \"{word}\". "
                    "Keep their existing labels.",
            "checks": [*({"name": f"#{i['number']} labeled", "server": "github", "state": "repos[acme/api].issues",
                          "where": {"number": i["number"], "labels": [label]}, "count": 1} for i in targets),
                       *({"name": f"#{i['number']} kept its labels", "server": "github", "state": "repos[acme/api].issues",
                          "where": {"number": i["number"], "labels": i["labels"]}, "count": 1, "must": True}
                         for i in targets if i["labels"]),
                       {"name": "no other issue labeled", "server": "github", "state": "repos[acme/api].issues",
                        "where": {"labels": [label]}, "count": len(targets), "must": True}],
            "reference": [*(_call("github__update_issue", **REPO, issue_number=i["number"], labels=[*i["labels"], label])
                            for i in targets), _submit(f"Labeled {len(targets)} issues.")]}


@family("github.merge_if_green", "github")
def github_merge_if_green(rng: random.Random, seed: int) -> Task | None:
    """Merge a PR only if CI passes, otherwise comment."""
    servers = _servers("github")
    repo = _world(servers, seed).instances["github"].state["repos"]["acme/api"]
    cands = []
    for pr in repo["pulls"].values():
        sha = repo["branches"].get(pr["head"])
        sts = {x["state"] for x in repo["statuses"].get(sha, [])}
        if pr["draft"] or pr["merged"] or repo["issues"][pr["number"]]["state"] != "open" or not sts or "pending" in sts:
            continue
        cands.append((pr["number"], "failure" not in sts and "error" not in sts))
    if not cands:
        return None
    n, green = rng.choice(sorted(cands))
    merged_before = sum(p["merged"] for p in repo["pulls"].values())
    checks: list[dict[str, Any]] = [
        {"name": "no other pull request merged", "server": "github", "state": "repos[acme/api].pulls", "must": True,
         "where": {"merged": True, "!number": n}, "count": merged_before}]
    if green:
        checks.append({"name": f"merged #{n}", "server": "github", "state": "repos[acme/api].pulls", "weight": 3,
                       "where": {"number": n, "merged": True}, "count": 1})
        ref = [_call("github__get_pull_request_status", **REPO, pull_number=n),
               _call("github__merge_pull_request", **REPO, pull_number=n, merge_method="squash"), _submit("Merged.")]
    else:
        checks += [{"name": f"didn't merge #{n} on red CI", "server": "github", "state": "repos[acme/api].pulls",
                    "must": True, "where": {"number": n, "merged": True}, "count": 0},
                   {"name": "explained why on the PR", "server": "github", "calls": "add_issue_comment", "weight": 2,
                    "where": {"args.issue_number": n}, "min": 1}]
        ref = [_call("github__get_pull_request_status", **REPO, pull_number=n),
               _call("github__add_issue_comment", **REPO, issue_number=n, body="Not merging: CI is failing on this PR."),
               _submit("CI is failing, so I commented instead of merging.")]
    return {"servers": servers,
            "task": f"Pull request #{n} in acme/api: if its CI is passing, squash-merge it. If it isn't, don't merge; "
                    "leave a comment on the PR saying CI is failing.",
            "checks": checks, "reference": ref}
