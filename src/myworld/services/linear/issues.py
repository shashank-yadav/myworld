"""linear: issue tools."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from ...core.instance import Instance
from ...core.tools import ToolError, tool
from .model import (
    V1,
    _check_estimate,
    _comment,
    _create,
    _err,
    _find_cycle,
    _find_issue,
    _find_project,
    _find_state,
    _find_team,
    _find_user,
    _iso,
    _issue_json,
    _labels_for,
    _not_found,
    _priority,
    _set_cycle,
    _set_state_times,
)


@tool("list_issues", read_only=True, until=V1)
def list_issues(ctx: Instance,
                query: Annotated[str | None, "Search issue titles and descriptions"] = None,
                team: Annotated[str | None, "Team name, key or ID"] = None,
                state: Annotated[str | None, "Status name or ID"] = None,
                assignee: Annotated[str | None, "User ID, name, email, or 'me'"] = None,
                label: Annotated[str | None, "Label name or ID"] = None,
                project: Annotated[str | None, "Project name or ID"] = None,
                cycle: Annotated[str | None, "Cycle ID"] = None,
                parentId: Annotated[str | None, "Parent issue ID or identifier"] = None,
                includeArchived: Annotated[bool | None, "Include archived issues"] = False,
                orderBy: Annotated[Literal["createdAt", "updatedAt"] | None, "Sort order"] = "updatedAt",
                limit: Annotated[int | None, "Max results (default 50, max 250)"] = 50) -> dict[str, Any]:
    """List issues in the user's Linear workspace"""
    s = ctx.state
    out = _filter_issues(ctx, query, team, state, assignee, label, project, {cycle} if cycle else None, parentId,
                         includeArchived, orderBy)
    lim = max(1, min(limit or 50, 250))
    return {"issues": [_issue_json(s, i) for i in out[:lim]], "hasNextPage": len(out) > lim}


def _filter_issues(ctx: Instance, query: str | None, team: str | None, state: str | None, assignee: str | None,
                   label: str | None, project: str | None, cycles: set[str] | None, parentId: str | None,
                   includeArchived: bool | None, orderBy: str | None) -> list[dict[str, Any]]:
    s = ctx.state
    cycle = cycles
    team_id = _find_team(s, team)["id"] if team else None
    assignee_id = _find_user(s, assignee)["id"] if assignee else None
    project_id = _find_project(s, project)["id"] if project else None
    parent = _find_issue(s, parentId)["id"] if parentId else None
    out = []
    for i in s["issues"].values():
        st = s["states"][i["stateId"]]
        if (team_id and i["teamId"] != team_id) or (assignee_id and i["assigneeId"] != assignee_id) \
                or (project_id and i["projectId"] != project_id) or (parent and i["parentId"] != parent) \
                or (cycle and i["cycleId"] not in cycle) or (i["archivedAt"] and not includeArchived):
            continue
        if state and state.lower() not in (st["name"].lower(), st["id"], st["type"]):
            continue
        if label and not any(label.lower() in (s["labels"][l]["name"].lower(), l) for l in i["labelIds"]):
            continue
        if query and query.lower() not in f"{i['title']} {i['description'] or ''} {i['identifier']}".lower():
            continue
        out.append(i)
    out.sort(key=lambda i: (i[orderBy or "updatedAt"], i["number"]), reverse=True)
    return out


@tool("list_issues", read_only=True, since=V1)
def list_issues_v1(ctx: Instance,
                   query: Annotated[str | None, "Search issue titles and descriptions"] = None,
                   team: Annotated[str | None, "Team name, key or ID"] = None,
                   state: Annotated[str | None, "Status name or ID"] = None,
                   assignee: Annotated[str | None, "User ID, name, email, or 'me'"] = None,
                   label: Annotated[str | None, "Label name or ID"] = None,
                   project: Annotated[str | None, "Project name or ID"] = None,
                   cycle: Annotated[str | None, "Cycle ID, number or name, or 'current' / 'next' / 'previous'"] = None,
                   parentId: Annotated[str | None, "Parent issue ID or identifier"] = None,
                   includeArchived: Annotated[bool | None, "Include archived issues"] = False,
                   orderBy: Annotated[Literal["createdAt", "updatedAt"] | None, "Sort order"] = "updatedAt",
                   limit: Annotated[int | None, "Max results (default 50, max 250)"] = 50,
                   cursor: Annotated[str | None, "Cursor from a previous page (nextCursor)"] = None) -> dict[str, Any]:
    """List issues in the user's Linear workspace"""
    s = ctx.state
    cycles = None
    if cycle:
        today = ctx.now().date().isoformat()
        teams = [_find_team(s, team)] if team else list(s["teams"].values())
        cycles = set()
        for t in teams:
            try:
                cycles.add(_find_cycle(s, t["id"], cycle, today)["id"])
            except ToolError:
                if team:
                    raise
    out = _filter_issues(ctx, query, team, state, assignee, label, project, cycles, parentId, includeArchived, orderBy)
    start = 0
    if cursor:
        start = next((n + 1 for n, i in enumerate(out) if i["id"] == cursor), -1)
        if start < 0:
            raise _err("Argument Validation Error: invalid cursor")
    lim = max(1, min(limit or 50, 250))
    page = out[start:start + lim]
    more = start + lim < len(out)
    return {"issues": [_issue_json(s, i) for i in page], "hasNextPage": more,
            "nextCursor": page[-1]["id"] if more and page else None}


