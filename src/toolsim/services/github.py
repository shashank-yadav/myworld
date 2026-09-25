"""GitHub.

Tools and parameters follow the reference GitHub MCP server (modelcontextprotocol/servers,
src/github), which returns raw GitHub REST API JSON. The model is git-like: branches point at
commits, commits hold full file trees, blob SHAs are real git blob hashes. Pull requests know
their merge base, so conflicts, "no commits between" and required status checks behave like
GitHub.

Seed format::

    viewer: alex-rivera                     # the authenticated user (owner of the token)
    users: [{login: john-park, name: John Park}]
    repos:
      - name: acme/api
        description: Acme public API
        private: true
        files: {README.md: "# Acme API", src/retry.py: "..."}
        branches:                           # branched from the default branch, with changes
          fix/flaky-retry: {files: {src/retry.py: "..."}, author: john-park}
        labels: [bug, p1, question]
        protected: {main: {required_checks: [ci]}}
        statuses: {fix/flaky-retry: [{context: ci, state: success}]}
        issues: [{title: Retries hammer the API, labels: [bug], author: john-park,
                  comments: [{author: priya-shah, body: Seeing this too}]}]
        pulls: [{title: Fix flaky retry, head: fix/flaky-retry, base: main, author: john-park}]
"""

from __future__ import annotations

import base64
import difflib
import hashlib
from typing import Annotated, Any, Literal

from ..core.instance import Instance, Service
from ..core.tools import ToolError, action, tool

API = "https://api.github.com"


def _err(status: int, message: str, **extra: Any) -> ToolError:
    doc = "https://docs.github.com/rest"
    return ToolError({"message": message, "documentation_url": doc, "status": str(status), **extra}, status=status)


def _not_found() -> ToolError:
    return _err(404, "Not Found")


def _unprocessable(message: str, errors: list[dict[str, Any]] | None = None) -> ToolError:
    return _err(422, message, **({"errors": errors} if errors else {}))


def blob_sha(content: str) -> str:
    data = content.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class GitHub(Service):
    name = "github"
    title = "GitHub"
    description = "Simulated GitHub. Behaves like the GitHub MCP server; nothing is really pushed."

    versions = {"2026-09-25": "Initial release: 26 tools modeled on the reference GitHub MCP server."}

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
                    if x.get("state") == "closed":
                        issue["state"], issue["closed_at"] = "closed", ctx.now().isoformat()
                    for c in x.get("comments", []):
                        _comment(ctx, repo, issue["number"], c.get("author", state["viewer"]), c["body"])
                else:
                    pr = _new_pull(ctx, state, repo, x["title"], x.get("body"), x["head"], x["base"],
                                   x.get("author", state["viewer"]), x.get("draft", False))
                    if x.get("state") == "closed":
                        repo["issues"][pr["number"]].update(state="closed", closed_at=ctx.now().isoformat())
                    for c in x.get("comments", []):
                        _comment(ctx, repo, pr["number"], c.get("author", state["viewer"]), c["body"])
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


# -- model helpers -----------------------------------------------------------------------

def _iso(ctx: Instance) -> str:
    return ctx.now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _user(ctx: Instance, state: dict[str, Any], login: str, name: str | None = None, type_: str = "User") -> dict[str, Any]:
    if login not in state["users"]:
        uid = ctx.next("user_id", 1000)
        state["users"][login] = {"login": login, "id": uid, "node_id": f"U_kgDO{ctx.token(6)}", "type": type_,
                                 "name": name or login, "html_url": f"https://github.com/{login}",
                                 "url": f"{API}/users/{login}", "site_admin": False}
    return state["users"][login]


def _brief_user(state: dict[str, Any], login: str) -> dict[str, Any]:
    u = state["users"][login]
    return {k: u[k] for k in ("login", "id", "node_id", "type", "html_url", "url")}


def _put_blob(state: dict[str, Any], content: str) -> str:
    sha = blob_sha(content)
    state["blobs"][sha] = content
    return sha


def _commit(ctx: Instance, state: dict[str, Any], repo: dict[str, Any], tree: dict[str, str], parents: list[str],
            message: str, author: str) -> str:
    when = _iso(ctx)
    raw = f"tree {sorted(tree.items())}\nparents {parents}\nauthor {author} {when}\n\n{message}".encode()
    sha = hashlib.sha1(raw).hexdigest()
    u = state["users"].get(author) or _user(ctx, state, author)
    repo["commits"][sha] = {"sha": sha, "message": message, "tree": tree, "parents": parents,
                            "author": {"name": u["name"], "email": f"{author}@users.noreply.github.com", "date": when,
                                       "login": author}}
    return sha


def _new_repo(ctx: Instance, state: dict[str, Any], owner: str, name: str, description: str | None, private: bool,
              default_branch: str, files: dict[str, str], author: str) -> dict[str, Any]:
    full = f"{owner}/{name}"
    rid = ctx.next("repo_id", 700000000)
    repo = {"id": rid, "node_id": f"R_kgDO{ctx.token(6)}", "name": name, "full_name": full, "owner": owner,
            "private": private, "description": description, "default_branch": default_branch, "fork": False,
            "parent": None, "created_at": _iso(ctx), "updated_at": _iso(ctx), "branches": {}, "commits": {},
            "issues": {}, "comments": {}, "pulls": {}, "reviews": {}, "review_comments": {}, "statuses": {},
            "labels": {}, "protected": {}, "next_number": 1, "archived": False}
    state["repos"][full] = repo
    if files:
        tree = {p: _put_blob(state, c) for p, c in files.items()}
        repo["branches"][default_branch] = _commit(ctx, state, repo, tree, [], "Initial commit", author)
    return repo


def _repo(state: dict[str, Any], owner: str, repo: str) -> dict[str, Any]:
    r = state["repos"].get(f"{owner}/{repo}")
    if r is None:
        raise _not_found()
    return r


def _repo_json(state: dict[str, Any], r: dict[str, Any]) -> dict[str, Any]:
    out = {k: r[k] for k in ("id", "node_id", "name", "full_name", "private", "description", "fork", "created_at",
                             "updated_at", "default_branch", "archived")}
    out.update(owner=_brief_user(state, r["owner"]), html_url=f"https://github.com/{r['full_name']}",
               url=f"{API}/repos/{r['full_name']}", visibility="private" if r["private"] else "public",
               open_issues_count=sum(i["state"] == "open" for i in r["issues"].values()))
    if r["parent"]:
        out["parent"] = {"full_name": r["parent"]}
    return out


