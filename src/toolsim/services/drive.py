"""Google Drive.

Tool names and parameters follow the Google Workspace MCP server (taylorwilsdon/google_workspace_mcp,
Drive tools). The model follows the Drive API: files and folders with parents, per-file
permissions (owner, writer, commenter, reader), link sharing, trash, and Drive's query syntax.
Files shared with you as a reader can't be edited, and a workspace policy can block sharing
outside the company, both common agent failures.

Seed format::

    user: {email: alex@acme.com}
    domain: acme.com
    policy: {external_sharing: false}            # optional admin restriction
    folders: [{key: finance, name: Finance}, {key: q4, name: Q4, parent: finance}]
    files:
      - {name: Q4 budget, type: sheet, parent: q4, content: "team,amount\\neng,120000"}
      - {name: Vendor contract.pdf, mimeType: application/pdf, size: 482113,
         owner: legal@acme.com, shared_role: reader}  # someone else's file, shared with you
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from ..core.instance import Instance, Service
from ..core.tools import ToolError, tool

FOLDER = "application/vnd.google-apps.folder"
TYPES = {"doc": "application/vnd.google-apps.document", "sheet": "application/vnd.google-apps.spreadsheet",
         "slides": "application/vnd.google-apps.presentation", "folder": FOLDER, "text": "text/plain",
         "pdf": "application/pdf"}
EXPORTS = {TYPES["doc"]: {"pdf": "application/pdf", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                          "txt": "text/plain"},
           TYPES["sheet"]: {"pdf": "application/pdf", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            "csv": "text/csv"},
           TYPES["slides"]: {"pdf": "application/pdf", "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}}
ROLES = ["reader", "commenter", "writer", "owner"]


def _err(code: int, message: str, reason: str) -> ToolError:
    return ToolError({"error": {"code": code, "message": message, "errors": [{"reason": reason, "message": message}]}},
                     status=code)


def _not_found(fid: str) -> ToolError:
    return _err(404, f"File not found: {fid}.", "notFound")


def _forbidden(message: str = "The user does not have sufficient permissions for this file.") -> ToolError:
    return _err(403, message, "insufficientFilePermissions")


class Drive(Service):
    name = "drive"
    title = "Google Drive"
    description = "Simulated Google Drive. Behaves like the Google Workspace MCP server's Drive tools; nothing is really shared."
    versions = {"2026-09-25": "Initial release: 12 Drive tools modeled on taylorwilsdon/google_workspace_mcp."}

    def probe(self, ctx: Instance) -> None:
        c = ctx.call
        c("search_drive_files", {"query": "budget"})
        c("search_drive_files", {"query": "mimeType = 'application/vnd.google-apps.folder' and trashed = false"})
        c("list_drive_items", {})
        f = next(x for x in ctx.state["files"].values() if x["name"] == "Q4 budget")
        c("get_drive_file_content", {"file_id": f["id"]})
        new = c("create_drive_file", {"file_name": "notes.txt", "content": "hello", "folder_id": f["parents"][0]})
        nid = re.search(r"ID: ([\w-]+)", new.text).group(1)
        c("update_drive_file", {"file_id": nid, "name": "meeting-notes.txt", "starred": True})
        c("copy_drive_file", {"file_id": f["id"], "new_name": "Q4 budget (copy)"})
        c("manage_drive_access", {"file_id": f["id"], "action": "grant", "share_with": "john@acme.com", "role": "writer"})
        c("manage_drive_access", {"file_id": f["id"], "action": "grant", "share_with": "cfo@partner.io", "role": "reader"})
        c("set_drive_file_permissions", {"file_id": f["id"], "link_sharing": "domain", "role": "reader"})
        c("get_drive_shareable_link", {"file_id": f["id"]})
        contract = next(x for x in ctx.state["files"].values() if x["name"] == "Vendor contract.pdf")
        c("update_drive_file", {"file_id": contract["id"], "name": "renamed.pdf"})
        c("get_drive_file_download_url", {"file_id": f["id"], "export_format": "csv"})
        c("update_drive_file", {"file_id": nid, "trashed": True})
        c("search_drive_files", {"query": "name contains 'notes'"})

    def default_seed(self) -> dict[str, Any]:
        return {
            "user": {"email": "alex@acme.com"},
            "domain": "acme.com",
            "policy": {"external_sharing": False},
            "folders": [{"key": "finance", "name": "Finance"}, {"key": "q4", "name": "Q4 planning", "parent": "finance"},
                        {"key": "eng", "name": "Engineering"}],
            "files": [
                {"name": "Q4 budget", "type": "sheet", "parent": "q4",
                 "content": "team,q3,q4\nengineering,110000,120000\nsupport,40000,42000\n"},
                {"name": "Q4 plan", "type": "doc", "parent": "q4",
                 "content": "Goals: cut infra cost 15%, ship Billing v2. Risks: hiring behind plan."},
                {"name": "Architecture overview", "type": "doc", "parent": "eng",
                 "content": "The API runs on Kubernetes. Payments go through the billing service."},
                {"name": "Vendor contract.pdf", "mimeType": "application/pdf", "size": 482113, "owner": "legal@acme.com",
                 "shared_role": "reader"},
                {"name": "Offsite ideas", "type": "doc", "owner": "john@acme.com", "shared_role": "writer",
                 "content": "Lisbon or Porto? Budget TBD."},
                {"name": "old-export.csv", "mimeType": "text/csv", "content": "a,b\n1,2\n", "trashed": True},
            ],
        }

    def initial_state(self, seed: dict[str, Any], ctx: Instance) -> dict[str, Any]:
        me = (seed.get("user") or {}).get("email", "alex@acme.com")
        state: dict[str, Any] = {"me": me, "domain": seed.get("domain", me.split("@")[1]),
                                 "policy": {"external_sharing": True, **(seed.get("policy") or {})}, "files": {}}
        root = _new_file(ctx, state, "My Drive", FOLDER, None, owner=me, fid="root")
        root["parents"] = []
        state["roots"] = {me: "root"}
        for person in seed.get("people", []):  # colleagues whose agents can join get their own My Drive
            email = person["email"].lower()
            proot = _new_file(ctx, state, "My Drive", FOLDER, None, owner=email)
            proot["parents"] = []
            state["roots"][email] = proot["id"]
        keys = {}
        for f in seed.get("folders", []):
            parent = keys.get(f.get("parent"), "root")
            keys[f["key"]] = _new_file(ctx, state, f["name"], FOLDER, parent, owner=me)["id"]
        for f in seed.get("files", []):
            owner = f.get("owner", me)
            mime = f.get("mimeType") or TYPES.get(f.get("type", "text"), "text/plain")
            parent = keys.get(f.get("parent"), state["roots"].get(owner))
            nf = _new_file(ctx, state, f["name"], mime, parent, owner=owner, content=f.get("content"), size=f.get("size"))
            nf["trashed"] = bool(f.get("trashed"))
            if owner != me:
                _grant(ctx, state, nf, "user", f.get("shared_role", "reader"), me)
        return state

    actor_key = "me"

    def resolve_actor(self, state: dict[str, Any], identity: str) -> str:
        email = identity.strip().lower()
        if email not in state.get("roots", {}) and email != state["me"]:
            raise ValueError(f"no Drive user {identity} in this environment")
        return email

    def fault_error(self, fault: Any) -> tuple[Any, int]:
        if fault.kind == "rate_limit":
            return {"error": {"code": 403, "message": "User rate limit exceeded.",
                              "errors": [{"reason": "userRateLimitExceeded"}]}}, 403
        return super().fault_error(fault)


# -- model --------------------------------------------------------------------------------

def _iso(ctx: Instance) -> str:
    return ctx.now().strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _new_file(ctx: Instance, state: dict[str, Any], name: str, mime: str, parent: str | None, *, owner: str,
              content: str | None = None, size: int | None = None, fid: str | None = None) -> dict[str, Any]:
    fid = fid or "1" + ctx.token(32, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
    kind = {TYPES["doc"]: "document", TYPES["sheet"]: "spreadsheets", TYPES["slides"]: "presentation"}.get(mime)
    link = (f"https://docs.google.com/{kind}/d/{fid}/edit" if kind else
            f"https://drive.google.com/drive/folders/{fid}" if mime == FOLDER else f"https://drive.google.com/file/d/{fid}/view")
    f = {"id": fid, "name": name, "mimeType": mime, "parents": [parent] if parent else [], "owner": owner,
         "content": content, "size": size if size is not None else (len(content.encode()) if content else None),
         "createdTime": _iso(ctx), "modifiedTime": _iso(ctx), "trashed": False, "starred": False, "description": None,
         "webViewLink": link, "permissions": [], "link_sharing": "restricted", "link_role": None}
    f["permissions"].append({"id": ctx.token(20, "0123456789"), "type": "user", "role": "owner", "emailAddress": owner})
    state["files"][fid] = f
    return f


def _grant(ctx: Instance, state: dict[str, Any], f: dict[str, Any], type_: str, role: str, who: str) -> dict[str, Any]:
    existing = next((p for p in f["permissions"] if p.get("emailAddress") == who), None)
    if existing:
        existing["role"] = role
        return existing
    p = {"id": ctx.token(20, "0123456789"), "type": type_, "role": role, "emailAddress": who}
    f["permissions"].append(p)
    return p


def _my_role(state: dict[str, Any], f: dict[str, Any]) -> str | None:
    roles = [p["role"] for p in f["permissions"] if p.get("emailAddress") == state["me"]]
    if f["link_sharing"] in ("anyone_with_link", "domain") and f["link_role"]:
        roles.append(f["link_role"])
    return max(roles, key=ROLES.index) if roles else None


def _file(state: dict[str, Any], fid: str, need: str = "reader", allow_trashed: bool = True) -> dict[str, Any]:
    if fid == "root":  # 'root' is the acting user's own My Drive
        fid = state.get("roots", {}).get(state["me"], "root")
    f = state["files"].get(fid)
    role = _my_role(state, f) if f else None
    if f is None or role is None:
        raise _not_found(fid)
    if not allow_trashed and f["trashed"]:
        raise _not_found(fid)
    if ROLES.index(role) < ROLES.index(need):
        raise _forbidden()
    return f


def _line(f: dict[str, Any]) -> str:
    size = f["size"] if f["size"] is not None else "N/A"
    return (f'- Name: "{f["name"]}" (ID: {f["id"]}, Type: {f["mimeType"]}, Size: {size}, '
            f'Modified: {f["modifiedTime"]}) Link: {f["webViewLink"]}')


# -- Drive query language ---------------------------------------------------------------------

_Q = re.compile(r"\s*(?:(\()|(\))|'((?:[^'\\]|\\.)*)'|(!=|>=|<=|=|>|<)|([A-Za-z_]+))")


def _drive_query(state: dict[str, Any], query: str) -> Any:
    if not re.search(r"\b(contains|in|=|!=|<|>|trashed|mimeType|name|fullText|starred|modifiedTime|parents)\b|[=<>]", query):
        esc = query.replace("'", "\\'")
        query = f"fullText contains '{esc}'"  # plain words: search names and contents, like the MCP server
    toks, pos = [], 0
    while pos < len(query.strip()):
        m = _Q.match(query, pos)
        if not m or m.end() == pos:
            raise _err(400, f"Invalid Value: q (near '{query[pos:pos + 12]}')", "invalid")
        pos = m.end()
        lp, rp, lit, op, word = m.groups()
        toks.append(("(",) if lp else (")",) if rp else ("lit", lit.replace("\\'", "'")) if lit is not None
                    else ("op", op) if op else ("w", word))
    i = 0

    def peek() -> Any:
        return toks[i] if i < len(toks) else None

    def take() -> Any:
        nonlocal i
        if i >= len(toks):
            raise _err(400, "Invalid Value: q (incomplete query)", "invalid")
        i += 1
        return toks[i - 1]

    def is_word(w: str) -> bool:
        t = peek()
        return bool(t and t[0] == "w" and t[1].lower() == w)

    def expr() -> Any:
        node = term()
        while is_word("or"):
            take()
            node = ("or", node, term())
        return node

    def term() -> Any:
        node = factor()
        while is_word("and"):
            take()
            node = ("and", node, factor())
        return node

    def factor() -> Any:
        if is_word("not"):
            take()
            return ("not", factor())
        if peek() and peek()[0] == "(":
            take()
            node = expr()
            if take()[0] != ")":
                raise _err(400, "Invalid Value: q (expected ')')", "invalid")
            return node
        a = take()
        if a[0] == "lit":  # 'id' in parents
            if not is_word("in"):
                raise _err(400, "Invalid Value: q", "invalid")
            take()
            field = take()
            return ("in", field[1], a[1])
        field = a[1]
        if is_word("contains"):
            take()
            return ("contains", field, take()[1])
        op = take()
        if op[0] != "op":
            raise _err(400, f"Invalid Value: q (unexpected '{op[1] if len(op) > 1 else op[0]}')", "invalid")
        val = take()
        return (op[1], field, val[1] if val[0] in ("lit", "w") else None)

    tree = expr()
    if peek():
        raise _err(400, "Invalid Value: q (unexpected trailing input)", "invalid")
    return tree


def _eval(node: Any, f: dict[str, Any]) -> bool:
    op = node[0]
    if op == "and":
        return _eval(node[1], f) and _eval(node[2], f)
    if op == "or":
        return _eval(node[1], f) or _eval(node[2], f)
    if op == "not":
        return not _eval(node[1], f)
    _, field, val = node
    if op == "in":
        return field == "parents" and val in f["parents"]
    if op == "contains":
        if field == "name":
            return str(val).lower() in f["name"].lower()
        if field == "fullText":
            return str(val).lower() in f"{f['name']} {f['content'] or ''} {f['description'] or ''}".lower()
        raise _err(400, f"Invalid Value: q (contains is not supported for {field})", "invalid")
    have = {"name": f["name"], "mimeType": f["mimeType"], "trashed": str(f["trashed"]).lower(),
            "starred": str(f["starred"]).lower(), "modifiedTime": f["modifiedTime"], "createdTime": f["createdTime"]}.get(field)
    if have is None:
        raise _err(400, f"Invalid Value: q (unknown field {field})", "invalid")
    v = str(val)
    return {"=": have == v, "!=": have != v, ">": have > v, "<": have < v, ">=": have >= v, "<=": have <= v}[op]


# -- tools ----------------------------------------------------------------------------------

EMAIL = Annotated[str | None, "The user's Google email address (optional in single-user mode)"]


@tool("search_drive_files", read_only=True)
def search_drive_files(ctx: Instance,
                       query: Annotated[str, "Search query: plain words, or Drive query syntax (e.g. \"name contains 'budget' and trashed = false\")"],
                       user_google_email: EMAIL = None,
                       page_size: Annotated[int | None, "Maximum number of files to return (default 10)"] = 10,
                       page_token: Annotated[str | None, "Page token for pagination"] = None,
                       drive_id: Annotated[str | None, "Shared drive ID to search"] = None,
                       file_type: Annotated[Literal["doc", "sheet", "slides", "folder", "pdf"] | None, "Restrict to a file type"] = None) -> str:
    """Search for files and folders within a user's Google Drive, including shared drives"""
    s = ctx.state
    tree = _drive_query(s, query)
    explicit_trash = "trashed" in query
    hits = [f for f in s["files"].values() if f["id"] != "root" and _my_role(s, f)
            and (explicit_trash or not f["trashed"]) and _eval(tree, f)
            and (not file_type or f["mimeType"] == TYPES[file_type])]
    hits.sort(key=lambda f: f["modifiedTime"], reverse=True)
    start = int(page_token) if page_token else 0
    size = max(1, min(page_size or 10, 100))
    page = hits[start:start + size]
    if not page:
        return f"No files found for '{query}'."
    out = f"Found {len(page)} files for {user_google_email or s['me']} matching '{query}':\n" + "\n".join(_line(f) for f in page)
    if start + size < len(hits):
        out += f"\nNext page token: {start + size}"
    return out


