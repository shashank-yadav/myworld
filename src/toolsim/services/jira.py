"""Jira.

Tools and parameters follow the most widely used Jira MCP server (sooperset/mcp-atlassian),
which returns simplified issue JSON. Issues move through a real workflow (only listed
transitions are allowed), and ``jira_search`` understands a practical subset of JQL.

Seed format::

    user: {account_id: alex, display_name: Alex Rivera, email: alex@acme.com}
    users: [{account_id: john, display_name: John Park, email: john@acme.com}]
    projects:
      - key: OPS
        name: Operations
        issues:
          - {summary: Rotate prod DB credentials, type: Task, status: In Progress, assignee: john,
             priority: High, labels: [security], comments: [{author: alex, body: Due Friday}]}
    boards:                                     # agile boards and sprints (2026-09-25.1)
      - name: OPS board
        project: OPS
        type: scrum                             # or kanban (no sprints)
        sprints:
          - {name: OPS Sprint 7, state: active, start: "2026-09-14", end: "2026-09-28",
             goal: Pass the audit, issues: [OPS-2]}

From 2026-09-25.1: boards and sprints tools, ``sprint`` in JQL (``sprint in openSprints()``,
``closedSprints()``, ``futureSprints()``, a sprint id or name), and issues join sprints with
``jira_update_issue`` fields ``{"sprint": <id>}`` (or customfield_10020). Like Jira, one sprint is
active per board, only active sprints can be completed, and completing one sends unfinished
issues back to the backlog (or to ``move_incomplete_to``).
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Annotated, Any

from ..core.instance import Instance, Service
from ..core.tools import ToolError, action, tool

SITE = "https://acme.atlassian.net"
V1 = "2026-09-25.1"
V2 = "2026-09-25.2"
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


class Jira(Service):
    name = "jira"
    title = "Jira"
    description = "Simulated Jira Cloud site. Behaves like the mcp-atlassian Jira tools; nothing is really changed."

    versions = {"2026-09-25": "Initial release: 16 tools modeled on sooperset/mcp-atlassian (Jira).",
                V1: "Agile boards and sprints (6 tools), sprint field and sprint functions in JQL.",
                V2: "JQL search is eventually consistent: new and edited issues reach search about 10 seconds "
                    "later (jira_get_issue is immediate)."}

    def probe(self, ctx: Instance) -> None:
        c = ctx.call
        c("jira_get_all_projects", {})
        c("jira_search", {"jql": 'project = OPS AND statusCategory != Done ORDER BY priority DESC'})
        c("jira_search", {"jql": "assignee = currentUser() OR labels in (billing)"})
        c("jira_search", {"jql": "bogus = 1"})
        c("jira_get_issue", {"issue_key": "OPS-2", "expand": "transitions"})
        c("jira_get_transitions", {"issue_key": "OPS-4"})
        c("jira_transition_issue", {"issue_key": "OPS-4", "transition_id": "21"})
        c("jira_transition_issue", {"issue_key": "OPS-4", "transition_id": "11", "comment": "Looking now"})
        c("jira_update_issue", {"issue_key": "OPS-4", "fields": '{"status": "Done"}'})
        c("jira_create_issue", {"project_key": "SUP", "summary": "Refund duplicate charge", "issue_type": "Task",
                                "additional_fields": '{"priority": {"name": "High"}, "labels": ["billing"]}'})
        c("jira_create_issue_link", {"link_type": "Relates", "inward_issue_key": "SUP-3", "outward_issue_key": "SUP-1"})
        c("jira_add_worklog", {"issue_key": "OPS-2", "time_spent": "1h 30m"})
        c("jira_link_to_epic", {"issue_key": "OPS-4", "epic_key": "OPS-1"})
        c("jira_add_comment", {"issue_key": "SUP-1", "body": "Refund issued in SUP-3"})
        c("jira_get_user_profile", {"user_identifier": "john@acme.com"})
        if ctx.at_least(V1):
            board = c("jira_get_agile_boards", {"project_key": "OPS"}).data[0]["id"]
            c("jira_get_agile_boards", {"board_type": "kanban"})
            sprints = c("jira_get_sprints_from_board", {"board_id": board, "state": "active,future"}).data
            c("jira_get_sprint_issues", {"sprint_id": sprints[0]["id"]})
            c("jira_search", {"jql": "sprint in openSprints() AND assignee = currentUser()"})
            c("jira_search", {"jql": "project = OPS AND sprint is EMPTY"})
            c("jira_update_issue", {"issue_key": "OPS-3", "fields": json.dumps({"sprint": int(sprints[0]["id"])})})
            c("jira_update_sprint", {"sprint_id": sprints[1]["id"], "state": "active"})
            c("jira_update_sprint", {"sprint_id": sprints[0]["id"], "state": "closed"})
            c("jira_get_board_issues", {"board_id": board, "jql": "sprint is EMPTY"})
            c("jira_create_sprint", {"board_id": board, "sprint_name": "OPS Sprint 9", "start_date": "2026-10-12",
                                     "end_date": "2026-10-26", "goal": "Cost review"})
            c("jira_get_sprints_from_board", {"board_id": "2"})
        if ctx.at_least(V2):
            c("jira_create_issue", {"project_key": "OPS", "summary": "Pager storm from disk alerts", "issue_type": "Bug"})
            c("jira_search", {"jql": 'project = OPS AND summary ~ "pager storm"'})
            c("jira_transition_issue", {"issue_key": "OPS-3", "transition_id": "11"})
            c("jira_search", {"jql": 'key = OPS-3 AND status = "In Progress"'})
            ctx.advance(15)
            c("jira_search", {"jql": 'project = OPS AND summary ~ "pager storm"'})
            c("jira_search", {"jql": 'key = OPS-3 AND status = "In Progress"'})

    def default_seed(self) -> dict[str, Any]:
        return {
            "user": {"account_id": "alex", "display_name": "Alex Rivera", "email": "alex@acme.com"},
            "users": [{"account_id": "john", "display_name": "John Park", "email": "john@acme.com"},
                      {"account_id": "priya", "display_name": "Priya Shah", "email": "priya@acme.com"}],
            "projects": [
                {"key": "OPS", "name": "Operations", "lead": "alex", "issues": [
                    {"summary": "Q4 infrastructure cost review", "type": "Epic", "status": "In Progress", "assignee": "alex"},
                    {"summary": "Rotate production database credentials", "type": "Task", "status": "In Progress",
                     "assignee": "john", "priority": "High", "labels": ["security"], "epic": 1,
                     "comments": [{"author": "alex", "body": "Needs to land before the audit on Friday."}]},
                    {"summary": "Right-size staging cluster", "type": "Task", "status": "To Do", "priority": "Medium",
                     "epic": 1},
                    {"summary": "Nightly backup job failing on replica-2", "type": "Bug", "status": "To Do",
                     "priority": "Highest", "labels": ["backups"], "assignee": "priya"},
                    {"summary": "Upgrade Postgres to 16", "type": "Story", "status": "Done", "assignee": "priya"},
                ]},
                {"key": "SUP", "name": "Support", "lead": "john", "issues": [
                    {"summary": "Customer charged twice for September invoice", "type": "Bug", "status": "To Do",
                     "priority": "High", "labels": ["billing"], "reporter": "john"},
                    {"summary": "Export to CSV times out for large workspaces", "type": "Bug", "status": "In Review",
                     "assignee": "priya"},
                ]},
            ],
            "boards": [
                {"name": "OPS board", "project": "OPS", "type": "scrum", "sprints": [
                    {"name": "OPS Sprint 6", "state": "closed", "start": "2026-08-31", "end": "2026-09-14",
                     "issues": ["OPS-5"]},
                    {"name": "OPS Sprint 7", "state": "active", "start": "2026-09-14", "end": "2026-09-28",
                     "goal": "Pass the security audit", "issues": ["OPS-2", "OPS-4"]},
                    {"name": "OPS Sprint 8", "state": "future"},
                ]},
                {"name": "SUP board", "project": "SUP", "type": "kanban"},
            ],
        }

    def initial_state(self, seed: dict[str, Any], ctx: Instance) -> dict[str, Any]:
        me = seed.get("user") or {"account_id": "alex", "display_name": "Alex Rivera", "email": "alex@acme.com"}
        state: dict[str, Any] = {"me": me["account_id"], "users": {}, "projects": {}, "issues": {}, "links": []}
        if ctx.at_least(V1):
            state.update(_v1=True, boards={}, sprints={})
        if ctx.at_least(V2):
            state["_search_lag"] = {**SEARCH_LAG, **(seed.get("search_lag") or {})}
        for u in [me, *seed.get("users", [])]:
            state["users"][u["account_id"]] = {"account_id": u["account_id"], "display_name": u["display_name"],
                                               "email": u.get("email"), "active": u.get("active", True)}
        for p in seed.get("projects", []):
            state["projects"][p["key"]] = {"key": p["key"], "name": p["name"], "id": str(ctx.next("project_id", 10000)),
                                           "lead": p.get("lead", me["account_id"]), "next": 1, "archived": p.get("archived", False)}
            keys: list[str] = []
            issues = p.get("issues", [])
            if any(i.get("key") for i in issues):  # imported data: keep the real keys (OPS-123)
                issues = sorted(issues, key=lambda i: int(str(i.get("key", "X-0")).rsplit("-", 1)[-1]))
            for i in issues:
                if i.get("key"):
                    n = int(str(i["key"]).rsplit("-", 1)[-1])
                    state["projects"][p["key"]]["next"] = max(state["projects"][p["key"]]["next"], n)
                issue = _new_issue(ctx, state, p["key"], i["summary"], i.get("type", "Task"), i.get("description"),
                                   i.get("assignee"), i.get("priority", "Medium"), i.get("labels"),
                                   reporter=i.get("reporter", me["account_id"]))
                issue["status"] = i.get("status", "To Do")
                if issue["status"] == "Done":
                    issue["resolution"], issue["resolutiondate"] = "Done", issue["updated"]
                if isinstance(i.get("epic"), int):
                    issue["epic"] = keys[i["epic"] - 1]
                elif i.get("epic"):
                    issue["epic"] = i["epic"]  # a key, resolved after all issues exist
                if i.get("created"):
                    issue["created"] = issue["updated"] = i["created"]
                for c in i.get("comments", []):
                    _comment(ctx, issue, c.get("author", me["account_id"]), c["body"])
                keys.append(issue["key"])
        if ctx.at_least(V1):
            for b in seed.get("boards", []):
                _seed_board(ctx, state, b)
        return state

    actor_key = "me"

    def resolve_actor(self, state: dict[str, Any], identity: str) -> str:
        try:
            return _resolve_user(state, identity)  # account id, email or display name
        except ToolError:
            raise ValueError(f"no Jira user {identity} on this site") from None

    def error_shape(self, status: int, message: str) -> Any:
        return {"errorMessages": [message], "errors": {}}

    def fault_error(self, fault: Any) -> tuple[Any, int]:
        if fault.kind == "rate_limit":
            return {"errorMessages": ["Rate limit exceeded."], "errors": {}, "retryAfter": fault.retry_after}, 429
        if fault.kind == "server_error":
            return {"errorMessages": ["Internal server error"], "errors": {}}, 500
        return super().fault_error(fault)

    def render(self, value: Any) -> str:
        if isinstance(value, dict) and "errorMessages" in value and len(value) <= 3:
            msgs = value["errorMessages"] + [f"{k}: {v}" for k, v in value.get("errors", {}).items()]
            return "Error: " + "; ".join(msgs)
        return super().render(value)


# -- model --------------------------------------------------------------------------------

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


# -- JQL ----------------------------------------------------------------------------------

_TOKEN = re.compile(r'\s*(?:(\()|(\))|(,)|(!=|>=|<=|!~|=|~|>|<)|"([^"]*)"|\'([^\']*)\'|([A-Za-z0-9_.@\-+/:]+(?:\(\))?))')
_KEYWORDS = {"and", "or", "not", "in", "is", "order", "by", "asc", "desc", "empty", "null"}


def _tokenize(jql: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    jql = jql.strip()
    while pos < len(jql):
        m = _TOKEN.match(jql, pos)
        if not m or m.end() == pos:
            raise _err(f"Error in the JQL Query: The character '{jql[pos]}' is a reserved JQL character.")
        pos = m.end()
        lp, rp, comma, op, dq, sq, word = m.groups()
        if lp:
            out.append(("(", "("))
        elif rp:
            out.append((")", ")"))
        elif comma:
            out.append((",", ","))
        elif op:
            out.append(("op", op))
        elif dq is not None or sq is not None:
            out.append(("str", dq if dq is not None else sq))
        else:
            out.append(("kw" if word.lower() in _KEYWORDS else "word", word))
    return out


class _JQL:
    def __init__(self, state: dict[str, Any], jql: str, now: dt.datetime):
        self.state, self.now = state, now
        self.toks = _tokenize(jql)
        self.i = 0
        self.order: list[tuple[str, bool]] = []
        self.tree = self._or() if self._peek() and not self._is_kw("order") else None
        if self._is_kw("order"):
            self.i += 1
            self._expect_kw("by")
            while self._peek():
                field = self._next()[1].lower()
                desc = False
                if self._is_kw("asc") or self._is_kw("desc"):
                    desc = self._next()[1].lower() == "desc"
                self.order.append((field, desc))
                if self._peek() and self._peek()[0] == ",":
                    self.i += 1
        if self._peek():
            raise _err(f"Error in the JQL Query: Expecting end of query but got '{self._peek()[1]}'.")

    def _peek(self) -> tuple[str, str] | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _next(self) -> tuple[str, str]:
        t = self._peek()
        if t is None:
            raise _err("Error in the JQL Query: The JQL query is incomplete.")
        self.i += 1
        return t

    def _is_kw(self, w: str) -> bool:
        t = self._peek()
        return bool(t and t[0] == "kw" and t[1].lower() == w)

    def _expect_kw(self, w: str) -> None:
        if not self._is_kw(w):
            raise _err(f"Error in the JQL Query: Expecting '{w}'.")
        self.i += 1

    def _or(self) -> Any:
        node = self._and()
        while self._is_kw("or"):
            self.i += 1
            node = ("or", node, self._and())
        return node

    def _and(self) -> Any:
        node = self._not()
        while self._is_kw("and"):
            self.i += 1
            node = ("and", node, self._not())
        return node

    def _not(self) -> Any:
        if self._is_kw("not"):
            self.i += 1
            return ("not", self._not())
        if self._peek() and self._peek()[0] == "(":
            self.i += 1
            node = self._or()
            if not self._peek() or self._next()[0] != ")":
                raise _err("Error in the JQL Query: Expecting ')'.")
            return node
        return self._clause()

    def _value(self) -> Any:
        t = self._next()
        if t[0] == "kw" and t[1].lower() in ("empty", "null"):
            return None
        return t[1]

    def _clause(self) -> Any:
        field = self._next()
        if field[0] not in ("word", "str"):
            raise _err(f"Error in the JQL Query: Expecting a field name but got '{field[1]}'.")
        name = field[1].lower()
        if name not in _FIELDS or (name == "sprint" and not self.state.get("_v1")):
            raise _err(f"Error in the JQL Query: Field '{field[1]}' does not exist or you do not have permission to view it.")
        if self._is_kw("not"):
            self.i += 1
            self._expect_kw("in")
            return ("not in", name, self._func() or self._list())
        if self._is_kw("in"):
            self.i += 1
            return ("in", name, self._func() or self._list())
        if self._is_kw("is"):
            self.i += 1
            neg = self._is_kw("not")
            if neg:
                self.i += 1
            self._value()
            return ("is not empty" if neg else "is empty", name, None)
        op = self._next()
        if op[0] != "op":
            raise _err(f"Error in the JQL Query: Expecting operator but got '{op[1]}'.")
        return (op[1], name, self._value())

    def _func(self) -> list[Any] | None:
        """``in openSprints()`` and friends: a function instead of a (list)."""
        t = self._peek()
        if t and t[0] == "word" and t[1].endswith("()"):
            self.i += 1
            return [t[1]]
        return None

    def _list(self) -> list[Any]:
        if self._next()[0] != "(":
            raise _err("Error in the JQL Query: Expecting '('.")
        vals = []
        while True:
            vals.append(self._value())
            t = self._next()
            if t[0] == ")":
                return vals
            if t[0] != ",":
                raise _err("Error in the JQL Query: Expecting ',' or ')'.")

    # evaluation
    def matches(self, i: dict[str, Any]) -> bool:
        return self.tree is None or self._eval(self.tree, i)

    def _eval(self, node: Any, i: dict[str, Any]) -> bool:
        op = node[0]
        if op == "and":
            return self._eval(node[1], i) and self._eval(node[2], i)
        if op == "or":
            return self._eval(node[1], i) or self._eval(node[2], i)
        if op == "not":
            return not self._eval(node[1], i)
        _, field, value = node
        have = self._field(i, field)
        if op == "is empty":
            return have in (None, [], "")
        if op == "is not empty":
            return have not in (None, [], "")
        vals = [self._norm_value(field, v) for v in (value if isinstance(value, list) else [value])]
        vals = [x for v in vals for x in (v if isinstance(v, tuple) else [v])]  # functions expand to many
        if op in ("in", "="):
            return any(self._eq(have, v) for v in vals)
        if op in ("not in", "!="):
            return not any(self._eq(have, v) for v in vals)
        if op in ("~", "!~"):
            text = " ".join(have) if isinstance(have, list) else str(have or "")
            hit = all(w in text.lower() for w in str(value).lower().replace("*", "").split())
            return hit if op == "~" else not hit
        if field in ("created", "updated", "duedate", "resolved", "resolutiondate"):
            if have is None:
                return False
            a = _to_dt(have)
            b = self._date(value)
            return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[op]
        if field == "priority":
            order = {p.lower(): n for n, p in enumerate(reversed(PRIORITIES))}
            a, b = order.get(str(have).lower(), -1), order.get(str(value).lower(), -1)
            return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[op]
        raise _err(f"Error in the JQL Query: The operator '{op}' is not supported by the '{field}' field.")

    def _field(self, i: dict[str, Any], field: str) -> Any:
        return {
            "project": i["project"], "key": i["key"], "issuekey": i["key"], "id": i["id"],
            "summary": i["summary"], "description": i["description"],
            "text": f"{i['summary']} {i['description'] or ''} {' '.join(c['body'] for c in i['comments'])}",
            "status": i["status"], "statuscategory": {"new": "To Do", "indeterminate": "In Progress", "done": "Done"}[STATUSES[i["status"]]],
            "type": i["issue_type"], "issuetype": i["issue_type"], "priority": i["priority"],
            "assignee": i["assignee"], "reporter": i["reporter"], "labels": i["labels"], "label": i["labels"],
            "component": i["components"], "resolution": i["resolution"], "created": i["created"], "updated": i["updated"],
            "duedate": i["duedate"], "resolved": i["resolutiondate"], "resolutiondate": i["resolutiondate"],
            "parent": i["parent"] or i["epic"], "\"epic link\"": i["epic"], "epic link": i["epic"],
            "watcher": i["watchers"],
            "sprint": [*i.get("closed_sprints", []), *([i["sprint"]] if i.get("sprint") else [])],
        }.get(field)

    def _norm_value(self, field: str, v: Any) -> Any:
        if isinstance(v, str) and v.lower() == "currentuser()":
            return self.state["me"]
        if field == "sprint" and v is not None:
            sprints = self.state["sprints"].values()
            fn = {"opensprints()": ("active", "future"), "closedsprints()": ("closed",),
                  "futuresprints()": ("future",)}.get(str(v).lower())
            if fn:
                return tuple(sp["id"] for sp in sprints if sp["state"] in fn)
            if str(v).lower().endswith("()"):
                raise _err(f"Error in the JQL Query: Unable to find JQL function '{v}'.")
            hit = next((sp["id"] for sp in sprints if sp["id"] == str(v) or sp["name"].lower() == str(v).lower()), None)
            if hit is None:
                raise _err(f"Error in the JQL Query: The value '{v}' does not exist for the field 'sprint'.")
            return hit
        if field in ("assignee", "reporter", "watcher") and isinstance(v, str):
            try:
                return _resolve_user(self.state, v)
            except ToolError:
                return v
        if field == "resolution" and isinstance(v, str) and v.lower() == "unresolved":
            return None
        return v

    @staticmethod
    def _eq(have: Any, v: Any) -> bool:
        if isinstance(have, list):
            return any(str(h).lower() == str(v).lower() for h in have)
        if have is None or v is None:
            return have is v
        return str(have).lower() == str(v).lower()

    def _date(self, v: Any) -> dt.datetime:
        s = str(v).strip().lower()
        if s in ("now()", "now"):
            return self.now
        if s.startswith("startofday"):
            return self.now.replace(hour=0, minute=0, second=0, microsecond=0)
        m = re.fullmatch(r"([-+]?\d+)([wdhm])", s)
        if m:
            n, unit = int(m.group(1)), m.group(2)
            return self.now + dt.timedelta(**{{"w": "weeks", "d": "days", "h": "hours", "m": "minutes"}[unit]: n})
        try:
            t = dt.datetime.fromisoformat(s.replace("/", "-"))
        except ValueError:
            raise _err(f"Error in the JQL Query: Date value '{v}' for field is invalid.") from None
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)

    def sort(self, issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for field, desc in reversed(self.order or [("created", True)]):
            def key(i: dict[str, Any], f: str = field) -> Any:
                if f == "priority":
                    return PRIORITIES[::-1].index(i["priority"])
                if f in ("key", "issuekey"):
                    return (i["project"], int(i["key"].split("-")[1]))
                v = self._field(i, f)
                return (v is None, str(v or ""))
            issues.sort(key=key, reverse=desc)
        return issues


_FIELDS = {"project", "key", "issuekey", "id", "summary", "description", "text", "status", "statuscategory", "type",
           "issuetype", "priority", "assignee", "reporter", "labels", "label", "component", "resolution", "created",
           "updated", "duedate", "resolved", "resolutiondate", "parent", "\"epic link\"", "epic link", "watcher", "sprint"}


def _to_dt(s: str) -> dt.datetime:
    return dt.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc) if "T" in s \
        else dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc)


# -- tools --------------------------------------------------------------------------------

@tool("jira_search", read_only=True)
def jira_search(ctx: Instance,
                jql: Annotated[str, "JQL query string (Jira Query Language). Examples: 'project = OPS AND status = \"In Progress\" ORDER BY priority DESC', 'assignee = currentUser() AND updated >= -7d'"],
                fields: Annotated[str | None, "Comma-separated fields to return, or '*all'"] = None,
                limit: Annotated[int | None, "Maximum number of results (1-50)"] = 10,
                start_at: Annotated[int | None, "Starting index for pagination (0-based)"] = 0,
                projects_filter: Annotated[str | None, "Comma-separated list of project keys to filter results"] = None,
                expand: Annotated[str | None, "Fields to expand: 'renderedFields', 'transitions', 'changelog'"] = None,
                page_token: Annotated[str | None, "Pagination token from a previous search result (Cloud only)"] = None,
                use_display_names: Annotated[bool | None, "Use human-readable names for custom fields"] = False) -> dict[str, Any]:
    """Search Jira issues using JQL (Jira Query Language)"""
    s = ctx.search_view("issues")
    q = _JQL(s, jql, ctx.now())
    projects = {p.strip().upper() for p in (projects_filter or "").split(",") if p.strip()}
    hits = [i for i in s["issues"].values() if q.matches(i) and (not projects or i["project"] in projects)]
    hits = q.sort(hits)
    lim = max(1, min(limit or 10, 50))
    start = int(page_token) if page_token else (start_at or 0)
    page = hits[start:start + lim]
    out = {"total": len(hits), "start_at": start, "max_results": lim,
           "issues": [_issue_json(s, i, comment_limit=0) for i in page]}
    if start + lim < len(hits):
        out["next_page_token"] = str(start + lim)
    return out


@tool("jira_get_issue", read_only=True)
def jira_get_issue(ctx: Instance,
                   issue_key: Annotated[str, "Jira issue key (e.g., 'PROJ-123')"],
                   fields: Annotated[str | None, "Comma-separated list of fields to return, or '*all'"] = None,
                   expand: Annotated[str | None, "Fields to expand, e.g. 'renderedFields', 'transitions', 'changelog'"] = None,
                   comment_limit: Annotated[int | None, "Maximum number of comments to include (0 for none)"] = 10,
                   properties: Annotated[str | None, "Comma-separated issue properties to return"] = None,
                   update_history: Annotated[bool | None, "Whether to update the issue view history"] = True,
                   include: Annotated[str | None, "Extra sections to include"] = None,
                   use_display_names: Annotated[bool | None, "Use human-readable names for custom fields"] = False) -> dict[str, Any]:
    """Get details of a specific Jira issue including its Epic links and relationship information"""
    s = ctx.state
    i = _get(s, issue_key)
    out = _issue_json(s, i, comment_limit=10 if comment_limit is None else comment_limit)
    if expand and "transitions" in expand:
        out["transitions"] = [{"id": t, "name": n, "to_status": to} for t, n, to in WORKFLOW[i["status"]]]
    return out


@tool("jira_create_issue")
def jira_create_issue(ctx: Instance,
                      project_key: Annotated[str, "The JIRA project key (e.g. 'PROJ', 'DEV', 'SUPPORT')"],
                      summary: Annotated[str, "Summary/title of the issue"],
                      issue_type: Annotated[str, "Issue type (e.g. 'Task', 'Bug', 'Story', 'Epic', 'Subtask')"],
                      assignee: Annotated[str | None, "Assignee's user identifier (email, display name, or account ID)"] = None,
                      description: Annotated[str | None, "Issue description in Markdown"] = None,
                      components: Annotated[str | None, "Comma-separated list of component names"] = None,
                      additional_fields: Annotated[str | None, "JSON string of additional fields, e.g. '{\"priority\": {\"name\": \"High\"}, \"labels\": [\"x\"], \"parent\": \"PROJ-1\"}'"] = None) -> dict[str, Any]:
    """Create a new Jira issue with optional Epic link or parent for subtasks"""
    s = ctx.state
    p = s["projects"].get(project_key.upper())
    if p is None:
        raise _err("", 400, project=f"Specify a valid project ID or key (got '{project_key}')")
    if issue_type not in TYPES:
        raise _err("", 400, issuetype="Specify an issue type (valid: " + ", ".join(TYPES) + ")")
    if not summary.strip():
        raise _err("", 400, summary="You must specify a summary of the issue.")
    extra = _fields(additional_fields)
    if issue_type == "Subtask" and not extra.get("parent"):
        raise _err("", 400, parent="Sub-task issues must have a parent.")
    i = _new_issue(ctx, s, p["key"], summary, issue_type, description, _resolve_user(s, assignee), "Medium", [],
                   reporter=s["me"], parent=(extra.get("parent") or {}).get("key") if isinstance(extra.get("parent"), dict)
                   else extra.get("parent"))
    if i["parent"]:
        _get(s, i["parent"])
    if components:
        i["components"] = [c.strip() for c in components.split(",") if c.strip()]
    _apply_fields(s, i, {k: v for k, v in extra.items() if k != "parent"})
    return {"message": "Issue created successfully", "issue": _issue_json(s, i)}


@tool("jira_batch_create_issues")
def jira_batch_create_issues(ctx: Instance,
                             issues: Annotated[str, "JSON array string of issue objects: [{project_key, summary, issue_type, description?, assignee?, components?}]"],
                             validate_only: Annotated[bool | None, "If true, only validates the issues without creating them"] = False) -> dict[str, Any]:
    """Create multiple Jira issues in a batch"""
    try:
        items = json.loads(issues)
        assert isinstance(items, list)
    except (ValueError, AssertionError):
        raise _err("issues must be a JSON array string") from None
    created = []
    for it in items:
        if validate_only:
            if it.get("project_key", "").upper() not in ctx.state["projects"]:
                raise _err("", 400, project=f"Specify a valid project ID or key (got '{it.get('project_key')}')")
            continue
        r = jira_create_issue.fn(ctx, project_key=it.get("project_key", ""), summary=it.get("summary", ""),
                                 issue_type=it.get("issue_type", "Task"), assignee=it.get("assignee"),
                                 description=it.get("description"), components=it.get("components"))
        created.append(r["issue"]["key"])
    return {"message": "Issues validated successfully" if validate_only else f"Created {len(created)} issues",
            "issues": created}


@tool("jira_update_issue", idempotent=True)
def jira_update_issue(ctx: Instance,
                      issue_key: Annotated[str, "Jira issue key (e.g., 'PROJ-123')"],
                      fields: Annotated[str | None, "JSON string of fields to update, e.g. '{\"summary\": \"New title\", \"priority\": {\"name\": \"High\"}}'"] = None,
                      additional_fields: Annotated[str | None, "JSON string of additional fields to update"] = None,
                      components: Annotated[str | None, "Comma-separated list of component names"] = None,
                      attachments: Annotated[str | None, "Comma-separated file paths to attach"] = None,
                      attachments_base64: Annotated[str | None, "JSON array of base64 attachments"] = None,
                      transition: Annotated[str | None, "Transition name or ID to apply as part of the update"] = None,
                      comment: Annotated[str | None, "Comment to add as part of the update"] = None,
                      comment_visibility: Annotated[str | None, "JSON visibility restriction for the comment"] = None,
                      worklog: Annotated[str | None, "Time to log, e.g. '1h 30m'"] = None,
                      worklog_started: Annotated[str | None, "ISO start time for the worklog"] = None,
                      return_fields: Annotated[str | None, "Fields to return"] = "*all") -> dict[str, Any]:
    """Update an existing Jira issue including changing status, adding Epic links, updating fields, etc."""
    s = ctx.state
    i = _get(s, issue_key)
    _apply_fields(s, i, {**_fields(fields), **_fields(additional_fields)})
    if components is not None:
        i["components"] = [c.strip() for c in components.split(",") if c.strip()]
    if attachments:
        i.setdefault("attachments", []).extend(p.strip().rsplit("/", 1)[-1] for p in attachments.split(","))
    if transition:
        _transition(s, i, transition, ctx)
    if comment:
        _comment(ctx, i, s["me"], comment)
    if worklog:
        _worklog(ctx, i, worklog, None, worklog_started)
    i["updated"] = _iso(ctx)
    return {"message": "Issue updated successfully", "issue": _issue_json(s, i)}


@tool("jira_delete_issue", destructive=True)
def jira_delete_issue(ctx: Instance, issue_key: Annotated[str, "Jira issue key (e.g. PROJ-123)"]) -> dict[str, Any]:
    """Delete an existing Jira issue"""
    s = ctx.state
    i = _get(s, issue_key)
    if any(x.get("parent") == i["key"] for x in s["issues"].values()):
        raise _err("The issue has subtasks. Delete the subtasks first, or delete them together.", 400)
    del s["issues"][i["key"]]
    s["links"] = [l for l in s["links"] if i["key"] not in (l["inward"], l["outward"])]
    for x in s["issues"].values():
        if x["epic"] == i["key"]:
            x["epic"] = None
    return {"message": f"Issue {i['key']} has been deleted successfully."}


@tool("jira_assign_issue", idempotent=True)
def jira_assign_issue(ctx: Instance,
                      issue_key: Annotated[str, "Jira issue key (e.g., 'PROJ-123')"],
                      assignee: Annotated[str | None, "User to assign (email, display name, or account ID). Omit to unassign."] = None) -> dict[str, Any]:
    """Assign or unassign a Jira issue"""
    s = ctx.state
    i = _get(s, issue_key)
    i["assignee"] = _resolve_user(s, assignee)
    i["updated"] = _iso(ctx)
    return {"message": f"Issue {i['key']} assigned to {i['assignee'] or 'nobody'}", "issue": _issue_json(s, i, 0)}


def _transition(s: dict[str, Any], i: dict[str, Any], transition: str, ctx: Instance) -> None:
    options = WORKFLOW[i["status"]]
    t = next((t for t in options if transition in (t[0], t[1]) or transition.lower() in (t[1].lower(), t[2].lower())), None)
    if t is None:
        raise _err(f"Transition '{transition}' is not valid for issue {i['key']} in status '{i['status']}'. "
                   f"Available: " + ", ".join(f"{tid} ({name})" for tid, name, _ in options), 400)
    i["status"] = t[2]
    done = STATUSES[t[2]] == "done"
    i["resolution"] = "Done" if done else None
    i["resolutiondate"] = _iso(ctx) if done else None


@tool("jira_transition_issue")
def jira_transition_issue(ctx: Instance,
                          issue_key: Annotated[str, "Jira issue key (e.g., 'PROJ-123')"],
                          transition_id: Annotated[str, "ID of the transition to perform. Use jira_get_transitions to find valid IDs"],
                          fields: Annotated[str | None, "JSON string of fields to update during the transition"] = None,
                          comment: Annotated[str | None, "Comment to add during the transition"] = None) -> dict[str, Any]:
    """Transition a Jira issue to a new status"""
    s = ctx.state
    i = _get(s, issue_key)
    before = i["status"]
    _transition(s, i, str(transition_id), ctx)
    _apply_fields(s, i, _fields(fields))
    if comment:
        _comment(ctx, i, s["me"], comment)
    i["updated"] = _iso(ctx)
    return {"message": f"Issue {i['key']} transitioned from '{before}' to '{i['status']}'", "issue": _issue_json(s, i, 0)}


@tool("jira_get_transitions", read_only=True)
def jira_get_transitions(ctx: Instance, issue_key: Annotated[str, "Jira issue key (e.g., 'PROJ-123')"]) -> list[dict[str, Any]]:
    """Get available status transitions for a Jira issue"""
    i = _get(ctx.state, issue_key)
    return [{"id": tid, "name": name, "to_status": to} for tid, name, to in WORKFLOW[i["status"]]]


@tool("jira_add_comment")
def jira_add_comment(ctx: Instance,
                     issue_key: Annotated[str, "Jira issue key (e.g., 'PROJ-123')"],
                     body: Annotated[str, "Comment text in Markdown format"],
                     visibility: Annotated[str | None, "JSON string for restricted comments"] = None,
                     public: Annotated[bool | None, "JSM/Service Desk only: true = customer-visible"] = None) -> dict[str, Any]:
    """Add a comment to a Jira issue"""
    s = ctx.state
    i = _get(s, issue_key)
    if not body.strip():
        raise _err("", 400, comment="Comment body can not be empty!")
    c = _comment(ctx, i, s["me"], body)
    return {"success": True, "comment": {"id": c["id"], "body": c["body"], "created": c["created"],
                                         "author": _user_json(s, c["author"])}}


_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*([wdhm])")


def _worklog(ctx: Instance, i: dict[str, Any], time_spent: str, comment: str | None, started: str | None) -> dict[str, Any]:
    parts = _DURATION.findall(time_spent.lower())
    if not parts or _DURATION.sub("", time_spent.lower()).strip():
        raise _err("", 400, timeLogged="Worklog must not be null and must be in a valid format, e.g. '1h 30m'.")
    unit = {"w": 5 * 8 * 3600, "d": 8 * 3600, "h": 3600, "m": 60}
    seconds = int(sum(float(n) * unit[u] for n, u in parts))
    w = {"id": str(ctx.next("worklog_id", 10800)), "author": ctx.state["me"], "time_spent": time_spent, "seconds": seconds,
         "comment": comment, "started": started or _iso(ctx)}
    i["worklogs"].append(w)
    return w


@tool("jira_add_worklog")
def jira_add_worklog(ctx: Instance,
                     issue_key: Annotated[str, "Jira issue key (e.g., 'PROJ-123')"],
                     time_spent: Annotated[str, "Time spent, e.g. '1h 30m', '1d', '30m', '4h'"],
                     comment: Annotated[str | None, "Worklog comment in Markdown"] = None,
                     started: Annotated[str | None, "Start time in ISO format (defaults to now)"] = None,
                     original_estimate: Annotated[str | None, "New original estimate value"] = None,
                     remaining_estimate: Annotated[str | None, "New remaining estimate value"] = None) -> dict[str, Any]:
    """Add a worklog entry to a Jira issue"""
    s = ctx.state
    i = _get(s, issue_key)
    w = _worklog(ctx, i, time_spent, comment, started)
    return {"message": "Worklog added successfully", "worklog": {k: w[k] for k in ("id", "time_spent", "started", "comment")}}


@tool("jira_get_all_projects", read_only=True)
def jira_get_all_projects(ctx: Instance,
                          include_archived: Annotated[bool | None, "Whether to include archived projects"] = False) -> list[dict[str, Any]]:
    """Get all Jira projects accessible to the current user"""
    s = ctx.state
    return [{"id": p["id"], "key": p["key"], "name": p["name"], "lead": _user_json(s, p["lead"]),
             "url": f"{SITE}/browse/{p['key']}", "archived": p["archived"]}
            for p in s["projects"].values() if include_archived or not p["archived"]]


@tool("jira_get_project_issues", read_only=True)
def jira_get_project_issues(ctx: Instance,
                            project_key: Annotated[str, "The project key"],
                            limit: Annotated[int | None, "Maximum number of results (1-50)"] = 10,
                            start_at: Annotated[int | None, "Starting index for pagination (0-based)"] = 0) -> dict[str, Any]:
    """Get all issues for a specific Jira project"""
    if project_key.upper() not in ctx.state["projects"]:
        raise _err(f"The value '{project_key}' does not exist for the field 'project'.", 400)
    return jira_search.fn(ctx, jql=f"project = {project_key.upper()} ORDER BY created DESC", limit=limit, start_at=start_at)


@tool("jira_create_issue_link")
def jira_create_issue_link(ctx: Instance,
                           link_type: Annotated[str, "The type of link, e.g. 'Blocks', 'Relates', 'Duplicate', 'Cloners'"],
                           inward_issue_key: Annotated[str, "The key of the inward issue (e.g., 'PROJ-123')"],
                           outward_issue_key: Annotated[str, "The key of the outward issue (e.g., 'PROJ-456')"],
                           comment: Annotated[str | None, "Optional comment to add to the link"] = None) -> dict[str, Any]:
    """Create a link between two Jira issues"""
    s = ctx.state
    lt = next((k for k in LINK_TYPES if k.lower() == link_type.lower()), None)
    if lt is None:
        raise _err(f"No issue link type with name '{link_type}' found.", 404)
    a, b = _get(s, inward_issue_key), _get(s, outward_issue_key)
    if a["key"] == b["key"]:
        raise _err("You cannot link an issue to itself.", 400)
    if any(l["type"] == lt and l["inward"] == a["key"] and l["outward"] == b["key"] for l in s["links"]):
        return {"success": True, "message": "Link already exists"}
    s["links"].append({"id": str(ctx.next("link_id", 10900)), "type": lt, "inward": a["key"], "outward": b["key"]})
    if comment:
        _comment(ctx, a, s["me"], comment)
    return {"success": True, "message": f"Link created between {a['key']} and {b['key']}"}


@tool("jira_link_to_epic", idempotent=True)
def jira_link_to_epic(ctx: Instance,
                      issue_key: Annotated[str, "The key of the issue to link (e.g., 'PROJ-123')"],
                      epic_key: Annotated[str, "The key of the epic to link to (e.g., 'PROJ-456')"]) -> dict[str, Any]:
    """Link an existing issue to an epic"""
    s = ctx.state
    i, e = _get(s, issue_key), _get(s, epic_key)
    if e["issue_type"] != "Epic":
        raise _err(f"{e['key']} is not an Epic.", 400)
    if i["issue_type"] == "Epic":
        raise _err("An Epic can't be added to another Epic.", 400)
    i["epic"] = e["key"]
    i["updated"] = _iso(ctx)
    return {"message": f"Issue {i['key']} has been linked to epic {e['key']}.", "issue": _issue_json(s, i, 0)}


@tool("jira_get_user_profile", read_only=True)
def jira_get_user_profile(ctx: Instance,
                          user_identifier: Annotated[str, "Identifier for the user (email, display name, or account ID)"]) -> dict[str, Any]:
    """Retrieve profile information for a specific Jira user"""
    s = ctx.state
    try:
        uid = _resolve_user(s, user_identifier)
    except ToolError:
        raise _err(f"User '{user_identifier}' not found.", 404) from None
    u = s["users"][uid]
    return {"success": True, "user": {**_user_json(s, uid), "active": u["active"], "time_zone": "America/Los_Angeles"}}


@tool("jira_get_agile_boards", read_only=True, since=V1)
def jira_get_agile_boards(ctx: Instance,
                          board_name: Annotated[str | None, "(Optional) The name of board, support fuzzy search"] = None,
                          project_key: Annotated[str | None, "(Optional) Jira project key (e.g., 'PROJ-123')"] = None,
                          board_type: Annotated[str | None, "(Optional) The type of jira board (e.g., 'scrum', 'kanban')"] = None,
                          start_at: Annotated[int | None, "Starting index for pagination (0-based)"] = 0,
                          limit: Annotated[int | None, "Maximum number of results (1-50)"] = 10) -> list[dict[str, Any]]:
    """Get jira agile boards by name, project key, or type"""
    boards = [{"id": b["id"], "name": b["name"], "type": b["type"]} for b in ctx.state["boards"].values()
              if (not board_name or board_name.lower() in b["name"].lower())
              and (not project_key or b["project"] == project_key.upper())
              and (not board_type or b["type"] == board_type.lower())]
    return _paged(boards, start_at, limit)


@tool("jira_get_board_issues", read_only=True, since=V1)
def jira_get_board_issues(ctx: Instance,
                          board_id: Annotated[str, "The id of the board (e.g., '1001')"],
                          jql: Annotated[str, "JQL query string (Jira Query Language). Examples: 'assignee = currentUser() AND status = \"In Progress\"'"],
                          fields: Annotated[str | None, "Comma-separated fields to return, or '*all'"] = None,
                          start_at: Annotated[int | None, "Starting index for pagination (0-based)"] = 0,
                          limit: Annotated[int | None, "Maximum number of results (1-50)"] = 10,
                          expand: Annotated[str | None, "Optional fields to expand in the response (e.g., 'changelog')"] = "version") -> dict[str, Any]:
    """Get all issues linked to a specific board filtered by JQL"""
    b = _board(ctx.state, board_id)
    q = f"project = {b['project']}" + (f" AND ({jql})" if jql.strip() and not jql.strip().lower().startswith("order by")
                                       else "") + (f" {jql}" if jql.strip().lower().startswith("order by") else "")
    return jira_search.fn(ctx, jql=q, limit=limit, start_at=start_at)


@tool("jira_get_sprints_from_board", read_only=True, since=V1)
def jira_get_sprints_from_board(ctx: Instance,
                                board_id: Annotated[str, "The id of board (e.g., '1000')"],
                                state: Annotated[str | None, "Sprint state (e.g., 'active', 'future', 'closed')"] = None,
                                start_at: Annotated[int | None, "Starting index for pagination (0-based)"] = 0,
                                limit: Annotated[int | None, "Maximum number of results (1-50)"] = 10) -> list[dict[str, Any]]:
    """Get jira sprints from board by state"""
    b = _board(ctx.state, board_id)
    if b["type"] != "scrum":
        raise _err("The board does not support sprints", 400)
    want = {x.strip().lower() for x in (state or "").split(",") if x.strip()}
    sprints = [_sprint_json(sp) for sp in ctx.state["sprints"].values()
               if sp["origin_board_id"] == b["id"] and (not want or sp["state"] in want)]
    return _paged(sprints, start_at, limit)


@tool("jira_get_sprint_issues", read_only=True, since=V1)
def jira_get_sprint_issues(ctx: Instance,
                           sprint_id: Annotated[str, "The id of sprint (e.g., '10001')"],
                           fields: Annotated[str | None, "Comma-separated fields to return, or '*all'"] = None,
                           start_at: Annotated[int | None, "Starting index for pagination (0-based)"] = 0,
                           limit: Annotated[int | None, "Maximum number of results (1-50)"] = 10) -> dict[str, Any]:
    """Get jira issues from sprint"""
    sp = _sprint(ctx.state, sprint_id)
    return jira_search.fn(ctx, jql=f"sprint = {sp['id']} ORDER BY key ASC", limit=limit, start_at=start_at)


@tool("jira_create_sprint", since=V1)
def jira_create_sprint(ctx: Instance,
                       board_id: Annotated[str, "The id of board (e.g., '1000')"],
                       sprint_name: Annotated[str, "Name of the sprint (e.g., 'Sprint 1')"],
                       start_date: Annotated[str, "Start time for sprint (ISO 8601 format)"],
                       end_date: Annotated[str, "End time for sprint (ISO 8601 format)"],
                       goal: Annotated[str | None, "(Optional) Goal of the sprint"] = None) -> dict[str, Any]:
    """Create Jira sprint for a board"""
    s = ctx.state
    b = _board(s, board_id)
    if b["type"] != "scrum":
        raise _err("The board does not support sprints", 400)
    if not sprint_name.strip():
        raise _err("", 400, name="Sprint name is required.")
    if _date_iso(end_date) <= _date_iso(start_date):
        raise _err("", 400, endDate="The sprint end date must be after the start date.")
    return _sprint_json(_new_sprint(ctx, s, b["id"], sprint_name, start_date, end_date, goal))


@tool("jira_update_sprint", since=V1)
def jira_update_sprint(ctx: Instance,
                       sprint_id: Annotated[str, "The id of sprint (e.g., '10001')"],
                       sprint_name: Annotated[str | None, "(Optional) New name for the sprint"] = None,
                       state: Annotated[str | None, "(Optional) New state for the sprint (future|active|closed)"] = None,
                       start_date: Annotated[str | None, "(Optional) New start date for the sprint"] = None,
                       end_date: Annotated[str | None, "(Optional) New end date for the sprint"] = None,
                       goal: Annotated[str | None, "(Optional) New goal for the sprint"] = None,
                       move_incomplete_to: Annotated[str | None, "(Optional) When closing: id of a future sprint for unfinished issues (default: the backlog)"] = None) -> dict[str, Any]:
    """Update jira sprint"""
    s = ctx.state
    sp = _sprint(s, sprint_id)
    if sp["state"] == "closed":
        raise _err("Cannot update a closed sprint.", 400)
    if sprint_name is not None:
        sp["name"] = sprint_name
    if goal is not None:
        sp["goal"] = goal
    if start_date:
        sp["start_date"] = _date_iso(start_date)
    if end_date:
        sp["end_date"] = _date_iso(end_date)
    new = (state or sp["state"]).lower()
    if new not in ("future", "active", "closed"):
        raise _err("", 400, state=f"Invalid sprint state '{state}'. Use future, active or closed.")
    if new == "active" and sp["state"] == "future":
        if not sp["start_date"] or not sp["end_date"]:
            raise _err("", 400, startDate="A sprint needs a start and end date to be started.")
        if any(o["state"] == "active" and o["origin_board_id"] == sp["origin_board_id"] for o in s["sprints"].values()):
            raise _err("Sprint cannot be started: another sprint is already active on this board. Complete it first.", 400)
        sp.update(state="active", activated_date=_iso(ctx).replace("+0000", "Z"))
    elif new == "closed" and sp["state"] != "closed":
        if sp["state"] != "active":
            raise _err("Sprint must be active to be completed.", 400)
        target = _sprint(s, move_incomplete_to) if move_incomplete_to else None
        if target is not None and (target["state"] != "future" or target["origin_board_id"] != sp["origin_board_id"]):
            raise _err("", 400, move_incomplete_to="Unfinished issues can only move to a future sprint on the same board.")
        for i in s["issues"].values():
            if i.get("sprint") == sp["id"]:
                i["closed_sprints"].append(sp["id"])
                i["sprint"] = None if STATUSES[i["status"]] == "done" or target is None else target["id"]
                i["updated"] = _iso(ctx)
        sp.update(state="closed", complete_date=_iso(ctx).replace("+0000", "Z"))
    elif new == "future" and sp["state"] == "active":
        raise _err("An active sprint can't be moved back to future.", 400)
    return _sprint_json(sp)


Jira.tools = [jira_search, jira_get_issue, jira_create_issue, jira_batch_create_issues, jira_update_issue,
              jira_delete_issue, jira_assign_issue, jira_transition_issue, jira_get_transitions, jira_add_comment,
              jira_add_worklog, jira_get_all_projects, jira_get_project_issues, jira_create_issue_link, jira_link_to_epic,
              jira_get_user_profile, jira_get_agile_boards, jira_get_board_issues, jira_get_sprints_from_board,
              jira_get_sprint_issues, jira_create_sprint, jira_update_sprint]


# -- world actions (triggered by environments, never by agents) --------------------------

@action("set_status")
def act_set_status(ctx: Instance, issue_key: str, status: str) -> None:
    """Someone moves an issue (bypassing the agent's view of the workflow)."""
    if status not in STATUSES:
        raise _err(f"unknown status {status}")
    i = _get(ctx.state, issue_key)
    i["status"] = status
    done = STATUSES[status] == "done"
    i["resolution"], i["resolutiondate"] = ("Done", _iso(ctx)) if done else (None, None)
    i["updated"] = _iso(ctx)


@action("add_comment")
def act_add_comment(ctx: Instance, issue_key: str, author: str, body: str) -> None:
    """A colleague comments on an issue."""
    _comment(ctx, _get(ctx.state, issue_key), _resolve_user(ctx.state, author), body)


@action("assign")
def act_assign(ctx: Instance, issue_key: str, assignee: str | None) -> None:
    """Someone reassigns an issue."""
    i = _get(ctx.state, issue_key)
    i["assignee"] = _resolve_user(ctx.state, assignee)
    i["updated"] = _iso(ctx)


@action("move_to_sprint")
def act_move_to_sprint(ctx: Instance, issue_key: str, sprint: str | None) -> None:
    """A teammate pulls an issue into a sprint (by id or name) or back to the backlog (None)."""
    s = ctx.state
    sid = None
    if sprint:
        sid = next((sp["id"] for sp in s["sprints"].values() if sprint in (sp["id"], sp["name"])), None)
        if sid is None:
            raise _err(f"no sprint {sprint}")
    i = _get(s, issue_key)
    _set_sprint(s, i, sid)
    i["updated"] = _iso(ctx)


Jira.actions = [act_set_status, act_add_comment, act_assign, act_move_to_sprint]