def _ref_sha(r: dict[str, Any], ref: str | None) -> str:
    ref = ref or r["default_branch"]
    if ref in r["branches"]:
        return r["branches"][ref]
    if ref in r["commits"]:
        return ref
    match = [s for s in r["commits"] if len(ref) >= 7 and s.startswith(ref)]
    if len(match) == 1:
        return match[0]
    raise _err(404, f"No commit found for the ref {ref}")


def _branch(r: dict[str, Any], name: str) -> str:
    if name not in r["branches"]:
        raise _err(404, f"Branch not found: {name}")
    return r["branches"][name]


def _ancestors(r: dict[str, Any], sha: str) -> list[str]:
    seen, order, stack = set(), [], [sha]
    while stack:
        s = stack.pop(0)
        if s in seen or s not in r["commits"]:
            continue
        seen.add(s)
        order.append(s)
        stack.extend(r["commits"][s]["parents"])
    return order


def _merge_base(r: dict[str, Any], a: str, b: str) -> str | None:
    anc_b = set(_ancestors(r, b))
    return next((s for s in _ancestors(r, a) if s in anc_b), None)


def _changes(state: dict[str, Any], old: dict[str, str], new: dict[str, str]) -> list[dict[str, Any]]:
    out = []
    for path in sorted(set(old) | set(new)):
        a, b = old.get(path), new.get(path)
        if a == b:
            continue
        before = state["blobs"].get(a, "") if a else ""
        after = state["blobs"].get(b, "") if b else ""
        diff = list(difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=3))[2:]
        adds = sum(1 for l in diff if l.startswith("+"))
        dels = sum(1 for l in diff if l.startswith("-"))
        out.append({"sha": b or a, "filename": path, "status": "added" if not a else "removed" if not b else "modified",
                    "additions": adds, "deletions": dels, "changes": adds + dels, "patch": "\n".join(diff)})
    return out


def _new_issue(ctx: Instance, state: dict[str, Any], r: dict[str, Any], title: str, body: str | None, author: str,
               labels: list[str] | None = None, assignees: list[str] | None = None) -> dict[str, Any]:
    n = r["next_number"]
    r["next_number"] += 1
    for lab in labels or []:
        r["labels"].setdefault(lab, {"name": lab, "color": ctx.hex(6), "default": False})
    issue = {"id": ctx.next("issue_id", 2400000000), "node_id": f"I_kwDO{ctx.token(10)}", "number": n,
             "title": title, "body": body, "user": author, "labels": list(labels or []),
             "assignees": list(assignees or []), "state": "open", "state_reason": None, "locked": False,
             "comments": 0, "created_at": _iso(ctx), "updated_at": _iso(ctx), "closed_at": None, "milestone": None,
             "is_pull": False}
    r["issues"][n] = issue
    r["comments"][n] = []
    return issue


def _issue_json(state: dict[str, Any], r: dict[str, Any], i: dict[str, Any]) -> dict[str, Any]:
    base = f"{API}/repos/{r['full_name']}"
    out = {k: i[k] for k in ("id", "node_id", "number", "title", "body", "state", "state_reason", "locked", "comments",
                             "created_at", "updated_at", "closed_at", "milestone")}
    out.update(url=f"{base}/issues/{i['number']}",
               html_url=f"https://github.com/{r['full_name']}/{'pull' if i['is_pull'] else 'issues'}/{i['number']}",
               user=_brief_user(state, i["user"]),
               labels=[{"name": l, "color": r["labels"].get(l, {}).get("color", "ededed")} for l in i["labels"]],
               assignees=[_brief_user(state, a) for a in i["assignees"] if a in state["users"]],
               author_association="MEMBER")
    if i["is_pull"]:
        out["pull_request"] = {"url": f"{base}/pulls/{i['number']}", "html_url": out["html_url"],
                               "merged_at": r["pulls"][i["number"]].get("merged_at")}
    return out


def _comment(ctx: Instance, r: dict[str, Any], number: int, author: str, body: str) -> dict[str, Any]:
    c = {"id": ctx.next("comment_id", 1900000000), "user": author, "body": body, "created_at": _iso(ctx),
         "updated_at": _iso(ctx)}
    r["comments"][number].append(c)
    r["issues"][number]["comments"] += 1
    r["issues"][number]["updated_at"] = _iso(ctx)
    return c


def _comment_json(state: dict[str, Any], r: dict[str, Any], number: int, c: dict[str, Any]) -> dict[str, Any]:
    return {"id": c["id"], "node_id": f"IC_kwDO{c['id']}", "body": c["body"], "user": _brief_user(state, c["user"]),
            "created_at": c["created_at"], "updated_at": c["updated_at"], "author_association": "MEMBER",
            "html_url": f"https://github.com/{r['full_name']}/issues/{number}#issuecomment-{c['id']}",
            "issue_url": f"{API}/repos/{r['full_name']}/issues/{number}"}


def _new_pull(ctx: Instance, state: dict[str, Any], r: dict[str, Any], title: str, body: str | None, head: str, base: str,
              author: str, draft: bool = False) -> dict[str, Any]:
    issue = _new_issue(ctx, state, r, title, body, author)
    issue["is_pull"] = True
    pr = {"number": issue["number"], "head": head, "base": base, "draft": draft, "merged": False, "merged_at": None,
          "merge_commit_sha": None, "merged_by": None, "maintainer_can_modify": True,
          "base_sha": r["branches"][base]}
    r["pulls"][issue["number"]] = pr
    r["reviews"][issue["number"]] = []
    r["review_comments"][issue["number"]] = []
    return pr


def _pr_state(state: dict[str, Any], r: dict[str, Any], pr: dict[str, Any]) -> dict[str, Any]:
    """Mergeability the way GitHub computes it: conflicts, draft, closed, required checks."""
    head_sha, base_sha = r["branches"].get(pr["head"]), r["branches"].get(pr["base"])
    if pr["merged"] or head_sha is None or base_sha is None:
        return {"mergeable": None, "mergeable_state": "unknown", "head_sha": head_sha, "base_sha": base_sha, "conflicts": []}
    mb = _merge_base(r, head_sha, base_sha)
    mb_tree = r["commits"][mb]["tree"] if mb else {}
    head_tree, base_tree = r["commits"][head_sha]["tree"], r["commits"][base_sha]["tree"]
    head_changed = {p for p in set(mb_tree) | set(head_tree) if mb_tree.get(p) != head_tree.get(p)}
    base_changed = {p for p in set(mb_tree) | set(base_tree) if mb_tree.get(p) != base_tree.get(p)}
    conflicts = sorted(p for p in head_changed & base_changed if head_tree.get(p) != base_tree.get(p))
    required = r["protected"].get(pr["base"], {}).get("required_checks", [])
    statuses = {s["context"]: s["state"] for s in r["statuses"].get(head_sha, [])}
    failing = [c for c in required if statuses.get(c) != "success"]
    if conflicts:
        ms = "dirty"
    elif pr["draft"]:
        ms = "draft"
    elif failing:
        ms = "blocked"
    elif mb != base_sha:
        ms = "behind"
    else:
        ms = "clean"
    return {"mergeable": not conflicts, "mergeable_state": ms, "head_sha": head_sha, "base_sha": base_sha,
            "merge_base": mb, "conflicts": conflicts, "failing_checks": failing, "head_changed": head_changed}


