"""drive: the Google Drive REST API (v3), as ``gog``, Google's client libraries and ``curl`` see it.

What real clients depend on: ``files.get`` returns only kind/id/name/mimeType unless ``fields``
asks for more; ``files.list`` includes trashed files unless ``q`` excludes them and rejects bare
words in ``q``; Google Docs can't be downloaded, only exported; uploads come as multipart or media
(or resumable) requests; sharing is ``permissions``. Sheets (v4) and Docs (v1) live in
``api_sheets`` and ``api_docs``.
"""

from __future__ import annotations

import base64
import hashlib
import re
import urllib.parse
from typing import Any

from ...api import Request, Response, operation
from ...api.google import error, page, select
from ...core.instance import Instance
from .files import _check_external
from .model import FOLDER, ROLES, TYPES, _file, _grant, _iso, _mail_share, _my_role, _new_file
from .query import _drive_query, _eval

HOSTS = ("www.googleapis.com", "drive.googleapis.com")
BASE = "/drive/v3"
DEFAULT_FILE = "kind,id,name,mimeType"
EXPORTS = {
    TYPES["doc"]: {"text/plain", "text/html", "application/pdf", "text/markdown", "application/rtf",
                   "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                   "application/vnd.oasis.opendocument.text", "application/epub+zip", "application/zip"},
    TYPES["sheet"]: {"text/csv", "text/tab-separated-values", "application/pdf", "application/zip",
                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     "application/x-vnd.oasis.opendocument.spreadsheet"},
    TYPES["slides"]: {"application/pdf", "text/plain",
                      "application/vnd.openxmlformats-officedocument.presentationml.presentation"},
}
EXPORT_LINK = {TYPES["doc"]: ("document", ["pdf", "docx", "txt", "html", "md"]),
               TYPES["sheet"]: ("spreadsheets", ["pdf", "xlsx", "csv"]),
               TYPES["slides"]: ("presentation", ["pdf", "pptx", "txt"])}


def op(op_id: str, method: str, path: str, **kw: Any):  # noqa: ANN201
    return operation(op_id, method, BASE + path, hosts=HOSTS, **kw)


def upload(op_id: str, method: str, path: str, **kw: Any):  # noqa: ANN201
    return operation(op_id, method, "/upload" + BASE + path, hosts=HOSTS, **kw)


# -- files as Drive returns them -----------------------------------------------------------------

def _name(state: dict[str, Any], email: str) -> str:
    return (state.get("names") or {}).get(email) or email.split("@")[0].replace(".", " ").title()


def _perm_id(email: str) -> str:
    return str(int(hashlib.sha1(email.lower().encode()).hexdigest(), 16))[:20]


def _user(state: dict[str, Any], email: str) -> dict[str, Any]:
    return {"kind": "drive#user", "displayName": _name(state, email),
            "photoLink": "https://lh3.googleusercontent.com/a/default-user=s64", "me": email == state["me"],
            "permissionId": _perm_id(email), "emailAddress": email}


def _permission(state: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
    out = {"kind": "drive#permission", "id": _perm_id(p["emailAddress"]) if p.get("emailAddress") else p["id"],
           "type": p["type"], "role": p["role"]}
    if p.get("emailAddress"):
        out.update(emailAddress=p["emailAddress"], displayName=_name(state, p["emailAddress"]), deleted=False,
                   pendingOwner=False)
    return out


def _link_permission(state: dict[str, Any], f: dict[str, Any]) -> dict[str, Any] | None:
    if f["link_sharing"] == "anyone_with_link":
        return {"kind": "drive#permission", "id": "anyoneWithLink", "type": "anyone", "role": f["link_role"],
                "allowFileDiscovery": False}
    if f["link_sharing"] == "domain":
        return {"kind": "drive#permission", "id": _perm_id("domain:" + state["domain"]), "type": "domain",
                "role": f["link_role"], "domain": state["domain"], "allowFileDiscovery": False}
    return None


def _file_json(state: dict[str, Any], f: dict[str, Any]) -> dict[str, Any]:
    """Every field Drive can return; ``fields`` picks from these (default: kind,id,name,mimeType)."""
    role = _my_role(state, f) or "reader"
    rank = ROLES.index(role)
    folder = f["mimeType"] == FOLDER
    native = f["mimeType"].startswith("application/vnd.google-apps.")
    out: dict[str, Any] = {
        "kind": "drive#file", "id": f["id"], "name": f["name"], "mimeType": f["mimeType"],
        "starred": f["starred"], "trashed": f["trashed"], "explicitlyTrashed": f["trashed"], "parents": list(f["parents"]),
        "spaces": ["drive"], "version": str(len(f["modifiedTime"]) * 7 + int(hashlib.sha1(f["modifiedTime"].encode())
                                                                            .hexdigest(), 16) % 100),
        "webViewLink": f["webViewLink"],
        "iconLink": f"https://drive-thirdparty.googleusercontent.com/16/type/{f['mimeType']}",
        "hasThumbnail": not folder, "viewedByMe": True, "viewedByMeTime": f["modifiedTime"],
        "createdTime": f["createdTime"], "modifiedTime": f["modifiedTime"],
        "modifiedByMe": f["owner"] == state["me"], "owners": [_user(state, f["owner"])],
        "lastModifyingUser": _user(state, f["owner"]), "shared": len(f["permissions"]) > 1 or f["link_sharing"] != "restricted",
        "ownedByMe": f["owner"] == state["me"],
        "capabilities": {"canEdit": rank >= 2 and not folder, "canComment": rank >= 1, "canShare": rank >= 2,
                         "canCopy": not folder, "canDownload": True, "canDelete": role == "owner",
                         "canTrash": role == "owner", "canUntrash": role == "owner", "canRename": rank >= 2,
                         "canAddChildren": folder and rank >= 2, "canListChildren": folder,
                         "canRemoveChildren": folder and rank >= 2, "canReadRevisions": rank >= 2,
                         "canModifyContent": rank >= 2, "canMoveItemWithinDrive": rank >= 2},
        "viewersCanCopyContent": True, "copyRequiresWriterPermission": False, "writersCanShare": True,
        "permissionIds": [_perm_id(p["emailAddress"]) for p in f["permissions"] if p.get("emailAddress")],
        "isAppAuthorized": False, "quotaBytesUsed": str(f["size"] or 0),
    }
    if f.get("description"):
        out["description"] = f["description"]
    if rank >= 2:
        out["permissions"] = [_permission(state, p) for p in f["permissions"]]
        link = _link_permission(state, f)
        if link:
            out["permissions"].append(link)
    if not folder and f["size"] is not None:
        out["size"] = str(f["size"])
    if not native and not folder:
        data = _bytes(f)
        out["md5Checksum"] = hashlib.md5(data).hexdigest()
        out["webContentLink"] = f"https://drive.google.com/uc?id={f['id']}&export=download"
        ext = f["name"].rsplit(".", 1)[-1] if "." in f["name"] else None
        if ext:
            out.update(fileExtension=ext, fullFileExtension=ext, originalFilename=f["name"])
        out["headRevisionId"] = "0B" + hashlib.sha1(data).hexdigest()[:30]
    if f["mimeType"] in EXPORT_LINK:
        kind, fmts = EXPORT_LINK[f["mimeType"]]
        out["exportLinks"] = {_mime(fmt): f"https://docs.google.com/feeds/download/{kind}/Export?id={f['id']}"
                              f"&exportFormat={fmt}" for fmt in fmts}
    return out


def _mime(fmt: str) -> str:
    return {"pdf": "application/pdf", "txt": "text/plain", "html": "text/html", "md": "text/markdown",
            "csv": "text/csv", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}[fmt]


def _bytes(f: dict[str, Any]) -> bytes:
    if f.get("data"):
        return base64.b64decode(f["data"])
    if f["content"] is not None:
        return f["content"].encode()
    size = int(f["size"] or 0)
    seed = hashlib.sha256(f["id"].encode()).digest()
    head = b"%PDF-1.4\n" if f["mimeType"] == "application/pdf" else b""
    return (head + seed * (size // 32 + 1))[:max(size, len(head))]


def _out(state: dict[str, Any], f: dict[str, Any], req: Request, default: str = DEFAULT_FILE) -> Any:
    return select(_file_json(state, f), req.arg("fields") or default)


def _get(ctx: Instance, req: Request, need: str = "reader", key: str = "fileId") -> dict[str, Any]:
    return _file(ctx.state, urllib.parse.unquote(req.params[key]), need=need)


# -- listing --------------------------------------------------------------------------------------

_OPERATORS = re.compile(r"\b(contains|in|and|or|not|has)\b|[=<>]")


def _query(state: dict[str, Any], q: str) -> Any:
    if not _OPERATORS.search(q):  # the REST API has no bare-word search, unlike the MCP server
        raise error(400, "Invalid Value", "invalid", location="q")
    q2 = re.sub(r"'([^'\\]*(?:\\.[^'\\]*)*)'\s+in\s+(owners|writers|readers)",
                lambda m: f"{m.group(2)} contains '{m.group(1)}'", q)
    q2 = re.sub(r"\bsharedWithMe\b(\s*=\s*true)?", "sharedWithMe = 'true'", q2)
    try:
        return _drive_query(state, q2)
    except Exception:
        raise error(400, "Invalid Value", "invalid", location="q") from None


def _match(state: dict[str, Any], node: Any, f: dict[str, Any]) -> bool:
    kind = node[0]
    if kind == "and":
        return _match(state, node[1], f) and _match(state, node[2], f)
    if kind == "or":
        return _match(state, node[1], f) or _match(state, node[2], f)
    if kind == "not":
        return not _match(state, node[1], f)
    _, field, val = node
    if field in ("owners", "writers", "readers") and kind == "contains":
        who = state["me"] if str(val) == "me" else str(val).lower()
        roles = {"owners": ("owner",), "writers": ("owner", "writer"),
                 "readers": ("owner", "writer", "commenter", "reader")}[field]
        return any(p.get("emailAddress", "").lower() == who and p["role"] in roles for p in f["permissions"])
    if field == "sharedWithMe":
        return f["owner"] != state["me"] and _my_role(state, f) is not None
    if field == "visibility":
        vis = {"restricted": "limited", "domain": "domainWithLink", "anyone_with_link": "anyoneWithLink"}
        return vis[f["link_sharing"]] == val
    try:
        return _eval(node, f)
    except Exception:
        raise error(400, "Invalid Value", "invalid", location="q") from None


def _order(files: list[dict[str, Any]], order: str | None) -> list[dict[str, Any]]:
    keys = [k.strip() for k in (order or "").split(",") if k.strip()]
    for k in reversed(keys or ["folder", "name"]) if order else []:
        field, _, desc = k.partition(" ")
        rev = desc.strip().lower() == "desc"
        get = {"folder": lambda f: f["mimeType"] != FOLDER, "name": lambda f: f["name"].lower(),
               "name_natural": lambda f: f["name"].lower(), "modifiedTime": lambda f: f["modifiedTime"],
               "createdTime": lambda f: f["createdTime"], "starred": lambda f: not f["starred"],
               "quotaBytesUsed": lambda f: f["size"] or 0, "viewedByMeTime": lambda f: f["modifiedTime"],
               "modifiedByMeTime": lambda f: f["modifiedTime"], "recency": lambda f: f["modifiedTime"]}.get(field)
        if get is None:
            raise error(400, f"Invalid Value: orderBy {field}", "invalid", location="orderBy")
        files.sort(key=get, reverse=rev)
    return files


@op("drive.files.list", "GET", "/files", read_only=True)
def files_list(ctx: Instance, req: Request) -> Any:
    s = ctx.search_view("files")
    q = req.arg("q")
    tree = _query(s, q) if q else None
    files = [f for f in s["files"].values() if f["id"] != "root" and f["parents"] != [] and _my_role(s, f)
             and (tree is None or _match(s, tree, f))]
    order = req.arg("orderBy")
    files = _order(files, order) if order else sorted(files, key=lambda f: f["modifiedTime"], reverse=True)
    chunk, token = page(files, req, size="pageSize", default=100, maximum=1000)
    out: dict[str, Any] = {"kind": "drive#fileList", "incompleteSearch": False,
                           "files": [_file_json(s, f) for f in chunk]}
    if token:
        out["nextPageToken"] = token
    return select(out, req.arg("fields") or "kind,incompleteSearch,nextPageToken,files(" + DEFAULT_FILE + ")")


@op("drive.files.get", "GET", "/files/{fileId}", read_only=True)
def files_get(ctx: Instance, req: Request) -> Any:
    f = _get(ctx, req)
    if req.arg("alt") == "media":
        if f["mimeType"].startswith("application/vnd.google-apps."):
            raise error(403, "Only files with binary content can be downloaded. Use Export with Docs Editors files.",
                        "fileNotDownloadable", location="alt")
        return Response(200, _bytes(f), {"content-type": f["mimeType"]})
    return _out(ctx.state, f, req)


@op("drive.files.export", "GET", "/files/{fileId}/export", read_only=True)
def files_export(ctx: Instance, req: Request) -> Any:
    f = _get(ctx, req)
    want = req.arg("mimeType")
    if not want:
        raise error(400, "Required parameter: mimeType", "required", location="mimeType")
    if f["mimeType"] not in EXPORTS:
        raise error(403, "Export only supports Docs Editors files.", "fileNotExportable")
    if want not in EXPORTS[f["mimeType"]]:
        raise error(400, "The requested conversion is not supported.", "badRequest", location="convertTo")
    return Response(200, _export(ctx, f, want), {"content-type": want})


def _export(ctx: Instance, f: dict[str, Any], want: str) -> bytes:
    text = f["content"] or ""
    if f["mimeType"] == TYPES["sheet"] and want == "text/tab-separated-values":
        import csv
        import io
        rows = list(csv.reader(io.StringIO(text)))
        return "\n".join("\t".join(r) for r in rows).encode()
    if want in ("text/plain", "text/csv", "text/markdown"):
        return ("\ufeff" + text if want == "text/plain" and f["mimeType"] == TYPES["doc"] else text).encode()
    if want == "text/html":
        from html import escape
        paras = "".join(f"<p>{escape(line)}</p>" for line in text.splitlines())
        return f"<html><head><meta content=\"text/html; charset=UTF-8\"></head><body>{paras}</body></html>".encode()
    if want == "application/pdf":
        return b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<</Type/Catalog>>endobj\n% " + text.encode()[:2000] + b"\n%%EOF\n"
    return b"PK\x03\x04" + hashlib.sha256(text.encode()).digest() + text.encode()[:2000]  # an office/zip container


# -- creating and changing ------------------------------------------------------------------------

def _media(req: Request) -> tuple[dict[str, Any], bytes | None]:
    """(metadata, content) from a simple, multipart or media request."""
    body = req.body
    if isinstance(body, (bytes, bytearray)):
        return {}, bytes(body)
    b = dict(req.json())
    media = b.pop("__media__", None)
    b.pop("__media_type__", None)
    return b, (base64.b64decode(media) if media is not None else None)


def _media_type(req: Request) -> str:
    if isinstance(req.body, dict) and req.body.get("__media_type__"):
        return req.body["__media_type__"].split(";")[0]
    return (req.headers.get("content-type") or "application/octet-stream").split(";")[0]


def _store_content(f: dict[str, Any], data: bytes, mime: str) -> None:
    text = None
    if mime.startswith("text/") or mime in ("application/json", "application/xml") or \
            f["mimeType"].startswith("application/vnd.google-apps."):
        try:
            text = data.decode()
        except UnicodeDecodeError:
            text = None
    f.pop("data", None)
    if text is not None:
        f["content"] = text
    else:
        f["content"] = None
        f["data"] = base64.b64encode(data).decode()
    f["size"] = len(data)


def _parent(ctx: Instance, pid: str) -> dict[str, Any]:
    p = _file(ctx.state, pid, need="writer", allow_trashed=False)
    if p["mimeType"] != FOLDER:
        raise error(400, "The specified parent is not a folder.", "invalidParent")
    return p


def _create(ctx: Instance, req: Request) -> Any:
    s = ctx.state
    meta, data = _media(req)
    parents = meta.get("parents") or ["root"]
    if len(parents) > 1:
        raise error(403, "Increasing the number of parents is not allowed.", "cannotAddParent")
    parent = _parent(ctx, parents[0])
    source_mime = _media_type(req) if data is not None else "application/octet-stream"
    mime = meta.get("mimeType") or (source_mime if data is not None else "application/octet-stream")
    if data is None and mime != FOLDER and not mime.startswith("application/vnd.google-apps."):
        mime = meta.get("mimeType") or "application/octet-stream"
    name = meta.get("name") or ("Untitled" if data is None else "Untitled")
    f = _new_file(ctx, s, name, mime, parent["id"], owner=s["me"], content="" if data is None and mime != FOLDER else None)
    if mime == FOLDER:
        f["content"], f["size"] = None, None
    if data is not None:
        _store_content(f, data, source_mime if not mime.startswith("application/vnd.google-apps.") else "text/plain")
    if mime == TYPES["sheet"]:
        from .sheets import _new_sheet, _sync_content
        f["sheets"] = [_new_sheet(0, "Sheet1", f["content"] or "")]
        _sync_content(f)
    for k in ("description", "starred"):
        if k in meta:
            f[k] = meta[k]
    return _out(s, f, req)


@op("drive.files.create", "POST", "/files")
def files_create(ctx: Instance, req: Request) -> Any:
    return _create(ctx, req)


@upload("drive.files.create", "POST", "/files")
def files_create_upload(ctx: Instance, req: Request) -> Any:
    kind = req.arg("uploadType") or "media"
    if kind == "resumable":
        return _begin_resumable(ctx, req)
    return _create(ctx, req)


def _begin_resumable(ctx: Instance, req: Request, file_id: str | None = None) -> Any:
    uid = "ABPtcP" + ctx.token(40, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
    ctx.state.setdefault("_uploads", {})[uid] = {"meta": req.json(), "file": file_id, "data": "",
                                                 "type": req.headers.get("x-upload-content-type", ""),
                                                 "fields": req.arg("fields")}
    path = f"/upload/drive/v3/files{'/' + file_id if file_id else ''}"
    return Response(200, b"", {"location": f"https://www.googleapis.com{path}?uploadType=resumable&upload_id={uid}",
                               "x-guploader-uploadid": uid})


def _resume(ctx: Instance, req: Request) -> tuple[dict[str, Any], Request] | Response:
    """A chunk of a resumable upload: 308 until the last byte arrives, then the finished request."""
    uploads = ctx.state.get("_uploads", {})
    uid = req.arg("upload_id") or ""
    up = uploads.get(uid)
    if up is None:
        raise error(404, "Not Found", "notFound")
    chunk = bytes(req.body) if isinstance(req.body, (bytes, bytearray)) else b""
    data = base64.b64decode(up["data"]) + chunk
    m = re.match(r"bytes (\d+)-(\d+)/(\d+|\*)", req.headers.get("content-range", ""))
    if m and m.group(3) != "*" and int(m.group(2)) + 1 < int(m.group(3)):
        up["data"] = base64.b64encode(data).decode()
        return Response(308, b"", {"range": f"bytes=0-{len(data) - 1}"})
    uploads.pop(uid)
    query = {"fields": [up["fields"]]} if up.get("fields") else {}
    return up, Request("POST", req.path, {"fileId": up["file"]} if up["file"] else {}, query,
                       {**up["meta"], "__media__": base64.b64encode(data).decode(),
                        "__media_type__": up["type"] or "application/octet-stream"}, {})


@upload("drive.files.create", "PUT", "/files")
def files_resumable_put(ctx: Instance, req: Request) -> Any:
    done = _resume(ctx, req)
    return done if isinstance(done, Response) else _create(ctx, done[1])


@upload("drive.files.update", "PUT", "/files/{fileId}")
def files_resumable_put_update(ctx: Instance, req: Request) -> Any:
    done = _resume(ctx, req)
    return done if isinstance(done, Response) else _update(ctx, done[1])


def _update(ctx: Instance, req: Request) -> Any:
    s = ctx.state
    meta, data = _media(req)
    f = _get(ctx, req)
    only_star = set(meta) <= {"starred"} and data is None and not req.arg("addParents") and not req.arg("removeParents")
    if not only_star:
        need = "owner" if meta.get("trashed") is not None else "writer"
        _get(ctx, req, need=need)
    for pid in [p for p in (req.arg("addParents") or "").split(",") if p]:
        _parent(ctx, pid)
        if pid not in f["parents"]:
            f["parents"].append(pid)
    for pid in [p for p in (req.arg("removeParents") or "").split(",") if p]:
        if pid in f["parents"]:
            f["parents"].remove(pid)
    if len(f["parents"]) > 1:
        raise error(403, "Increasing the number of parents is not allowed.", "cannotAddParent")
    for k in ("name", "description", "starred", "trashed"):
        if k in meta:
            f[k] = meta[k]
    if "mimeType" in meta and meta["mimeType"] != f["mimeType"]:
        raise error(403, "The user does not have sufficient permissions for this file.", "insufficientFilePermissions")
    if data is not None:
        if f["mimeType"] == FOLDER:
            raise error(400, "Folders have no content.", "invalid")
        _store_content(f, data, _media_type(req) if _media_type(req) != "application/octet-stream" else f["mimeType"])
        if f["mimeType"] == TYPES["sheet"]:
            from .sheets import _new_sheet, _sync_content
            f["sheets"] = [_new_sheet(0, "Sheet1", f["content"] or "")]
            _sync_content(f)
    f["modifiedTime"] = _iso(ctx)
    return _out(s, f, req)


@op("drive.files.update", "PATCH", "/files/{fileId}")
def files_update(ctx: Instance, req: Request) -> Any:
    return _update(ctx, req)


@upload("drive.files.update", "PATCH", "/files/{fileId}")
def files_update_upload(ctx: Instance, req: Request) -> Any:
    if req.arg("uploadType") == "resumable":
        return _begin_resumable(ctx, req, req.params["fileId"])
    return _update(ctx, req)


@op("drive.files.copy", "POST", "/files/{fileId}/copy")
def files_copy(ctx: Instance, req: Request) -> Any:
    s = ctx.state
    src = _get(ctx, req)
    if src["mimeType"] == FOLDER:
        raise error(403, "This file cannot be copied by the user.", "cannotCopyFile")
    b = req.json()
    parents = b.get("parents") or [next((p for p in src["parents"] if _my_role(s, s["files"].get(p, src))
                                         in ("writer", "owner")), "root")]
    parent = _parent(ctx, parents[0])
    f = _new_file(ctx, s, b.get("name") or f"Copy of {src['name']}", src["mimeType"], parent["id"], owner=s["me"],
                  content=src["content"], size=src["size"])
    if src.get("data"):
        f["data"] = src["data"]
    if "sheets" in src:
        import copy
        f["sheets"] = copy.deepcopy(src["sheets"])
    if b.get("description"):
        f["description"] = b["description"]
    return _out(s, f, req)


@op("drive.files.emptyTrash", "DELETE", "/files/trash", destructive=True)
def files_empty_trash(ctx: Instance, req: Request) -> Any:
    s = ctx.state
    for fid in [f["id"] for f in s["files"].values() if f["trashed"] and f["owner"] == s["me"]]:
        s["files"].pop(fid, None)
    return Response(204)


@op("drive.files.delete", "DELETE", "/files/{fileId}", destructive=True)
def files_delete(ctx: Instance, req: Request) -> Any:
    s = ctx.state
    f = _get(ctx, req)
    if _my_role(s, f) != "owner":
        raise error(403, "The user does not have sufficient permissions for this file.", "insufficientFilePermissions")
    doomed = [f["id"]]
    while True:  # deleting a folder deletes what's inside it
        more = [x["id"] for x in s["files"].values() if set(x["parents"]) & set(doomed) and x["id"] not in doomed]
        if not more:
            break
        doomed += more
    for fid in doomed:
        s["files"].pop(fid, None)
    return Response(204)


# -- sharing ----------------------------------------------------------------------------------------

@op("drive.permissions.list", "GET", "/files/{fileId}/permissions", read_only=True)
def permissions_list(ctx: Instance, req: Request) -> Any:
    s = ctx.state
    f = _get(ctx, req)
    if _my_role(s, f) not in ("writer", "owner"):
        raise error(403, "The user does not have sufficient permissions for this file.", "insufficientFilePermissions")
    perms = [_permission(s, p) for p in f["permissions"]]
    link = _link_permission(s, f)
    if link:
        perms.append(link)
    return select({"kind": "drive#permissionList", "permissions": perms},
                  req.arg("fields") or "kind,nextPageToken,permissions(kind,id,type,role)")


def _find_perm(s: dict[str, Any], f: dict[str, Any], pid: str) -> dict[str, Any] | None:
    return next((p for p in f["permissions"] if p.get("emailAddress") and _perm_id(p["emailAddress"]) == pid), None)


@op("drive.permissions.get", "GET", "/files/{fileId}/permissions/{permissionId}", read_only=True)
def permissions_get(ctx: Instance, req: Request) -> Any:
    s = ctx.state
    f = _get(ctx, req)
    pid = req.params["permissionId"]
    p = _find_perm(s, f, pid)
    if p is None:
        link = _link_permission(s, f)
        if link and link["id"] == pid:
            return link
        raise error(404, f"Permission not found: {pid}.", "notFound", location="permissionId")
    return select(_permission(s, p), req.arg("fields") or "kind,id,type,role")


@op("drive.permissions.create", "POST", "/files/{fileId}/permissions")
def permissions_create(ctx: Instance, req: Request) -> Any:
    s = ctx.state
    f = _get(ctx, req, need="writer")
    b = req.json()
    typ, role = b.get("type"), b.get("role")
    if typ not in ("user", "group", "domain", "anyone"):
        raise error(400, "The permission type field is required.", "required", location="permission.type")
    if role not in ("owner", "organizer", "fileOrganizer", "writer", "commenter", "reader"):
        raise error(400, "The permission role field is required.", "required", location="permission.role")
    notify = req.bool_arg("sendNotificationEmail", default=typ in ("user", "group"))
    if typ in ("anyone", "domain"):
        if typ == "anyone" and not s["policy"]["external_sharing"]:
            raise error(403, "The user does not have sufficient permissions for this file.", "publishOutNotPermitted")
        if typ == "domain" and b.get("domain", s["domain"]).lower() != s["domain"]:
            raise error(403, "Sharing with that domain is not allowed.", "publishOutNotPermitted")
        f["link_sharing"] = "anyone_with_link" if typ == "anyone" else "domain"
        f["link_role"] = role
        f["modifiedTime"] = _iso(ctx)
        return select(_link_permission(s, f), req.arg("fields") or "kind,id,type,role")
    email = (b.get("emailAddress") or "").strip()
    if "@" not in email:
        raise error(400, "The permission emailAddress field is required.", "required", location="permission.emailAddress")
    try:
        _check_external(s, email)
    except Exception:
        raise error(403, f"Bad Request. User message: \"Sharing with {email} is not allowed: your administrator has "
                         f"restricted sharing outside {s['domain']}.\"", "shareOutNotPermitted") from None
    if role == "owner":
        if not req.bool_arg("transferOwnership"):
            raise error(403, "The transferOwnership parameter must be enabled when the permission role is 'owner'.",
                        "forbidden", location="transferOwnership")
        _get(ctx, req, need="owner")
        for p in f["permissions"]:
            if p["role"] == "owner":
                p["role"] = "writer"
        f["owner"] = email.lower()
    p = _grant(ctx, s, f, typ, role, email.lower())
    if notify and role != "owner" and s.get("_v3"):
        _mail_share(ctx, f, email.lower(), role, b.get("emailMessage") or req.arg("emailMessage"))
    return select(_permission(s, p), req.arg("fields") or "kind,id,type,role")


@op("drive.permissions.update", "PATCH", "/files/{fileId}/permissions/{permissionId}")
def permissions_update(ctx: Instance, req: Request) -> Any:
    s = ctx.state
    f = _get(ctx, req, need="writer")
    pid, b = req.params["permissionId"], req.json()
    if pid in ("anyoneWithLink",) or (_link_permission(s, f) or {}).get("id") == pid:
        f["link_role"] = b.get("role", f["link_role"])
        return _link_permission(s, f)
    p = _find_perm(s, f, pid)
    if p is None:
        raise error(404, f"Permission not found: {pid}.", "notFound", location="permissionId")
    if p["role"] == "owner":
        raise error(403, "The owner of a file cannot be removed.", "cannotModifyOwner")
    p["role"] = b.get("role", p["role"])
    return select(_permission(s, p), req.arg("fields") or "kind,id,type,role")


@op("drive.permissions.delete", "DELETE", "/files/{fileId}/permissions/{permissionId}", destructive=True)
def permissions_delete(ctx: Instance, req: Request) -> Any:
    s = ctx.state
    f = _get(ctx, req, need="writer")
    pid = req.params["permissionId"]
    if (_link_permission(s, f) or {}).get("id") == pid:
        f["link_sharing"], f["link_role"] = "restricted", None
        return Response(204)
    p = _find_perm(s, f, pid)
    if p is None:
        raise error(404, f"Permission not found: {pid}.", "notFound", location="permissionId")
    if p["role"] == "owner":
        raise error(403, "The owner of a file cannot be removed.", "cannotRemoveOwner")
    f["permissions"].remove(p)
    return Response(204)


# -- account ----------------------------------------------------------------------------------------

@op("drive.about.get", "GET", "/about", read_only=True)
def about_get(ctx: Instance, req: Request) -> Any:
    s = ctx.state
    if not req.arg("fields"):
        raise error(400, "The 'fields' parameter is required for this method.", "required", location="fields")
    used = sum(int(f["size"] or 0) for f in s["files"].values() if f["owner"] == s["me"])
    trash = sum(int(f["size"] or 0) for f in s["files"].values() if f["owner"] == s["me"] and f["trashed"])
    return select({"kind": "drive#about", "user": _user(s, s["me"]),
                   "storageQuota": {"limit": str(15 * 1024**3), "usage": str(used), "usageInDrive": str(used),
                                    "usageInDriveTrash": str(trash)},
                   "maxUploadSize": str(5 * 1024**4), "appInstalled": False, "canCreateDrives": True,
                   "importFormats": {"text/plain": [TYPES["doc"]], "text/csv": [TYPES["sheet"]]},
                   "exportFormats": {k: sorted(v) for k, v in EXPORTS.items()}}, req.arg("fields"))


@op("drive.drives.list", "GET", "/drives", read_only=True)
def drives_list(ctx: Instance, req: Request) -> Any:
    return {"kind": "drive#driveList", "drives": []}


@op("drive.changes.getStartPageToken", "GET", "/changes/startPageToken", read_only=True)
def start_page_token(ctx: Instance, req: Request) -> Any:
    return {"kind": "drive#startPageToken", "startPageToken": str(len(ctx.state["files"]) * 13 + 1000)}


@op("drive.revisions.list", "GET", "/files/{fileId}/revisions", read_only=True)
def revisions_list(ctx: Instance, req: Request) -> Any:
    s = ctx.state
    f = _get(ctx, req, need="writer")
    rev = {"kind": "drive#revision", "id": "1", "mimeType": f["mimeType"], "modifiedTime": f["modifiedTime"],
           "keepForever": False, "published": False, "lastModifyingUser": _user(s, f["owner"])}
    return select({"kind": "drive#revisionList", "revisions": [rev]}, req.arg("fields"))


@op("drive.comments.list", "GET", "/files/{fileId}/comments", read_only=True)
def comments_list(ctx: Instance, req: Request) -> Any:
    _get(ctx, req)
    if not req.arg("fields"):
        raise error(400, "The 'fields' parameter is required for this method.", "required", location="fields")
    return select({"kind": "drive#commentList", "comments": []}, req.arg("fields"))


from . import api_docs, api_sheets  # noqa: E402,F401  (register the Sheets and Docs surfaces)