@tool("get_issue", read_only=True)
def get_issue(ctx: Instance, id: Annotated[str, "Issue ID or identifier (e.g. ENG-123)"]) -> dict[str, Any]:
    """Retrieve detailed information about an issue by ID, including attachments and git branch name"""
    return _issue_json(ctx.state, _find_issue(ctx.state, id), full=True)


@tool("create_issue", until=V1)
def create_issue(ctx: Instance,
                 title: Annotated[str, "Issue title"],
                 team: Annotated[str, "Team name, key or ID"],
                 description: Annotated[str | None, "Content as Markdown"] = None,
                 assignee: Annotated[str | None, "User ID, name, email, or 'me'"] = None,
                 state: Annotated[str | None, "State type, name, or ID"] = None,
                 priority: Annotated[int | None, "0 = No priority, 1 = Urgent, 2 = High, 3 = Normal, 4 = Low"] = None,
                 labels: Annotated[list[str] | None, "Label names or IDs"] = None,
                 project: Annotated[str | None, "Project name or ID"] = None,
                 parentId: Annotated[str | None, "Parent issue ID or identifier"] = None,
                 dueDate: Annotated[str | None, "Due date (ISO format)"] = None,
                 estimate: Annotated[int | None, "Issue estimate value"] = None) -> dict[str, Any]:
    """Create a new Linear issue"""
    s = ctx.state
    issue = _create(ctx, s, {"title": title, "team": team, "description": description, "assignee": assignee,
                             "state": state, "priority": priority, "labels": labels, "project": project,
                             "parentId": parentId, "dueDate": dueDate, "estimate": estimate}, creator=s["viewer"])
    return _issue_json(s, issue, full=True)


@tool("update_issue", idempotent=True, until=V1)
def update_issue(ctx: Instance,
                 id: Annotated[str, "Issue ID or identifier"],
                 title: Annotated[str | None, "New title"] = None,
                 description: Annotated[str | None, "Content as Markdown"] = None,
                 assignee: Annotated[str | None, "User ID, name, email, or 'me'. Empty string to unassign"] = None,
                 state: Annotated[str | None, "State type, name, or ID"] = None,
                 priority: Annotated[int | None, "0 = No priority, 1 = Urgent, 2 = High, 3 = Normal, 4 = Low"] = None,
                 labels: Annotated[list[str] | None, "Label names or IDs (replaces existing)"] = None,
                 project: Annotated[str | None, "Project name or ID"] = None,
                 parentId: Annotated[str | None, "Parent issue ID or identifier"] = None,
                 dueDate: Annotated[str | None, "Due date (ISO format)"] = None,
                 estimate: Annotated[int | None, "Issue estimate value"] = None) -> dict[str, Any]:
    """Update an existing Linear issue"""
    s = ctx.state
    i = _find_issue(s, id)
    if title is not None:
        if not title.strip():
            raise _err("Argument Validation Error: title should not be empty")
        i["title"] = title
    if description is not None:
        i["description"] = description
    if assignee is not None:
        i["assigneeId"] = _find_user(s, assignee)["id"] if assignee else None
    if state is not None:
        st = _find_state(s, i["teamId"], state)
        i["stateId"] = st["id"]
        _set_state_times(ctx, i, st)
    if priority is not None:
        i["priority"] = _priority(priority)
    if labels is not None:
        i["labelIds"] = _labels_for(s, i["teamId"], labels)
    if project is not None:
        i["projectId"] = _find_project(s, project)["id"] if project else None
    if parentId is not None:
        p = _find_issue(s, parentId) if parentId else None
        if p and p["id"] == i["id"]:
            raise _err("An issue cannot be its own parent")
        i["parentId"] = p["id"] if p else None
    if dueDate is not None:
        i["dueDate"] = dueDate or None
    if estimate is not None:
        i["estimate"] = estimate
    i["updatedAt"] = _iso(ctx)
    return _issue_json(s, i, full=True)


