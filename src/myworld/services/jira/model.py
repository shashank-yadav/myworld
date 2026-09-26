"""jira: state model, constants and helpers shared by the tools."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from ...core.instance import Instance
from ...core.tools import ToolError

SITE = "https://acme.atlassian.net"
V1 = "2026-09-25.1"
V2 = "2026-09-25.2"
V3 = "2026-09-25.3"
SEARCH_LAG = {"issues": 10}  # seconds until JQL search reflects a change
STATUSES = {"To Do": "new", "In Progress": "indeterminate", "In Review": "indeterminate", "Done": "done"}
# the workflow: from-status -> [(transition id, name, to-status)]
WORKFLOW = {
    "To Do": [("11", "Start Progress", "In Progress"), ("41", "Done", "Done")],
    "In Progress": [("21", "Submit for Review", "In Review"), ("31", "Stop Progress", "To Do"), ("41", "Done", "Done")],
    "In Review": [("41", "Done", "Done"), ("51", "Request Changes", "In Progress")],
    "Done": [("61", "Reopen", "To Do")],
}
TYPES = ["Task", "Bug", "Story", "Epic", "Subtask"]
PRIORITIES = ["Highest", "High", "Medium", "Low", "Lowest"]
LINK_TYPES = {"Blocks": ("blocks", "is blocked by"), "Relates": ("relates to", "relates to"),
              "Duplicate": ("duplicates", "is duplicated by"), "Cloners": ("clones", "is cloned by")}


def _err(message: str, http_status: int = 400, /, **errors: str) -> ToolError:
    return ToolError({"errorMessages": [message] if message else [], "errors": errors}, status=http_status)


def _missing(key: str) -> ToolError:
    return _err("Issue does not exist or you do not have permission to see it.", 404)

def _iso(ctx: Instance) -> str:
    return ctx.now().strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def _new_issue(ctx: Instance, state: dict[str, Any], project: str, summary: str, type_: str, description: str | None,
               assignee: str | None, priority: str, labels: list[str] | None, reporter: str,
               parent: str | None = None) -> dict[str, Any]:
    p = state["projects"][project]
    key = f"{project}-{p['next']}"
    p["next"] += 1
    issue = {"id": str(ctx.next("issue_id", 10100)), "key": key, "project": project, "summary": summary,
             "issue_type": type_, "description": description, "status": "To Do", "priority": priority,
             "assignee": assignee, "reporter": reporter, "labels": list(labels or []), "components": [],
             "created": _iso(ctx), "updated": _iso(ctx), "resolution": None, "resolutiondate": None, "duedate": None,
             "comments": [], "worklogs": [], "epic": None, "parent": parent, "watchers": [reporter]}
    if state.get("_v1"):
        issue.update(sprint=None, closed_sprints=[])
    state["issues"][key] = issue
    return issue


def _comment(ctx: Instance, issue: dict[str, Any], author: str, body: str) -> dict[str, Any]:
    c = {"id": str(ctx.next("comment_id", 10500)), "author": author, "body": body, "created": _iso(ctx),
         "updated": _iso(ctx)}
    issue["comments"].append(c)
    issue["updated"] = _iso(ctx)
    return c


def _user_json(state: dict[str, Any], uid: str | None) -> dict[str, Any] | None:
    if not uid:
        return None
    u = state["users"].get(uid, {"account_id": uid, "display_name": uid, "email": None})
    return {"account_id": u["account_id"], "display_name": u["display_name"], "email": u.get("email"),
            "name": u["display_name"]}


def _get(state: dict[str, Any], key: str) -> dict[str, Any]:
    i = state["issues"].get((key or "").upper())
    if i is None:
        raise _missing(key)
    return i


def _resolve_user(state: dict[str, Any], who: str | None) -> str | None:
    if not who:
        return None
    w = who.strip().lower()
    for u in state["users"].values():
        if w in (u["account_id"].lower(), (u.get("email") or "").lower(), u["display_name"].lower()):
            return u["account_id"]
    raise _err("", 400, assignee=f"User '{who}' does not exist or cannot be assigned.")


def _fields(raw: Any) -> dict[str, Any]:
    if raw in (None, ""):
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw)
    except ValueError:
        raise _err("fields/additional_fields must be a JSON object string") from None
    if not isinstance(v, dict):
        raise _err("fields/additional_fields must be a JSON object string")
    return v


def _issue_json(state: dict[str, Any], i: dict[str, Any], comment_limit: int = 10) -> dict[str, Any]:
    links = [{"type": l["type"], "direction": "outward" if l["outward"] == i["key"] else "inward",
              "issue": l["inward"] if l["outward"] == i["key"] else l["outward"]}
             for l in state["links"] if i["key"] in (l["inward"], l["outward"])]
    return {
        "id": i["id"], "key": i["key"], "summary": i["summary"], "description": i["description"],
        "status": {"name": i["status"], "category": STATUSES[i["status"]]},
        "issue_type": {"name": i["issue_type"]}, "priority": {"name": i["priority"]},
        "assignee": _user_json(state, i["assignee"]), "reporter": _user_json(state, i["reporter"]),
        "labels": i["labels"], "components": i["components"], "created": i["created"], "updated": i["updated"],
        "resolution": {"name": i["resolution"]} if i["resolution"] else None, "duedate": i["duedate"],
        "project": {"key": i["project"], "name": state["projects"][i["project"]]["name"]},
        "epic_key": i["epic"], "parent": i["parent"], "issuelinks": links,
        "comments": [{"id": c["id"], "author": _user_json(state, c["author"]), "body": c["body"], "created": c["created"]}
                     for c in i["comments"][-comment_limit:]] if comment_limit else [],
        "timetracking": {"timeSpentSeconds": sum(w["seconds"] for w in i["worklogs"])},
        "url": f"{SITE}/browse/{i['key']}",
        **({"sprint": _sprint_brief(state, i["sprint"])} if state.get("_v1") else {}),
    }


def _apply_fields(state: dict[str, Any], i: dict[str, Any], f: dict[str, Any]) -> None:
    for k, v in f.items():
        if k == "summary":
            if not str(v).strip():
                raise _err("", 400, summary="You must specify a summary of the issue.")
            i["summary"] = v
        elif k == "description":
            i["description"] = v
        elif k == "assignee":
            i["assignee"] = _resolve_user(state, v.get("accountId") if isinstance(v, dict) else v)
        elif k == "priority":
            name = v.get("name") if isinstance(v, dict) else v
            if name not in PRIORITIES:
                raise _err("", 400, priority=f"Priority name '{name}' is not valid")
            i["priority"] = name
        elif k == "labels":
            if any(" " in l for l in v):
                raise _err("", 400, labels="The label can't contain spaces.")
            i["labels"] = list(v)
        elif k == "duedate":
            i["duedate"] = v
        elif k == "components":
            i["components"] = [c.get("name") if isinstance(c, dict) else c for c in v]
        elif k in ("status",):
            raise _err("", 400, status="Field 'status' cannot be set. Use a transition instead.")
        elif k in ("sprint", "customfield_10020") and state.get("_v1"):
            _set_sprint(state, i, v.get("id") if isinstance(v, dict) else v)
        elif k in ("parent", "epicKey", "epic_link"):
            key = v.get("key") if isinstance(v, dict) else v
            _get(state, key)
            i["epic"] = key
        else:
            raise _err("", 400, **{k: f"Field '{k}' cannot be set. It is not on the appropriate screen, or unknown."})


# -- boards & sprints (2026-09-25.1) ------------------------------------------------------

def _date_iso(v: Any) -> str | None:
    if v in (None, ""):
        return None
    try:
        t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        raise _err(f"Invalid date '{v}'. Use ISO 8601, e.g. 2026-10-12T09:00:00.000Z.", 400) from None
    t = t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _seed_board(ctx: Instance, state: dict[str, Any], b: dict[str, Any]) -> None:
    if b.get("project") not in state["projects"]:
        raise ValueError(f"board {b.get('name')!r} needs an existing project")
    bid = str(ctx.next("board_id"))
    state["boards"][bid] = {"id": bid, "name": b["name"], "type": b.get("type", "scrum"), "project": b["project"]}
    for sp in b.get("sprints", []):
        sprint = _new_sprint(ctx, state, bid, sp["name"], sp.get("start"), sp.get("end"), sp.get("goal"))
        sprint["state"] = sp.get("state", "future")
        if sprint["state"] != "future":
            sprint["activated_date"] = sprint["start_date"]
        if sprint["state"] == "closed":
            sprint["complete_date"] = sprint["end_date"]
        for key in sp.get("issues", []):
            i = state["issues"].get(key)
            if i is None:
                raise ValueError(f"sprint {sp['name']!r}: no issue {key}")
            if sprint["state"] == "closed":
                i["closed_sprints"].append(sprint["id"])
            else:
                i["sprint"] = sprint["id"]


def _new_sprint(ctx: Instance, state: dict[str, Any], board: str, name: str, start: Any, end: Any,
                goal: str | None) -> dict[str, Any]:
    sid = str(ctx.next("sprint_id", 40))
    sprint = {"id": sid, "name": name, "state": "future", "start_date": _date_iso(start), "end_date": _date_iso(end),
              "activated_date": None, "complete_date": None, "origin_board_id": board, "goal": goal or ""}
    state["sprints"][sid] = sprint
    return sprint


def _board(state: dict[str, Any], board_id: Any) -> dict[str, Any]:
    b = state["boards"].get(str(board_id))
    if b is None:
        raise _err(f"Board does not exist or you do not have permission to see it (board {board_id}).", 404)
    return b


def _sprint(state: dict[str, Any], sprint_id: Any) -> dict[str, Any]:
    sp = state["sprints"].get(str(sprint_id))
    if sp is None:
        raise _err(f"Sprint with id {sprint_id} does not exist or you do not have permission to see it.", 404)
    return sp


def _sprint_json(sp: dict[str, Any]) -> dict[str, Any]:
    return {k: sp[k] for k in ("id", "state", "name", "start_date", "end_date", "activated_date", "complete_date",
                               "origin_board_id", "goal") if sp[k] is not None}


def _sprint_brief(state: dict[str, Any], sid: str | None) -> dict[str, Any] | None:
    sp = state["sprints"].get(sid) if sid else None
    return {"id": sp["id"], "name": sp["name"], "state": sp["state"]} if sp else None


def _set_sprint(state: dict[str, Any], i: dict[str, Any], sprint_id: Any) -> None:
    if sprint_id in (None, "", 0):
        i["sprint"] = None  # back to the backlog
        return
    sp = _sprint(state, sprint_id)
    if sp["state"] == "closed":
        raise _err("", 400, sprint="Issue can be assigned only to an active or future sprint.")
    if state["boards"][sp["origin_board_id"]]["project"] != i["project"]:
        raise _err("", 400, sprint=f"Issue {i['key']} is not on the board of sprint {sp['name']}.")
    i["sprint"] = sp["id"]


def _paged(items: list[Any], start_at: Any, limit: Any, cap: int = 50) -> list[Any]:
    start, lim = max(0, int(start_at or 0)), max(1, min(int(limit or 10), cap))
    return items[start:start + lim]
