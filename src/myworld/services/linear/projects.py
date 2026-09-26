"""linear: project tools."""

from __future__ import annotations

from typing import Annotated, Any

from ...core.instance import Instance
from ...core.tools import tool
from .model import PROJECT_STATES, _err, _find_project, _find_team, _find_user, _iso, _project_json, _slug, _uuid


@tool("list_projects", read_only=True)
def list_projects(ctx: Instance,
                  team: Annotated[str | None, "Team name, key or ID"] = None,
                  query: Annotated[str | None, "Search for project name"] = None,
                  state: Annotated[str | None, "State name or ID"] = None,
                  member: Annotated[str | None, "User ID, name, email, or 'me'"] = None,
                  includeArchived: Annotated[bool | None, "Include archived projects"] = False,
                  limit: Annotated[int | None, "Max results (default 50)"] = 50) -> dict[str, Any]:
    """List projects in the user's Linear workspace"""
    s = ctx.state
    tid = _find_team(s, team)["id"] if team else None
    lead = _find_user(s, member)["id"] if member else None
    ps = [p for p in s["projects"].values()
          if (not tid or tid in p["teamIds"]) and (not query or query.lower() in p["name"].lower())
          and (not state or p["state"] == state.lower()) and (not lead or p["leadId"] == lead)
          and (includeArchived or not p["archivedAt"])]
    return {"projects": [_project_json(s, p) for p in ps[: max(1, limit or 50)]]}


@tool("get_project", read_only=True)
def get_project(ctx: Instance, query: Annotated[str, "Project ID or name"]) -> dict[str, Any]:
    """Retrieve details of a specific project in Linear"""
    return _project_json(ctx.state, _find_project(ctx.state, query))


@tool("create_project")
def create_project(ctx: Instance,
                   name: Annotated[str, "Project name"],
                   team: Annotated[str, "Team name, key or ID"],
                   summary: Annotated[str | None, "A concise plaintext summary (max 255 chars)"] = None,
                   description: Annotated[str | None, "Content as Markdown"] = None,
                   lead: Annotated[str | None, "User ID, name, email, or 'me'"] = None,
                   state: Annotated[str | None, "Project state: backlog, planned, started, paused, completed, canceled"] = None,
                   startDate: Annotated[str | None, "Start date (ISO format)"] = None,
                   targetDate: Annotated[str | None, "Target date (ISO format)"] = None,
                   labels: Annotated[list[str] | None, "Project label names"] = None) -> dict[str, Any]:
    """Create a new project in Linear"""
    s = ctx.state
    t = _find_team(s, team)
    if any(p["name"].lower() == name.lower() and not p["archivedAt"] for p in s["projects"].values()):
        raise _err(f"A project named '{name}' already exists")
    if state and state.lower() not in PROJECT_STATES:
        raise _err(f"Argument Validation Error: state must be one of {', '.join(PROJECT_STATES)}")
    if summary and len(summary) > 255:
        raise _err("Argument Validation Error: summary must be shorter than or equal to 255 characters")
    if startDate and targetDate and targetDate < startDate:
        raise _err("Argument Validation Error: targetDate must be after startDate")
    pid = _uuid(ctx)
    s["projects"][pid] = {"id": pid, "name": name, "state": (state or "planned").lower(), "summary": summary,
                          "description": description, "leadId": _find_user(s, lead)["id"] if lead else None,
                          "teamIds": [t["id"]], "startDate": startDate, "targetDate": targetDate, "createdAt": _iso(ctx),
                          "updatedAt": _iso(ctx), "archivedAt": None, "labels": list(labels or []),
                          "url": f"https://linear.app/acme/project/{_slug(name)}-{pid[:12]}"}
    return _project_json(s, s["projects"][pid])


@tool("update_project", idempotent=True)
def update_project(ctx: Instance,
                   id: Annotated[str, "Project ID or name"],
                   name: Annotated[str | None, "New name"] = None,
                   summary: Annotated[str | None, "New summary"] = None,
                   description: Annotated[str | None, "Content as Markdown"] = None,
                   lead: Annotated[str | None, "User ID, name, email, or 'me'"] = None,
                   state: Annotated[str | None, "Project state"] = None,
                   startDate: Annotated[str | None, "Start date (ISO format)"] = None,
                   targetDate: Annotated[str | None, "Target date (ISO format)"] = None,
                   labels: Annotated[list[str] | None, "Project label names"] = None) -> dict[str, Any]:
    """Update an existing Linear project"""
    s = ctx.state
    p = _find_project(s, id)
    if state is not None and state.lower() not in PROJECT_STATES:
        raise _err(f"Argument Validation Error: state must be one of {', '.join(PROJECT_STATES)}")
    for k, v in (("name", name), ("summary", summary), ("description", description), ("startDate", startDate),
                 ("targetDate", targetDate), ("labels", labels)):
        if v is not None:
            p[k] = v
    if state is not None:
        p["state"] = state.lower()
    if lead is not None:
        p["leadId"] = _find_user(s, lead)["id"] if lead else None
    p["updatedAt"] = _iso(ctx)
    return _project_json(s, p)


@tool("list_project_labels", read_only=True)
def list_project_labels(ctx: Instance, name: Annotated[str | None, "Filter by label name"] = None) -> dict[str, Any]:
    """List available project labels in the Linear workspace"""
    names = sorted({l for p in ctx.state["projects"].values() for l in p["labels"]})
    return {"labels": [{"name": n} for n in names if not name or name.lower() in n.lower()]}