@tool("get_drive_file_content", read_only=True)
def get_drive_file_content(ctx: Instance, file_id: Annotated[str, "The Drive file ID"], user_google_email: EMAIL = None) -> str:
    """Retrieve the content of a Drive file (Docs, Sheets and text files are returned as text)"""
    f = _file(ctx.state, file_id)
    if f["mimeType"] == FOLDER:
        raise _err(400, "The file is a folder and has no content.", "fileNotDownloadable")
    body = f["content"] if f["content"] is not None else f"[binary file, {f['size']} bytes, not shown]"
    return f'File: "{f["name"]}" (ID: {f["id"]}, Type: {f["mimeType"]})\nLink: {f["webViewLink"]}\n\n--- CONTENT ---\n{body}'


@tool("get_drive_file_download_url", read_only=True)
def get_drive_file_download_url(ctx: Instance,
                                file_id: Annotated[str, "The Drive file ID"],
                                export_format: Annotated[str | None, "Export format for Google files (pdf, docx, xlsx, csv, pptx, txt)"] = None,
                                user_google_email: EMAIL = None) -> str:
    """Get a temporary download URL for a Drive file, exporting Google files if needed"""
    f = _file(ctx.state, file_id)
    exports = EXPORTS.get(f["mimeType"])
    if exports:
        fmt = export_format or "pdf"
        if fmt not in exports:
            raise _err(400, f"Export only supports these formats: {', '.join(exports)}", "badRequest")
        return f"Download URL for \"{f['name']}\" as {fmt} (valid 1 hour): https://drive.toolsim.local/export/{f['id']}.{fmt}"
    if export_format:
        raise _err(400, "Export is only supported for Google Docs, Sheets and Slides.", "fileNotExportable")
    return f"Download URL for \"{f['name']}\" (valid 1 hour): https://drive.toolsim.local/download/{f['id']}"


