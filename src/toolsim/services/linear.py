"""Linear.

Tool names follow Linear's official hosted MCP server (mcp.linear.app). Parameters and objects
follow Linear's API: issues have team-scoped identifiers (ENG-12), each team has its own
workflow states, priorities are 0 (none) to 4 (low), and most references accept a name, a
key, an email or an ID.

Seed format::

    viewer: {name: Alex Rivera, email: alex@acme.com}
    users: [{name: John Park, email: john@acme.com}]
    teams:
      - key: ENG
        name: Engineering
        labels: [Bug, Feature]
        projects: [{name: Billing v2, state: started, lead: john@acme.com}]
        issues:
          - {title: Duplicate charges on retry, state: In Progress, priority: 1, assignee: john@acme.com,
             labels: [Bug], project: Billing v2, comments: [{author: alex@acme.com, body: Customer escalated}]}
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Annotated, Any, Literal

from ..core.instance import Instance, Service
from ..core.tools import ToolError, tool

PRIORITY_NAMES = {0: "No priority", 1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}
DEFAULT_STATES = [("Backlog", "backlog", "#bec2c8"), ("Todo", "unstarted", "#e2e2e2"), ("In Progress", "started", "#f2c94c"),
                  ("In Review", "started", "#0f783c"), ("Done", "completed", "#5e6ad2"), ("Canceled", "canceled", "#95a2b3"),
                  ("Duplicate", "canceled", "#95a2b3")]
PROJECT_STATES = ["backlog", "planned", "started", "paused", "completed", "canceled"]
DOCS = [
    ("Issue priorities", "Priorities range from Urgent (1) to Low (4); 0 means no priority."),
    ("Workflow states", "Each team has its own workflow: Backlog, Todo, In Progress, Done, Canceled. Types: backlog, unstarted, started, completed, canceled."),
    ("Cycles", "Cycles are time-boxed iterations per team. Issues can belong to one cycle at a time."),
    ("Projects", "Projects group issues across teams toward a deliverable with a lead, target date and state."),
    ("Sub-issues", "Set parentId to make an issue a sub-issue. Sub-issues inherit the parent's project."),
]


def _err(message: str, kind: str = "InvalidInput") -> ToolError:
    return ToolError({"error": message, "type": kind})


def _not_found(what: str) -> ToolError:
    return ToolError({"error": f"Entity not found: {what}", "type": "NotFound"})


class Linear(Service):
    name = "linear"
    title = "Linear"
    description = "Simulated Linear workspace. Behaves like Linear's MCP server; nothing is really changed."
    fidelity = "preview"  # tool names are real; some parameters/response shapes are inferred
    versions = {"2026-09-25": "Initial release: 23 tools modeled on Linear's hosted MCP server."}

    def probe(self, ctx: Instance) -> None:
        c = ctx.call
        c("list_teams", {})
        c("list_issue_statuses", {"team": "ENG"})
        c("list_issues", {"team": "ENG", "state": "In Progress"})
        c("list_issues", {"assignee": "me"})
        c("get_issue", {"id": "ENG-1"})
        c("get_issue", {"id": "ENG-999"})
        c("create_issue", {"title": "Add idempotency keys to refunds", "team": "ENG", "priority": 2, "labels": ["Bug"],
                           "assignee": "john@acme.com", "project": "Billing v2"})
        c("update_issue", {"id": "ENG-1", "state": "Done"})
        c("update_issue", {"id": "ENG-1", "state": "Nope"})
        c("create_comment", {"issueId": "ENG-2", "body": "Repro steps attached"})
        c("list_comments", {"issueId": "ENG-2"})
        c("list_projects", {})
        c("create_issue_label", {"name": "Customer", "teamId": "ENG"})
        c("list_cycles", {"teamId": "ENG", "type": "current"})
        c("get_user", {"query": "me"})
        c("search_documentation", {"query": "priority"})

    def default_seed(self) -> dict[str, Any]:
        return {
            "viewer": {"name": "Alex Rivera", "email": "alex@acme.com"},
            "users": [{"name": "John Park", "email": "john@acme.com"}, {"name": "Priya Shah", "email": "priya@acme.com"}],
            "teams": [
                {"key": "ENG", "name": "Engineering", "labels": ["Bug", "Feature", "Tech debt"],
                 "projects": [{"name": "Billing v2", "state": "started", "lead": "john@acme.com",
                               "targetDate": "2026-10-31", "summary": "Rebuild invoicing and refunds"}],
                 "issues": [
                     {"title": "Duplicate charges when payment retries", "state": "In Progress", "priority": 1,
                      "assignee": "john@acme.com", "labels": ["Bug"], "project": "Billing v2",
                      "comments": [{"author": "alex@acme.com", "body": "Two customers escalated this week."}]},
                     {"title": "Invoice PDF renders wrong currency symbol", "state": "Todo", "priority": 3,
                      "labels": ["Bug"], "project": "Billing v2"},
                     {"title": "Migrate cron jobs to the new scheduler", "state": "Backlog", "priority": 4,
                      "labels": ["Tech debt"], "assignee": "priya@acme.com"},
                     {"title": "Rotate Stripe webhook secret", "state": "Done", "priority": 2, "assignee": "alex@acme.com"},
                 ]},
                {"key": "OPS", "name": "Operations", "labels": ["Incident"],
                 "issues": [{"title": "Postmortem: API latency spike", "state": "In Review", "priority": 2,
                             "assignee": "alex@acme.com", "labels": ["Incident"]}]},
            ],
        }

    def initial_state(self, seed: dict[str, Any], ctx: Instance) -> dict[str, Any]:
        state: dict[str, Any] = {"users": {}, "teams": {}, "states": {}, "labels": {}, "projects": {}, "issues": {},
                                 "comments": {}, "cycles": {}, "documents": {}, "project_labels": {}}
        v = seed.get("viewer") or {"name": "Alex Rivera", "email": "alex@acme.com"}
        state["viewer"] = _new_user(ctx, state, v, admin=True)["id"]
        for u in seed.get("users", []):
            _new_user(ctx, state, u)
        for t in seed.get("teams", []):
            tid = _uuid(ctx)
            state["teams"][tid] = {"id": tid, "key": t["key"], "name": t["name"], "issueCount": 0,
                                   "description": t.get("description"), "createdAt": _iso(ctx)}
            for pos, (name, typ, color) in enumerate(DEFAULT_STATES):
                sid = _uuid(ctx)
                state["states"][sid] = {"id": sid, "name": name, "type": typ, "color": color, "position": pos, "teamId": tid}
            for name in t.get("labels", []):
                _new_label(ctx, state, name, tid)
            start = ctx.now().date() - dt.timedelta(days=ctx.now().weekday())
            for n, offset in ((1, -2), (2, 0), (3, 2)):
                cid = _uuid(ctx)
                s0 = start + dt.timedelta(weeks=offset)
                state["cycles"][cid] = {"id": cid, "number": n, "teamId": tid, "startsAt": s0.isoformat(),
                                        "endsAt": (s0 + dt.timedelta(weeks=2)).isoformat(), "name": f"Cycle {n}"}
            for p in t.get("projects", []):
                pid = _uuid(ctx)
                state["projects"][pid] = {"id": pid, "name": p["name"], "state": p.get("state", "planned"),
                                          "summary": p.get("summary"), "description": p.get("description"),
                                          "leadId": _find_user(state, p["lead"])["id"] if p.get("lead") else None,
                                          "teamIds": [tid], "startDate": p.get("startDate"), "targetDate": p.get("targetDate"),
                                          "createdAt": _iso(ctx), "updatedAt": _iso(ctx), "archivedAt": None,
                                          "url": f"https://linear.app/acme/project/{_slug(p['name'])}-{pid[:12]}", "labels": []}
            for i in t.get("issues", []):
                issue = _create(ctx, state, {**i, "team": t["key"]}, creator=state["viewer"])
                for c in i.get("comments", []):
                    _comment(ctx, state, issue["id"], c["body"], _find_user(state, c.get("author", "me"))["id"])
        for d in seed.get("documents", []):
            did = _uuid(ctx)
            state["documents"][did] = {"id": did, "title": d["title"], "content": d.get("content", ""), "projectId": None,
                                       "createdAt": _iso(ctx), "updatedAt": _iso(ctx)}
        return state

    def fault_error(self, fault: Any) -> tuple[Any, int]:
        if fault.kind == "rate_limit":
            return {"error": "Rate limit exceeded. Please retry after the reset time.", "type": "Ratelimited",
                    "retryAfter": fault.retry_after}, 429
        if fault.kind == "server_error":
            return {"error": "Internal server error", "type": "InternalError"}, 500
        return super().fault_error(fault)


# -- model --------------------------------------------------------------------------------

def _uuid(ctx: Instance) -> str:
    return str(uuid.UUID(int=ctx.rng.getrandbits(128), version=4))


def _iso(ctx: Instance) -> str:
    return ctx.now().strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def _new_user(ctx: Instance, state: dict[str, Any], u: dict[str, Any], admin: bool = False) -> dict[str, Any]:
    uid = _uuid(ctx)
    user = {"id": uid, "name": u["name"], "displayName": u.get("displayName", u["name"].split()[0].lower()),
            "email": u["email"], "active": u.get("active", True), "admin": admin, "createdAt": _iso(ctx)}
    state["users"][uid] = user
    return user


def _new_label(ctx: Instance, state: dict[str, Any], name: str, team_id: str | None, color: str | None = None,
               description: str | None = None) -> dict[str, Any]:
    lid = _uuid(ctx)
    label = {"id": lid, "name": name, "color": color or f"#{ctx.hex(6)}", "description": description, "teamId": team_id}
    state["labels"][lid] = label
    return label


def _find_user(state: dict[str, Any], q: str | None) -> dict[str, Any]:
    if q is None:
        raise _not_found("User")
    if q.lower() == "me":
        return state["users"][state["viewer"]]
    ql = q.lower()
    for u in state["users"].values():
        if ql in (u["id"], u["email"].lower(), u["name"].lower(), u["displayName"].lower()):
            return u
    raise _not_found("User")


def _find_team(state: dict[str, Any], q: str | None) -> dict[str, Any]:
    if not q:
        raise _err("Argument Validation Error: team is required")
    ql = q.lower()
    for t in state["teams"].values():
        if ql in (t["id"], t["key"].lower(), t["name"].lower()):
            return t
    raise _not_found("Team")


def _find_state(state: dict[str, Any], team_id: str, q: str) -> dict[str, Any]:
    ql = q.lower()
    for s in state["states"].values():
        if s["teamId"] == team_id and ql in (s["id"], s["name"].lower()):
            return s
    if any(s["id"] == q for s in state["states"].values()):
        raise _err("Workflow state does not belong to the issue's team")
    raise _not_found("WorkflowState")


def _find_project(state: dict[str, Any], q: str) -> dict[str, Any]:
    ql = q.lower()
    for p in state["projects"].values():
        if ql in (p["id"], p["name"].lower(), _slug(p["name"])):
            return p
    raise _not_found("Project")


def _find_issue(state: dict[str, Any], q: str) -> dict[str, Any]:
    qu = (q or "").upper()
    for i in state["issues"].values():
        if q == i["id"] or qu == i["identifier"]:
            return i
    raise _not_found("Issue")


def _labels_for(state: dict[str, Any], team_id: str, names: list[str]) -> list[str]:
    out = []
    for n in names:
        lab = next((l for l in state["labels"].values() if n.lower() in (l["name"].lower(), l["id"])
                    and l["teamId"] in (team_id, None)), None)
        if lab is None:
            raise _not_found(f"IssueLabel '{n}'")
        out.append(lab["id"])
    return out


def _priority(p: Any) -> int:
    if p is None:
        return 0
    if isinstance(p, str) and not p.isdigit():
        by_name = {v.lower(): k for k, v in PRIORITY_NAMES.items()} | {"normal": 3, "none": 0}
        if p.lower() not in by_name:
            raise _err(f"Argument Validation Error: invalid priority '{p}'")
        return by_name[p.lower()]
    v = int(p)
    if v not in PRIORITY_NAMES:
        raise _err("Argument Validation Error: priority must be between 0 and 4")
    return v


def _create(ctx: Instance, state: dict[str, Any], a: dict[str, Any], creator: str) -> dict[str, Any]:
    team = _find_team(state, a.get("team"))
    if not str(a.get("title") or "").strip():
        raise _err("Argument Validation Error: title should not be empty")
    team["issueCount"] += 1
    number = team["issueCount"]
    st = _find_state(state, team["id"], a.get("state") or "Todo")
    iid = _uuid(ctx)
    identifier = f"{team['key']}-{number}"
    parent = _find_issue(state, a["parentId"]) if a.get("parentId") else None
    project = _find_project(state, a["project"]) if a.get("project") else None
    if parent and not project and parent["projectId"]:
        project = state["projects"][parent["projectId"]]
    assignee = _find_user(state, a["assignee"])["id"] if a.get("assignee") else None
    issue = {"id": iid, "identifier": identifier, "number": number, "title": a["title"],
             "description": a.get("description"), "priority": _priority(a.get("priority")), "stateId": st["id"],
             "teamId": team["id"], "assigneeId": assignee, "creatorId": creator,
             "labelIds": _labels_for(state, team["id"], a.get("labels") or []),
             "projectId": project["id"] if project else None, "parentId": parent["id"] if parent else None,
             "cycleId": None, "dueDate": a.get("dueDate"), "estimate": a.get("estimate"),
             "createdAt": _iso(ctx), "updatedAt": _iso(ctx), "startedAt": None, "completedAt": None,
             "canceledAt": None, "archivedAt": None,
             "url": f"https://linear.app/acme/issue/{identifier}/{_slug(a['title'])}",
             "branchName": f"{state['users'][creator]['displayName']}/{identifier.lower()}-{_slug(a['title'])[:40]}"}
    _set_state_times(ctx, issue, st)
    state["issues"][iid] = issue
    state["comments"][iid] = []
    return issue


def _set_state_times(ctx: Instance, issue: dict[str, Any], st: dict[str, Any]) -> None:
    now = _iso(ctx)
    issue["startedAt"] = issue["startedAt"] or (now if st["type"] in ("started", "completed") else None)
    issue["completedAt"] = now if st["type"] == "completed" else None
    issue["canceledAt"] = now if st["type"] == "canceled" else None


def _comment(ctx: Instance, state: dict[str, Any], issue_id: str, body: str, author: str,
             parent_id: str | None = None) -> dict[str, Any]:
    c = {"id": _uuid(ctx), "body": body, "userId": author, "parentId": parent_id, "createdAt": _iso(ctx),
         "updatedAt": _iso(ctx), "issueId": issue_id}
    state["comments"][issue_id].append(c)
    state["issues"][issue_id]["updatedAt"] = _iso(ctx)
    return c


def _issue_json(state: dict[str, Any], i: dict[str, Any], full: bool = False) -> dict[str, Any]:
    st = state["states"][i["stateId"]]
    team = state["teams"][i["teamId"]]
    user = lambda uid: state["users"][uid]["name"] if uid else None  # noqa: E731
    out = {"id": i["id"], "identifier": i["identifier"], "title": i["title"],
           "priority": {"value": i["priority"], "name": PRIORITY_NAMES[i["priority"]]},
           "url": i["url"], "gitBranchName": i["branchName"], "createdAt": i["createdAt"], "updatedAt": i["updatedAt"],
           "archivedAt": i["archivedAt"], "completedAt": i["completedAt"], "dueDate": i["dueDate"],
           "status": st["name"], "statusType": st["type"],
           "labels": [state["labels"][l]["name"] for l in i["labelIds"]],
           "assignee": user(i["assigneeId"]), "assigneeId": i["assigneeId"], "createdBy": user(i["creatorId"]),
           "project": state["projects"][i["projectId"]]["name"] if i["projectId"] else None, "projectId": i["projectId"],
           "team": team["name"], "teamId": team["id"],
           "parentId": i["parentId"], "estimate": i["estimate"]}
    if full:
        out["description"] = i["description"]
        out["children"] = [x["identifier"] for x in state["issues"].values() if x["parentId"] == i["id"]]
    return out


def _project_json(state: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
    issues = [i for i in state["issues"].values() if i["projectId"] == p["id"]]
    done = sum(state["states"][i["stateId"]]["type"] == "completed" for i in issues)
    return {"id": p["id"], "name": p["name"], "summary": p["summary"], "description": p["description"],
            "state": p["state"], "url": p["url"], "lead": state["users"][p["leadId"]]["name"] if p["leadId"] else None,
            "startDate": p["startDate"], "targetDate": p["targetDate"], "createdAt": p["createdAt"],
            "updatedAt": p["updatedAt"], "archivedAt": p["archivedAt"],
            "teams": [state["teams"][t]["name"] for t in p["teamIds"]], "labels": p["labels"],
            "progress": round(done / len(issues), 2) if issues else 0.0}


# -- tools: issues -------------------------------------------------------------------------

@tool("list_issues", read_only=True)
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
    team_id = _find_team(s, team)["id"] if team else None
    assignee_id = _find_user(s, assignee)["id"] if assignee else None
    project_id = _find_project(s, project)["id"] if project else None
    parent = _find_issue(s, parentId)["id"] if parentId else None
    out = []
    for i in s["issues"].values():
        st = s["states"][i["stateId"]]
        if (team_id and i["teamId"] != team_id) or (assignee_id and i["assigneeId"] != assignee_id) \
                or (project_id and i["projectId"] != project_id) or (parent and i["parentId"] != parent) \
                or (cycle and i["cycleId"] != cycle) or (i["archivedAt"] and not includeArchived):
            continue
        if state and state.lower() not in (st["name"].lower(), st["id"], st["type"]):
            continue
        if label and not any(label.lower() in (s["labels"][l]["name"].lower(), l) for l in i["labelIds"]):
            continue
        if query and query.lower() not in f"{i['title']} {i['description'] or ''} {i['identifier']}".lower():
            continue
        out.append(i)
    out.sort(key=lambda i: (i[orderBy or "updatedAt"], i["number"]), reverse=True)
    lim = max(1, min(limit or 50, 250))
    return {"issues": [_issue_json(s, i) for i in out[:lim]], "hasNextPage": len(out) > lim}


@tool("get_issue", read_only=True)
def get_issue(ctx: Instance, id: Annotated[str, "Issue ID or identifier (e.g. ENG-123)"]) -> dict[str, Any]:
    """Retrieve detailed information about an issue by ID, including attachments and git branch name"""
    return _issue_json(ctx.state, _find_issue(ctx.state, id), full=True)


@tool("create_issue")
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


@tool("update_issue", idempotent=True)
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


# -- tools: teams, states, labels, cycles --------------------------------------------------

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


# -- tools: projects -----------------------------------------------------------------------

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


# -- tools: users, documents, docs ----------------------------------------------------------

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


Linear.tools = [list_comments, create_comment, list_cycles, get_document, list_documents, get_issue, list_issues,
                create_issue, update_issue, list_issue_statuses, get_issue_status, list_issue_labels, create_issue_label,
                list_projects, get_project, create_project, update_project, list_project_labels, list_teams, get_team,
                list_users, get_user, search_documentation]
