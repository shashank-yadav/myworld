"""Notion.

Tool names follow Notion's hosted MCP server (mcp.notion.com). Pages hold Notion-flavored
Markdown; databases ("data sources") have typed schemas and their rows are pages with
properties. Pages the user can't access behave as if they don't exist, as in Notion.

Not simulated in this version: file uploads and attachments, views, Notion AI search over
connected sources, custom agents, skills, and SQL queries over data sources.

Seed format::

    user: {name: Alex Rivera, email: alex@acme.com}
    teams: [{name: Engineering}]
    pages:
      - {key: wiki, title: Engineering Wiki, team: Engineering, content: "# Welcome"}
      - {key: runbook, title: On-call runbook, parent: wiki, content: "## Paging\\n..."}
      - {title: Compensation 2026, restricted: true}
    databases:
      - key: tasks
        title: Tasks
        parent: wiki
        properties: {Name: title, Status: {status: [Not started, In progress, Done]}, Owner: people,
                     Due: date, Tags: {multi_select: [infra, billing]}, Points: number, Shipped: checkbox}
        rows: [{Name: Rotate DB credentials, Status: In progress, Owner: john@acme.com, Due: 2026-09-26}]
"""

from __future__ import annotations

import copy
import re
import uuid
from typing import Annotated, Any, Literal

from ..core.instance import Instance, Service
from ..core.tools import ToolError, action, tool

PROPERTY_TYPES = {"title", "text", "number", "select", "multi_select", "status", "date", "people", "checkbox", "url",
                  "email", "phone"}


def _err(message: str, code: str = "validation_error", status: int = 400) -> ToolError:
    return ToolError({"object": "error", "status": status, "code": code, "message": message}, status=status)


def _not_found(what: str) -> ToolError:
    return _err(f"Could not find {what}. Make sure the relevant pages and databases are shared with your integration.",
                "object_not_found", 404)