@tool("create_drive_file")
def create_drive_file(ctx: Instance,
                      file_name: Annotated[str, "Name for the new file"],
                      user_google_email: EMAIL = None,
                      content: Annotated[str | None, "Text content of the file"] = None,
                      folder_id: Annotated[str | None, "Parent folder ID (default: root)"] = "root",
                      mime_type: Annotated[str | None, "MIME type (default: text/plain)"] = "text/plain",
                      fileUrl: Annotated[str | None, "URL to fetch the file content from"] = None) -> str:
    """Create a new file in Google Drive"""
    s = ctx.state
    parent = _file(s, folder_id or "root", need="writer", allow_trashed=False)
    if parent["mimeType"] != FOLDER:
        raise _err(400, "The specified parent is not a folder.", "invalidParent")
    if content is None and not fileUrl:
        raise _err(400, "Either content or fileUrl must be provided.", "required")
    body = content if content is not None else f"[content fetched from {fileUrl}]"
    f = _new_file(ctx, s, file_name, mime_type or "text/plain", parent["id"], owner=s["me"], content=body)
    return f"Successfully created file '{f['name']}' (ID: {f['id']}) in folder '{parent['name']}'. Link: {f['webViewLink']}"


@tool("create_drive_folder")
def create_drive_folder(ctx: Instance,
                        folder_name: Annotated[str, "Name for the new folder"],
                        user_google_email: EMAIL = None,
                        parent_folder_id: Annotated[str | None, "Parent folder ID (default: root)"] = "root") -> str:
    """Create a new folder in Google Drive"""
    s = ctx.state
    parent = _file(s, parent_folder_id or "root", need="writer", allow_trashed=False)
    if parent["mimeType"] != FOLDER:
        raise _err(400, "The specified parent is not a folder.", "invalidParent")
    f = _new_file(ctx, s, folder_name, FOLDER, parent["id"], owner=s["me"])
    return f"Successfully created folder '{f['name']}' (ID: {f['id']}). Link: {f['webViewLink']}"


