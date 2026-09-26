"""github: the GitHub REST API (v3) and GraphQL API (v4), as ``gh``, Octokit and ``curl`` see them.

REST reuses the tools' GitHub-shaped JSON, adds what clients depend on (``Link`` pagination,
``application/vnd.github.diff``, rate-limit and OAuth-scope headers) and serves GitHub Actions
(workflow runs, jobs, logs as the zip ``gh run view --log-failed`` reads, re-runs). GraphQL is
in ``graphql_api``.
"""

from __future__ import annotations

import base64
import difflib
import io
import urllib.parse
import zipfile
from typing import Any

from ...api import Request, Response, operation
from ...core.instance import Instance
from . import files as files_tools
from . import issues as issue_tools
from . import pulls as pull_tools
from . import search as search_tools
from .model import (
    API,
    STEPS,
    WORKFLOW,
    _brief_user,
    _commit_json,
    _err,
    _pr_state,
    _repo,
    _repo_json,
    _rerun,
    _unprocessable,
)

HOSTS = ("api.github.com",)


def op(op_id: str, method: str, path: str, **kw: Any):  # noqa: ANN201
    return operation(op_id, method, path, hosts=HOSTS, **kw)


def _r(ctx: Instance, req: Request) -> dict[str, Any]:
    return _repo(ctx.state, req.params["owner"], req.params["repo"])


