"""github: state model, constants and helpers shared by the tools."""

from __future__ import annotations

import base64
import difflib
import hashlib
import re
from typing import Any

from ...core.instance import Instance
from ...core.tools import ToolError

API = "https://api.github.com"
V1 = "2026-09-25.1"
V2 = "2026-09-25.2"
V3 = "2026-09-25.3"
SEARCH_LAG = {"issues": 60, "repos": 60, "code": 300}  # seconds until the search index catches up
CLOSING = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+#(\d+)\b", re.I)


def _err(status: int, message: str, **extra: Any) -> ToolError:
    doc = "https://docs.github.com/rest"
    return ToolError({"message": message, "documentation_url": doc, "status": str(status), **extra}, status=status)


def _not_found() -> ToolError:
    return _err(404, "Not Found")


def _unprocessable(message: str, errors: list[dict[str, Any]] | None = None) -> ToolError:
    return _err(422, message, **({"errors": errors} if errors else {}))


def blob_sha(content: str) -> str:
    data = content.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()  # git's object id

def _delay(v: Any) -> float:
    m = re.fullmatch(r"\+?(\d+(?:\.\d+)?)([smhd]?)", str(v).strip())
    if not m:
        raise ValueError(f"invalid duration {v!r} (e.g. 90s, 4m)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def _check_push(state: dict[str, Any], r: dict[str, Any], branch: str) -> None:
    """Protected branches only change through pull requests (2026-09-25.1)."""
    rules = r["protected"].get(branch)
    if state.get("_v1") and rules is not None and not rules.get("allow_direct_push"):
        raise _err(409, "Repository rule violations found\n\nChanges must be made through a pull request.\n\n")


def _ci_outcome(ctx: Instance, r: dict[str, Any], sha: str) -> str | None:
    """Why CI fails on ``sha`` (a matching fail_if rule, or a flaky test), or None if it passes."""
    ci = r["ci"]
    tree = r["commits"][sha]["tree"]
    blobs = ctx.state["blobs"]
    reason = next((f"{p}: matched {rule.get('content~')!r}" for rule in ci["fail_if"] for p, b in sorted(tree.items())
                   if p.startswith(rule.get("path", "")) and (rule.get("content~") or "") in blobs[b]), None)
    return reason or (ci["flaky"] and ctx.rng.random() < ci["flaky"] and "flaky test: test_timeout_under_load") or None


def _pushed(ctx: Instance, r: dict[str, Any], sha: str) -> None:
    """A commit landed on a branch: CI starts on it and reports back later (2026-09-25.1)."""
    ci = r.get("ci")
    if not ctx.state.get("_v1") or not ci:
        return
    for context in ci["contexts"]:
        failed = _ci_outcome(ctx, r, sha)
        r["statuses"][sha] = [x for x in r["statuses"].get(sha, []) if x["context"] != context] + [
            {"context": context, "state": "pending", "description": "In progress"}]
        _start_run(ctx, r, sha, context)
        ctx.schedule(ci["duration"], "set_status", {
            "repo": r["full_name"], "ref": sha, "context": context, "state": "failure" if failed else "success",
            "description": f"Failed: {failed}" if failed else "All checks have passed"},
            reason=f"{context} finishes on {sha[:7]}")


# -- GitHub Actions: each CI context is a workflow run with one job --------------------------------

WORKFLOW = {"id": 90210001, "name": "CI", "path": ".github/workflows/ci.yml", "state": "active"}
STEPS = ["Set up job", "Run actions/checkout@v4", "Set up Python", "Install dependencies", "Run tests",
         "Post Run actions/checkout@v4", "Complete job"]


def _branch_of(r: dict[str, Any], sha: str) -> str | None:
    return next((b for b, x in r["branches"].items() if x == sha), None)


def _start_run(ctx: Instance, r: dict[str, Any], sha: str, context: str, attempt_of: dict[str, Any] | None = None
               ) -> dict[str, Any]:
    runs = r.setdefault("runs", {})
    now = _iso(ctx)
    if attempt_of is not None:
        run = attempt_of
        run.update(status="in_progress", conclusion=None, run_attempt=run["run_attempt"] + 1, updated_at=now,
                   run_started_at=now)
    else:
        commit = r["commits"][sha]
        run = {"id": ctx.next("run_id", 17880000000), "context": context, "workflow_id": WORKFLOW["id"],
               "name": WORKFLOW["name"], "display_title": commit["message"].split("\n")[0], "head_sha": sha,
               "head_branch": _branch_of(r, sha), "event": "push", "status": "in_progress", "conclusion": None,
               "run_number": len(runs) + 1, "run_attempt": 1, "created_at": now, "updated_at": now,
               "run_started_at": now, "actor": commit["author"].get("login") or ctx.state["viewer"]}
        runs[run["id"]] = run
    run["jobs"] = [{"id": ctx.next("job_id", 50110000000), "name": context, "status": "in_progress",
                    "conclusion": None, "started_at": now, "completed_at": None, "log": ""}]
    return run


def _finish_run(ctx: Instance, r: dict[str, Any], sha: str, context: str, state: str, description: str) -> None:
    runs = [x for x in r.get("runs", {}).values() if x["head_sha"] == sha and x["context"] == context]
    if not runs:
        if state == "pending":
            return
        run = _start_run(ctx, r, sha, context)
    else:
        run = max(runs, key=lambda x: x["id"])
    if state == "pending":
        return
    conclusion = "success" if state == "success" else "failure"
    now = _iso(ctx)
    run.update(status="completed", conclusion=conclusion, updated_at=now)
    for job in run["jobs"]:
        job.update(status="completed", conclusion=conclusion, completed_at=now,
                   log=_job_log(job["started_at"], now, context, conclusion, description))


def _job_log(start: str, end: str, context: str, conclusion: str, description: str) -> str:
    t0 = start.replace("Z", ".1234567Z")
    t1 = end.replace("Z", ".7654321Z")
    lines = [f"{t0} Current runner version: '2.328.0'", f"{t0} ##[group]Operating System", f"{t0} Ubuntu",
             f"{t0} 24.04.3", f"{t0} LTS", f"{t0} ##[endgroup]", f"{t0} ##[group]Run actions/checkout@v4",
             f"{t0} ##[endgroup]", f"{t0} ##[group]Run pytest -q", f"{t0} pytest -q", f"{t0} ##[endgroup]"]
    if conclusion == "success":
        lines += [f"{t1} {description or 'All checks have passed'}", f"{t1} Cleaning up orphan processes"]
    else:
        detail = description.removeprefix("Failed: ") or "tests failed"
        path = detail.split(":")[0] if ":" in detail else "tests"
        lines += [f"{t1} F", f"{t1} =================================== FAILURES ===================================",
                  f"{t1} ______________________________ {path} ______________________________",
                  f"{t1} E   {detail}", f"{t1} =========================== short test summary info ============================",
                  f"{t1} FAILED {path} - {detail}", f"{t1} 1 failed in 3.21s",
                  f"{t1} ##[error]Process completed with exit code 1."]
    return "\n".join(lines) + "\n"


def _rerun(ctx: Instance, r: dict[str, Any], run: dict[str, Any]) -> None:
    ci = r.get("ci") or {"duration": 240, "contexts": [run["context"]], "fail_if": [], "flaky": 0}
    r["ci"] = ci
    failed = _ci_outcome(ctx, r, run["head_sha"])
    _start_run(ctx, r, run["head_sha"], run["context"], attempt_of=run)
    r["statuses"][run["head_sha"]] = [x for x in r["statuses"].get(run["head_sha"], [])
                                      if x["context"] != run["context"]] + [
        {"context": run["context"], "state": "pending", "description": "In progress"}]
    ctx.schedule(ci["duration"], "set_status", {
        "repo": r["full_name"], "ref": run["head_sha"], "context": run["context"],
        "state": "failure" if failed else "success",
        "description": f"Failed: {failed}" if failed else "All checks have passed"},
        reason=f"re-run of {run['context']} on {run['head_sha'][:7]}")


def _approvals(r: dict[str, Any], pr: dict[str, Any], head_sha: str) -> tuple[int, bool]:
    """(approvals that count, whether changes are requested): each reviewer's latest verdict."""
    rules = r["protected"].get(pr["base"], {})
    author = r["issues"][pr["number"]]["user"]
    latest: dict[str, dict[str, Any]] = {}
    for rv in r["reviews"][pr["number"]]:
        if rv["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED") and rv["user"]["login"] != author:
            latest[rv["user"]["login"]] = rv
    ok = sum(rv["state"] == "APPROVED" and (not rules.get("dismiss_stale_reviews") or rv["commit_id"] == head_sha)
             for rv in latest.values())
    return ok, any(rv["state"] == "CHANGES_REQUESTED" for rv in latest.values())


def _close_referenced(ctx: Instance, r: dict[str, Any], pr: dict[str, Any], extra: str) -> list[int]:
    """Merging into the default branch closes issues the PR says it fixes (2026-09-25.1)."""
    if not ctx.state.get("_v1") or pr["base"] != r["default_branch"]:
        return []
    i = r["issues"][pr["number"]]
    closed = []
    for m in CLOSING.finditer(" ".join(filter(None, [i["title"], i["body"], extra]))):
        target = r["issues"].get(int(m.group(1)))
        if target and not target["is_pull"] and target["state"] == "open":
            target.update(state="closed", state_reason="completed", closed_at=_iso(ctx), updated_at=_iso(ctx))
            closed.append(target["number"])
    return closed


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
    sha = hashlib.sha1(raw, usedforsecurity=False).hexdigest()
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
    need = int(r["protected"].get(pr["base"], {}).get("required_approvals", 0)) if state.get("_v1") else 0
    approved, changes_requested = _approvals(r, pr, head_sha) if need else (0, False)
    review_block = None
    if changes_requested:
        review_block = "Changes were requested by a reviewer. Address them before merging."
    elif approved < need:
        review_block = (f"At least {need} approving review{'s are' if need > 1 else ' is'} required by reviewers "
                        "with write access.")
    if conflicts:
        ms = "dirty"
    elif pr["draft"]:
        ms = "draft"
    elif failing or review_block:
        ms = "blocked"
    elif mb != base_sha:
        ms = "behind"
    else:
        ms = "clean"
    return {"mergeable": not conflicts, "mergeable_state": ms, "head_sha": head_sha, "base_sha": base_sha,
            "merge_base": mb, "conflicts": conflicts, "failing_checks": failing, "head_changed": head_changed,
            "review_block": review_block}


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