@tool("import_to_google_doc")
def import_to_google_doc(ctx: Instance,
                         file_name: Annotated[str, "Name for the new Google Doc"],
                         content: Annotated[str, "Text, Markdown or HTML to convert into the Doc"],
                         user_google_email: EMAIL = None,
                         folder_id: Annotated[str | None, "Parent folder ID (default: root)"] = "root") -> str:
    """Create a Google Doc by importing text, Markdown or HTML content"""
    s = ctx.state
    parent = _file(s, folder_id or "root", need="writer", allow_trashed=False)
    f = _new_file(ctx, s, file_name, TYPES["doc"], parent["id"], owner=s["me"], content=content)
    return f"Successfully imported to Google Doc '{f['name']}' (ID: {f['id']}). Link: {f['webViewLink']}"


@tool("list_drive_items", read_only=True)
def list_drive_items(ctx: Instance,
                     folder_id: Annotated[str | None, "Folder ID to list (default: root)"] = "root",
                     user_google_email: EMAIL = None,
                     page_size: Annotated[int | None, "Maximum number of items (default 100)"] = 100,
                     drive_id: Annotated[str | None, "Shared drive ID"] = None) -> str:
    """List files and folders in a Drive folder"""
    s = ctx.state
    folder = _file(s, folder_id or "root", allow_trashed=False)
    items = [f for f in s["files"].values() if folder["id"] in f["parents"] and not f["trashed"] and _my_role(s, f)]
    items.sort(key=lambda f: (f["mimeType"] != FOLDER, f["name"].lower()))
    items = items[: max(1, page_size or 100)]
    if not items:
        return f"No items found in folder '{folder['name']}'."
    return f"Found {len(items)} items in folder '{folder['name']}' (ID: {folder['id']}):\n" + "\n".join(_line(f) for f in items)


