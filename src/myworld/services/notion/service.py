"""The notion service: identity, versions, seed format, state, errors and the version probe."""

from __future__ import annotations

from typing import Any

from ...core.instance import Instance, Service
from .model import ACCESS, SEARCH_LAG, V1, _id, _new_data_source, _new_page, _new_row, _new_user, _schema


class Notion(Service):
    name = "notion"
    title = "Notion"
    description = "Simulated Notion workspace. Behaves like Notion's hosted MCP server; nothing is really changed."
    fidelity = "preview"  # tool names are real; some parameters/response shapes are inferred
    versions = {"2026-09-25": "Initial release: 12 core tools modeled on Notion's hosted MCP server.",
                V1: "Page access levels (view/comment/edit), eventually consistent search, typed data source "
                    "filters with cursors, asynchronous page duplication."}

    def probe(self, ctx: Instance) -> None:
        c = ctx.call
        c("notion-search", {"query": "runbook"})
        wiki = c("notion-search", {"query": "Engineering Wiki"}).data["results"][0]["id"]
        c("notion-fetch", {"id": wiki})
        c("notion-fetch", {"id": "00000000-0000-0000-0000-000000000000"})
        tasks = next(d for d in ctx.state["data_sources"].values() if d["title"] == "Tasks")
        c("notion-query-data-sources", {"mode": "rows", "data_source_url": tasks["url"], "filter": {"Status": "In progress"}})
        page = c("notion-create-pages", {"parent": {"page_id": wiki}, "pages": [{"properties": {"title": "Q4 retro"},
                                                                               "content": "## Went well\n- Shipped v2.3"}]})
        pid = page.data["pages"][0]["id"]
        c("notion-update-page", {"page_id": pid, "command": "insert_content_after", "selection_with_ellipsis": "## Went...v2.3",
                                 "new_str": "\n## To improve\n- Flaky tests"})
        c("notion-update-page", {"page_id": pid, "command": "replace_content_range",
                                 "selection_with_ellipsis": "- Flaky...tests", "new_str": "- Flaky retry test"})
        c("notion-create-pages", {"parent": {"data_source_id": tasks["id"]},
                                  "pages": [{"properties": {"Name": "Refund duplicate charge", "Status": "Not started",
                                                            "Tags": ["billing"]}}]})
        c("notion-create-pages", {"parent": {"data_source_id": tasks["id"]},
                                  "pages": [{"properties": {"Name": "x", "Status": "Blocked"}}]})
        c("notion-create-comment", {"page_id": pid, "content": "Looks good"})
        c("notion-get-comments", {"page_id": pid})
        c("notion-get-users", {})
        c("notion-get-teams", {})
        if ctx.at_least(V1):
            q4 = next(p for p in ctx.state["pages"].values() if p["title"] == "Q4 Planning")
            c("notion-update-page", {"page_id": q4["id"], "command": "replace_content", "new_str": "x"})
            c("notion-create-comment", {"page_id": q4["id"], "content": "Can we revisit the hiring risk?"})
            c("notion-search", {"query": "Q4 retro"})
            ctx.advance(150)
            c("notion-search", {"query": "Q4 retro"})
            c("notion-query-data-sources", {"data_source_url": tasks["url"], "filter": {"Points": {">=": 3}},
                                            "sort": {"property": "Points", "direction": "descending"}, "limit": 1})
            c("notion-query-data-sources", {"data_source_url": tasks["url"], "filter": {"Owner": {"is_empty": True}}})
            c("notion-query-data-sources", {"data_source_url": tasks["url"], "filter": {"Name": {">": 3}}})
            dup = c("notion-duplicate-page", {"page_url": wiki}).data["page_id"]
            c("notion-fetch", {"id": dup})
            ctx.advance(60)
            c("notion-fetch", {"id": dup})

    def default_seed(self) -> dict[str, Any]:
        return {
            "user": {"name": "Alex Rivera", "email": "alex@acme.com"},
            "users": [{"name": "John Park", "email": "john@acme.com"}, {"name": "Priya Shah", "email": "priya@acme.com"}],
            "teams": [{"name": "Engineering"}, {"name": "Operations"}],
            "pages": [
                {"key": "wiki", "title": "Engineering Wiki", "team": "Engineering", "icon": "📚",
                 "content": "# Engineering Wiki\n\nStart here. Runbooks, architecture notes and team rituals."},
                {"key": "runbook", "title": "On-call runbook", "parent": "wiki",
                 "content": "## Paging\nPages go to #api-oncall first.\n\n## Rollback\n1. Find the last green deploy.\n2. Run `deploy rollback <sha>`.\n3. Post in #api-oncall."},
                {"key": "q4", "title": "Q4 Planning", "team": "Operations", "access": "comment",
                 "content": "## Goals\n- Cut infra cost 15%\n- Ship Billing v2\n\n## Risks\n- Hiring is behind plan"},
                {"title": "1:1 notes: John", "parent": "q4", "content": "- Wants to lead Billing v2"},
                {"title": "Compensation 2026", "team": "Operations", "restricted": True,
                 "content": "Confidential salary bands."},
            ],
            "databases": [{
                "key": "tasks", "title": "Tasks", "parent": "wiki",
                "properties": {"Name": "title", "Status": {"status": ["Not started", "In progress", "Done"]},
                               "Owner": "people", "Due": "date", "Tags": {"multi_select": ["infra", "billing", "security"]},
                               "Points": "number", "Shipped": "checkbox"},
                "rows": [
                    {"Name": "Rotate DB credentials", "Status": "In progress", "Owner": "john@acme.com", "Due": "2026-09-26",
                     "Tags": ["security"], "Points": 3},
                    {"Name": "Right-size staging cluster", "Status": "Not started", "Tags": ["infra"], "Points": 5},
                    {"Name": "Invoice currency bug", "Status": "Done", "Owner": "priya@acme.com", "Tags": ["billing"],
                     "Points": 2, "Shipped": True},
                ],
            }],
        }

    def initial_state(self, seed: dict[str, Any], ctx: Instance) -> dict[str, Any]:
        state: dict[str, Any] = {"users": {}, "teams": {}, "pages": {}, "data_sources": {}, "comments": {}}
        me = seed.get("user") or {"name": "Alex Rivera", "email": "alex@acme.com"}
        state["me"] = _new_user(ctx, state, me)["id"]
        for u in seed.get("users", []):
            _new_user(ctx, state, u)
        for t in seed.get("teams", []):
            tid = _id(ctx)
            state["teams"][tid] = {"id": tid, "name": t["name"], "role": t.get("role", "member")}
        keys: dict[str, str] = {}
        for p in seed.get("pages", []):
            parent = {"type": "page_id", "id": keys[p["parent"]]} if p.get("parent") else {"type": "workspace", "id": None}
            team = next((tid for tid, t in state["teams"].items() if t["name"] == p.get("team")), None)
            page = _new_page(ctx, state, p["title"], parent, p.get("content", ""), icon=p.get("icon"), team=team)
            if p.get("id"):  # a seed may fix the id (e.g. one a task refers to)
                state["pages"][p["id"]] = state["pages"].pop(page["id"])
                page["id"], page["url"] = p["id"], page["url"].rsplit("-", 1)[0] + "-" + p["id"]
            page["restricted"] = bool(p.get("restricted"))
            if ctx.at_least(V1) and p.get("access"):
                if p["access"] not in ACCESS:
                    raise ValueError(f"page {p['title']!r}: access must be one of {', '.join(ACCESS)}")
                page["access"] = p["access"]
            if p.get("key"):
                keys[p["key"]] = page["id"]
        for d in seed.get("databases", []):
            ds = _new_data_source(ctx, state, d["title"], keys[d["parent"]], _schema(d["properties"]))
            if d.get("key"):
                keys[d["key"]] = ds["id"]
            for row in d.get("rows", []):
                _new_row(ctx, state, ds, row)
        if ctx.at_least(V1):
            state["_v1"] = True
            state["_search_lag"] = {**SEARCH_LAG, **(seed.get("search_lag") or {})}
        return state

    actor_key = "me"

    def resolve_actor(self, state: dict[str, Any], identity: str) -> str:
        q = identity.strip().lower()
        for u in state["users"].values():
            if q in (u["id"], u["email"].lower(), u["name"].lower()):
                return u["id"]
        raise ValueError(f"no Notion user {identity} in this workspace")

    def error_shape(self, status: int, message: str) -> Any:
        code = {404: "object_not_found", 500: "internal_server_error", 502: "internal_server_error",
                503: "service_unavailable", 504: "gateway_timeout"}.get(status, "internal_server_error")
        return {"object": "error", "status": status, "code": code, "message": message}

    def fault_error(self, fault: Any) -> tuple[Any, int]:
        if fault.kind == "rate_limit":
            return {"object": "error", "status": 429, "code": "rate_limited",
                    "message": "You have been rate limited. Please try again in a few minutes."}, 429
        if fault.kind == "server_error":
            return {"object": "error", "status": 502, "code": "internal_server_error",
                    "message": "Unexpected error occurred."}, 502
        return super().fault_error(fault)