def _pr_json(state: dict[str, Any], r: dict[str, Any], pr: dict[str, Any]) -> dict[str, Any]:
    i = r["issues"][pr["number"]]
    st = _pr_state(state, r, pr)
    base_url = f"{API}/repos/{r['full_name']}"
    head_sha = st["head_sha"] or r["commits"] and next(iter(r["commits"]))
    return {
        "id": i["id"], "node_id": f"PR_kwDO{i['number']}", "number": pr["number"], "state": i["state"],
        "title": i["title"], "body": i["body"], "draft": pr["draft"], "locked": False,
        "user": _brief_user(state, i["user"]), "created_at": i["created_at"], "updated_at": i["updated_at"],
        "closed_at": i["closed_at"], "merged_at": pr["merged_at"], "merge_commit_sha": pr["merge_commit_sha"],
        "merged": pr["merged"], "mergeable": st["mergeable"], "mergeable_state": st["mergeable_state"],
        "merged_by": _brief_user(state, pr["merged_by"]) if pr["merged_by"] else None,
        "comments": i["comments"], "review_comments": len(r["review_comments"][pr["number"]]),
        "maintainer_can_modify": pr["maintainer_can_modify"],
        "head": {"label": f"{r['owner']}:{pr['head']}", "ref": pr["head"], "sha": head_sha},
        "base": {"label": f"{r['owner']}:{pr['base']}", "ref": pr["base"], "sha": st["base_sha"]},
        "labels": [{"name": l} for l in i["labels"]],
        "url": f"{base_url}/pulls/{pr['number']}", "html_url": f"https://github.com/{r['full_name']}/pull/{pr['number']}",
    }


def _pull(r: dict[str, Any], number: int) -> dict[str, Any]:
    pr = r["pulls"].get(number)
    if pr is None:
        raise _not_found()
    return pr


def _paginate(items: list[Any], page: Any, per_page: Any) -> list[Any]:
    page, per_page = max(1, int(page or 1)), max(1, min(int(per_page or 30), 100))
    return items[(page - 1) * per_page: page * per_page]


def _content_json(r: dict[str, Any], path: str, sha: str, content: str, ref: str) -> dict[str, Any]:
    name = path.rsplit("/", 1)[-1]
    return {"type": "file", "encoding": "base64", "size": len(content.encode()), "name": name, "path": path,
            "content": base64.encodebytes(content.encode()).decode(), "sha": sha,
            "url": f"{API}/repos/{r['full_name']}/contents/{path}?ref={ref}",
            "html_url": f"https://github.com/{r['full_name']}/blob/{ref}/{path}",
            "download_url": f"https://raw.githubusercontent.com/{r['full_name']}/{ref}/{path}"}


def _commit_json(r: dict[str, Any], sha: str) -> dict[str, Any]:
    c = r["commits"][sha]
    return {"sha": sha, "node_id": f"C_kwDO{sha[:10]}", "html_url": f"https://github.com/{r['full_name']}/commit/{sha}",
            "commit": {"message": c["message"], "author": {k: c["author"][k] for k in ("name", "email", "date")},
                       "committer": {k: c["author"][k] for k in ("name", "email", "date")}},
            "parents": [{"sha": p} for p in c["parents"]]}


def _search_terms(q: str) -> tuple[dict[str, list[str]], list[str]]:
    quals: dict[str, list[str]] = {}
    words: list[str] = []
    for tok in q.split():
        if ":" in tok and not tok.startswith(":"):
            k, v = tok.split(":", 1)
            quals.setdefault(k.lower(), []).append(v.strip('"').lower())
        else:
            words.append(tok.strip('"').lower())
    return quals, words


# -- tools: files & branches -------------------------------------------------------------

@tool("create_or_update_file")
def create_or_update_file(ctx: Instance,
                          owner: Annotated[str, "Repository owner (username or organization)"],
                          repo: Annotated[str, "Repository name"],
                          path: Annotated[str, "Path where to create/update the file"],
                          content: Annotated[str, "Content of the file"],
                          message: Annotated[str, "Commit message"],
                          branch: Annotated[str, "Branch to create/update the file in"],
                          sha: Annotated[str | None, "SHA of the file being replaced (required when updating existing files)"] = None) -> dict[str, Any]:
    """Create or update a single file in a GitHub repository"""
    s = ctx.state
    r = _repo(s, owner, repo)
    head = _branch(r, branch)
    tree = dict(r["commits"][head]["tree"])
    if path in tree:
        if not sha:
            raise _unprocessable('Invalid request.\n\n"sha" wasn\'t supplied.')
        if sha != tree[path]:
            raise _err(409, f"{path} does not match {sha}")
    elif sha:
        raise _err(404, "Not Found")
    tree[path] = _put_blob(s, content)
    new = _commit(ctx, s, r, tree, [head], message, s["viewer"])
    r["branches"][branch] = new
    r["updated_at"] = _iso(ctx)
    return {"content": _content_json(r, path, tree[path], content, branch), "commit": _commit_json(r, new)}


@tool("push_files")
def push_files(ctx: Instance,
               owner: Annotated[str, "Repository owner (username or organization)"],
               repo: Annotated[str, "Repository name"],
               branch: Annotated[str, "Branch to push to (e.g., 'main' or 'master')"],
               files: Annotated[list[dict], "Array of files to push, each {path, content}"],
               message: Annotated[str, "Commit message"]) -> dict[str, Any]:
    """Push multiple files to a GitHub repository in a single commit"""
    s = ctx.state
    r = _repo(s, owner, repo)
    head = _branch(r, branch)
    if not files:
        raise _unprocessable("Invalid request: files must not be empty")
    tree = dict(r["commits"][head]["tree"])
    for f in files:
        if not isinstance(f, dict) or "path" not in f or "content" not in f:
            raise _unprocessable("Invalid request: each file needs path and content")
        tree[f["path"]] = _put_blob(s, f["content"])
    new = _commit(ctx, s, r, tree, [head], message, s["viewer"])
    r["branches"][branch] = new
    return {"ref": f"refs/heads/{branch}", "node_id": f"REF_kwDO{new[:10]}",
            "url": f"{API}/repos/{r['full_name']}/git/refs/heads/{branch}", "object": {"sha": new, "type": "commit"}}