class Notion(Service):
    name = "notion"
    title = "Notion"
    description = "Simulated Notion workspace. Behaves like Notion's hosted MCP server; nothing is really changed."
    fidelity = "preview"  # tool names are real; some parameters/response shapes are inferred
    versions = {"2026-09-25": "Initial release: 12 core tools modeled on Notion's hosted MCP server."}

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
                {"key": "q4", "title": "Q4 Planning", "team": "Operations",
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
            page["restricted"] = bool(p.get("restricted"))
            if p.get("key"):
                keys[p["key"]] = page["id"]
        for d in seed.get("databases", []):
            ds = _new_data_source(ctx, state, d["title"], keys[d["parent"]], _schema(d["properties"]))
            if d.get("key"):
                keys[d["key"]] = ds["id"]
            for row in d.get("rows", []):
                _new_row(ctx, state, ds, row)
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


# -- model --------------------------------------------------------------------------------

def _id(ctx: Instance) -> str:
    return str(uuid.UUID(int=ctx.rng.getrandbits(128), version=4))


def _iso(ctx: Instance) -> str:
    return ctx.now().strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _url(title: str, pid: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-")
    return f"https://www.notion.so/{slug}-{pid.replace('-', '')}"


def _new_user(ctx: Instance, state: dict[str, Any], u: dict[str, Any]) -> dict[str, Any]:
    uid = _id(ctx)
    state["users"][uid] = {"id": uid, "type": "person", "name": u["name"], "email": u["email"]}
    return state["users"][uid]


def _new_page(ctx: Instance, state: dict[str, Any], title: str, parent: dict[str, Any], content: str,
              properties: dict[str, Any] | None = None, icon: str | None = None, team: str | None = None) -> dict[str, Any]:
    pid = _id(ctx)
    page = {"id": pid, "title": title, "parent": parent, "content": content, "properties": properties or {},
            "icon": icon, "team": team, "in_trash": False, "restricted": False, "url": _url(title, pid),
            "created_time": _iso(ctx), "last_edited_time": _iso(ctx), "created_by": state["me"]}
    state["pages"][pid] = page
    state["comments"][pid] = []
    return page


def _schema(props: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, spec in props.items():
        if isinstance(spec, str):
            typ, options = spec, None
        elif isinstance(spec, dict) and len(spec) == 1:
            typ, options = next(iter(spec.items()))
        else:
            typ, options = spec.get("type"), spec.get("options")
        if typ not in PROPERTY_TYPES:
            raise _err(f"Invalid property type '{typ}' for property '{name}'")
        out[name] = {"type": typ, **({"options": list(options)} if options is not None else {})}
    if sum(p["type"] == "title" for p in out.values()) != 1:
        raise _err("A database must have exactly one title property")
    return out


def _new_data_source(ctx: Instance, state: dict[str, Any], title: str, parent_id: str,
                     schema: dict[str, dict[str, Any]]) -> dict[str, Any]:
    did = _id(ctx)
    ds = {"id": did, "title": title, "parent_page_id": parent_id, "schema": schema, "url": f"collection://{did}",
          "database_url": _url(title, did), "created_time": _iso(ctx), "in_trash": False}
    state["data_sources"][did] = ds
    return ds


def _coerce_props(state: dict[str, Any], ds: dict[str, Any], props: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    title, out = None, {}
    for name, value in props.items():
        spec = ds["schema"].get(name)
        if spec is None:
            raise _err(f"{name} is not a property that exists.")
        typ = spec["type"]
        if value is None:
            out[name] = None
            continue
        if typ == "title":
            title = str(value)
            continue
        if typ in ("select", "status"):
            if typ == "status" and value not in spec.get("options", []):
                raise _err(f"Invalid status option \"{value}\" for property {name}. "
                           f"Valid options: {', '.join(spec.get('options', []))}")
            if typ == "select" and value not in spec.setdefault("options", []):
                spec["options"].append(value)  # Notion creates missing select options on write
        elif typ == "multi_select":
            value = [value] if isinstance(value, str) else list(value)
            for v in value:
                if v not in spec.setdefault("options", []):
                    spec["options"].append(v)
        elif typ == "number":
            try:
                value = float(value) if "." in str(value) else int(value)
            except ValueError:
                raise _err(f"{name} is expected to be number.") from None
        elif typ == "checkbox":
            value = value if isinstance(value, bool) else str(value).lower() in ("true", "yes", "1", "__yes__")
        elif typ == "date":
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(T[\d:.]+(Z|[+-]\d{2}:\d{2})?)?", str(value)):
                raise _err(f"{name} is expected to be an ISO 8601 date.")
        elif typ == "people":
            value = [value] if isinstance(value, str) else list(value)
            ids = []
            for v in value:
                u = next((u for u in state["users"].values() if v in (u["id"], u["email"], u["name"])), None)
                if u is None:
                    raise _err(f"User '{v}' was not found for property {name}.")
                ids.append(u["id"])
            value = ids
        out[name] = value
    return title or "", out


def _new_row(ctx: Instance, state: dict[str, Any], ds: dict[str, Any], props: dict[str, Any], content: str = "") -> dict[str, Any]:
    title, values = _coerce_props(state, ds, props)
    if not title:
        raise _err("The title property is required.")
    return _new_page(ctx, state, title, {"type": "data_source_id", "id": ds["id"]}, content, values)


def _visible(page: dict[str, Any] | None) -> bool:
    return bool(page) and not page["restricted"]


def _resolve_id(ref: str) -> str:
    ref = ref.strip()
    if ref.startswith("collection://"):
        return ref[len("collection://"):]
    m = re.search(r"([0-9a-f]{32})(?:\?.*)?$", ref.replace("-", "")) if "notion.so" in ref else None
    raw = m.group(1) if m else ref.replace("-", "")
    return str(uuid.UUID(raw)) if re.fullmatch(r"[0-9a-fA-F]{32}", raw) else ref


def _page(state: dict[str, Any], ref: str, editable: bool = False) -> dict[str, Any]:
    p = state["pages"].get(_resolve_id(ref))
    if not _visible(p):
        raise _not_found(f"page with ID: {ref}")
    if editable and p["in_trash"]:
        raise _err("Can't edit block that is archived. You must unarchive the block before editing.")
    return p


def _path(state: dict[str, Any], p: dict[str, Any]) -> list[str]:
    out, cur = [], p
    while cur and cur["parent"]["type"] != "workspace":
        pid = cur["parent"]["id"]
        if cur["parent"]["type"] == "data_source_id":
            ds = state["data_sources"][pid]
            out.append(ds["title"])
            cur = state["pages"].get(ds["parent_page_id"])
        else:
            cur = state["pages"].get(pid)
        if cur and cur["parent"]["type"] != "data_source_id":
            out.append(cur["title"])
    return list(reversed(out))


def _props_view(state: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
    if p["parent"]["type"] != "data_source_id":
        return {"title": p["title"]}
    ds = state["data_sources"][p["parent"]["id"]]
    title_prop = next(n for n, s in ds["schema"].items() if s["type"] == "title")
    out = {title_prop: p["title"]}
    for name, v in p["properties"].items():
        out[name] = [state["users"][u]["email"] for u in v] if ds["schema"][name]["type"] == "people" and v else v
    return out


def _find_selection(content: str, selection: str) -> tuple[int, int]:
    if "..." not in selection:
        i = content.find(selection)
        if i < 0:
            raise _err(f"Could not find text matching selection: {selection!r}")
        return i, i + len(selection)
    start, end = selection.split("...", 1)
    i = content.find(start)
    j = content.find(end, i + len(start)) if i >= 0 else -1
    if i < 0 or j < 0:
        raise _err(f"Could not find text matching selection: {selection!r}")
    if content.find(start, i + 1) >= 0 and content.find(end, content.find(start, i + 1) + len(start)) >= 0:
        raise _err("Selection matched multiple ranges; include more text to make it unique.")
    return i, j + len(end)


# -- tools --------------------------------------------------------------------------------

@tool("notion-search", read_only=True)
def notion_search(ctx: Instance,
                  query: Annotated[str, "Semantic search query over your workspace"],
                  query_type: Annotated[Literal["internal", "user"] | None, "'internal' searches pages and databases; 'user' searches people"] = "internal",
                  filters: Annotated[dict | None, "Optional filters, e.g. {\"created_by_user_ids\": [...], \"created_date_range\": {...}}"] = None,
                  location: Annotated[str | None, "Restrict results to a page (URL or ID) and its children"] = None,
                  sort: Annotated[str | None, "Sort order, e.g. 'last_edited'"] = None,
                  limit: Annotated[int | None, "Maximum results (default 10)"] = 10) -> dict[str, Any]:
    """Search the Notion workspace for pages and databases (or users)"""
    s = ctx.state
    words = query.lower().split()
    if query_type == "user":
        us = [u for u in s["users"].values() if all(w in f"{u['name']} {u['email']}".lower() for w in words)]
        return {"results": [{"id": u["id"], "name": u["name"], "email": u["email"], "type": "user"} for u in us]}
    scope = _page(s, location)["id"] if location else None
    results = []
    for p in s["pages"].values():
        if not _visible(p) or p["in_trash"]:
            continue
        if scope and scope != p["id"] and not _descends(s, p, scope):
            continue
        text = f"{p['title']} {p['content']}".lower()
        score = sum(w in p["title"].lower() for w in words) * 2 + sum(w in text for w in words)
        if score and all(w in text for w in words):
            results.append((score, {"id": p["id"], "title": p["title"], "url": p["url"], "type": "page",
                                    "highlight": _snippet(p["content"], words), "timestamp": p["last_edited_time"]}))
    for d in s["data_sources"].values():
        if not d["in_trash"] and all(w in d["title"].lower() for w in words):
            results.append((5, {"id": d["id"], "title": d["title"], "url": d["database_url"], "type": "database",
                                "data_source_url": d["url"], "timestamp": d["created_time"]}))
    results.sort(key=lambda r: -r[0])
    return {"results": [r for _, r in results[: max(1, limit or 10)]], "type": "workspace_search"}


def _descends(s: dict[str, Any], p: dict[str, Any], ancestor: str) -> bool:
    cur = p
    while cur and cur["parent"]["type"] != "workspace":
        pid = cur["parent"]["id"]
        if cur["parent"]["type"] == "data_source_id":
            pid = s["data_sources"][pid]["parent_page_id"]
        if pid == ancestor:
            return True
        cur = s["pages"].get(pid)
    return False


def _snippet(content: str, words: list[str]) -> str:
    low = content.lower()
    i = min((low.find(w) for w in words if w in low), default=0)
    return content[max(0, i - 40): i + 120].replace("\n", " ")


@tool("notion-fetch", read_only=True)
def notion_fetch(ctx: Instance, id: Annotated[str, "A Notion page or database URL or ID, or a data source URL (collection://...)"]) -> dict[str, Any]:
    """Retrieve the content of a Notion page or database by its URL or ID"""
    s = ctx.state
    rid = _resolve_id(id)
    ds = s["data_sources"].get(rid) or next((d for d in s["data_sources"].values()
                                             if d["database_url"].endswith(rid.replace("-", ""))), None)
    if ds and not ds["in_trash"]:
        schema = "\n".join(f"- {n}: {p['type']}" + (f" ({', '.join(p['options'])})" if p.get("options") else "")
                           for n, p in ds["schema"].items())
        return {"metadata": {"type": "database"}, "title": ds["title"], "url": ds["database_url"],
                "text": f'<database url="{ds["database_url"]}">\n<data-source url="{ds["url"]}">\n'
                        f"<schema>\n{schema}\n</schema>\n</data-source>\n</database>"}
    p = _page(s, id)
    children = [c for c in s["pages"].values() if c["parent"] == {"type": "page_id", "id": p["id"]} and _visible(c)
                and not c["in_trash"]]
    dbs = [d for d in s["data_sources"].values() if d["parent_page_id"] == p["id"] and not d["in_trash"]]
    body = p["content"] + "".join(f'\n<page url="{c["url"]}">{c["title"]}</page>' for c in children) + \
        "".join(f'\n<database url="{d["database_url"]}" data-source-url="{d["url"]}">{d["title"]}</database>' for d in dbs)
    import json as _json
    return {"metadata": {"type": "page"}, "title": p["title"], "url": p["url"],
            "text": f'<page url="{p["url"]}">\n<ancestor-path>{" / ".join(_path(s, p))}</ancestor-path>\n'
                    f"<properties>\n{_json.dumps(_props_view(s, p))}\n</properties>\n<content>\n{body}\n</content>\n</page>"}


def _parent(s: dict[str, Any], parent: dict[str, Any] | None) -> tuple[str, Any]:
    if not parent:
        return "workspace", None
    if parent.get("data_source_id") or parent.get("database_id"):
        ref = _resolve_id(parent.get("data_source_id") or parent.get("database_id"))
        ds = s["data_sources"].get(ref)
        if ds is None or ds["in_trash"]:
            raise _not_found(f"data source with ID: {ref}")
        return "data_source", ds
    if parent.get("page_id"):
        return "page", _page(s, parent["page_id"], editable=True)
    raise _err("parent must include page_id, data_source_id or database_id")


@tool("notion-create-pages")
def notion_create_pages(ctx: Instance,
                        pages: Annotated[list[dict], "Pages to create: [{\"properties\": {\"title\": \"...\"} (or database properties), \"content\": \"Notion-flavored Markdown\"}]"],
                        parent: Annotated[dict | None, "Where to create them: {\"page_id\": ...} or {\"data_source_id\": ...}. Omit for a private workspace page"] = None,
                        allow_async: Annotated[bool | None, "Allow the operation to run asynchronously"] = False) -> dict[str, Any]:
    """Create one or more Notion pages, optionally as rows in a database"""
    s = ctx.state
    kind, target = _parent(s, parent)
    if not pages:
        raise _err("pages must contain at least one page")
    created = []
    for spec in pages:
        props = dict(spec.get("properties") or {})
        content = spec.get("content", "")
        if kind == "data_source":
            p = _new_row(ctx, s, target, props, content)
        else:
            title = props.pop("title", None)
            if not title:
                raise _err("properties.title is required for pages outside a database")
            if props:
                raise _err(f"{next(iter(props))} is not a property that exists.")
            par = {"type": "page_id", "id": target["id"]} if kind == "page" else {"type": "workspace", "id": None}
            p = _new_page(ctx, s, title, par, content, icon=spec.get("icon"))
        created.append({"id": p["id"], "url": p["url"], "properties": _props_view(s, p)})
    return {"pages": created}


@tool("notion-update-page", idempotent=False)
def notion_update_page(ctx: Instance,
                       page_id: Annotated[str, "The page to update (ID or URL)"],
                       command: Annotated[Literal["update_properties", "replace_content", "replace_content_range", "insert_content_after"], "What to change"],
                       properties: Annotated[dict | None, "For update_properties: property values to set (use \"title\" for a page's title)"] = None,
                       new_str: Annotated[str | None, "For content commands: the new Notion-flavored Markdown"] = None,
                       selection_with_ellipsis: Annotated[str | None, "For range commands: the start and end of the target text joined by '...', e.g. '## Risks...behind plan'"] = None,
                       allow_async: Annotated[bool | None, "Allow the operation to run asynchronously"] = False) -> dict[str, Any]:
    """Update a Notion page's properties or content"""
    s = ctx.state
    p = _page(s, page_id, editable=True)
    if command == "update_properties":
        if not properties:
            raise _err("properties is required for update_properties")
        if p["parent"]["type"] == "data_source_id":
            ds = s["data_sources"][p["parent"]["id"]]
            title_prop = next(n for n, sp in ds["schema"].items() if sp["type"] == "title")
            props = dict(properties)
            if "title" in props and "title" not in ds["schema"]:
                props[title_prop] = props.pop("title")
            title, values = _coerce_props(s, ds, props)
            if title_prop in props:
                p["title"] = title
            p["properties"].update(values)
        else:
            bad = [k for k in properties if k != "title"]
            if bad:
                raise _err(f"{bad[0]} is not a property that exists.")
            p["title"] = properties["title"]
        p["url"] = _url(p["title"], p["id"])
    else:
        if new_str is None:
            raise _err(f"new_str is required for {command}")
        if command == "replace_content":
            p["content"] = new_str
        else:
            if not selection_with_ellipsis:
                raise _err(f"selection_with_ellipsis is required for {command}")
            i, j = _find_selection(p["content"], selection_with_ellipsis)
            p["content"] = (p["content"][:i] + new_str + p["content"][j:] if command == "replace_content_range"
                            else p["content"][:j] + new_str + p["content"][j:])
    p["last_edited_time"] = _iso(ctx)
    return {"page_id": p["id"], "url": p["url"], "status": "updated"}


@tool("notion-move-pages")
def notion_move_pages(ctx: Instance,
                      page_ids: Annotated[list[str], "IDs or URLs of the pages to move"],
                      parent: Annotated[dict, "Destination: {\"page_id\": ...}, {\"data_source_id\": ...} or {\"workspace\": true}"]) -> dict[str, Any]:
    """Move pages to a new parent"""
    s = ctx.state
    kind, target = ("workspace", None) if parent.get("workspace") else _parent(s, parent)
    moved = []
    for ref in page_ids:
        p = _page(s, ref, editable=True)
        if kind == "page" and (target["id"] == p["id"] or _descends(s, target, p["id"])):
            raise _err("Cannot move a page into itself or one of its descendants.")
        if kind == "data_source":
            _coerce_props(s, target, {})  # schema check happens on property writes
            p["parent"] = {"type": "data_source_id", "id": target["id"]}
        else:
            p["parent"] = {"type": "page_id", "id": target["id"]} if kind == "page" else {"type": "workspace", "id": None}
        p["last_edited_time"] = _iso(ctx)
        moved.append(p["id"])
    return {"moved": moved}


@tool("notion-duplicate-page")
def notion_duplicate_page(ctx: Instance, page_url: Annotated[str, "The page to duplicate (URL or ID)"]) -> dict[str, Any]:
    """Duplicate a Notion page (including its content) under the same parent"""
    s = ctx.state
    src = _page(s, page_url)
    p = _new_page(ctx, s, src["title"], copy.deepcopy(src["parent"]), src["content"], copy.deepcopy(src["properties"]),
                  icon=src["icon"], team=src["team"])
    return {"page_id": p["id"], "url": p["url"], "status": "succeeded"}


@tool("notion-create-database")
def notion_create_database(ctx: Instance,
                           title: Annotated[str, "Database title"],
                           properties: Annotated[dict, "Schema: {\"Name\": \"title\", \"Status\": {\"status\": [\"Todo\", \"Done\"]}, \"Due\": \"date\", ...}"],
                           parent: Annotated[dict | None, "Parent page: {\"page_id\": ...}"] = None) -> dict[str, Any]:
    """Create a new Notion database with a schema"""
    s = ctx.state
    kind, target = _parent(s, parent)
    if kind != "page":
        raise _err("Databases must be created inside a page (parent.page_id)")
    ds = _new_data_source(ctx, s, title, target["id"], _schema(properties))
    return {"database_url": ds["database_url"], "data_source_url": ds["url"], "id": ds["id"], "schema": ds["schema"]}


@tool("notion-query-data-sources", read_only=True)
def notion_query_data_sources(ctx: Instance,
                              data_source_url: Annotated[str, "The data source to query (collection://... or ID)"],
                              mode: Annotated[Literal["rows", "sql", "view"] | None, "Query mode (this simulator supports 'rows')"] = "rows",
                              filter: Annotated[dict | None, "Property equality filters, e.g. {\"Status\": \"In progress\", \"Tags\": \"billing\"}"] = None,
                              sort: Annotated[dict | None, "Sort: {\"property\": \"Due\", \"direction\": \"ascending\"}"] = None,
                              query: Annotated[str | None, "SQL query (mode 'sql')"] = None,
                              limit: Annotated[int | None, "Maximum rows (default 25)"] = 25) -> dict[str, Any]:
    """Query rows of a Notion database (data source)"""
    s = ctx.state
    if mode not in (None, "rows"):
        raise _err(f"mode '{mode}' is not supported by this simulator version; use mode 'rows'", "unsupported")
    ds = s["data_sources"].get(_resolve_id(data_source_url))
    if ds is None or ds["in_trash"]:
        raise _not_found(f"data source: {data_source_url}")
    rows = [p for p in s["pages"].values() if p["parent"] == {"type": "data_source_id", "id": ds["id"]} and not p["in_trash"]]
    views = [_props_view(s, p) | {"id": p["id"], "url": p["url"]} for p in rows]
    for prop, want in (filter or {}).items():
        if prop not in ds["schema"]:
            raise _err(f"Could not find property with name or id: {prop}")
        def ok(v: dict[str, Any], prop: str = prop, want: Any = want) -> bool:
            have = v.get(prop)
            return want in have if isinstance(have, list) else str(have).lower() == str(want).lower()
        views = [v for v in views if ok(v)]
    if sort:
        prop = sort.get("property")
        views.sort(key=lambda v: (v.get(prop) is None, str(v.get(prop))), reverse=sort.get("direction") == "descending")
    lim = max(1, min(limit or 25, 100))
    return {"results": views[:lim], "has_more": len(views) > lim}


@tool("notion-create-comment")
def notion_create_comment(ctx: Instance,
                          page_id: Annotated[str, "The page to comment on"],
                          content: Annotated[str, "Comment text (Markdown)"],
                          block_id: Annotated[str | None, "Comment on a specific block instead of the whole page"] = None) -> dict[str, Any]:
    """Add a comment to a Notion page"""
    s = ctx.state
    p = _page(s, page_id)
    if not content.strip():
        raise _err("Comment content can't be empty.")
    c = {"id": _id(ctx), "discussion_id": _id(ctx), "block_id": block_id, "content": content, "created_by": s["me"], "created_time": _iso(ctx),
         "resolved": False}
    s["comments"][p["id"]].append(c)
    return c


@tool("notion-get-comments", read_only=True)
def notion_get_comments(ctx: Instance, page_id: Annotated[str, "The page whose comments to fetch"]) -> dict[str, Any]:
    """Get all comments and discussions on a Notion page"""
    s = ctx.state
    p = _page(s, page_id)
    return {"comments": [{**c, "author": s["users"][c["created_by"]]["name"]} for c in s["comments"][p["id"]]]}


@tool("notion-get-users", read_only=True)
def notion_get_users(ctx: Instance,
                     query: Annotated[str | None, "Filter by name or email"] = None,
                     limit: Annotated[int | None, "Maximum results"] = 100) -> dict[str, Any]:
    """List users in the Notion workspace"""
    us = [u for u in ctx.state["users"].values() if not query or query.lower() in f"{u['name']} {u['email']}".lower()]
    return {"results": us[: max(1, limit or 100)]}


@tool("notion-get-teams", read_only=True)
def notion_get_teams(ctx: Instance, query: Annotated[str | None, "Filter teamspaces by name"] = None) -> dict[str, Any]:
    """List teamspaces in the Notion workspace"""
    ts = [t for t in ctx.state["teams"].values() if not query or query.lower() in t["name"].lower()]
    return {"teams": ts}


Notion.tools = [notion_search, notion_fetch, notion_create_pages, notion_update_page, notion_move_pages,
                notion_duplicate_page, notion_create_database, notion_query_data_sources, notion_create_comment,
                notion_get_comments, notion_get_users, notion_get_teams]


# -- world actions (triggered by environments, never by agents) --------------------------

def _titled(state: dict[str, Any], title: str) -> dict[str, Any]:
    p = next((p for p in state["pages"].values() if p["title"] == title), None)
    if p is None:
        raise _not_found(f"page titled {title!r}")
    return p


@action("edit_page")
def act_edit_page(ctx: Instance, title: str, content: str) -> None:
    """A teammate rewrites a page (what the agent read earlier is now stale)."""
    p = _titled(ctx.state, title)
    p["content"], p["last_edited_time"] = content, _iso(ctx)


@action("restrict_page")
def act_restrict_page(ctx: Instance, title: str, restricted: bool = True) -> None:
    """A page's permissions change: it disappears for everyone without access."""
    _titled(ctx.state, title)["restricted"] = restricted


Notion.actions = [act_edit_page, act_restrict_page]
