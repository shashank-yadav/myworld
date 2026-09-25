"""notion: state model, constants and helpers shared by the tools."""

from __future__ import annotations

import re
import uuid
from typing import Any

from ...core.instance import Instance
from ...core.tools import ToolError

V1 = "2026-09-25.1"
SEARCH_LAG = {"pages": 120}
ACCESS = ["view", "comment", "edit", "full"]
PROPERTY_TYPES = {"title", "text", "number", "select", "multi_select", "status", "date", "people", "checkbox", "url",
                  "email", "phone"}


def _err(message: str, code: str = "validation_error", status: int = 400) -> ToolError:
    return ToolError({"object": "error", "status": status, "code": code, "message": message}, status=status)


def _not_found(what: str) -> ToolError:
    return _err(f"Could not find {what}. Make sure the relevant pages and databases are shared with your integration.",
                "object_not_found", 404)

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
    if editable:
        _need(state, p, "edit")
    return p


def _access(state: dict[str, Any], p: dict[str, Any] | None) -> str:
    """Your access to a page: its own level, else its parent's (database rows: the database's page)."""
    seen = 0
    while p is not None and seen < 100:
        if p.get("access"):
            return p["access"]
        par = p["parent"]
        pid = state["data_sources"][par["id"]]["parent_page_id"] if par["type"] == "data_source_id" else par["id"]
        p = state["pages"].get(pid) if pid else None
        seen += 1
    return "full"


def _need(state: dict[str, Any], p: dict[str, Any], level: str) -> None:
    if state.get("_v1") and ACCESS.index(_access(state, p)) < ACCESS.index(level):
        raise _err(f"Insufficient permissions: you have '{_access(state, p)}' access to this page and need '{level}'.",
                   "restricted_resource", 403)


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
