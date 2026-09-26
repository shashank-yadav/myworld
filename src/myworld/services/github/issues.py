"""github: issue tools."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from ...core.instance import Instance
from ...core.tools import tool
from .model import _comment, _comment_json, _err, _iso, _issue_json, _new_issue, _not_found, _paginate, _repo, _unprocessable


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
