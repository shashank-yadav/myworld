"""linear: teams, workflow states, labels and cycles."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from ...core.instance import Instance
from ...core.tools import tool
from .model import _err, _find_state, _find_team, _new_label


@tool("list_teams", read_only=True)
def list_teams(ctx: Instance, query: Annotated[str | None, "Search query for team name"] = None) -> dict[str, Any]:
    """List teams in the user's Linear workspace"""
    teams = [t for t in ctx.state["teams"].values() if not query or query.lower() in t["name"].lower()]
    return {"teams": [{k: t[k] for k in ("id", "key", "name", "description", "createdAt")} for t in teams]}


@tool("get_team", read_only=True)
def get_team(ctx: Instance, query: Annotated[str, "Team UUID, key, or name"]) -> dict[str, Any]:
    """Retrieve details of a specific Linear team"""
    t = _find_team(ctx.state, query)
    return {k: t[k] for k in ("id", "key", "name", "description", "createdAt")}


@tool("list_issue_statuses", read_only=True)
def list_issue_statuses(ctx: Instance, team: Annotated[str, "Team name, key or ID"]) -> dict[str, Any]:
    """List available issue statuses in a Linear team"""
    tid = _find_team(ctx.state, team)["id"]
    return {"statuses": sorted((dict(s) for s in ctx.state["states"].values() if s["teamId"] == tid),
                               key=lambda s: s["position"])}


@tool("get_issue_status", read_only=True)
def get_issue_status(ctx: Instance,
                     id: Annotated[str, "Status ID or name"],
                     team: Annotated[str, "Team name, key or ID"]) -> dict[str, Any]:
    """Retrieve detailed information about an issue status in Linear by name or ID"""
    return dict(_find_state(ctx.state, _find_team(ctx.state, team)["id"], id))


@tool("list_issue_labels", read_only=True)
def list_issue_labels(ctx: Instance,
                      team: Annotated[str | None, "Team name, key or ID"] = None,
                      name: Annotated[str | None, "Filter by label name"] = None) -> dict[str, Any]:
    """List available issue labels in a Linear workspace or team"""
    s = ctx.state
    tid = _find_team(s, team)["id"] if team else None
    labels = [l for l in s["labels"].values() if (not tid or l["teamId"] in (tid, None))
              and (not name or name.lower() in l["name"].lower())]
    return {"labels": labels}


@tool("create_issue_label")
def create_issue_label(ctx: Instance,
                       name: Annotated[str, "Label name"],
                       color: Annotated[str | None, "Hex color code"] = None,
                       description: Annotated[str | None, "Label description"] = None,
                       teamId: Annotated[str | None, "Team name, key or ID (omit for a workspace label)"] = None) -> dict[str, Any]:
    """Create a new Linear issue label"""
    s = ctx.state
    tid = _find_team(s, teamId)["id"] if teamId else None
    if any(l["name"].lower() == name.lower() and l["teamId"] in (tid, None) for l in s["labels"].values()):
        raise _err(f"Duplicate label name: a label named '{name}' already exists")
    if color and not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise _err("Argument Validation Error: color must be a hex color")
    return _new_label(ctx, s, name, tid, color, description)


@tool("list_cycles", read_only=True)
def list_cycles(ctx: Instance,
                teamId: Annotated[str, "Team name, key or ID"],
                type: Annotated[Literal["current", "previous", "next"] | None, "Which cycle(s) to return"] = None) -> dict[str, Any]:
    """Retrieve cycles for a specific Linear team"""
    s = ctx.state
    tid = _find_team(s, teamId)["id"]
    today = ctx.now().date().isoformat()
    cycles = sorted((c for c in s["cycles"].values() if c["teamId"] == tid), key=lambda c: c["startsAt"])
    if type == "current":
        cycles = [c for c in cycles if c["startsAt"] <= today < c["endsAt"]]
    elif type == "previous":
        cycles = [c for c in cycles if c["endsAt"] <= today][-1:]
    elif type == "next":
        cycles = [c for c in cycles if c["startsAt"] > today][:1]
    return {"cycles": cycles}