@tool("copy_drive_file")
def copy_drive_file(ctx: Instance,
                    file_id: Annotated[str, "The file to copy"],
                    user_google_email: EMAIL = None,
                    new_name: Annotated[str | None, "Name for the copy (default: 'Copy of <name>')"] = None,
                    parent_folder_id: Annotated[str | None, "Folder for the copy (default: same folder, or root)"] = None) -> str:
    """Make a copy of a Drive file"""
    s = ctx.state
    src = _file(s, file_id, allow_trashed=False)
    if src["mimeType"] == FOLDER:
        raise _err(403, "Folders can't be copied.", "cannotCopyFile")
    target = parent_folder_id or next((p for p in src["parents"] if _my_role(s, s["files"][p]) in ("writer", "owner")), "root")
    parent = _file(s, target, need="writer", allow_trashed=False)
    f = _new_file(ctx, s, new_name or f"Copy of {src['name']}", src["mimeType"], parent["id"], owner=s["me"],
                  content=src["content"], size=src["size"])
    return f"Successfully copied '{src['name']}' to '{f['name']}' (ID: {f['id']}). Link: {f['webViewLink']}"


@tool("update_drive_file")
def update_drive_file(ctx: Instance,
                      file_id: Annotated[str, "The file to update"],
                      user_google_email: EMAIL = None,
                      name: Annotated[str | None, "New name"] = None,
                      description: Annotated[str | None, "New description"] = None,
                      add_parents: Annotated[str | None, "Comma-separated folder IDs to add as parents (move)"] = None,
                      remove_parents: Annotated[str | None, "Comma-separated folder IDs to remove as parents"] = None,
                      starred: Annotated[bool | None, "Star or unstar the file"] = None,
                      trashed: Annotated[bool | None, "Move to or restore from trash"] = None,
                      content: Annotated[str | None, "Replace the file's text content"] = None) -> str:
    """Update a Drive file's metadata, location, trash state, or content"""
    s = ctx.state
    f = _file(s, file_id)
    only_star = starred is not None and all(v is None for v in (name, description, add_parents, remove_parents, trashed, content))
    if not only_star:
        _file(s, file_id, need="owner" if trashed else "writer")  # only owners can trash
    changes = []
    if name is not None:
        f["name"] = name
        changes.append("name")
    if description is not None:
        f["description"] = description
        changes.append("description")
    for pid in [p.strip() for p in (add_parents or "").split(",") if p.strip()]:
        parent = _file(s, pid, need="writer", allow_trashed=False)
        if parent["mimeType"] != FOLDER:
            raise _err(400, "The specified parent is not a folder.", "invalidParent")
        if pid not in f["parents"]:
            f["parents"].append(pid)
        changes.append("parents")
    for pid in [p.strip() for p in (remove_parents or "").split(",") if p.strip()]:
        if pid in f["parents"]:
            f["parents"].remove(pid)
            changes.append("parents")
    if starred is not None:
        f["starred"] = starred
        changes.append("starred")
    if trashed is not None:
        f["trashed"] = trashed
        changes.append("trashed")
    if content is not None:
        if f["mimeType"] == FOLDER:
            raise _err(400, "Folders have no content.", "invalid")
        f["content"], f["size"] = content, len(content.encode())
        changes.append("content")
    if not changes:
        raise _err(400, "No updates specified.", "required")
    f["modifiedTime"] = _iso(ctx)
    return f"Successfully updated '{f['name']}' (ID: {f['id']}): {', '.join(dict.fromkeys(changes))}."


