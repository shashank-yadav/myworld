"""linear: state model, constants and helpers shared by the tools."""

from __future__ import annotations

import re
import uuid
from typing import Any

from ...core.instance import Instance
from ...core.tools import ToolError

V1 = "2026-09-25.1"
V2 = "2026-09-25.2"
SCALES = {"fibonacci": [1, 2, 3, 5, 8], "exponential": [1, 2, 4, 8, 16], "linear": [1, 2, 3, 4, 5],
          "tshirt": [1, 2, 3, 5, 8], "none": []}
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
    if state.get("_v1"):
        seen: dict[str, str] = {}
        for lid in out:
            lab = state["labels"][lid]
            if lab.get("isGroup"):
                raise _err(f"Argument Validation Error: '{lab['name']}' is a label group and can't be applied to issues")
            g = lab.get("parentId")
            if g in seen:
                raise _err(f"Argument Validation Error: labels '{seen[g]}' and '{lab['name']}' are both in the "
                           f"'{state['labels'][g]['name']}' group; an issue can have only one label from a group")
            if g:
                seen[g] = lab["name"]
    return out


def _find_cycle(state: dict[str, Any], team_id: str, q: str, today: str) -> dict[str, Any]:
    cycles = sorted((c for c in state["cycles"].values() if c["teamId"] == team_id), key=lambda c: c["startsAt"])
    ql = q.lower()
    if ql == "current":
        hit = next((c for c in cycles if c["startsAt"] <= today < c["endsAt"]), None)
    elif ql == "next":
        hit = next((c for c in cycles if c["startsAt"] > today), None)
    elif ql == "previous":
        hit = ([c for c in cycles if c["endsAt"] <= today] or [None])[-1]
    else:
        hit = next((c for c in cycles if ql in (c["id"], str(c["number"]), c["name"].lower())), None)
    if hit is None:
        raise _not_found(f"Cycle '{q}'")
    return hit


def _check_estimate(state: dict[str, Any], team: dict[str, Any], estimate: Any) -> None:
    if estimate is None or not state.get("_v1"):
        return
    scale = SCALES[team.get("estimates", "none")]
    if not scale:
        raise _err(f"Argument Validation Error: estimates are not enabled for team {team['key']}")
    if estimate not in scale:
        raise _err(f"Argument Validation Error: estimate must be one of {', '.join(map(str, scale))} for team {team['key']}")


def _set_cycle(ctx: Instance, state: dict[str, Any], issue: dict[str, Any], q: str | None) -> None:
    if q is None:
        return
    if q == "":
        issue["cycleId"] = None
        return
    today = ctx.now().date().isoformat()
    c = _find_cycle(state, issue["teamId"], q, today)
    if c["endsAt"] <= today:
        raise _err(f"Argument Validation Error: cycle {c['number']} is completed; issues can't be added to it")
    issue["cycleId"] = c["id"]


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
    _check_estimate(state, team, a.get("estimate"))
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
    if state.get("_v1"):
        c = state["cycles"].get(i["cycleId"]) if i["cycleId"] else None
        out["cycle"] = {"id": c["id"], "number": c["number"], "name": c["name"]} if c else None
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
