"""github: search."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from ...core.instance import Instance
from ...core.tools import tool
from .model import API, _brief_user, _issue_json, _paginate, _search_terms, _unprocessable


@tool("search_code", read_only=True)
def search_code(ctx: Instance,
                q: Annotated[str, "Search query (GitHub code search syntax, e.g. 'backoff repo:acme/api')"],
                sort: Annotated[str | None, "Sort field (only 'indexed' is supported)"] = None,
                order: Annotated[Literal["asc", "desc"] | None, "Sort order"] = None,
                per_page: Annotated[int | None, "Results per page (max 100)"] = None,
                page: Annotated[int | None, "Page number"] = None) -> dict[str, Any]:
    """Search for code across GitHub repositories (default branches)"""
    s = ctx.search_view("code")
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
    s = ctx.search_view("issues")
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