@tool("get_file_contents", read_only=True)
def get_file_contents(ctx: Instance,
                      owner: Annotated[str, "Repository owner (username or organization)"],
                      repo: Annotated[str, "Repository name"],
                      path: Annotated[str, "Path to the file or directory"],
                      branch: Annotated[str | None, "Branch to get contents from"] = None) -> Any:
    """Get the contents of a file or directory from a GitHub repository"""
    s = ctx.state
    r = _repo(s, owner, repo)
    ref = branch or r["default_branch"]
    tree = r["commits"][_ref_sha(r, ref)]["tree"]
    path = path.strip("/")
    if path in tree:
        return _content_json(r, path, tree[path], s["blobs"][tree[path]], ref)
    prefix = f"{path}/" if path else ""
    entries: dict[str, dict[str, Any]] = {}
    for p, sha in tree.items():
        if p.startswith(prefix):
            rest = p[len(prefix):]
            name = rest.split("/", 1)[0]
            is_dir = "/" in rest
            entries[name] = {"type": "dir" if is_dir else "file", "name": name, "path": prefix + name,
                             "sha": "" if is_dir else sha, "size": 0 if is_dir else len(s["blobs"][sha].encode()),
                             "url": f"{API}/repos/{r['full_name']}/contents/{prefix + name}?ref={ref}"}
    if not entries:
        raise _not_found()
    return sorted(entries.values(), key=lambda e: (e["type"] != "dir", e["name"]))


@tool("create_branch")
def create_branch(ctx: Instance,
                  owner: Annotated[str, "Repository owner (username or organization)"],
                  repo: Annotated[str, "Repository name"],
                  branch: Annotated[str, "Name for the new branch"],
                  from_branch: Annotated[str | None, "Optional: source branch to create from (defaults to the repository's default branch)"] = None) -> dict[str, Any]:
    """Create a new branch in a GitHub repository"""
    r = _repo(ctx.state, owner, repo)
    if branch in r["branches"]:
        raise _unprocessable("Reference already exists")
    sha = _branch(r, from_branch or r["default_branch"])
    r["branches"][branch] = sha
    return {"ref": f"refs/heads/{branch}", "node_id": f"REF_kwDO{sha[:10]}",
            "url": f"{API}/repos/{r['full_name']}/git/refs/heads/{branch}", "object": {"sha": sha, "type": "commit"}}


@tool("list_commits", read_only=True)
def list_commits(ctx: Instance,
                 owner: Annotated[str, "Repository owner (username or organization)"],
                 repo: Annotated[str, "Repository name"],
                 sha: Annotated[str | None, "SHA or branch name to start listing commits from"] = None,
                 page: Annotated[str | None, "Page number"] = None,
                 per_page: Annotated[str | None, "Results per page (max 100)"] = None) -> list[dict[str, Any]]:
    """Get list of commits of a branch in a GitHub repository"""
    r = _repo(ctx.state, owner, repo)
    return [_commit_json(r, c) for c in _paginate(_ancestors(r, _ref_sha(r, sha)), page, per_page)]


# -- tools: repositories -----------------------------------------------------------------

@tool("search_repositories", read_only=True)
def search_repositories(ctx: Instance,
                        query: Annotated[str, "Search query (see GitHub search syntax)"],
                        page: Annotated[int | None, "Page number for pagination (default: 1)"] = None,
                        perPage: Annotated[int | None, "Number of results per page (default: 30, max: 100)"] = None) -> dict[str, Any]:
    """Search for GitHub repositories"""
    s = ctx.state
    quals, words = _search_terms(query)
    hits = []
    for r in s["repos"].values():
        if r["private"] and r["owner"] != s["viewer"] and s["users"][r["owner"]]["type"] != "Organization":
            continue
        text = f"{r['full_name']} {r['description'] or ''}".lower()
        if any(w not in text for w in words):
            continue
        if any(r["owner"].lower() not in v for k, v in quals.items() if k in ("user", "org")):
            continue
        if "repo" in quals and r["full_name"].lower() not in quals["repo"]:
            continue
        hits.append(_repo_json(s, r))
    return {"total_count": len(hits), "incomplete_results": False, "items": _paginate(hits, page, perPage)}


@tool("create_repository")
def create_repository(ctx: Instance,
                      name: Annotated[str, "Repository name"],
                      description: Annotated[str | None, "Repository description"] = None,
                      private: Annotated[bool | None, "Whether the repository should be private"] = None,
                      autoInit: Annotated[bool | None, "Initialize with README.md"] = None) -> dict[str, Any]:
    """Create a new GitHub repository in your account"""
    s = ctx.state
    if f"{s['viewer']}/{name}" in s["repos"]:
        raise _unprocessable("Repository creation failed.", [{"resource": "Repository", "code": "custom", "field": "name",
                                                             "message": "name already exists on this account"}])
    r = _new_repo(ctx, s, s["viewer"], name, description, bool(private), "main",
                  {"README.md": f"# {name}\n"} if autoInit else {}, s["viewer"])
    return _repo_json(s, r)


@tool("fork_repository")
def fork_repository(ctx: Instance,
                    owner: Annotated[str, "Repository owner (username or organization)"],
                    repo: Annotated[str, "Repository name"],
                    organization: Annotated[str | None, "Optional: organization to fork to (defaults to your personal account)"] = None) -> dict[str, Any]:
    """Fork a GitHub repository to your account or specified organization"""
    s = ctx.state
    src = _repo(s, owner, repo)
    target_owner = organization or s["viewer"]
    full = f"{target_owner}/{src['name']}"
    if full in s["repos"]:
        return _repo_json(s, s["repos"][full])  # GitHub returns the existing fork
    _user(ctx, s, target_owner, type_="Organization" if organization else "User")
    fork = _new_repo(ctx, s, target_owner, src["name"], src["description"], src["private"], src["default_branch"], {},
                     s["viewer"])
    fork.update(fork=True, parent=src["full_name"], commits=dict(src["commits"]), branches=dict(src["branches"]))
    return _repo_json(s, fork)


# -- tools: issues -----------------------------------------------------------------------

def _issue(r: dict[str, Any], number: int) -> dict[str, Any]:
    i = r["issues"].get(number)
    if i is None:
        raise _not_found()
    return i


