"""The jira service: identity, versions, seed format, state, errors and the version probe."""

from __future__ import annotations

import json
from typing import Any

from ...core.instance import Instance, Service
from ...core.tools import ToolError
from . import notify
from .model import SEARCH_LAG, V1, V2, V3, _comment, _new_issue, _resolve_user, _seed_board


class Jira(Service):
    name = "jira"
    title = "Jira"
    description = "Simulated Jira Cloud site. Behaves like the mcp-atlassian Jira tools; nothing is really changed."

    versions = {"2026-09-25": "Initial release: 16 tools modeled on sooperset/mcp-atlassian (Jira).",
                V1: "Agile boards and sprints (6 tools), sprint field and sprint functions in JQL.",
                V2: "JQL search is eventually consistent: new and edited issues reach search about 10 seconds "
                    "later (jira_get_issue is immediate).",
                V3: "Email notifications: assignments, status changes and comments email the assignee, reporter, "
                    "watchers and earlier commenters (not whoever made the change), through any Gmail in the "
                    "environment."}

    def notifications(self, ctx: Instance, before: dict[str, Any], after: dict[str, Any], by: str | None) -> None:
        notify.notifications(ctx, before, after, by)

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
        if ctx.at_least(V3):
            c("jira_assign_issue", {"issue_key": "OPS-4", "assignee": "john@acme.com"})
            c("jira_add_comment", {"issue_key": "OPS-4", "body": "Can you take this one?"})
            c("jira_add_comment", {"issue_key": "OPS-4", "body": "Sure, on it."}, as_="john@acme.com")
            c("jira_transition_issue", {"issue_key": "OPS-4", "transition_id": "21"}, as_="john@acme.com")
            c("jira_create_issue", {"project_key": "OPS", "summary": "Renew TLS certs", "issue_type": "Task",
                                    "assignee": "priya@acme.com"})

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
        if ctx.at_least(V3):
            state["_notify"] = True
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