@tool("create_issue", since=V1)
def create_issue_v1(ctx: Instance,
                    title: Annotated[str, "Issue title"],
                    team: Annotated[str, "Team name, key or ID"],
                    description: Annotated[str | None, "Content as Markdown"] = None,
                    assignee: Annotated[str | None, "User ID, name, email, or 'me'"] = None,
                    state: Annotated[str | None, "State type, name, or ID"] = None,
                    priority: Annotated[int | None, "0 = No priority, 1 = Urgent, 2 = High, 3 = Normal, 4 = Low"] = None,
                    labels: Annotated[list[str] | None, "Label names or IDs"] = None,
                    project: Annotated[str | None, "Project name or ID"] = None,
                    cycle: Annotated[str | None, "Cycle ID, number or name, or 'current' / 'next'"] = None,
                    parentId: Annotated[str | None, "Parent issue ID or identifier"] = None,
                    dueDate: Annotated[str | None, "Due date (ISO format)"] = None,
                    estimate: Annotated[int | None, "Issue estimate value (on the team's estimate scale)"] = None) -> dict[str, Any]:
    """Create a new Linear issue"""
    s = ctx.state
    issue = _create(ctx, s, {"title": title, "team": team, "description": description, "assignee": assignee,
                             "state": state, "priority": priority, "labels": labels, "project": project,
                             "parentId": parentId, "dueDate": dueDate, "estimate": estimate}, creator=s["viewer"])
    _set_cycle(ctx, s, issue, cycle)
    return _issue_json(s, issue, full=True)


@tool("update_issue", idempotent=True, since=V1)
def update_issue_v1(ctx: Instance,
                    id: Annotated[str, "Issue ID or identifier"],
                    title: Annotated[str | None, "New title"] = None,
                    description: Annotated[str | None, "Content as Markdown"] = None,
                    assignee: Annotated[str | None, "User ID, name, email, or 'me'. Empty string to unassign"] = None,
                    state: Annotated[str | None, "State type, name, or ID"] = None,
                    priority: Annotated[int | None, "0 = No priority, 1 = Urgent, 2 = High, 3 = Normal, 4 = Low"] = None,
                    labels: Annotated[list[str] | None, "Label names or IDs (replaces existing)"] = None,
                    project: Annotated[str | None, "Project name or ID"] = None,
                    cycle: Annotated[str | None, "Cycle ID, number or name, or 'current' / 'next'. Empty string to remove"] = None,
                    parentId: Annotated[str | None, "Parent issue ID or identifier"] = None,
                    dueDate: Annotated[str | None, "Due date (ISO format)"] = None,
                    estimate: Annotated[int | None, "Issue estimate value (on the team's estimate scale)"] = None) -> dict[str, Any]:
    """Update an existing Linear issue"""
    s = ctx.state
    i = _find_issue(s, id)
    _check_estimate(s, s["teams"][i["teamId"]], estimate)
    update_issue.fn(ctx, id=id, title=title, description=description, assignee=assignee, state=state,
                    priority=priority, labels=labels, project=project, parentId=parentId, dueDate=dueDate,
                    estimate=estimate)
    _set_cycle(ctx, s, i, cycle)
    return _issue_json(s, i, full=True)


@tool("list_comments", read_only=True)
def list_comments(ctx: Instance, issueId: Annotated[str, "Issue ID or identifier"]) -> dict[str, Any]:
    """List comments for a specific Linear issue"""
    s = ctx.state
    i = _find_issue(s, issueId)
    return {"comments": [{**c, "author": s["users"][c["userId"]]["name"]} for c in s["comments"][i["id"]]]}


@tool("create_comment")
def create_comment(ctx: Instance,
                   issueId: Annotated[str, "Issue ID or identifier"],
                   body: Annotated[str, "Content as Markdown"],
                   parentId: Annotated[str | None, "A parent comment ID to reply to"] = None) -> dict[str, Any]:
    """Create a comment on a specific Linear issue"""
    s = ctx.state
    i = _find_issue(s, issueId)
    if not body.strip():
        raise _err("Argument Validation Error: body should not be empty")
    if parentId and not any(c["id"] == parentId for c in s["comments"][i["id"]]):
        raise _not_found("Comment")
    c = _comment(ctx, s, i["id"], body, s["viewer"], parentId)
    return {**c, "author": s["users"][c["userId"]]["name"]}
