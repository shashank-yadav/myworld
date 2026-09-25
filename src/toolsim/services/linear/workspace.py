"""linear: users, documents and Linear docs search."""

from __future__ import annotations

from typing import Annotated, Any

from ...core.instance import Instance
from ...core.tools import tool
from .model import DOCS, _find_project, _find_user, _not_found, _slug


@tool("list_users", read_only=True)
def list_users(ctx: Instance, query: Annotated[str | None, "Filter by name or email"] = None) -> dict[str, Any]:
    """Retrieve users in the Linear workspace"""
    us = [u for u in ctx.state["users"].values()
          if not query or query.lower() in f"{u['name']} {u['email']} {u['displayName']}".lower()]
    return {"users": us}


@tool("get_user", read_only=True)
def get_user(ctx: Instance, query: Annotated[str, "User ID, name, email, or 'me'"]) -> dict[str, Any]:
    """Retrieve details of a specific Linear user"""
    return _find_user(ctx.state, query)


@tool("list_documents", read_only=True)
def list_documents(ctx: Instance,
                   query: Annotated[str | None, "Search query"] = None,
                   projectId: Annotated[str | None, "Filter by project ID or name"] = None,
                   limit: Annotated[int | None, "Max results (default 50)"] = 50) -> dict[str, Any]:
    """List documents in the user's Linear workspace"""
    s = ctx.state
    pid = _find_project(s, projectId)["id"] if projectId else None
    docs = [d for d in s["documents"].values() if (not pid or d["projectId"] == pid)
            and (not query or query.lower() in f"{d['title']} {d['content']}".lower())]
    return {"documents": [{k: v for k, v in d.items() if k != "content"} for d in docs[: max(1, limit or 50)]]}


@tool("get_document", read_only=True)
def get_document(ctx: Instance, id: Annotated[str, "Document ID or slug"]) -> dict[str, Any]:
    """Retrieve a Linear document by ID or slug"""
    d = ctx.state["documents"].get(id) or next((d for d in ctx.state["documents"].values() if _slug(d["title"]) == id), None)
    if d is None:
        raise _not_found("Document")
    return dict(d)


@tool("search_documentation", read_only=True)
def search_documentation(ctx: Instance,
                         query: Annotated[str, "The search query"],
                         page: Annotated[int | None, "The page number"] = 0) -> dict[str, Any]:
    """Search Linear's documentation to learn about features and usage"""
    words = query.lower().split()
    hits = [{"title": t, "content": c} for t, c in DOCS if any(w in f"{t} {c}".lower() for w in words)]
    return {"results": hits}