@tool("create_issue")
def create_issue(ctx: Instance,
                 owner: Annotated[str, "Repository owner"],
                 repo: Annotated[str, "Repository name"],
                 title: Annotated[str, "Issue title"],
                 body: Annotated[str | None, "Issue body"] = None,
                 assignees: Annotated[list[str] | None, "Usernames to assign"] = None,
                 milestone: Annotated[int | None, "Milestone number"] = None,
                 labels: Annotated[list[str] | None, "Labels to apply"] = None) -> dict[str, Any]:
    """Create a new issue in a GitHub repository"""
    s = ctx.state
    r = _repo(s, owner, repo)
    if not title.strip():
        raise _unprocessable("Validation Failed", [{"resource": "Issue", "code": "missing_field", "field": "title"}])
    for a in assignees or []:
        if a not in s["users"]:
            raise _unprocessable("Validation Failed", [{"resource": "Issue", "code": "invalid", "field": "assignees",
                                                       "value": a}])
    if milestone is not None:
        raise _unprocessable("Validation Failed", [{"resource": "Issue", "code": "invalid", "field": "milestone"}])
    return _issue_json(s, r, _new_issue(ctx, s, r, title, body, s["viewer"], labels, assignees))


@tool("list_issues", read_only=True)
def list_issues(ctx: Instance,
                owner: Annotated[str, "Repository owner"],
                repo: Annotated[str, "Repository name"],
                state: Annotated[Literal["open", "closed", "all"] | None, "Filter by state"] = None,
                labels: Annotated[list[str] | None, "Filter by labels"] = None,
                sort: Annotated[Literal["created", "updated", "comments"] | None, "Sort by"] = None,
                direction: Annotated[Literal["asc", "desc"] | None, "Sort direction"] = None,
                since: Annotated[str | None, "Filter by date (ISO 8601 timestamp)"] = None,
                page: Annotated[int | None, "Page number"] = None,
                per_page: Annotated[int | None, "Results per page"] = None) -> list[dict[str, Any]]:
    """List issues in a GitHub repository with filtering options (pull requests are included, as in the GitHub API)"""
    s = ctx.state
    r = _repo(s, owner, repo)
    want = state or "open"
    items = [i for i in r["issues"].values()
             if (want == "all" or i["state"] == want) and all(l in i["labels"] for l in labels or [])
             and (not since or i["updated_at"] >= since)]
    key = {"created": "created_at", "updated": "updated_at", "comments": "comments"}[sort or "created"]
    items.sort(key=lambda i: (i[key], i["number"]), reverse=(direction or "desc") == "desc")
    return [_issue_json(s, r, i) for i in _paginate(items, page, per_page)]


@tool("update_issue", idempotent=True)
def update_issue(ctx: Instance,
                 owner: Annotated[str, "Repository owner"],
                 repo: Annotated[str, "Repository name"],
                 issue_number: Annotated[int, "Issue number"],
                 title: Annotated[str | None, "New title"] = None,
                 body: Annotated[str | None, "New description"] = None,
                 state: Annotated[Literal["open", "closed"] | None, "New state"] = None,
                 labels: Annotated[list[str] | None, "New labels (replaces existing)"] = None,
                 assignees: Annotated[list[str] | None, "New assignees (replaces existing)"] = None,
                 milestone: Annotated[int | None, "New milestone number"] = None) -> dict[str, Any]:
    """Update an existing issue in a GitHub repository"""
    s = ctx.state
    r = _repo(s, owner, repo)
    i = _issue(r, issue_number)
    if title is not None:
        i["title"] = title
    if body is not None:
        i["body"] = body
    if labels is not None:
        for lab in labels:
            r["labels"].setdefault(lab, {"name": lab, "color": ctx.hex(6), "default": False})
        i["labels"] = list(labels)
    if assignees is not None:
        i["assignees"] = [a for a in assignees if a in s["users"]]
    if state is not None and state != i["state"]:
        if i["is_pull"] and r["pulls"][issue_number]["merged"]:
            raise _unprocessable("Validation Failed", [{"resource": "PullRequest", "code": "custom",
                                                       "message": "state cannot be changed. The pull request has been merged."}])
        i["state"] = state
        i["closed_at"] = _iso(ctx) if state == "closed" else None
        i["state_reason"] = "completed" if state == "closed" else "reopened"
    i["updated_at"] = _iso(ctx)
    return _issue_json(s, r, i)


@tool("add_issue_comment")
def add_issue_comment(ctx: Instance,
                      owner: Annotated[str, "Repository owner"],
                      repo: Annotated[str, "Repository name"],
                      issue_number: Annotated[int, "Issue number"],
                      body: Annotated[str, "Comment text"]) -> dict[str, Any]:
    """Add a comment to an existing issue"""
    s = ctx.state
    r = _repo(s, owner, repo)
    i = _issue(r, issue_number)
    if i["locked"]:
        raise _err(403, "Unable to create comment because issue is locked.")
    if not body.strip():
        raise _unprocessable("Validation Failed", [{"resource": "IssueComment", "code": "missing_field", "field": "body"}])
    return _comment_json(s, r, issue_number, _comment(ctx, r, issue_number, s["viewer"], body))


@tool("get_issue", read_only=True)
def get_issue(ctx: Instance,
              owner: Annotated[str, "Repository owner (username or organization)"],
              repo: Annotated[str, "Repository name"],
              issue_number: Annotated[int, "Issue number"]) -> dict[str, Any]:
    """Get details of a specific issue in a GitHub repository"""
    s = ctx.state
    r = _repo(s, owner, repo)
    return _issue_json(s, r, _issue(r, issue_number))


# -- tools: search -----------------------------------------------------------------------

@tool("search_code", read_only=True)
def search_code(ctx: Instance,
                q: Annotated[str, "Search query (GitHub code search syntax, e.g. 'backoff repo:acme/api')"],
                sort: Annotated[str | None, "Sort field (only 'indexed' is supported)"] = None,
                order: Annotated[Literal["asc", "desc"] | None, "Sort order"] = None,
                per_page: Annotated[int | None, "Results per page (max 100)"] = None,
                page: Annotated[int | None, "Page number"] = None) -> dict[str, Any]:
    """Search for code across GitHub repositories (default branches)"""
    s = ctx.state
    quals, words = _search_terms(q)
    if not words:
        raise _unprocessable("Validation Failed", [{"resource": "Search", "field": "q", "code": "missing"}])
    items = []
    for r in s["repos"].values():
        if "repo" in quals and r["full_name"].lower() not in quals["repo"]:
            continue
        if any(r["owner"].lower() not in v for k, v in quals.items() if k in ("user", "org")):
            continue
        head = r["branches"].get(r["default_branch"])
        for path, sha in (r["commits"][head]["tree"].items() if head else []):
            text = s["blobs"][sha].lower()
            if "path" in quals and not any(path.lower().startswith(p.strip("/")) for p in quals["path"]):
                continue
            if "extension" in quals and not any(path.endswith("." + e) for e in quals["extension"]):
                continue
            if all(w in text or w in path.lower() for w in words):
                items.append({"name": path.rsplit("/", 1)[-1], "path": path, "sha": sha, "score": 1.0,
                              "url": f"{API}/repos/{r['full_name']}/contents/{path}",
                              "html_url": f"https://github.com/{r['full_name']}/blob/{head}/{path}",
                              "repository": {"full_name": r["full_name"], "private": r["private"]}})
    return {"total_count": len(items), "incomplete_results": False, "items": _paginate(items, page, per_page)}


