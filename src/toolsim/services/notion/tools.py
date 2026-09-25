"""notion: agent-facing tools."""

from __future__ import annotations

import copy
from typing import Annotated, Any, Literal

from ...core.instance import Instance
from ...core.tools import tool
from .model import (
    V1,
    _access,
    _coerce_props,
    _err,
    _find_selection,
    _id,
    _iso,
    _need,
    _new_data_source,
    _new_page,
    _new_row,
    _not_found,
    _page,
    _path,
    _props_view,
    _resolve_id,
    _schema,
    _url,
    _visible,
)


@tool("notion-search", read_only=True)
def notion_search(ctx: Instance,
                  query: Annotated[str, "Semantic search query over your workspace"],
                  query_type: Annotated[Literal["internal", "user"] | None, "'internal' searches pages and databases; 'user' searches people"] = "internal",
                  filters: Annotated[dict | None, "Optional filters, e.g. {\"created_by_user_ids\": [...], \"created_date_range\": {...}}"] = None,
                  location: Annotated[str | None, "Restrict results to a page (URL or ID) and its children"] = None,
                  sort: Annotated[str | None, "Sort order, e.g. 'last_edited'"] = None,
                  limit: Annotated[int | None, "Maximum results (default 10)"] = 10) -> dict[str, Any]:
    """Search the Notion workspace for pages and databases (or users)"""
    s = ctx.search_view("pages")
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
    if s.get("_v1"):
        return {"metadata": {"type": "page", "access": _access(s, p)}, "title": p["title"], "url": p["url"],
                "text": f'<page url="{p["url"]}">\n<ancestor-path>{" / ".join(_path(s, p))}</ancestor-path>\n'
                        f"<properties>\n{_json.dumps(_props_view(s, p))}\n</properties>\n<content>\n{body}\n</content>\n</page>"}
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
        if s["pages"].get(ds["parent_page_id"]):
            _need(s, s["pages"][ds["parent_page_id"]], "edit")
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
    if s.get("_v1"):  # like Notion: the copy appears at once, its content a little later
        parent = s["pages"].get(src["parent"]["id"]) if src["parent"]["type"] == "page_id" else None
        if parent is not None:
            _need(s, parent, "edit")
        p = _new_page(ctx, s, src["title"], copy.deepcopy(src["parent"]), "", copy.deepcopy(src["properties"]),
                      icon=src["icon"], team=src["team"])
        ctx.schedule(ctx.rng.randint(10, 45), "finish_duplicate", {"source": src["id"], "copy": p["id"]},
                     reason="duplication finishes")
        return {"page_id": p["id"], "url": p["url"], "status": "in_progress",
                "message": "Duplication started. The page content will appear once it completes."}
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


@tool("notion-query-data-sources", read_only=True, until=V1)
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


_OPS = {"equals", "does_not_equal", "contains", "does_not_contain", ">", ">=", "<", "<=", "before", "after",
        "on_or_before", "on_or_after", "is_empty", "is_not_empty"}
_OPS_FOR = {"number": {"equals", "does_not_equal", ">", ">=", "<", "<=", "is_empty", "is_not_empty"},
            "date": {"equals", "before", "after", "on_or_before", "on_or_after", "is_empty", "is_not_empty"},
            "checkbox": {"equals", "does_not_equal"},
            "multi_select": {"contains", "does_not_contain", "is_empty", "is_not_empty"},
            "people": {"contains", "does_not_contain", "is_empty", "is_not_empty"},
            "relation": {"contains", "does_not_contain", "is_empty", "is_not_empty"}}


