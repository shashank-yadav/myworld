"""The github service: identity, versions, seed format, state, errors and the version probe."""

from __future__ import annotations

from typing import Any

from ...core.instance import Instance, Service
from .model import SEARCH_LAG, V1, V2, _comment, _commit, _delay, _new_issue, _new_pull, _new_repo, _put_blob, _user


class GitHub(Service):
    name = "github"
    title = "GitHub"
    description = "Simulated GitHub. Behaves like the GitHub MCP server; nothing is really pushed."

    versions = {"2026-09-25": "Initial release: 26 tools modeled on the reference GitHub MCP server.",
                V1: "Protected branches reject direct pushes, required approving reviews, closing keywords close "
                    "issues on merge, simulated CI runs on every push.",
                V2: "Search is eventually consistent, like GitHub's: issues/PRs and repos show up in search about "
                    "a minute after they change, code about five minutes (the list/get tools are immediate)."}

    def probe(self, ctx: Instance) -> None:
        c = ctx.call
        o = {"owner": "acme", "repo": "api"}
        c("list_issues", o)
        c("search_issues", {"q": "repo:acme/api is:open label:bug"})
        f = c("get_file_contents", {**o, "path": "README.md"})
        c("create_or_update_file", {**o, "path": "README.md", "content": "x", "message": "m", "branch": "main"})
        c("create_or_update_file", {**o, "path": "README.md", "content": "# Acme\n", "message": "Update README",
                                    "branch": "main", "sha": f.data["sha"]})
        c("create_branch", {**o, "branch": "docs"})
        c("push_files", {**o, "branch": "docs", "files": [{"path": "docs/a.md", "content": "hi"}], "message": "docs"})
        c("create_pull_request", {**o, "title": "Docs", "head": "docs", "base": "main"})
        c("create_pull_request", {**o, "title": "dup", "head": "fix/flaky-retry", "base": "main"})
        c("get_pull_request", {**o, "pull_number": 4})
        c("get_pull_request_files", {**o, "pull_number": 4})
        c("get_pull_request_status", {**o, "pull_number": 4})
        c("create_pull_request_review", {**o, "pull_number": 4, "body": "LGTM", "event": "APPROVE"})
        c("merge_pull_request", {**o, "pull_number": 4, "merge_method": "squash"})
        c("merge_pull_request", {**o, "pull_number": 4})
        c("list_commits", {**o, "per_page": "5"})
        c("search_code", {"q": "backoff repo:acme/api"})
        c("add_issue_comment", {**o, "issue_number": 1, "body": "Fixed by #4"})
        c("update_issue", {**o, "issue_number": 1, "state": "closed"})
        if ctx.at_least(V1):
            c("push_files", {**o, "branch": "main", "files": [{"path": "x.md", "content": "x"}], "message": "direct"})
            c("create_branch", {**o, "branch": "fix/keys"})
            c("push_files", {**o, "branch": "fix/keys", "message": "Rotate keys",
                             "files": [{"path": "docs/keys.md", "content": "How to rotate keys"}]})
            pr = c("create_pull_request", {**o, "title": "Key rotation docs", "head": "fix/keys", "base": "main",
                                           "body": "Closes #2"}).data["number"]
            c("get_pull_request_status", {**o, "pull_number": pr})
            ctx.advance(300)
            c("get_pull_request_status", {**o, "pull_number": pr})
            ctx.state["viewer"] = "john-park"
            c("create_pull_request_review", {**o, "pull_number": pr, "body": "ok", "event": "APPROVE"})
            ctx.state["viewer"] = "alex-rivera"
            c("merge_pull_request", {**o, "pull_number": pr, "merge_method": "squash"})
            c("get_issue", {**o, "issue_number": 2})
            c("push_files", {**o, "branch": "fix/flaky-retry", "message": "wip",
                             "files": [{"path": "tests/test_x.py", "content": "def test_x():\n    assert False\n"}]})
            ctx.advance(300)
            c("get_pull_request_status", {**o, "pull_number": 4})
        if ctx.at_least(V2):
            c("create_issue", {**o, "title": "Webhook retries exhaust connection pool", "labels": ["bug"]})
            c("search_issues", {"q": "repo:acme/api webhook retries"})
            c("list_issues", {**o, "state": "open"})
            ctx.advance(90)
            c("search_issues", {"q": "repo:acme/api webhook retries"})
            c("search_code", {"q": "rotate repo:acme/api"})

    def default_seed(self) -> dict[str, Any]:
        retry_v1 = ("import random, time\n\n\ndef backoff(attempt: int) -> float:\n"
                    "    jitter = random.random()\n    return min(30, 2 ** attempt) + jitter\n")
        retry_v2 = ("import random, time\n\nrng = random.Random()\n\n\ndef backoff(attempt: int) -> float:\n"
                    "    jitter = rng.random()\n    return min(30, 2 ** attempt) + jitter\n")
        return {
            "viewer": "alex-rivera",
            "users": [{"login": "alex-rivera", "name": "Alex Rivera"}, {"login": "john-park", "name": "John Park"},
                      {"login": "priya-shah", "name": "Priya Shah"}, {"login": "acme", "name": "Acme", "type": "Organization"}],
            "repos": [{
                "name": "acme/api", "description": "Acme public API", "private": True,
                "files": {"README.md": "# Acme API\n\nPayments and billing API.\n", "src/retry.py": retry_v1,
                          "src/app.py": "from .retry import backoff\n\n\ndef handler(req):\n    return {'ok': True}\n",
                          "tests/test_retry.py": "from src.retry import backoff\n\n\ndef test_backoff():\n    assert backoff(0) < 2\n"},
                "branches": {"fix/flaky-retry": {"files": {"src/retry.py": retry_v2}, "author": "john-park",
                                                 "message": "Seed retry jitter from a dedicated RNG"}},
                "labels": ["bug", "p1", "question", "enhancement"],
                "protected": {"main": {"required_checks": ["ci"]}},
                "ci": {"contexts": ["ci"], "duration": "4m", "fail_if": [{"path": "tests/", "content~": "assert False"}]},
                "statuses": {"fix/flaky-retry": [{"context": "ci", "state": "success", "description": "142 tests passed"}]},
                "issues": [
                    {"title": "Retries hammer the API during outages", "body": "Backoff has no cap on jitter.",
                     "labels": ["bug", "p1"], "author": "priya-shah",
                     "comments": [{"author": "john-park", "body": "I have a fix in progress."}]},
                    {"title": "How do I rotate API keys?", "labels": ["question"], "author": "john-park"},
                    {"title": "Add idempotency keys to POST /charges", "labels": ["enhancement"], "author": "alex-rivera",
                     "state": "closed"},
                ],
                "pulls": [{"title": "Fix flaky retry jitter", "head": "fix/flaky-retry", "base": "main",
                           "author": "john-park", "body": "Fixes the flaky backoff test."}],
            }, {
                "name": "acme/web", "description": "Marketing site", "private": False,
                "files": {"README.md": "# acme.com\n", "index.html": "<h1>Acme</h1>\n"},
            }],
        }

    def initial_state(self, seed: dict[str, Any], ctx: Instance) -> dict[str, Any]:
        state: dict[str, Any] = {"viewer": seed.get("viewer", "alex-rivera"), "users": {}, "repos": {}, "blobs": {}}
        for u in seed.get("users", []):
            _user(ctx, state, u["login"], u.get("name"), u.get("type", "User"))
            if u.get("email"):
                state["users"][u["login"]]["email"] = u["email"]
        _user(ctx, state, state["viewer"])
        for r in seed.get("repos", []):
            owner, name = r["name"].split("/", 1)
            _user(ctx, state, owner, type_="Organization" if owner != state["viewer"] else "User")
            repo = _new_repo(ctx, state, owner, name, r.get("description"), r.get("private", False),
                             r.get("default_branch", "main"), r.get("files") or {"README.md": f"# {name}\n"},
                             author=r.get("author", state["viewer"]))
            for lab in r.get("labels", []):
                repo["labels"][lab] = {"name": lab, "color": ctx.hex(6), "default": lab in ("bug", "enhancement", "question")}
            for bname, b in (r.get("branches") or {}).items():
                base = repo["branches"][b.get("from", repo["default_branch"])]
                tree = dict(repo["commits"][base]["tree"])
                tree.update({p: _put_blob(state, c) for p, c in (b.get("files") or {}).items()})
                repo["branches"][bname] = _commit(ctx, state, repo, tree, [base], b.get("message", f"Update {bname}"),
                                                  b.get("author", state["viewer"]))
            repo["protected"] = r.get("protected") or {}
            if ctx.at_least(V1) and r.get("ci"):
                repo["ci"] = {"contexts": list(r["ci"].get("contexts") or ["ci"]),
                              "duration": _delay(r["ci"].get("duration", "4m")),
                              "fail_if": list(r["ci"].get("fail_if") or []), "flaky": float(r["ci"].get("flaky", 0))}
            for ref, sts in (r.get("statuses") or {}).items():
                sha = repo["branches"].get(ref, ref)
                repo["statuses"][sha] = [{"context": s["context"], "state": s["state"],
                                          "description": s.get("description", "")} for s in sts]
            items = [("issue", i) for i in r.get("issues", [])] + [("pull", p) for p in r.get("pulls", [])]
            if any("number" in x for _, x in items):  # imported data: keep the real numbers
                items.sort(key=lambda kx: kx[1].get("number", 0))
            for kind, x in items:
                if x.get("number") and x["number"] > repo["next_number"]:
                    repo["next_number"] = int(x["number"])
                if kind == "issue":
                    issue = _new_issue(ctx, state, repo, x["title"], x.get("body"), x.get("author", state["viewer"]),
                                       x.get("labels"), x.get("assignees"))
                    if x.get("created"):  # imported or generated history
                        issue["created_at"] = issue["updated_at"] = x["created"]
                    if x.get("state") == "closed":
                        issue["state"], issue["closed_at"] = "closed", ctx.now().isoformat()
                    for c in x.get("comments", []):
                        _comment(ctx, repo, issue["number"], c.get("author", state["viewer"]), c["body"])
                else:
                    pr = _new_pull(ctx, state, repo, x["title"], x.get("body"), x["head"], x["base"],
                                   x.get("author", state["viewer"]), x.get("draft", False))
                    if x.get("created"):
                        repo["issues"][pr["number"]].update(created_at=x["created"], updated_at=x["created"])
                    if x.get("state") == "closed":
                        repo["issues"][pr["number"]].update(state="closed", closed_at=ctx.now().isoformat())
                    for c in x.get("comments", []):
                        _comment(ctx, repo, pr["number"], c.get("author", state["viewer"]), c["body"])
        if ctx.at_least(V1):
            state["_v1"] = True
        if ctx.at_least(V2):
            state["_search_lag"] = {**SEARCH_LAG, **(seed.get("search_lag") or {})}
        return state

    actor_key = "viewer"

    def resolve_actor(self, state: dict[str, Any], identity: str) -> str:
        q = identity.strip().lower()
        for u in state["users"].values():
            if u["type"] == "User" and q in (u["login"].lower(), (u.get("email") or "").lower(), u["name"].lower()):
                return u["login"]
        raise ValueError(f"no GitHub user {identity} in this environment")

    def error_shape(self, status: int, message: str) -> Any:
        return {"message": message, "documentation_url": "https://docs.github.com/rest", "status": str(status)}

    def fault_error(self, fault: Any) -> tuple[Any, int]:
        if fault.kind == "rate_limit":
            return {"message": "API rate limit exceeded for user ID 1.", "documentation_url":
                    "https://docs.github.com/rest/overview/rate-limits-for-the-rest-api", "status": "403"}, 403
        if fault.kind == "server_error":
            return {"message": "Server Error", "status": "502"}, 502
        return super().fault_error(fault)