@tool("search_issues", read_only=True)
def search_issues(ctx: Instance,
                  q: Annotated[str, "Search query (GitHub issues search syntax, e.g. 'repo:acme/api is:open label:bug')"],
                  sort: Annotated[Literal["comments", "reactions", "created", "updated"] | None, "Sort field"] = None,
                  order: Annotated[Literal["asc", "desc"] | None, "Sort order"] = None,
                  per_page: Annotated[int | None, "Results per page (max 100)"] = None,
                  page: Annotated[int | None, "Page number"] = None) -> dict[str, Any]:
    """Search for issues and pull requests across GitHub repositories"""
    s = ctx.state
    quals, words = _search_terms(q)
    hits = []
    for r in s["repos"].values():
        if "repo" in quals and r["full_name"].lower() not in quals["repo"]:
            continue
        if any(r["owner"].lower() not in v for k, v in quals.items() if k in ("user", "org")):
            continue
        for i in r["issues"].values():
            kinds = quals.get("is", []) + quals.get("type", [])
            if "issue" in kinds and i["is_pull"] or ("pr" in kinds or "pull-request" in kinds) and not i["is_pull"]:
                continue
            if "open" in kinds and i["state"] != "open" or "closed" in kinds and i["state"] != "closed":
                continue
            if "merged" in kinds and not (i["is_pull"] and r["pulls"][i["number"]]["merged"]):
                continue
            if "state" in quals and i["state"] not in quals["state"]:
                continue
            if any(l not in [x.lower() for x in i["labels"]] for l in quals.get("label", [])):
                continue
            if "author" in quals and i["user"].lower() not in quals["author"]:
                continue
            if "assignee" in quals and not set(quals["assignee"]) & {a.lower() for a in i["assignees"]}:
                continue
            text = f"{i['title']} {i['body'] or ''}".lower()
            if any(w not in text for w in words):
                continue
            hits.append((r, i))
    key = {"comments": "comments", "created": "created_at", "updated": "updated_at"}.get(sort or "created", "created_at")
    hits.sort(key=lambda ri: ri[1][key], reverse=(order or "desc") == "desc")
    return {"total_count": len(hits), "incomplete_results": False,
            "items": [_issue_json(s, r, i) | {"repository_url": f"{API}/repos/{r['full_name']}"}
                      for r, i in _paginate(hits, page, per_page)]}


@tool("search_users", read_only=True)
def search_users(ctx: Instance,
                 q: Annotated[str, "Search query (GitHub users search syntax)"],
                 sort: Annotated[Literal["followers", "repositories", "joined"] | None, "Sort field"] = None,
                 order: Annotated[Literal["asc", "desc"] | None, "Sort order"] = None,
                 per_page: Annotated[int | None, "Results per page (max 100)"] = None,
                 page: Annotated[int | None, "Page number"] = None) -> dict[str, Any]:
    """Search for users on GitHub"""
    s = ctx.state
    quals, words = _search_terms(q)
    hits = [u for u in s["users"].values()
            if all(w in f"{u['login']} {u['name']}".lower() for w in words)
            and (not quals.get("type") or u["type"].lower() in quals["type"])]
    return {"total_count": len(hits), "incomplete_results": False,
            "items": [_brief_user(s, u["login"]) | {"score": 1.0} for u in _paginate(hits, page, per_page)]}


# -- tools: pull requests ----------------------------------------------------------------

@tool("create_pull_request")
def create_pull_request(ctx: Instance,
                        owner: Annotated[str, "Repository owner (username or organization)"],
                        repo: Annotated[str, "Repository name"],
                        title: Annotated[str, "Pull request title"],
                        head: Annotated[str, "The name of the branch where your changes are implemented"],
                        base: Annotated[str, "The name of the branch you want the changes pulled into"],
                        body: Annotated[str | None, "Pull request body/description"] = None,
                        draft: Annotated[bool | None, "Whether to create the pull request as a draft"] = None,
                        maintainer_can_modify: Annotated[bool | None, "Whether maintainers can modify the pull request"] = None) -> dict[str, Any]:
    """Create a new pull request in a GitHub repository"""
    s = ctx.state
    r = _repo(s, owner, repo)
    head_ref = head.split(":", 1)[-1]
    errors = [{"resource": "PullRequest", "field": f, "code": "invalid"}
              for f, b in (("head", head_ref), ("base", base)) if b not in r["branches"]]
    if errors:
        raise _unprocessable("Validation Failed", errors)
    for pr in r["pulls"].values():
        if pr["head"] == head_ref and pr["base"] == base and r["issues"][pr["number"]]["state"] == "open":
            raise _unprocessable("Validation Failed", [{"resource": "PullRequest", "code": "custom",
                                                       "message": f"A pull request already exists for {owner}:{head_ref}."}])
    head_sha, base_sha = r["branches"][head_ref], r["branches"][base]
    if head_sha == base_sha or head_sha in _ancestors(r, base_sha):
        raise _unprocessable("Validation Failed", [{"resource": "PullRequest", "code": "custom",
                                                   "message": f"No commits between {base} and {head_ref}"}])
    pr = _new_pull(ctx, s, r, title, body, head_ref, base, s["viewer"], bool(draft))
    if maintainer_can_modify is not None:
        pr["maintainer_can_modify"] = maintainer_can_modify
    return _pr_json(s, r, pr)


@tool("get_pull_request", read_only=True)
def get_pull_request(ctx: Instance,
                     owner: Annotated[str, "Repository owner (username or organization)"],
                     repo: Annotated[str, "Repository name"],
                     pull_number: Annotated[int, "Pull request number"]) -> dict[str, Any]:
    """Get details of a specific pull request"""
    s = ctx.state
    r = _repo(s, owner, repo)
    return _pr_json(s, r, _pull(r, pull_number))


