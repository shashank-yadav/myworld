"""github: pull request tools."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from ...core.instance import Instance
from ...core.tools import tool
from .model import (
    _ancestors,
    _brief_user,
    _changes,
    _close_referenced,
    _commit,
    _err,
    _iso,
    _merge_base,
    _new_pull,
    _paginate,
    _pr_json,
    _pr_state,
    _pull,
    _pushed,
    _repo,
    _unprocessable,
)


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
        current = {x["context"]: x["state"] for x in r["statuses"].get(st["head_sha"], [])}.get(st["failing_checks"][0])
        raise _err(405, f"Required status check \"{st['failing_checks'][0]}\" is "
                        f"{'expected' if current is None else 'in progress' if current == 'pending' and s.get('_v1') else 'failing'}.")
    if st["review_block"]:
        raise _err(405, st["review_block"])
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
    _close_referenced(ctx, r, pr, message)
    _pushed(ctx, r, sha)
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
    _pushed(ctx, r, sha)
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