@tool("get_drive_shareable_link", read_only=True)
def get_drive_shareable_link(ctx: Instance, file_id: Annotated[str, "The Drive file ID"], user_google_email: EMAIL = None) -> str:
    """Get the shareable link and current sharing settings for a file"""
    s = ctx.state
    f = _file(s, file_id)
    perms = "\n".join(f"  - {p.get('emailAddress') or p['type']}: {p['role']}" for p in f["permissions"])
    link = {"restricted": "Restricted (only people with access)", "domain": f"Anyone at {s['domain']} with the link",
            "anyone_with_link": "Anyone with the link"}[f["link_sharing"]]
    return f"Link: {f['webViewLink']}\nGeneral access: {link}" + (f" ({f['link_role']})" if f["link_role"] else "") + \
        f"\nPeople with access:\n{perms}"


def _check_external(s: dict[str, Any], who: str) -> None:
    domain = who.rsplit("@", 1)[-1].lower() if "@" in who else ""
    if domain and domain != s["domain"] and not s["policy"]["external_sharing"]:
        raise _err(403, f"Sharing with {who} is not allowed: your administrator has restricted sharing outside "
                        f"{s['domain']}.", "shareOutNotPermitted")


@tool("manage_drive_access")
def manage_drive_access(ctx: Instance,
                        file_id: Annotated[str, "The Drive file ID"],
                        action: Annotated[Literal["grant", "update", "revoke", "transfer_owner"], "What to do"],
                        user_google_email: EMAIL = None,
                        share_with: Annotated[str | None, "Email address of the person (or group) to change access for"] = None,
                        role: Annotated[Literal["reader", "commenter", "writer"] | None, "Role to grant or update to"] = "reader",
                        send_notification: Annotated[bool | None, "Email the person about the change"] = True,
                        email_message: Annotated[str | None, "Message to include in the notification"] = None) -> str:
    """Grant, update, or revoke someone's access to a file, or transfer ownership"""
    s = ctx.state
    f = _file(s, file_id, need="writer")
    if not share_with or "@" not in share_with:
        raise _err(400, "share_with must be an email address.", "invalidSharingRequest")
    if action in ("grant", "update", "transfer_owner"):
        _check_external(s, share_with)
    existing = next((p for p in f["permissions"] if p.get("emailAddress", "").lower() == share_with.lower()), None)
    if action == "grant":
        if existing and existing["role"] == "owner":
            raise _err(400, f"{share_with} already owns this file.", "invalidSharingRequest")
        _grant(ctx, s, f, "user", role or "reader", share_with)
        note = " and notified them by email" if send_notification else ""
        return f"Granted {role} access on '{f['name']}' to {share_with}{note}."
    if existing is None:
        raise _err(404, f"Permission not found: {share_with}.", "notFound")
    if action == "update":
        if existing["role"] == "owner":
            raise _err(403, "The owner's role can't be changed; use transfer_owner.", "cannotModifyOwner")
        existing["role"] = role or "reader"
        return f"Updated {share_with} to {existing['role']} on '{f['name']}'."
    if action == "revoke":
        if existing["role"] == "owner":
            raise _err(403, "The owner's access can't be removed.", "cannotRemoveOwner")
        f["permissions"].remove(existing)
        return f"Revoked {share_with}'s access to '{f['name']}'."
    _file(s, file_id, need="owner")
    for p in f["permissions"]:
        if p["role"] == "owner":
            p["role"] = "writer"
    existing["role"] = "owner"
    f["owner"] = share_with
    return f"Transferred ownership of '{f['name']}' to {share_with}. You now have writer access."