@tool("list_pull_requests", read_only=True)
def list_pull_requests(ctx: Instance,
                       owner: Annotated[str, "Repository owner (username or organization)"],
                       repo: Annotated[str, "Repository name"],
                       state: Annotated[Literal["open", "closed", "all"] | None, "State of the pull requests to return"] = None,
                       head: Annotated[str | None, "Filter by head user or head organization and branch name (user:ref-name)"] = None,
                       base: Annotated[str | None, "Filter by base branch name"] = None,
                       sort: Annotated[Literal["created", "updated", "popularity", "long-running"] | None, "What to sort results by"] = None,
                       direction: Annotated[Literal["asc", "desc"] | None, "The direction of the sort"] = None,
                       per_page: Annotated[int | None, "Results per page (max 100)"] = None,
                       page: Annotated[int | None, "Page number of the results"] = None) -> list[dict[str, Any]]:
    """List and filter repository pull requests"""
    s = ctx.state
    r = _repo(s, owner, repo)
    want = state or "open"
    prs = [p for p in r["pulls"].values()
           if (want == "all" or r["issues"][p["number"]]["state"] == want)
           and (not head or p["head"] == head.split(":", 1)[-1]) and (not base or p["base"] == base)]
    key = "updated_at" if sort == "updated" else "created_at"
    prs.sort(key=lambda p: (r["issues"][p["number"]][key], p["number"]), reverse=(direction or "desc") == "desc")
    return [_pr_json(s, r, p) for p in _paginate(prs, page, per_page)]


