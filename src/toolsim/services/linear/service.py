"""The linear service: identity, versions, seed format, state, errors and the version probe."""

from __future__ import annotations

import datetime as dt
from typing import Any

from ...core.instance import Instance, Service
from ...core.tools import ToolError
from .model import (
    DEFAULT_STATES,
    SCALES,
    V1,
    _comment,
    _create,
    _find_cycle,
    _find_user,
    _iso,
    _new_label,
    _new_user,
    _slug,
    _uuid,
)


class Linear(Service):
    name = "linear"
    title = "Linear"
    description = "Simulated Linear workspace. Behaves like Linear's MCP server; nothing is really changed."
    fidelity = "preview"  # tool names are real; some parameters/response shapes are inferred
    versions = {"2026-09-25": "Initial release: 23 tools modeled on Linear's hosted MCP server.",
                V1: "Cycles on issues, team estimate scales, exclusive label groups, cursor pagination for list_issues."}

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
        if ctx.at_least(V1):
            c("create_issue", {"title": "Refund webhooks", "team": "ENG", "cycle": "current", "estimate": 3})
            c("create_issue", {"title": "x", "team": "ENG", "estimate": 4})
            c("create_issue", {"title": "x", "team": "OPS", "estimate": 1})
            c("update_issue", {"id": "ENG-2", "labels": ["Bug", "Feature"]})
            c("update_issue", {"id": "ENG-2", "cycle": "1"})
            c("update_issue", {"id": "ENG-2", "cycle": "next", "labels": ["Feature", "Tech debt"]})
            page = c("list_issues", {"team": "ENG", "limit": 2}).data
            c("list_issues", {"team": "ENG", "limit": 2, "cursor": page["nextCursor"]})
            c("list_issues", {"cycle": "current"})
            c("list_issue_labels", {"team": "ENG"})

    def default_seed(self) -> dict[str, Any]:
        return {
            "viewer": {"name": "Alex Rivera", "email": "alex@acme.com"},
            "users": [{"name": "John Park", "email": "john@acme.com"}, {"name": "Priya Shah", "email": "priya@acme.com"}],
            "teams": [
                {"key": "ENG", "name": "Engineering", "labels": ["Bug", "Feature", "Tech debt"],
                 "estimates": "fibonacci", "label_groups": {"Type": ["Bug", "Feature"]},
                 "projects": [{"name": "Billing v2", "state": "started", "lead": "john@acme.com",
                               "targetDate": "2026-10-31", "summary": "Rebuild invoicing and refunds"}],
                 "issues": [
                     {"title": "Duplicate charges when payment retries", "state": "In Progress", "priority": 1,
                      "assignee": "john@acme.com", "labels": ["Bug"], "project": "Billing v2", "cycle": "current",
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
            if ctx.at_least(V1):
                scale = t.get("estimates", "none")
                if scale not in SCALES:
                    raise ValueError(f"team {t['key']}: estimates must be one of {', '.join(SCALES)}")
                state["teams"][tid]["estimates"] = scale
                for group, members in (t.get("label_groups") or {}).items():
                    gid = _new_label(ctx, state, group, tid)["id"]
                    state["labels"][gid]["isGroup"] = True
                    for m in members:
                        lab = next((l for l in state["labels"].values() if l["name"] == m and l["teamId"] == tid), None)
                        lab = lab or _new_label(ctx, state, m, tid)
                        lab["parentId"] = gid
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
            if ctx.at_least(V1):
                state["_v1"] = True
            for i in t.get("issues", []):
                issue = _create(ctx, state, {**i, "team": t["key"]}, creator=state["viewer"])
                if i.get("cycle") and state.get("_v1"):
                    issue["cycleId"] = _find_cycle(state, issue["teamId"], str(i["cycle"]), ctx.now().date().isoformat())["id"]
                for c in i.get("comments", []):
                    _comment(ctx, state, issue["id"], c["body"], _find_user(state, c.get("author", "me"))["id"])
        for d in seed.get("documents", []):
            did = _uuid(ctx)
            state["documents"][did] = {"id": did, "title": d["title"], "content": d.get("content", ""), "projectId": None,
                                       "createdAt": _iso(ctx), "updatedAt": _iso(ctx)}
        return state

    actor_key = "viewer"

    def resolve_actor(self, state: dict[str, Any], identity: str) -> str:
        try:
            return _find_user(state, identity)["id"]
        except ToolError:
            raise ValueError(f"no Linear user {identity} in this workspace") from None

    def error_shape(self, status: int, message: str) -> Any:
        return {"error": message, "type": {404: "NotFound", 503: "ServiceUnavailable"}.get(status, "InternalError")}

    def fault_error(self, fault: Any) -> tuple[Any, int]:
        if fault.kind == "rate_limit":
            return {"error": "Rate limit exceeded. Please retry after the reset time.", "type": "Ratelimited",
                    "retryAfter": fault.retry_after}, 429
        if fault.kind == "server_error":
            return {"error": "Internal server error", "type": "InternalError"}, 500
        return super().fault_error(fault)