def _paged(req: Request, items: list[Any], base_path: str) -> Response:
    """A page of ``items`` with GitHub's ``Link`` header (``gh`` follows ``rel="next"``)."""
    per = max(1, min(req.int_arg("per_page", 30), 100))
    page = max(1, req.int_arg("page", 1))
    chunk = items[(page - 1) * per: page * per]
    last = max(1, -(-len(items) // per))
    links = []
    q = {k: v[-1] for k, v in req.query.items() if k not in ("page",)}

    def url(p: int) -> str:
        return f"{API}{base_path}?" + urllib.parse.urlencode({**q, "page": p})
    if page < last:
        links += [f'<{url(page + 1)}>; rel="next"', f'<{url(last)}>; rel="last"']
    if page > 1:
        links = [f'<{url(page - 1)}>; rel="prev"', f'<{url(1)}>; rel="first"'] + links
    return Response(200, chunk, {"link": ", ".join(links)} if links else {})


def _json_args(req: Request) -> dict[str, Any]:
    b = req.body
    if b is None:
        return {}
    if not isinstance(b, dict):
        raise _err(400, "Problems parsing JSON")
    return b


# -- users and repositories ------------------------------------------------------------------------

def _full_user(ctx: Instance, login: str) -> dict[str, Any]:
    s = ctx.state
    u = s["users"].get(login)
    if u is None:
        raise _err(404, "Not Found")
    out = {**_brief_user(s, login), "avatar_url": f"https://avatars.githubusercontent.com/u/{u['id']}?v=4",
           "gravatar_id": "", "name": u.get("name"), "company": None, "blog": "", "location": None,
           "email": u.get("email"), "hireable": None, "bio": None, "twitter_username": None,
           "public_repos": sum(1 for r in s["repos"].values() if r["owner"] == login and not r["private"]),
           "public_gists": 0, "followers": 0, "following": 0, "created_at": "2019-03-04T17:22:05Z",
           "updated_at": "2026-09-01T10:00:00Z"}
    return out


@op("github.meta.root", "GET", "/", read_only=True)
def api_root(ctx: Instance, req: Request) -> Any:
    return {"current_user_url": f"{API}/user", "authorizations_url": "https://github.com/settings/connections/applications{/client_id}",
            "emails_url": f"{API}/user/emails", "issues_url": f"{API}/issues", "repository_url": f"{API}/repos/{{owner}}/{{repo}}",
            "rate_limit_url": f"{API}/rate_limit", "user_url": f"{API}/users/{{user}}"}


@op("github.users.getAuthenticated", "GET", "/user", read_only=True)
def get_viewer(ctx: Instance, req: Request) -> Any:
    return Response(200, _full_user(ctx, ctx.state["viewer"]),
                    {"x-oauth-scopes": "gist, read:org, repo, workflow"})


@op("github.users.getByUsername", "GET", "/users/{login}", read_only=True)
def get_user(ctx: Instance, req: Request) -> Any:
    return _full_user(ctx, req.params["login"])


def _repo_full(ctx: Instance, r: dict[str, Any]) -> dict[str, Any]:
    s = ctx.state
    out = _repo_json(s, r)
    admin = r["owner"] == s["viewer"] or s["users"].get(r["owner"], {}).get("type") == "Organization"
    out.update(permissions={"admin": admin, "maintain": admin, "push": True, "triage": True, "pull": True},
               has_issues=True, has_projects=True, has_wiki=False, has_discussions=False, forks_count=0,
               stargazers_count=0, watchers_count=0, size=len(r["commits"]) * 12, language="Python",
               allow_squash_merge=True, allow_merge_commit=True, allow_rebase_merge=True,
               delete_branch_on_merge=False, pushed_at=r["updated_at"],
               clone_url=f"https://github.com/{r['full_name']}.git", ssh_url=f"git@github.com:{r['full_name']}.git")
    return out


@op("github.repos.get", "GET", "/repos/{owner}/{repo}", read_only=True)
def repos_get(ctx: Instance, req: Request) -> Any:
    return _repo_full(ctx, _r(ctx, req))


@op("github.repos.listForAuthenticatedUser", "GET", "/user/repos", read_only=True)
def repos_mine(ctx: Instance, req: Request) -> Any:
    s = ctx.state
    return _paged(req, [_repo_json(s, r) for r in s["repos"].values()], "/user/repos")


@op("github.repos.listForOrg", "GET", "/orgs/{org}/repos", read_only=True)
def repos_org(ctx: Instance, req: Request) -> Any:
    s = ctx.state
    return _paged(req, [_repo_json(s, r) for r in s["repos"].values() if r["owner"] == req.params["org"]],
                  f"/orgs/{req.params['org']}/repos")


@op("github.repos.listBranches", "GET", "/repos/{owner}/{repo}/branches", read_only=True)
def branches(ctx: Instance, req: Request) -> Any:
    r = _r(ctx, req)
    items = [{"name": b, "commit": {"sha": sha, "url": f"{API}/repos/{r['full_name']}/commits/{sha}"},
              "protected": b in r["protected"]} for b, sha in sorted(r["branches"].items())]
    return _paged(req, items, f"/repos/{r['full_name']}/branches")


@op("github.repos.getContent", "GET", "/repos/{owner}/{repo}/contents/{path+}", read_only=True)
def contents(ctx: Instance, req: Request) -> Any:
    return files_tools.get_file_contents.fn(ctx, owner=req.params["owner"], repo=req.params["repo"],
                                            path=urllib.parse.unquote(req.params["path"]), branch=req.arg("ref"))


@op("github.repos.createOrUpdateFileContents", "PUT", "/repos/{owner}/{repo}/contents/{path+}")
def put_contents(ctx: Instance, req: Request) -> Any:
    b = _json_args(req)
    try:
        text = base64.b64decode(b.get("content", "")).decode()
    except (ValueError, UnicodeDecodeError):
        raise _unprocessable("content is not valid Base64") from None
    return files_tools.create_or_update_file.fn(
        ctx, owner=req.params["owner"], repo=req.params["repo"], path=urllib.parse.unquote(req.params["path"]),
        content=text, message=b.get("message", ""), branch=b.get("branch") or _r(ctx, req)["default_branch"],
        sha=b.get("sha"))


def _ref_json(r: dict[str, Any], name: str) -> dict[str, Any]:
    sha = r["branches"][name]
    return {"ref": f"refs/heads/{name}", "node_id": "REF_kwDO" + base64.b64encode(name.encode()).decode().rstrip("="),
            "url": f"{API}/repos/{r['full_name']}/git/refs/heads/{name}",
            "object": {"sha": sha, "type": "commit", "url": f"{API}/repos/{r['full_name']}/git/commits/{sha}"}}


@op("github.repos.getReadme", "GET", "/repos/{owner}/{repo}/readme", read_only=True)
def readme(ctx: Instance, req: Request) -> Any:
    r = _r(ctx, req)
    ref = req.arg("ref") or r["default_branch"]
    from .model import _ref_sha
    tree = r["commits"][_ref_sha(r, ref)]["tree"]
    path = next((p for p in tree if p.lower() in ("readme.md", "readme", "readme.rst", "readme.txt")), None)
    if path is None:
        raise _err(404, "Not Found")
    return files_tools.get_file_contents.fn(ctx, owner=req.params["owner"], repo=req.params["repo"], path=path,
                                            branch=ref)


@op("github.git.getRef", "GET", "/repos/{owner}/{repo}/git/ref/{ref+}", read_only=True)
def get_ref(ctx: Instance, req: Request) -> Any:
    r = _r(ctx, req)
    name = urllib.parse.unquote(req.params["ref"]).removeprefix("heads/")
    if name not in r["branches"]:
        raise _err(404, "Not Found")
    return _ref_json(r, name)


@op("github.git.listMatchingRefs", "GET", "/repos/{owner}/{repo}/git/matching-refs/{ref+}", read_only=True)
def matching_refs(ctx: Instance, req: Request) -> Any:
    r = _r(ctx, req)
    prefix = urllib.parse.unquote(req.params["ref"]).removeprefix("heads/")
    return [_ref_json(r, b) for b in sorted(r["branches"]) if b.startswith(prefix)]


@op("github.git.updateRef", "PATCH", "/repos/{owner}/{repo}/git/refs/{ref+}")
def update_ref(ctx: Instance, req: Request) -> Any:
    from .model import _ancestors, _check_push, _pushed
    r = _r(ctx, req)
    name = urllib.parse.unquote(req.params["ref"]).removeprefix("heads/")
    b = _json_args(req)
    if name not in r["branches"]:
        raise _unprocessable("Reference does not exist")
    if b.get("sha") not in r["commits"]:
        raise _unprocessable("Object does not exist")
    if not b.get("force") and r["branches"][name] not in _ancestors(r, b["sha"]):
        raise _unprocessable("Update is not a fast forward")
    _check_push(ctx.state, r, name)
    r["branches"][name] = b["sha"]
    _pushed(ctx, r, b["sha"])
    return _ref_json(r, name)


@op("github.git.deleteRef", "DELETE", "/repos/{owner}/{repo}/git/refs/{ref+}", destructive=True)
def delete_ref(ctx: Instance, req: Request) -> Any:
    r = _r(ctx, req)
    name = urllib.parse.unquote(req.params["ref"]).removeprefix("heads/")
    if name not in r["branches"]:
        raise _unprocessable("Reference does not exist")
    if name == r["default_branch"]:
        raise _unprocessable("Cannot delete the default branch")
    del r["branches"][name]
    return Response(204)


@op("github.repos.getBranch", "GET", "/repos/{owner}/{repo}/branches/{branch+}", read_only=True)
def branch_get(ctx: Instance, req: Request) -> Any:
    r = _r(ctx, req)
    name = urllib.parse.unquote(req.params["branch"])
    if name not in r["branches"]:
        raise _err(404, "Branch not found")
    sha = r["branches"][name]
    rules = r["protected"].get(name)
    out = {"name": name, "commit": _commit_json(r, sha), "protected": rules is not None,
           "_links": {"self": f"{API}/repos/{r['full_name']}/branches/{name}",
                      "html": f"https://github.com/{r['full_name']}/tree/{name}"}}
    if rules is not None:
        out["protection"] = {"enabled": True, "required_status_checks": {
            "enforcement_level": "everyone", "contexts": rules.get("required_checks", [])}}
    return out


@op("github.git.createRef", "POST", "/repos/{owner}/{repo}/git/refs")
def create_ref(ctx: Instance, req: Request) -> Any:
    b = _json_args(req)
    r = _r(ctx, req)
    ref = b.get("ref", "")
    if not ref.startswith("refs/heads/"):
        raise _unprocessable("Reference name must start with 'refs/' and have at least two slashes.")
    name = ref.removeprefix("refs/heads/")
    if name in r["branches"]:
        raise _unprocessable("Reference already exists")
    if b.get("sha") not in r["commits"]:
        raise _unprocessable("Object does not exist")
    r["branches"][name] = b["sha"]
    return Response(201, {"ref": ref, "node_id": "REF_" + name, "url": f"{API}/repos/{r['full_name']}/git/{ref}",
                          "object": {"sha": b["sha"], "type": "commit"}})


@op("github.repos.listCommits", "GET", "/repos/{owner}/{repo}/commits", read_only=True)
def commits(ctx: Instance, req: Request) -> Any:
    r = _r(ctx, req)
    out = files_tools.list_commits.fn(ctx, owner=req.params["owner"], repo=req.params["repo"],
                                      sha=req.arg("sha"), page="1", per_page="100")
    return _paged(req, out, f"/repos/{r['full_name']}/commits")


@op("github.repos.getCommit", "GET", "/repos/{owner}/{repo}/commits/{ref}", read_only=True)
def commit_get(ctx: Instance, req: Request) -> Any:
    from .model import _ref_sha
    r = _r(ctx, req)
    return _commit_json(r, _ref_sha(r, req.params["ref"]))


# -- issues -------------------------------------------------------------------------------------------

@op("github.issues.listForRepo", "GET", "/repos/{owner}/{repo}/issues", read_only=True)
def issues_list(ctx: Instance, req: Request) -> Any:
    r = _r(ctx, req)
    items = issue_tools.list_issues.fn(ctx, owner=req.params["owner"], repo=req.params["repo"],
                                       state=req.arg("state"), labels=[x for x in (req.arg("labels") or "").split(",") if x] or None,
                                       sort=req.arg("sort"), direction=req.arg("direction"), since=req.arg("since"),
                                       page=1, per_page=100000)
    if req.arg("assignee"):
        items = [i for i in items if any(a["login"] == req.arg("assignee") for a in i["assignees"])]
    if req.arg("creator"):
        items = [i for i in items if i["user"]["login"] == req.arg("creator")]
    return _paged(req, items, f"/repos/{r['full_name']}/issues")


@op("github.issues.create", "POST", "/repos/{owner}/{repo}/issues")
def issues_create(ctx: Instance, req: Request) -> Any:
    b = _json_args(req)
    if "title" not in b:
        raise _unprocessable("Invalid request.\n\n\"title\" wasn't supplied.")
    out = issue_tools.create_issue.fn(ctx, owner=req.params["owner"], repo=req.params["repo"], title=b["title"],
                                      body=b.get("body"), assignees=b.get("assignees"), milestone=b.get("milestone"),
                                      labels=b.get("labels"))
    return Response(201, out)


@op("github.issues.get", "GET", "/repos/{owner}/{repo}/issues/{number}", read_only=True)
def issues_get(ctx: Instance, req: Request) -> Any:
    return issue_tools.get_issue.fn(ctx, owner=req.params["owner"], repo=req.params["repo"],
                                    issue_number=int(req.params["number"]))


@op("github.issues.update", "PATCH", "/repos/{owner}/{repo}/issues/{number}")
def issues_update(ctx: Instance, req: Request) -> Any:
    b = _json_args(req)
    out = issue_tools.update_issue.fn(ctx, owner=req.params["owner"], repo=req.params["repo"],
                                      issue_number=int(req.params["number"]), title=b.get("title"), body=b.get("body"),
                                      state=b.get("state"), labels=b.get("labels"), assignees=b.get("assignees"),
                                      milestone=b.get("milestone"))
    if b.get("state") == "closed" and b.get("state_reason"):
        r = _r(ctx, req)
        r["issues"][int(req.params["number"])]["state_reason"] = b["state_reason"]
        out["state_reason"] = b["state_reason"]
    return out


@op("github.issues.listComments", "GET", "/repos/{owner}/{repo}/issues/{number}/comments", read_only=True)
def comments_list(ctx: Instance, req: Request) -> Any:
    from .model import _comment_json
    r = _r(ctx, req)
    n = int(req.params["number"])
    issue_tools._issue(r, n)
    return _paged(req, [_comment_json(ctx.state, r, n, c) for c in r["comments"][n]],
                  f"/repos/{r['full_name']}/issues/{n}/comments")


@op("github.issues.createComment", "POST", "/repos/{owner}/{repo}/issues/{number}/comments")
def comments_create(ctx: Instance, req: Request) -> Any:
    return Response(201, issue_tools.add_issue_comment.fn(ctx, owner=req.params["owner"], repo=req.params["repo"],
                                                          issue_number=int(req.params["number"]),
                                                          body=_json_args(req).get("body", "")))


@op("github.issues.addLabels", "POST", "/repos/{owner}/{repo}/issues/{number}/labels")
def labels_add(ctx: Instance, req: Request) -> Any:
    r = _r(ctx, req)
    i = issue_tools._issue(r, int(req.params["number"]))
    b = req.body
    names = b if isinstance(b, list) else (b or {}).get("labels") or []
    names = [x["name"] if isinstance(x, dict) else x for x in names]
    labels = list(dict.fromkeys(i["labels"] + names))
    issue_tools.update_issue.fn(ctx, owner=req.params["owner"], repo=req.params["repo"], issue_number=i["number"],
                                labels=labels)
    return [{"name": x, "color": r["labels"][x]["color"], "default": r["labels"][x]["default"]} for x in labels]


@op("github.issues.removeLabel", "DELETE", "/repos/{owner}/{repo}/issues/{number}/labels/{name}", destructive=True)
def labels_remove(ctx: Instance, req: Request) -> Any:
    r = _r(ctx, req)
    i = issue_tools._issue(r, int(req.params["number"]))
    name = urllib.parse.unquote(req.params["name"])
    if name not in i["labels"]:
        raise _err(404, "Label does not exist")
    i["labels"].remove(name)
    return [{"name": x, "color": r["labels"][x]["color"]} for x in i["labels"]]


@op("github.issues.listLabelsForRepo", "GET", "/repos/{owner}/{repo}/labels", read_only=True)
def repo_labels(ctx: Instance, req: Request) -> Any:
    r = _r(ctx, req)
    return _paged(req, [{"id": abs(hash(x)) % 10**10, "name": x, "color": v["color"], "default": v["default"],
                         "description": None} for x, v in sorted(r["labels"].items())],
                  f"/repos/{r['full_name']}/labels")


# -- pull requests ----------------------------------------------------------------------------------

@op("github.pulls.list", "GET", "/repos/{owner}/{repo}/pulls", read_only=True)
def pulls_list(ctx: Instance, req: Request) -> Any:
    r = _r(ctx, req)
    items = pull_tools.list_pull_requests.fn(ctx, owner=req.params["owner"], repo=req.params["repo"],
                                             state=req.arg("state"), head=req.arg("head"), base=req.arg("base"),
                                             sort=req.arg("sort"), direction=req.arg("direction"), per_page=100, page=1)
    return _paged(req, items, f"/repos/{r['full_name']}/pulls")


def _diff(ctx: Instance, r: dict[str, Any], number: int) -> str:
    s = ctx.state
    st = _pr_state(s, r, r["pulls"][number])
    base_tree = r["commits"][st["merge_base"] or st["base_sha"]]["tree"] if st["base_sha"] else {}
    head_tree = r["commits"][st["head_sha"]]["tree"] if st["head_sha"] else {}
    out = []
    for path in sorted(set(base_tree) | set(head_tree)):
        a, b = base_tree.get(path), head_tree.get(path)
        if a == b:
            continue
        before = s["blobs"].get(a, "") if a else ""
        after = s["blobs"].get(b, "") if b else ""
        out.append(f"diff --git a/{path} b/{path}")
        if not a:
            out.append("new file mode 100644")
        elif not b:
            out.append("deleted file mode 100644")
        out.append(f"index {(a or '0' * 40)[:7]}..{(b or '0' * 40)[:7]}" + ("" if not a or not b else " 100644"))
        out += list(difflib.unified_diff(before.splitlines(), after.splitlines(),
                                         "/dev/null" if not a else f"a/{path}", "/dev/null" if not b else f"b/{path}",
                                         lineterm=""))
    return "\n".join(out) + "\n"


@op("github.pulls.get", "GET", "/repos/{owner}/{repo}/pulls/{number}", read_only=True)
def pulls_get(ctx: Instance, req: Request) -> Any:
    r = _r(ctx, req)
    n = int(req.params["number"])
    pr = pull_tools.get_pull_request.fn(ctx, owner=req.params["owner"], repo=req.params["repo"], pull_number=n)
    accept = req.headers.get("accept", "")
    if "diff" in accept or "patch" in accept:
        return Response(200, _diff(ctx, r, n).encode(), {"content-type": "text/plain; charset=utf-8"})
    return pr


@op("github.pulls.create", "POST", "/repos/{owner}/{repo}/pulls")
def pulls_create(ctx: Instance, req: Request) -> Any:
    b = _json_args(req)
    for k in ("head", "base"):
        if not b.get(k):
            raise _unprocessable("Validation Failed", [{"resource": "PullRequest", "field": k, "code": "missing_field"}])
    return Response(201, pull_tools.create_pull_request.fn(
        ctx, owner=req.params["owner"], repo=req.params["repo"], title=b.get("title", ""), head=b["head"],
        base=b["base"], body=b.get("body"), draft=b.get("draft"), maintainer_can_modify=b.get("maintainer_can_modify")))


@op("github.pulls.update", "PATCH", "/repos/{owner}/{repo}/pulls/{number}")
def pulls_update(ctx: Instance, req: Request) -> Any:
    b = _json_args(req)
    n = int(req.params["number"])
    issue_tools.update_issue.fn(ctx, owner=req.params["owner"], repo=req.params["repo"], issue_number=n,
                                title=b.get("title"), body=b.get("body"), state=b.get("state"))
    r = _r(ctx, req)
    if b.get("base"):
        if b["base"] not in r["branches"]:
            raise _unprocessable("Validation Failed", [{"resource": "PullRequest", "field": "base", "code": "invalid"}])
        r["pulls"][n]["base"] = b["base"]
    return pull_tools.get_pull_request.fn(ctx, owner=req.params["owner"], repo=req.params["repo"], pull_number=n)


@op("github.pulls.listFiles", "GET", "/repos/{owner}/{repo}/pulls/{number}/files", read_only=True)
def pulls_files(ctx: Instance, req: Request) -> Any:
    r = _r(ctx, req)
    out = pull_tools.get_pull_request_files.fn(ctx, owner=req.params["owner"], repo=req.params["repo"],
                                               pull_number=int(req.params["number"]))
    return _paged(req, out, f"/repos/{r['full_name']}/pulls/{req.params['number']}/files")


@op("github.pulls.listReviews", "GET", "/repos/{owner}/{repo}/pulls/{number}/reviews", read_only=True)
def pulls_reviews(ctx: Instance, req: Request) -> Any:
    return pull_tools.get_pull_request_reviews.fn(ctx, owner=req.params["owner"], repo=req.params["repo"],
                                                  pull_number=int(req.params["number"]))


@op("github.pulls.createReview", "POST", "/repos/{owner}/{repo}/pulls/{number}/reviews")
def pulls_review(ctx: Instance, req: Request) -> Any:
    b = _json_args(req)
    return pull_tools.create_pull_request_review.fn(ctx, owner=req.params["owner"], repo=req.params["repo"],
                                                    pull_number=int(req.params["number"]), body=b.get("body") or "",
                                                    event=b.get("event") or "COMMENT", commit_id=b.get("commit_id"),
                                                    comments=b.get("comments"))


@op("github.pulls.merge", "PUT", "/repos/{owner}/{repo}/pulls/{number}/merge")
def pulls_merge(ctx: Instance, req: Request) -> Any:
    b = _json_args(req)
    return pull_tools.merge_pull_request.fn(ctx, owner=req.params["owner"], repo=req.params["repo"],
                                            pull_number=int(req.params["number"]), commit_title=b.get("commit_title"),
                                            commit_message=b.get("commit_message"), merge_method=b.get("merge_method"))


@op("github.pulls.listReviewComments", "GET", "/repos/{owner}/{repo}/pulls/{number}/comments", read_only=True)
def pulls_review_comments(ctx: Instance, req: Request) -> Any:
    return pull_tools.get_pull_request_comments.fn(ctx, owner=req.params["owner"], repo=req.params["repo"],
                                                   pull_number=int(req.params["number"]))


@op("github.pulls.requestReviewers", "POST", "/repos/{owner}/{repo}/pulls/{number}/requested_reviewers")
def pulls_request_reviewers(ctx: Instance, req: Request) -> Any:
    r = _r(ctx, req)
    n = int(req.params["number"])
    pr = r["pulls"].get(n) or (_ for _ in ()).throw(_err(404, "Not Found"))
    for login in _json_args(req).get("reviewers") or []:
        if login not in ctx.state["users"]:
            raise _unprocessable("Reviews may only be requested from collaborators. One or more of the users or teams "
                                 "you specified is not a collaborator of the " + r["full_name"] + " repository.")
        if login == r["issues"][n]["user"]:
            raise _unprocessable("Review cannot be requested from pull request author.")
        pr.setdefault("requested_reviewers", [])
        if login not in pr["requested_reviewers"]:
            pr["requested_reviewers"].append(login)
    return Response(201, pull_tools.get_pull_request.fn(ctx, owner=req.params["owner"], repo=req.params["repo"],
                                                        pull_number=n))


# -- statuses and checks ------------------------------------------------------------------------------

def _statuses(r: dict[str, Any], sha: str) -> list[dict[str, Any]]:
    return r["statuses"].get(sha, [])


@op("github.repos.getCombinedStatusForRef", "GET", "/repos/{owner}/{repo}/commits/{ref}/status", read_only=True)
def combined_status(ctx: Instance, req: Request) -> Any:
    from .model import _ref_sha
    r = _r(ctx, req)
    sha = _ref_sha(r, req.params["ref"])
    sts = _statuses(r, sha)
    states = {x["state"] for x in sts}
    state = "failure" if states & {"failure", "error"} else "pending" if "pending" in states or not sts else "success"
    return {"state": state, "sha": sha, "total_count": len(sts),
            "statuses": [{"context": x["context"], "state": x["state"], "description": x["description"],
                          "target_url": None, "id": abs(hash((sha, x["context"]))) % 10**10} for x in sts],
            "repository": _repo_json(ctx.state, r)}


@op("github.checks.listForRef", "GET", "/repos/{owner}/{repo}/commits/{ref}/check-runs", read_only=True)
def check_runs(ctx: Instance, req: Request) -> Any:
    from .model import _ref_sha
    r = _r(ctx, req)
    sha = _ref_sha(r, req.params["ref"])
    runs = [x for x in r.get("runs", {}).values() if x["head_sha"] == sha]
    items = [_check_run(r, run, job) for run in runs for job in run["jobs"]]
    return {"total_count": len(items), "check_runs": items}


def _check_run(r: dict[str, Any], run: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    return {"id": job["id"], "name": job["name"], "head_sha": run["head_sha"], "status": job["status"],
            "conclusion": job["conclusion"], "started_at": job["started_at"], "completed_at": job["completed_at"],
            "details_url": f"https://github.com/{r['full_name']}/actions/runs/{run['id']}/job/{job['id']}",
            "html_url": f"https://github.com/{r['full_name']}/actions/runs/{run['id']}/job/{job['id']}",
            "app": {"slug": "github-actions", "name": "GitHub Actions"},
            "check_suite": {"id": run["id"] + 1}, "output": {"title": None, "summary": None}}


@op("github.repos.createCommitStatus", "POST", "/repos/{owner}/{repo}/statuses/{sha}")
def create_status(ctx: Instance, req: Request) -> Any:
    r = _r(ctx, req)
    b = _json_args(req)
    sha = req.params["sha"]
    if sha not in r["commits"]:
        raise _unprocessable("No commit found for SHA: " + sha)
    if b.get("state") not in ("error", "failure", "pending", "success"):
        raise _unprocessable("Validation Failed", [{"resource": "Status", "field": "state", "code": "custom"}])
    ctx_name = b.get("context", "default")
    r["statuses"][sha] = [x for x in r["statuses"].get(sha, []) if x["context"] != ctx_name] + [
        {"context": ctx_name, "state": b["state"], "description": b.get("description", "")}]
    return Response(201, {"state": b["state"], "context": ctx_name, "description": b.get("description")})


# -- GitHub Actions ------------------------------------------------------------------------------------

def _run_json(r: dict[str, Any], run: dict[str, Any], attempt: int | None = None) -> dict[str, Any]:
    base = f"{API}/repos/{r['full_name']}/actions/runs/{run['id']}"
    prs = [{"number": n, "head": {"ref": p["head"], "sha": run["head_sha"]}, "base": {"ref": p["base"]}}
           for n, p in r["pulls"].items() if p["head"] == run["head_branch"] and r["issues"][n]["state"] == "open"]
    return {"id": run["id"], "name": run["name"], "node_id": f"WFR_kwLO{run['id']}", "head_branch": run["head_branch"],
            "head_sha": run["head_sha"], "path": WORKFLOW["path"], "display_title": run["display_title"],
            "run_number": run["run_number"], "event": run["event"], "status": run["status"],
            "conclusion": run["conclusion"], "workflow_id": run["workflow_id"],
            "url": base, "html_url": f"https://github.com/{r['full_name']}/actions/runs/{run['id']}",
            "pull_requests": prs, "created_at": run["created_at"], "updated_at": run["updated_at"],
            "run_attempt": attempt or run["run_attempt"], "run_started_at": run["run_started_at"],
            "jobs_url": f"{base}/jobs", "logs_url": f"{base}/logs",
            "actor": {"login": run["actor"]}, "triggering_actor": {"login": run["actor"]},
            "head_commit": {"id": run["head_sha"], "message": r["commits"][run["head_sha"]]["message"]},
            "repository": {"full_name": r["full_name"], "name": r["name"]},
            "workflow_url": f"{API}/repos/{r['full_name']}/actions/workflows/{run['workflow_id']}"}


def _run(ctx: Instance, req: Request) -> tuple[dict[str, Any], dict[str, Any]]:
    r = _r(ctx, req)
    run = r.get("runs", {}).get(int(req.params["run_id"]))
    if run is None:
        raise _err(404, "Not Found")
    return r, run


@op("github.actions.listRepoWorkflows", "GET", "/repos/{owner}/{repo}/actions/workflows", read_only=True)
def workflows(ctx: Instance, req: Request) -> Any:
    r = _r(ctx, req)
    has = bool(r.get("ci") or r.get("runs"))
    items = [{**WORKFLOW, "node_id": "W_kwDO1", "created_at": r["created_at"], "updated_at": r["updated_at"],
              "url": f"{API}/repos/{r['full_name']}/actions/workflows/{WORKFLOW['id']}",
              "html_url": f"https://github.com/{r['full_name']}/blob/{r['default_branch']}/{WORKFLOW['path']}",
              "badge_url": f"https://github.com/{r['full_name']}/workflows/CI/badge.svg"}] if has else []
    return {"total_count": len(items), "workflows": items}


def _list_runs(ctx: Instance, req: Request, workflow: str | None = None) -> Any:
    r = _r(ctx, req)
    runs = sorted(r.get("runs", {}).values(), key=lambda x: x["id"], reverse=True)
    for key, field in (("branch", "head_branch"), ("event", "event"), ("head_sha", "head_sha")):
        if req.arg(key):
            runs = [x for x in runs if x[field] == req.arg(key)]
    if req.arg("status"):
        want = req.arg("status")
        runs = [x for x in runs if x["status"] == want or x["conclusion"] == want]
    if req.arg("actor"):
        runs = [x for x in runs if x["actor"] == req.arg("actor")]
    if workflow and workflow not in (str(WORKFLOW["id"]), WORKFLOW["path"].rsplit("/", 1)[-1], WORKFLOW["name"]):
        raise _err(404, "Not Found")
    per = max(1, min(req.int_arg("per_page", 30), 100))
    page = max(1, req.int_arg("page", 1))
    chunk = runs[(page - 1) * per: page * per]
    return {"total_count": len(runs), "workflow_runs": [_run_json(r, x) for x in chunk]}


@op("github.actions.listWorkflowRunsForRepo", "GET", "/repos/{owner}/{repo}/actions/runs", read_only=True)
def runs_list(ctx: Instance, req: Request) -> Any:
    return _list_runs(ctx, req)


@op("github.actions.listWorkflowRuns", "GET", "/repos/{owner}/{repo}/actions/workflows/{workflow}/runs", read_only=True)
def runs_for_workflow(ctx: Instance, req: Request) -> Any:
    return _list_runs(ctx, req, urllib.parse.unquote(req.params["workflow"]))


@op("github.actions.getWorkflow", "GET", "/repos/{owner}/{repo}/actions/workflows/{workflow}", read_only=True)
def workflow_get(ctx: Instance, req: Request) -> Any:
    wf = workflows(ctx, req)
    if not wf["workflows"]:
        raise _err(404, "Not Found")
    return wf["workflows"][0]


@op("github.actions.getWorkflowRun", "GET", "/repos/{owner}/{repo}/actions/runs/{run_id}", read_only=True)
def run_get(ctx: Instance, req: Request) -> Any:
    r, run = _run(ctx, req)
    return _run_json(r, run)


@op("github.actions.getWorkflowRunAttempt", "GET", "/repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{attempt}",
    read_only=True)
def run_attempt(ctx: Instance, req: Request) -> Any:
    r, run = _run(ctx, req)
    n = int(req.params["attempt"])
    if not 1 <= n <= run["run_attempt"]:
        raise _err(404, "Not Found")
    return _run_json(r, run, n)


def _job_json(r: dict[str, Any], run: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    failed = job["conclusion"] == "failure"
    steps = []
    for i, name in enumerate(STEPS, 1):
        done = job["status"] == "completed"
        failing_step = failed and name == "Run tests"
        skipped = failed and i > STEPS.index("Run tests") + 1 and not name.startswith(("Post", "Complete"))
        steps.append({"name": name, "status": "completed" if done or i < 5 else "in_progress" if i == 5 else "queued",
                      "conclusion": ("failure" if failing_step else "skipped" if skipped else "success") if done else None,
                      "number": i, "started_at": job["started_at"], "completed_at": job["completed_at"]})
    return {"id": job["id"], "run_id": run["id"], "workflow_name": run["name"], "head_branch": run["head_branch"],
            "run_url": f"{API}/repos/{r['full_name']}/actions/runs/{run['id']}", "run_attempt": run["run_attempt"],
            "node_id": f"CR_kwDO{job['id']}", "head_sha": run["head_sha"],
            "url": f"{API}/repos/{r['full_name']}/actions/jobs/{job['id']}",
            "html_url": f"https://github.com/{r['full_name']}/actions/runs/{run['id']}/job/{job['id']}",
            "status": job["status"], "conclusion": job["conclusion"], "created_at": job["started_at"],
            "started_at": job["started_at"], "completed_at": job["completed_at"], "name": job["name"], "steps": steps,
            "check_run_url": f"{API}/repos/{r['full_name']}/check-runs/{job['id']}", "labels": ["ubuntu-latest"],
            "runner_name": "GitHub Actions 7", "runner_group_name": "GitHub Actions"}


@op("github.actions.listJobsForWorkflowRun", "GET", "/repos/{owner}/{repo}/actions/runs/{run_id}/jobs", read_only=True)
def run_jobs(ctx: Instance, req: Request) -> Any:
    r, run = _run(ctx, req)
    return {"total_count": len(run["jobs"]), "jobs": [_job_json(r, run, j) for j in run["jobs"]]}


@op("github.actions.listJobsForWorkflowRunAttempt", "GET",
    "/repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs", read_only=True)
def run_attempt_jobs(ctx: Instance, req: Request) -> Any:
    return run_jobs(ctx, req)


def _job(ctx: Instance, req: Request) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    r = _r(ctx, req)
    jid = int(req.params["job_id"])
    for run in r.get("runs", {}).values():
        for job in run["jobs"]:
            if job["id"] == jid:
                return r, run, job
    raise _err(404, "Not Found")


@op("github.actions.getJobForWorkflowRun", "GET", "/repos/{owner}/{repo}/actions/jobs/{job_id}", read_only=True)
def job_get(ctx: Instance, req: Request) -> Any:
    r, run, job = _job(ctx, req)
    return _job_json(r, run, job)


@op("github.actions.downloadJobLogsForWorkflowRun", "GET", "/repos/{owner}/{repo}/actions/jobs/{job_id}/logs",
    read_only=True)
def job_logs(ctx: Instance, req: Request) -> Any:
    _, _, job = _job(ctx, req)
    if job["status"] != "completed":
        raise _err(404, "Not Found")
    return Response(200, job["log"].encode(), {"content-type": "text/plain; charset=utf-8"})


def _logs_zip(r: dict[str, Any], run: dict[str, Any]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, job in enumerate(run["jobs"]):
            name = job["name"].replace("/", "").replace(":", "")
            z.writestr(f"{n}_{name}.txt", job["log"])
            lines = job["log"].splitlines(keepends=True)
            for step in _job_json(r, run, job)["steps"]:
                part = "".join(lines) if step["name"] == "Run tests" else ""
                z.writestr(f"{name}/{step['number']}_{step['name'].replace('/', '')}.txt", part)
    return buf.getvalue()


@op("github.actions.downloadWorkflowRunLogs", "GET", "/repos/{owner}/{repo}/actions/runs/{run_id}/logs", read_only=True)
def run_logs(ctx: Instance, req: Request) -> Any:
    r, run = _run(ctx, req)
    if run["status"] != "completed":
        raise _err(404, "Not Found")
    return Response(200, _logs_zip(r, run), {"content-type": "application/zip"})


@op("github.actions.downloadWorkflowRunAttemptLogs", "GET",
    "/repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{attempt}/logs", read_only=True)
def run_attempt_logs(ctx: Instance, req: Request) -> Any:
    return run_logs(ctx, req)


def _rerun_op(ctx: Instance, req: Request, failed_only: bool) -> Any:
    r, run = _run(ctx, req)
    if run["status"] != "completed":
        raise _err(403, "This workflow is already running")
    if failed_only and run["conclusion"] != "failure":
        raise _err(403, "This workflow run cannot be retried")
    _rerun(ctx, r, run)
    return Response(201, {})


@op("github.actions.reRunWorkflow", "POST", "/repos/{owner}/{repo}/actions/runs/{run_id}/rerun")
def rerun(ctx: Instance, req: Request) -> Any:
    return _rerun_op(ctx, req, False)


@op("github.actions.reRunWorkflowFailedJobs", "POST", "/repos/{owner}/{repo}/actions/runs/{run_id}/rerun-failed-jobs")
def rerun_failed(ctx: Instance, req: Request) -> Any:
    return _rerun_op(ctx, req, True)


@op("github.actions.reRunJobForWorkflowRun", "POST", "/repos/{owner}/{repo}/actions/jobs/{job_id}/rerun")
def rerun_job(ctx: Instance, req: Request) -> Any:
    r, run, _ = _job(ctx, req)
    if run["status"] != "completed":
        raise _err(403, "This workflow is already running")
    _rerun(ctx, r, run)
    return Response(201, {})


@op("github.actions.cancelWorkflowRun", "POST", "/repos/{owner}/{repo}/actions/runs/{run_id}/cancel")
def cancel(ctx: Instance, req: Request) -> Any:
    _, run = _run(ctx, req)
    if run["status"] == "completed":
        raise _err(409, "Cannot cancel a workflow run that is completed.")
    run.update(status="completed", conclusion="cancelled")
    for job in run["jobs"]:
        job.update(status="completed", conclusion="cancelled")
    return Response(202, {})


@op("github.actions.listWorkflowRunArtifacts", "GET", "/repos/{owner}/{repo}/actions/runs/{run_id}/artifacts",
    read_only=True)
def artifacts(ctx: Instance, req: Request) -> Any:
    _run(ctx, req)
    return {"total_count": 0, "artifacts": []}


# -- search, rate limit -------------------------------------------------------------------------------

def _ungroup(q: str) -> str:
    """GitHub search groups with parentheses (``gh`` sends ``( words ) repo:x``); drop the grouping."""
    import re
    return re.sub(r"\s+", " ", re.sub(r"(?<![\w:])[()]|[()](?!\w)", " ", q)).strip()


@op("github.search.issuesAndPullRequests", "GET", "/search/issues", read_only=True)
def search_issues(ctx: Instance, req: Request) -> Any:
    return search_tools.search_issues.fn(ctx, q=_ungroup(req.arg("q") or ""), sort=req.arg("sort"), order=req.arg("order"),
                                         per_page=req.int_arg("per_page", 30), page=req.int_arg("page", 1))


@op("github.search.code", "GET", "/search/code", read_only=True)
def search_code(ctx: Instance, req: Request) -> Any:
    return search_tools.search_code.fn(ctx, q=req.arg("q") or "", per_page=req.int_arg("per_page", 30),
                                       page=req.int_arg("page", 1))


@op("github.search.repos", "GET", "/search/repositories", read_only=True)
def search_repos(ctx: Instance, req: Request) -> Any:
    from .repos import search_repositories
    return search_repositories.fn(ctx, query=req.arg("q") or "", perPage=req.int_arg("per_page", 30),
                                  page=req.int_arg("page", 1))


@op("github.rateLimit.get", "GET", "/rate_limit", read_only=True)
def rate_limit(ctx: Instance, req: Request) -> Any:
    reset = int(ctx.now().timestamp()) + 3600
    core = {"limit": 5000, "used": len(ctx.calls), "remaining": 5000 - len(ctx.calls), "reset": reset}
    return {"resources": {"core": core, "graphql": core, "search": {"limit": 30, "used": 0, "remaining": 30,
                                                                     "reset": reset}}, "rate": core}


from . import graphql_api  # noqa: E402,F401  (POST /graphql)