@tool("create_pull_request_review")
def create_pull_request_review(ctx: Instance,
                               owner: Annotated[str, "Repository owner (username or organization)"],
                               repo: Annotated[str, "Repository name"],
                               pull_number: Annotated[int, "Pull request number"],
                               body: Annotated[str, "The body text of the review"],
                               event: Annotated[Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"], "The review action to perform"],
                               commit_id: Annotated[str | None, "The SHA of the commit that needs a review"] = None,
                               comments: Annotated[list[dict] | None, "Comments to post as part of the review: {path, position|line, body}"] = None) -> dict[str, Any]:
    """Create a review on a pull request"""
    s = ctx.state
    r = _repo(s, owner, repo)
    pr = _pull(r, pull_number)
    author = r["issues"][pull_number]["user"]
    if event in ("APPROVE", "REQUEST_CHANGES") and author == s["viewer"]:
        verb = "approve" if event == "APPROVE" else "request changes on"
        raise _unprocessable("Unprocessable Entity", [f"Can not {verb} your own pull request"])  # type: ignore[list-item]
    if event == "REQUEST_CHANGES" and not body.strip():
        raise _unprocessable("Unprocessable Entity", ["Review body is required for REQUEST_CHANGES"])  # type: ignore[list-item]
    head_sha = r["branches"].get(pr["head"])
    changed = {f["filename"] for f in get_pull_request_files.fn(ctx, owner=owner, repo=repo, pull_number=pull_number)}
    for c in comments or []:
        if c.get("path") not in changed:
            raise _unprocessable("Unprocessable Entity", [f"Path could not be resolved: {c.get('path')}"])  # type: ignore[list-item]
    review = {"id": ctx.next("review_id", 1700000000), "user": _brief_user(s, s["viewer"]), "body": body,
              "state": {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED", "COMMENT": "COMMENTED"}[event],
              "commit_id": commit_id or head_sha, "submitted_at": _iso(ctx),
              "html_url": f"https://github.com/{r['full_name']}/pull/{pull_number}#pullrequestreview-{ctx.counters['review_id']}"}
    r["reviews"][pull_number].append(review)
    for c in comments or []:
        r["review_comments"][pull_number].append({
            "id": ctx.next("review_comment_id", 1600000000), "pull_request_review_id": review["id"], "path": c["path"],
            "line": c.get("line"), "position": c.get("position"), "body": c.get("body", ""),
            "user": _brief_user(s, s["viewer"]), "commit_id": review["commit_id"], "created_at": _iso(ctx)})
    return review


@tool("merge_pull_request")
def merge_pull_request(ctx: Instance,
                       owner: Annotated[str, "Repository owner (username or organization)"],
                       repo: Annotated[str, "Repository name"],
                       pull_number: Annotated[int, "Pull request number"],
                       commit_title: Annotated[str | None, "Title for the automatic commit message"] = None,
                       commit_message: Annotated[str | None, "Extra detail to append to automatic commit message"] = None,
                       merge_method: Annotated[Literal["merge", "squash", "rebase"] | None, "Merge method to use"] = None) -> dict[str, Any]:
    """Merge a pull request"""
    s = ctx.state
    r = _repo(s, owner, repo)
    pr = _pull(r, pull_number)
    issue = r["issues"][pull_number]
    if pr["merged"]:
        raise _err(405, "Pull Request is not mergeable")
    if issue["state"] != "open":
        raise _err(405, "Pull Request is not mergeable")
    st = _pr_state(s, r, pr)
    if st["mergeable_state"] == "draft":
        raise _err(405, "Pull Request is still a draft")
    if st["conflicts"]:
        raise _err(405, "Pull Request is not mergeable")
    if st["failing_checks"]:
        raise _err(405, f"Required status check \"{st['failing_checks'][0]}\" is "
                        f"{'expected' if not r['statuses'].get(st['head_sha']) else 'failing'}.")
    head_tree = r["commits"][st["head_sha"]]["tree"]
    tree = dict(r["commits"][st["base_sha"]]["tree"])
    for p in st["head_changed"]:  # three-way: take the head's side of every path it changed
        if p in head_tree:
            tree[p] = head_tree[p]
        else:
            tree.pop(p, None)
    method = merge_method or "merge"
    title = commit_title or (f"Merge pull request #{pull_number} from {r['owner']}/{pr['head']}" if method == "merge"
                             else f"{issue['title']} (#{pull_number})")
    message = title + (f"\n\n{commit_message}" if commit_message else "")
    parents = [st["base_sha"], st["head_sha"]] if method == "merge" else [st["base_sha"]]
    sha = _commit(ctx, s, r, tree, parents, message, s["viewer"])
    r["branches"][pr["base"]] = sha
    pr.update(merged=True, merged_at=_iso(ctx), merge_commit_sha=sha, merged_by=s["viewer"])
    issue.update(state="closed", closed_at=_iso(ctx), updated_at=_iso(ctx))
    return {"sha": sha, "merged": True, "message": "Pull Request successfully merged"}


@tool("get_pull_request_files", read_only=True)
def get_pull_request_files(ctx: Instance,
                           owner: Annotated[str, "Repository owner (username or organization)"],
                           repo: Annotated[str, "Repository name"],
                           pull_number: Annotated[int, "Pull request number"]) -> list[dict[str, Any]]:
    """Get the list of files changed in a pull request"""
    s = ctx.state
    r = _repo(s, owner, repo)
    pr = _pull(r, pull_number)
    head_sha = r["branches"].get(pr["head"])
    if head_sha is None:
        return []
    mb = _merge_base(r, head_sha, r["branches"][pr["base"]]) if not pr["merged"] else pr["base_sha"]
    return _changes(s, r["commits"][mb]["tree"] if mb else {}, r["commits"][head_sha]["tree"])


@tool("get_pull_request_status", read_only=True)
def get_pull_request_status(ctx: Instance,
                            owner: Annotated[str, "Repository owner (username or organization)"],
                            repo: Annotated[str, "Repository name"],
                            pull_number: Annotated[int, "Pull request number"]) -> dict[str, Any]:
    """Get the combined status of all status checks for a pull request"""
    s = ctx.state
    r = _repo(s, owner, repo)
    pr = _pull(r, pull_number)
    sha = r["branches"].get(pr["head"]) or pr["merge_commit_sha"]
    sts = r["statuses"].get(sha, [])
    states = {x["state"] for x in sts}
    combined = ("failure" if states & {"failure", "error"} else "pending" if not sts or "pending" in states else "success")
    return {"state": combined, "sha": sha, "total_count": len(sts),
            "statuses": [{"context": x["context"], "state": x["state"], "description": x.get("description", "")} for x in sts],
            "repository": {"full_name": r["full_name"]}}


@tool("update_pull_request_branch")
def update_pull_request_branch(ctx: Instance,
                               owner: Annotated[str, "Repository owner (username or organization)"],
                               repo: Annotated[str, "Repository name"],
                               pull_number: Annotated[int, "Pull request number"],
                               expected_head_sha: Annotated[str | None, "The expected SHA of the pull request's HEAD ref"] = None) -> dict[str, Any]:
    """Update a pull request branch with the latest changes from the base branch"""
    s = ctx.state
    r = _repo(s, owner, repo)
    pr = _pull(r, pull_number)
    st = _pr_state(s, r, pr)
    if expected_head_sha and expected_head_sha != st["head_sha"]:
        raise _unprocessable("expected head sha didn't match current head ref.")
    if st["mergeable_state"] != "behind" and st["merge_base"] == st["base_sha"]:
        raise _unprocessable("There are no new commits on the base branch.")
    if st["conflicts"]:
        raise _unprocessable("merge conflict between base and head")
    base_tree = r["commits"][st["base_sha"]]["tree"]
    head_tree = r["commits"][st["head_sha"]]["tree"]
    tree = dict(base_tree)
    for p in st["head_changed"]:
        if p in head_tree:
            tree[p] = head_tree[p]
        else:
            tree.pop(p, None)
    sha = _commit(ctx, s, r, tree, [st["head_sha"], st["base_sha"]], f"Merge branch '{pr['base']}' into {pr['head']}",
                  s["viewer"])
    r["branches"][pr["head"]] = sha
    return {"message": "Updating pull request branch.", "url": f"https://github.com/{r['full_name']}/pull/{pull_number}"}


@tool("get_pull_request_comments", read_only=True)
def get_pull_request_comments(ctx: Instance,
                              owner: Annotated[str, "Repository owner (username or organization)"],
                              repo: Annotated[str, "Repository name"],
                              pull_number: Annotated[int, "Pull request number"]) -> list[dict[str, Any]]:
    """Get the review comments on a pull request"""
    r = _repo(ctx.state, owner, repo)
    _pull(r, pull_number)
    return r["review_comments"][pull_number]


@tool("get_pull_request_reviews", read_only=True)
def get_pull_request_reviews(ctx: Instance,
                             owner: Annotated[str, "Repository owner (username or organization)"],
                             repo: Annotated[str, "Repository name"],
                             pull_number: Annotated[int, "Pull request number"]) -> list[dict[str, Any]]:
    """Get the reviews on a pull request"""
    r = _repo(ctx.state, owner, repo)
    _pull(r, pull_number)
    return r["reviews"][pull_number]


GitHub.tools = [create_or_update_file, push_files, search_repositories, create_repository, get_file_contents,
                create_issue, create_pull_request, fork_repository, create_branch, list_issues, update_issue,
                add_issue_comment, search_code, search_issues, search_users, list_commits, get_issue, get_pull_request,
                list_pull_requests, create_pull_request_review, merge_pull_request, get_pull_request_files,
                get_pull_request_status, update_pull_request_branch, get_pull_request_comments, get_pull_request_reviews]


# -- world actions (triggered by environments, never by agents) --------------------------

def _repo_named(state: dict[str, Any], full_name: str) -> dict[str, Any]:
    owner, _, name = full_name.partition("/")
    return _repo(state, owner, name)


@action("set_status")
def act_set_status(ctx: Instance, repo: str, ref: str, context: str, state: str, description: str = "") -> None:
    """CI reports a status on a branch or commit (e.g. flips from success to failure)."""
    if state not in ("success", "failure", "error", "pending"):
        raise _unprocessable(f"invalid state {state}")
    r = _repo_named(ctx.state, repo)
    sha = r["branches"].get(ref, ref)
    sts = [x for x in r["statuses"].get(sha, []) if x["context"] != context]
    r["statuses"][sha] = sts + [{"context": context, "state": state, "description": description}]


@action("push_commit")
def act_push_commit(ctx: Instance, repo: str, branch: str, files: dict, author: str, message: str = "Update") -> str:
    """A teammate pushes to a branch (e.g. main moves under an open PR, causing a conflict)."""
    s = ctx.state
    r = s["repos"].get(repo) or (_ for _ in ()).throw(_not_found())
    head = _branch(r, branch)
    tree = dict(r["commits"][head]["tree"])
    tree.update({p: _put_blob(s, c) for p, c in files.items()})
    r["branches"][branch] = _commit(ctx, s, r, tree, [head], message, author)
    return r["branches"][branch]


@action("add_comment")
def act_add_comment(ctx: Instance, repo: str, number: int, author: str, body: str) -> None:
    """A teammate comments on an issue or pull request."""
    r = _repo_named(ctx.state, repo)
    _issue(r, number)
    _user(ctx, ctx.state, author)
    _comment(ctx, r, number, author, body)


@action("set_issue_state")
def act_set_issue_state(ctx: Instance, repo: str, number: int, state: str) -> None:
    """Someone opens or closes an issue."""
    r = _repo_named(ctx.state, repo)
    i = _issue(r, number)
    i["state"] = state
    i["closed_at"] = _iso(ctx) if state == "closed" else None


GitHub.actions = [act_set_status, act_push_commit, act_add_comment, act_set_issue_state]
