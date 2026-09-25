"""github: repository tools."""

from __future__ import annotations

from typing import Annotated, Any

from ...core.instance import Instance
from ...core.tools import tool
from .model import _new_repo, _paginate, _repo, _repo_json, _search_terms, _unprocessable, _user


@tool("search_repositories", read_only=True)
def search_repositories(ctx: Instance,
                        query: Annotated[str, "Search query (see GitHub search syntax)"],
                        page: Annotated[int | None, "Page number for pagination (default: 1)"] = None,
                        perPage: Annotated[int | None, "Number of results per page (default: 30, max: 100)"] = None) -> dict[str, Any]:
    """Search for GitHub repositories"""
    s = ctx.search_view("repos")
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