def _filter_ok(schema: dict[str, Any], prop: str, have: Any, cond: Any) -> bool:
    typ = schema[prop]["type"]
    shorthand = "contains" if typ in ("multi_select", "people", "relation") else "equals"
    conds = cond if isinstance(cond, dict) else {shorthand: cond}
    for op, want in conds.items():
        if op not in _OPS:
            raise _err(f"Invalid filter operator '{op}'. Use one of: {', '.join(sorted(_OPS))}")
        allowed = _OPS_FOR.get(typ, {"equals", "does_not_equal", "contains", "does_not_contain", "is_empty", "is_not_empty"})
        if op not in allowed:
            raise _err(f"Invalid filter for property '{prop}' of type {typ}: '{op}' is not supported "
                       f"(supported: {', '.join(sorted(allowed))})")
        empty = have in (None, "", [])
        if op == "is_empty":
            ok = empty == bool(want)
        elif op == "is_not_empty":
            ok = (not empty) == bool(want)
        elif empty:
            ok = op in ("does_not_equal", "does_not_contain")
        elif typ == "number":
            try:
                w = float(want)
            except (TypeError, ValueError):
                raise _err(f"Filter value for {prop} must be a number") from None
            h = float(have)
            ok = {"equals": h == w, "does_not_equal": h != w, ">": h > w, ">=": h >= w, "<": h < w, "<=": h <= w}[op]
        elif typ == "date":
            h, w = str(have)[:10], str(want)[:10]
            ok = {"equals": h == w, "before": h < w, "after": h > w, "on_or_before": h <= w, "on_or_after": h >= w}[op]
        elif typ == "checkbox":
            w = want if isinstance(want, bool) else str(want).lower() in ("true", "yes", "1", "__yes__")
            ok = (bool(have) == w) == (op == "equals")
        elif isinstance(have, list):
            hit = any(str(want).lower() == str(x).lower() for x in have)
            ok = hit if op in ("contains", "equals") else not hit
        else:
            h, w = str(have).lower(), str(want).lower()
            ok = {"equals": h == w, "does_not_equal": h != w, "contains": w in h, "does_not_contain": w not in h}[op]
        if not ok:
            return False
    return True


@tool("notion-query-data-sources", read_only=True, since=V1)
def notion_query_data_sources_v1(ctx: Instance,
                                 data_source_url: Annotated[str, "The data source to query (collection://... or ID)"],
                                 mode: Annotated[Literal["rows", "sql", "view"] | None, "Query mode (this simulator supports 'rows')"] = "rows",
                                 filter: Annotated[dict | None, "Filters per property: a value (equals) or operators, e.g. {\"Status\": \"Done\", \"Points\": {\">=\": 3}, \"Due\": {\"before\": \"2026-10-01\"}, \"Owner\": {\"is_empty\": true}}"] = None,
                                 sort: Annotated[dict | None, "Sort: {\"property\": \"Due\", \"direction\": \"ascending\"}"] = None,
                                 query: Annotated[str | None, "SQL query (mode 'sql')"] = None,
                                 limit: Annotated[int | None, "Maximum rows per page (default 25, max 100)"] = 25,
                                 start_cursor: Annotated[str | None, "next_cursor from a previous page"] = None) -> dict[str, Any]:
    """Query rows of a Notion database (data source)"""
    s = ctx.state
    if mode not in (None, "rows"):
        raise _err(f"mode '{mode}' is not supported by this simulator version; use mode 'rows'", "unsupported")
    ds = s["data_sources"].get(_resolve_id(data_source_url))
    if ds is None or ds["in_trash"]:
        raise _not_found(f"data source: {data_source_url}")
    title_prop = next(n for n, sp in ds["schema"].items() if sp["type"] == "title")
    rows = sorted((p for p in s["pages"].values() if p["parent"] == {"type": "data_source_id", "id": ds["id"]}
                   and not p["in_trash"]), key=lambda p: p["created_time"])
    views = [_props_view(s, p) | {"id": p["id"], "url": p["url"]} for p in rows]
    for prop, cond in (filter or {}).items():
        if prop not in ds["schema"]:
            raise _err(f"Could not find property with name or id: {prop}")
        views = [v for v in views if _filter_ok(ds["schema"], prop, v.get(prop, v.get(title_prop) if prop == title_prop else None), cond)]
    if sort:
        prop = sort.get("property")
        if prop not in ds["schema"]:
            raise _err(f"Could not find sort property with name or id: {prop}")
        views.sort(key=lambda v: (v.get(prop) is None, v.get(prop) if isinstance(v.get(prop), (int, float)) else str(v.get(prop))),
                   reverse=sort.get("direction") == "descending")
    start = 0
    if start_cursor:
        start = next((n + 1 for n, v in enumerate(views) if v["id"] == start_cursor), -1)
        if start < 0:
            raise _err("start_cursor is invalid or expired")
    lim = max(1, min(limit or 25, 100))
    page = views[start:start + lim]
    more = start + lim < len(views)
    return {"results": page, "has_more": more, "next_cursor": page[-1]["id"] if more and page else None}


@tool("notion-create-comment")
def notion_create_comment(ctx: Instance,
                          page_id: Annotated[str, "The page to comment on"],
                          content: Annotated[str, "Comment text (Markdown)"],
                          block_id: Annotated[str | None, "Comment on a specific block instead of the whole page"] = None) -> dict[str, Any]:
    """Add a comment to a Notion page"""
    s = ctx.state
    p = _page(s, page_id)
    _need(s, p, "comment")
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