@tool("set_drive_file_permissions")
def set_drive_file_permissions(ctx: Instance,
                               file_id: Annotated[str, "The Drive file ID"],
                               link_sharing: Annotated[Literal["restricted", "domain", "anyone_with_link"], "Who can open the file with its link"],
                               user_google_email: EMAIL = None,
                               role: Annotated[Literal["reader", "commenter", "writer"] | None, "Role for people using the link"] = "reader") -> str:
    """Set a file's general (link) access"""
    s = ctx.state
    f = _file(s, file_id, need="writer")
    if link_sharing == "anyone_with_link" and not s["policy"]["external_sharing"]:
        raise _err(403, f"Your administrator doesn't allow sharing files publicly outside {s['domain']}.", "shareOutNotPermitted")
    f["link_sharing"] = link_sharing
    f["link_role"] = None if link_sharing == "restricted" else (role or "reader")
    return f"General access for '{f['name']}' is now: {link_sharing}" + (f" ({f['link_role']})" if f["link_role"] else "") + "."


Drive.tools = [search_drive_files, get_drive_file_content, get_drive_file_download_url, create_drive_file,
               create_drive_folder, import_to_google_doc, list_drive_items, copy_drive_file, update_drive_file,
               get_drive_shareable_link, manage_drive_access, set_drive_file_permissions]
