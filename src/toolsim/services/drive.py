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
      - {name: Headcount, type: sheet, sheets: {Plan: "team,hc\neng,12", Summary: "total,=SUM(Plan!B2:B9)"}}

From 2026-09-25.1 there are Sheets and Docs tools too. Sheets hold real cells: A1 ranges with
optional sheet names ('Q4 plan'!A1:C9), USER_ENTERED values parse numbers and formulas (SUM,
AVERAGE, MIN, MAX, COUNT, arithmetic, cross-sheet refs) while RAW keeps text, writes can't spill
out of an explicit range or past the grid, and reads return formatted values with trailing
blanks trimmed, like the API. Docs use the Docs API's 1-based indices.
"""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Annotated, Any, Literal

from ..core.instance import Instance, Service
from ..core.tools import ToolError, action, tool

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
V1 = "2026-09-25.1"


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
    versions = {"2026-09-25": "Initial release: 12 Drive tools modeled on taylorwilsdon/google_workspace_mcp.",
                V1: "Sheets (6 tools: cells, A1 ranges, formulas, grid limits) and Docs (5 tools) from the same server."}

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
        if ctx.at_least(V1):
            c("list_spreadsheets", {})
            c("get_spreadsheet_info", {"spreadsheet_id": f["id"]})
            c("read_sheet_values", {"spreadsheet_id": f["id"]})
            c("modify_sheet_values", {"spreadsheet_id": f["id"], "range_name": "A4:C5",
                                      "values": [["sales", "90000", "95000"], ["total", "=SUM(B2:B4)", "=SUM(C2:C4)"]]})
            c("modify_sheet_values", {"spreadsheet_id": f["id"], "range_name": "A6:B6", "values": [["x", "1", "2"]]})
            c("read_sheet_values", {"spreadsheet_id": f["id"], "range_name": "Sheet1!A4:C5"})
            c("read_sheet_values", {"spreadsheet_id": f["id"], "range_name": "Budget!A1"})
            c("create_sheet", {"spreadsheet_id": f["id"], "sheet_name": "Notes"})
            c("modify_sheet_values", {"spreadsheet_id": f["id"], "range_name": "Notes!A1",
                                      "values": [["=Sheet1!C5*2"]], "value_input_option": "RAW"})
            c("read_sheet_values", {"spreadsheet_id": f["id"], "range_name": "Notes"})
            sid = re.search(r"ID: ([\w-]+)", c("create_spreadsheet", {"title": "Hiring", "sheet_names": ["Q4"]}).text).group(1)
            c("modify_sheet_values", {"spreadsheet_id": sid, "range_name": "Q4!A1001", "values": [["late"]]})
            doc = next(x for x in ctx.state["files"].values() if x["name"] == "Q4 plan")
            c("search_docs", {"query": "plan"})
            c("get_doc_content", {"document_id": doc["id"]})
            c("modify_doc_text", {"document_id": doc["id"], "start_index": 1, "text": "DRAFT. "})
            c("find_and_replace_doc", {"document_id": doc["id"], "find_text": "billing v2", "replace_text": "Billing v3"})
            c("modify_doc_text", {"document_id": doc["id"], "start_index": 500, "text": "x"})
            c("create_doc", {"title": "Retro", "content": "What went well"})
            c("get_doc_content", {"document_id": f["id"]})

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
            if ctx.at_least(V1) and f.get("sheets") and mime == TYPES["sheet"]:
                nf["sheets"] = [_new_sheet(n, title, body) for n, (title, body) in enumerate(f["sheets"].items())]
                _sync_content(nf)
            if owner != me:
                _grant(ctx, state, nf, "user", f.get("shared_role", "reader"), me)
        return state

    actor_key = "me"

    def resolve_actor(self, state: dict[str, Any], identity: str) -> str:
        email = identity.strip().lower()
        if email not in state.get("roots", {}) and email != state["me"]:
            raise ValueError(f"no Drive user {identity} in this environment")
        return email

    def error_shape(self, status: int, message: str) -> Any:
        reason = {404: "notFound", 500: "backendError", 502: "backendError", 503: "backendError"}.get(status, "error")
        return {"error": {"code": status, "message": message, "errors": [{"reason": reason, "message": message}]}}

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
    if "sheets" in src:
        f["sheets"] = json.loads(json.dumps(src["sheets"]))
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


# -- Sheets (2026-09-25.1) -------------------------------------------------------------------

ROWS, COLS = 1000, 26
_CELL = re.compile(r"^\$?([A-Za-z]{0,3})\$?(\d*)$")


class _FormulaError(Exception):
    pass


def _col(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + ord(ch) - 64
    return n - 1


def _letters(i: int) -> str:
    out, i = "", i + 1
    while i:
        i, r = divmod(i - 1, 26)
        out = chr(65 + r) + out
    return out


def _parse_input(v: Any, user_entered: bool) -> Any:
    """A value as Sheets stores it: USER_ENTERED parses numbers and formulas, RAW keeps text."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return v
    v = str(v)
    if not user_entered:
        return v
    t = v.strip()
    if t.startswith("=") and len(t) > 1:
        return {"f": t}
    if re.fullmatch(r"[-+]?\d{1,3}(,\d{3})+(\.\d+)?|[-+]?\d+", t):
        n = t.replace(",", "")
        return int(n) if "." not in n else float(n)
    if re.fullmatch(r"[-+]?(\d+\.\d*|\.\d+)(e[-+]?\d+)?", t, re.I):
        return float(t)
    return v


def _new_sheet(sid: int, title: str, csv_text: str = "") -> dict[str, Any]:
    rows = [[_parse_input(v, True) for v in r] for r in csv.reader(io.StringIO(csv_text))] if csv_text else []
    return {"sheetId": sid, "title": title, "rows": rows, "rowCount": max(ROWS, len(rows)),
            "columnCount": max(COLS, max((len(r) for r in rows), default=0))}


def _sheets(f: dict[str, Any]) -> list[dict[str, Any]]:
    if f["mimeType"] != TYPES["sheet"]:
        raise _err(400, f"File {f['id']} is not a Google Sheets spreadsheet.", "badRequest")
    if "sheets" not in f:
        f["sheets"] = [_new_sheet(0, "Sheet1", f.get("content") or "")]
    return f["sheets"]


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else f"{v:.10g}"
    return "" if v is None else str(v)


def _raw(sheet: dict[str, Any], r: int, c: int) -> Any:
    rows = sheet["rows"]
    return rows[r][c] if r < len(rows) and c < len(rows[r]) else ""


def _value(sheets: list[dict[str, Any]], sheet: dict[str, Any], r: int, c: int, depth: int = 0) -> Any:
    v = _raw(sheet, r, c)
    if isinstance(v, dict):
        if depth > 40:
            raise _FormulaError("#REF!")  # circular dependency
        return _Formula(sheets, sheet, v["f"][1:], depth + 1).run()
    return v


def _display(sheets: list[dict[str, Any]], sheet: dict[str, Any], r: int, c: int) -> str:
    try:
        return _fmt(_value(sheets, sheet, r, c))
    except _FormulaError as e:
        return str(e)


_FTOK = re.compile(r"\s*(?:(\d+\.?\d*|\.\d+)|((?:'[^']+'|[A-Za-z_][\w.]*)!)?(\$?[A-Za-z]{1,3}\$?\d+(?::\$?[A-Za-z]{1,3}\$?\d+)?)"
                   r"|([A-Za-z_]+)\s*\(|\"([^\"]*)\"|([-+*/(),]))")


class _Formula:
    """=SUM(B2:B4)*2 and friends: numbers, refs, ranges, + - * /, SUM/AVERAGE/MIN/MAX/COUNT."""
    FUNCS = {"SUM": sum, "MIN": lambda xs: min(xs, default=0), "MAX": lambda xs: max(xs, default=0),
             "COUNT": len, "AVERAGE": lambda xs: sum(xs) / len(xs) if xs else (_ for _ in ()).throw(_FormulaError("#DIV/0!"))}

    def __init__(self, sheets: list[dict[str, Any]], sheet: dict[str, Any], text: str, depth: int):
        self.sheets, self.sheet, self.depth = sheets, sheet, depth
        self.toks, pos = [], 0
        while pos < len(text.rstrip()):
            m = _FTOK.match(text, pos)
            if not m or m.end() == pos:
                raise _FormulaError("#ERROR!")
            self.toks.append(m.groups())
            pos = m.end()
        self.i = 0

    def run(self) -> Any:
        v = self._expr()
        if self.i != len(self.toks):
            raise _FormulaError("#ERROR!")
        return self._scalar(v)

    def _peek(self, op: str) -> bool:
        return self.i < len(self.toks) and self.toks[self.i][5] == op

    def _expr(self) -> Any:
        v = self._term()
        while self._peek("+") or self._peek("-"):
            op = self.toks[self.i][5]
            self.i += 1
            a, b = self._num(v), self._num(self._term())
            v = a + b if op == "+" else a - b
        return v

    def _term(self) -> Any:
        v = self._factor()
        while self._peek("*") or self._peek("/"):
            op = self.toks[self.i][5]
            self.i += 1
            a, b = self._num(v), self._num(self._factor())
            if op == "/" and b == 0:
                raise _FormulaError("#DIV/0!")
            v = a * b if op == "*" else a / b
        return v

    def _factor(self) -> Any:
        if self.i >= len(self.toks):
            raise _FormulaError("#ERROR!")
        num, sheet_ref, ref, func, string, op = self.toks[self.i]
        self.i += 1
        if op in ("-", "+"):
            v = self._num(self._factor())
            return -v if op == "-" else v
        if op == "(":
            v = self._expr()
            if not self._peek(")"):
                raise _FormulaError("#ERROR!")
            self.i += 1
            return v
        if num:
            return float(num) if "." in num else int(num)
        if string is not None:
            return string
        if ref:
            return self._ref(sheet_ref, ref)
        if func:
            name = func.upper()
            if name not in self.FUNCS:
                raise _FormulaError("#NAME?")
            args: list[Any] = []
            while not self._peek(")"):
                args.append(self._expr())
                if self._peek(","):
                    self.i += 1
                elif not self._peek(")"):
                    raise _FormulaError("#ERROR!")
            self.i += 1
            nums = []
            for a in args:
                if isinstance(a, list):  # ranges skip text and blanks, like Sheets
                    nums += [x for x in a if isinstance(x, (int, float)) and not isinstance(x, bool)]
                else:
                    nums.append(self._num(a))
            return self.FUNCS[name](nums)
        raise _FormulaError("#ERROR!")

    def _ref(self, sheet_ref: str | None, ref: str) -> Any:
        sheet = self.sheet
        if sheet_ref:
            name = sheet_ref[:-1].strip("'")
            sheet = next((sh for sh in self.sheets if sh["title"] == name), None)
            if sheet is None:
                raise _FormulaError("#REF!")
        a, _, b = ref.replace("$", "").partition(":")
        (c1, r1), (c2, r2) = [(_col(re.match(r"[A-Za-z]+", x).group()), int(re.search(r"\d+", x).group()) - 1)
                              for x in (a, b or a)]
        cells = [_value(self.sheets, sheet, r, c, self.depth) for r in range(r1, r2 + 1) for c in range(c1, c2 + 1)]
        return cells if b else cells[0]

    @staticmethod
    def _num(v: Any) -> float | int:
        if isinstance(v, list):
            raise _FormulaError("#VALUE!")
        if v == "":
            return 0
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v
        raise _FormulaError("#VALUE!")

    @staticmethod
    def _scalar(v: Any) -> Any:
        if isinstance(v, list):
            raise _FormulaError("#VALUE!")
        return v


def _a1(f: dict[str, Any], rng: str) -> tuple[dict[str, Any], int, int, int, int, bool]:
    """Resolve an A1 range: (sheet, row1, col1, row2, col2, bounded). 'Sheet 1'!A1:C9, B2, A:C, Sheet1."""
    sheets = _sheets(f)
    text = (rng or "").strip()
    bad = _err(400, f"Unable to parse range: {rng}", "badRequest")
    if "!" in text:
        name, _, cells = text.rpartition("!")
    elif re.fullmatch(r"\$?[A-Za-z]{1,3}\$?\d*(:\$?[A-Za-z]{0,3}\$?\d*)?|\d+:\d+", text):
        name, cells = "", text
    else:
        name, cells = text, ""
    name = name.strip()
    if len(name) > 1 and name[0] == name[-1] == "'":
        name = name[1:-1].replace("''", "'")
    sheet = next((sh for sh in sheets if sh["title"] == name), None) if name else sheets[0]
    if sheet is None:
        raise bad
    if not cells:
        return sheet, 0, 0, sheet["rowCount"] - 1, sheet["columnCount"] - 1, True
    a, colon, b = cells.partition(":")
    ma, mb = _CELL.match(a), _CELL.match(b) if colon else None
    if not ma or not (ma.group(1) or ma.group(2)) or (colon and (not mb or not (mb.group(1) or mb.group(2)))):
        raise bad
    r1 = int(ma.group(2)) - 1 if ma.group(2) else 0
    c1 = _col(ma.group(1)) if ma.group(1) else 0
    if not colon:
        if not ma.group(1) or not ma.group(2):
            raise bad
        r2, c2 = r1, c1
    else:
        r2 = int(mb.group(2)) - 1 if mb.group(2) else sheet["rowCount"] - 1
        c2 = _col(mb.group(1)) if mb.group(1) else sheet["columnCount"] - 1
    if r1 < 0 or r2 < r1 or c2 < c1:
        raise bad
    if r2 >= sheet["rowCount"] or c2 >= sheet["columnCount"]:
        raise _err(400, f"Range ('{sheet['title']}'!{cells}) exceeds grid limits. Max rows: {sheet['rowCount']}, "
                        f"max columns: {sheet['columnCount']}", "badRequest")
    return sheet, r1, c1, r2, c2, bool(colon)


def _grid(sheets: list[dict[str, Any]], sheet: dict[str, Any], r1: int, c1: int, r2: int, c2: int) -> list[list[str]]:
    """Formatted values like the API returns them: trailing blank cells and rows dropped."""
    out = []
    for r in range(r1, min(r2, len(sheet["rows"]) - 1) + 1):
        row = [_display(sheets, sheet, r, c) for c in range(c1, min(c2, max(len(sheet["rows"][r]) - 1, c1 - 1)) + 1)]
        while row and row[-1] == "":
            row.pop()
        out.append(row)
    while out and not out[-1]:
        out.pop()
    return out


def _sync_content(f: dict[str, Any]) -> None:
    """Keep the Drive view of a spreadsheet (its first sheet as CSV) in step with its cells."""
    sheets = f["sheets"]
    first = sheets[0]
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(_grid(sheets, first, 0, 0, first["rowCount"] - 1, first["columnCount"] - 1))
    f["content"] = buf.getvalue()
    f["size"] = len(f["content"].encode())


def _spreadsheet(ctx: Instance, fid: str, need: str = "reader") -> dict[str, Any]:
    f = _file(ctx.state, fid, need=need, allow_trashed=False)
    _sheets(f)
    return f


@tool("list_spreadsheets", read_only=True, since=V1)
def list_spreadsheets(ctx: Instance, user_google_email: EMAIL = None,
                      max_results: Annotated[int | None, "Maximum number of spreadsheets to return (default 25)"] = 25) -> str:
    """Lists spreadsheets from Google Drive that the user has access to"""
    s = ctx.state
    files = sorted((f for f in s["files"].values() if f["mimeType"] == TYPES["sheet"] and not f["trashed"] and _my_role(s, f)),
                   key=lambda f: f["modifiedTime"], reverse=True)[: max(1, max_results or 25)]
    if not files:
        return f"No spreadsheets found for {user_google_email or s['me']}."
    return (f"Successfully listed {len(files)} spreadsheets for {user_google_email or s['me']}:\n"
            + "\n".join(f'- "{f["name"]}" (ID: {f["id"]}) | Modified: {f["modifiedTime"]} | Link: {f["webViewLink"]}'
                        for f in files))


@tool("get_spreadsheet_info", read_only=True, since=V1)
def get_spreadsheet_info(ctx: Instance, spreadsheet_id: Annotated[str, "The ID of the spreadsheet"],
                         user_google_email: EMAIL = None) -> str:
    """Gets information about a specific spreadsheet including its sheets"""
    f = _spreadsheet(ctx, spreadsheet_id)
    lines = [f'  - "{sh["title"]}" (ID: {sh["sheetId"]}) | Size: {sh["rowCount"]}x{sh["columnCount"]}' for sh in f["sheets"]]
    return f'Spreadsheet: "{f["name"]}" (ID: {f["id"]})\nSheets ({len(lines)}):\n' + "\n".join(lines)


@tool("read_sheet_values", read_only=True, since=V1)
def read_sheet_values(ctx: Instance, spreadsheet_id: Annotated[str, "The ID of the spreadsheet"],
                      user_google_email: EMAIL = None,
                      range_name: Annotated[str | None, "The range to read (e.g., 'Sheet1!A1:D10', 'A1:D10')"] = "A1:Z1000") -> str:
    """Reads values from a specific range in a Google Sheet"""
    f = _spreadsheet(ctx, spreadsheet_id)
    rows = _grid(f["sheets"], *_a1(f, range_name or "A1:Z1000")[:5])
    who = user_google_email or ctx.state["me"]
    if not rows:
        return f"No data found in range '{range_name}' for {who}."
    return (f"Successfully read {len(rows)} rows from range '{range_name}' in spreadsheet {spreadsheet_id} for {who}:\n"
            + "\n".join(f"Row {n:2d}: {json.dumps(r)}" for n, r in enumerate(rows, 1)))


@tool("modify_sheet_values", since=V1)
def modify_sheet_values(ctx: Instance, spreadsheet_id: Annotated[str, "The ID of the spreadsheet"],
                        range_name: Annotated[str, "The range to modify (e.g., 'Sheet1!A1:D10', 'A1:D10')"],
                        user_google_email: EMAIL = None,
                        values: Annotated[list | str | None, "2D array of values to write/update, e.g. [[\"a\", \"1\"], [\"b\", \"=A1*2\"]] (JSON string accepted). Required unless clear_values is true"] = None,
                        value_input_option: Annotated[Literal["RAW", "USER_ENTERED"] | None, "How to interpret input values"] = "USER_ENTERED",
                        clear_values: Annotated[bool | None, "If true, clears the range instead of writing values"] = False) -> str:
    """Modifies values in a specific range of a Google Sheet - can write, update, or clear values"""
    f = _spreadsheet(ctx, spreadsheet_id, need="writer")
    sheet, r1, c1, r2, c2, bounded = _a1(f, range_name)
    who = user_google_email or ctx.state["me"]
    if clear_values:
        for r in range(r1, min(r2, len(sheet["rows"]) - 1) + 1):
            for c in range(c1, min(c2, len(sheet["rows"][r]) - 1) + 1):
                sheet["rows"][r][c] = ""
        _sync_content(f)
        f["modifiedTime"] = _iso(ctx)
        return f"Successfully cleared range '{range_name}' in spreadsheet {spreadsheet_id} for {who}."
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except ValueError:
            raise _err(400, "values must be a 2D array (list of rows).", "badRequest") from None
    if not values or not isinstance(values, list) or not all(isinstance(row, list) for row in values):
        raise _err(400, "values must be a 2D array (list of rows).", "badRequest")
    width = max(len(row) for row in values)
    if bounded and (r1 + len(values) - 1 > r2 or c1 + width - 1 > c2):
        where = (f"row [{r1 + len(values)}]" if r1 + len(values) - 1 > r2 else f"column [{_letters(c1 + width - 1)}]")
        raise _err(400, f"Requested writing within range [{range_name}], but tried writing to {where}", "badRequest")
    if r1 + len(values) > sheet["rowCount"] or c1 + width > sheet["columnCount"]:
        raise _err(400, f"Range ('{sheet['title']}'!{_letters(c1)}{r1 + 1}) exceeds grid limits. Max rows: "
                        f"{sheet['rowCount']}, max columns: {sheet['columnCount']}", "badRequest")
    user_entered = (value_input_option or "USER_ENTERED") == "USER_ENTERED"
    for dr, row in enumerate(values):
        r = r1 + dr
        while len(sheet["rows"]) <= r:
            sheet["rows"].append([])
        cells = sheet["rows"][r]
        for dc, v in enumerate(row):
            while len(cells) <= c1 + dc:
                cells.append("")
            cells[c1 + dc] = _parse_input(v, user_entered)
    _sync_content(f)
    f["modifiedTime"] = _iso(ctx)
    n = sum(len(row) for row in values)
    return (f"Successfully updated range '{range_name}' in spreadsheet {spreadsheet_id} for {who}. "
            f"Updated: {n} cells, {len(values)} rows, {width} columns.")


@tool("create_spreadsheet", since=V1)
def create_spreadsheet(ctx: Instance, title: Annotated[str, "The title of the new spreadsheet"],
                       user_google_email: EMAIL = None,
                       sheet_names: Annotated[list[str] | None, "List of sheet names to create (default: one 'Sheet1')"] = None) -> str:
    """Creates a new Google Spreadsheet"""
    s = ctx.state
    names = sheet_names or ["Sheet1"]
    if len(set(names)) != len(names):
        raise _err(400, "Sheet names must be unique.", "badRequest")
    root = _file(s, "root", need="writer")
    f = _new_file(ctx, s, title, TYPES["sheet"], root["id"], owner=s["me"], content="")
    f["sheets"] = [_new_sheet(n, name) for n, name in enumerate(names)]
    _sync_content(f)
    return (f"Successfully created spreadsheet '{title}' for {user_google_email or s['me']}. "
            f"ID: {f['id']} | URL: {f['webViewLink']} | Locale: en_US")


@tool("create_sheet", since=V1)
def create_sheet(ctx: Instance, spreadsheet_id: Annotated[str, "The ID of the spreadsheet"],
                 sheet_name: Annotated[str, "The name of the new sheet"], user_google_email: EMAIL = None) -> str:
    """Creates a new sheet within an existing spreadsheet"""
    f = _spreadsheet(ctx, spreadsheet_id, need="writer")
    if any(sh["title"].lower() == sheet_name.lower() for sh in f["sheets"]):
        raise _err(400, f'Invalid requests[0].addSheet: A sheet with the name "{sheet_name}" already exists. '
                        "Please enter another name.", "badRequest")
    sid = ctx.next("sheet_id", 1000000)
    f["sheets"].append(_new_sheet(sid, sheet_name))
    f["modifiedTime"] = _iso(ctx)
    return f"Successfully added sheet '{sheet_name}' (ID: {sid}) to spreadsheet {spreadsheet_id}."


# -- Docs (2026-09-25.1) ---------------------------------------------------------------------

def _doc(ctx: Instance, fid: str, need: str = "reader") -> dict[str, Any]:
    f = _file(ctx.state, fid, need=need, allow_trashed=False)
    if f["mimeType"] != TYPES["doc"]:
        raise _err(400, f"File {fid} is not a Google Docs document.", "badRequest")
    return f


def _set_doc(ctx: Instance, f: dict[str, Any], text: str) -> None:
    f["content"], f["size"], f["modifiedTime"] = text, len(text.encode()), _iso(ctx)


@tool("search_docs", read_only=True, since=V1)
def search_docs(ctx: Instance, query: Annotated[str, "Text to search for in document names"],
                user_google_email: EMAIL = None,
                page_size: Annotated[int | None, "Maximum number of results (default 10)"] = 10) -> str:
    """Searches for Google Docs by name using Drive API"""
    s = ctx.state
    hits = sorted((f for f in s["files"].values() if f["mimeType"] == TYPES["doc"] and not f["trashed"] and _my_role(s, f)
                   and query.lower() in f["name"].lower()), key=lambda f: f["modifiedTime"], reverse=True)
    hits = hits[: max(1, page_size or 10)]
    if not hits:
        return f"No Google Docs found matching '{query}'."
    return (f"Found {len(hits)} Google Docs matching '{query}':\n"
            + "\n".join(f"- {f['name']} (ID: {f['id']}) Modified: {f['modifiedTime']} Link: {f['webViewLink']}" for f in hits))


@tool("get_doc_content", read_only=True, since=V1)
def get_doc_content(ctx: Instance, document_id: Annotated[str, "The ID of the Google Doc"],
                    user_google_email: EMAIL = None) -> str:
    """Retrieves content of a Google Doc as plain text"""
    f = _doc(ctx, document_id)
    return (f'File: "{f["name"]}" (ID: {f["id"]}, Type: {f["mimeType"]})\nLink: {f["webViewLink"]}\n\n'
            f'--- CONTENT ---\n{f["content"] or ""}')


@tool("create_doc", since=V1)
def create_doc(ctx: Instance, title: Annotated[str, "Title of the new document"], user_google_email: EMAIL = None,
               content: Annotated[str | None, "Initial text content"] = "") -> str:
    """Creates a new Google Doc and optionally inserts initial content"""
    s = ctx.state
    root = _file(s, "root", need="writer")
    f = _new_file(ctx, s, title, TYPES["doc"], root["id"], owner=s["me"], content=content or "")
    return f"Created Google Doc '{title}' (ID: {f['id']}) for {user_google_email or s['me']}. Link: {f['webViewLink']}"


@tool("modify_doc_text", since=V1)
def modify_doc_text(ctx: Instance, document_id: Annotated[str, "The ID of the Google Doc"],
                    start_index: Annotated[int, "Start index (1-based, as in the Docs API; 1 is the start of the body)"],
                    user_google_email: EMAIL = None,
                    end_index: Annotated[int | None, "End index (exclusive) of text to replace; omit to insert at start_index"] = None,
                    text: Annotated[str | None, "Text to insert, or to replace the range with"] = None,
                    bold: Annotated[bool | None, "Make the text bold"] = None,
                    italic: Annotated[bool | None, "Make the text italic"] = None,
                    underline: Annotated[bool | None, "Underline the text"] = None,
                    font_size: Annotated[int | None, "Font size in points"] = None,
                    font_family: Annotated[str | None, "Font family name"] = None) -> str:
    """Modifies text in a Google Doc - can insert/replace text and/or apply formatting"""
    f = _doc(ctx, document_id, need="writer")
    body = f["content"] or ""
    end_of_segment = len(body) + 2  # the body's text plus its final newline, from index 1
    if text is None and all(x is None for x in (bold, italic, underline, font_size, font_family)):
        raise _err(400, "Provide text to insert/replace, or formatting to apply.", "badRequest")
    if start_index < 1:
        raise _err(400, f"Invalid requests[0].insertText: Index {start_index} must be greater than or equal to 1.",
                   "badRequest")
    if start_index >= end_of_segment:
        raise _err(400, f"Invalid requests[0].insertText: Index {start_index} must be less than the end index of the "
                        f"referenced segment, {end_of_segment}.", "badRequest")
    if end_index is not None:
        if end_index <= start_index:
            raise _err(400, "Invalid requests[0].deleteContentRange: The range should not be empty.", "badRequest")
        if end_index >= end_of_segment:
            raise _err(400, "Invalid requests[0].deleteContentRange: The range cannot include the newline character "
                            "at the end of the segment.", "badRequest")
    if text is not None:
        a = start_index - 1
        b = end_index - 1 if end_index is not None else a
        _set_doc(ctx, f, body[:a] + text + body[b:])
    what = (f"replaced text at {start_index}-{end_index}" if end_index is not None and text is not None
            else f"inserted text at index {start_index}" if text is not None else "applied formatting")
    return f"Successfully {what} in document {document_id}. Link: {f['webViewLink']}"


@tool("find_and_replace_doc", since=V1)
def find_and_replace_doc(ctx: Instance, document_id: Annotated[str, "The ID of the Google Doc"],
                         find_text: Annotated[str, "Text to find"], replace_text: Annotated[str, "Replacement text"],
                         user_google_email: EMAIL = None,
                         match_case: Annotated[bool | None, "Whether to match case"] = False) -> str:
    """Finds and replaces text throughout a Google Doc"""
    f = _doc(ctx, document_id, need="writer")
    if not find_text:
        raise _err(400, "Invalid requests[0].replaceAllText: The find text must not be empty.", "badRequest")
    pattern = re.compile(re.escape(find_text), 0 if match_case else re.I)
    new, n = pattern.subn(lambda _: replace_text, f["content"] or "")
    if n:
        _set_doc(ctx, f, new)
    return f"Replaced {n} occurrence(s) of '{find_text}' with '{replace_text}' in document {document_id}."


Drive.tools = [search_drive_files, get_drive_file_content, get_drive_file_download_url, create_drive_file,
               create_drive_folder, import_to_google_doc, list_drive_items, copy_drive_file, update_drive_file,
               get_drive_shareable_link, manage_drive_access, set_drive_file_permissions,
               list_spreadsheets, get_spreadsheet_info, read_sheet_values, modify_sheet_values, create_spreadsheet,
               create_sheet, search_docs, get_doc_content, create_doc, modify_doc_text, find_and_replace_doc]


# -- world actions (triggered by environments, never by agents) --------------------------

def _named(state: dict[str, Any], file: str) -> dict[str, Any]:
    f = state["files"].get(file) or next((x for x in state["files"].values() if x["name"] == file), None)
    if f is None:
        raise _not_found(file)
    return f


@action("revoke_access")
def act_revoke_access(ctx: Instance, file: str, email: str | None = None) -> None:
    """The owner removes someone's access (default: the drive's own user), so the agent starts getting 404s."""
    email = email or ctx.state["me"]
    f = _named(ctx.state, file)
    f["permissions"] = [p for p in f["permissions"] if p.get("emailAddress", "").lower() != email.lower()
                        or p["role"] == "owner"]


@action("share")
def act_share(ctx: Instance, file: str, email: str, role: str = "reader") -> None:
    """Someone shares a file with a person."""
    if role not in ROLES:
        raise _err(400, f"invalid role {role}", "invalid")
    _grant(ctx, ctx.state, _named(ctx.state, file), "user", role, email.lower())


@action("edit_content")
def act_edit_content(ctx: Instance, file: str, content: str) -> None:
    """A collaborator edits a file (what the agent read earlier is now stale)."""
    f = _named(ctx.state, file)
    f["content"], f["size"], f["modifiedTime"] = content, len(content.encode()), _iso(ctx)
    if "sheets" in f:  # a collaborator rewrote the first sheet
        first = f["sheets"][0]
        f["sheets"][0] = {**_new_sheet(first["sheetId"], first["title"], content),
                          "rowCount": first["rowCount"], "columnCount": first["columnCount"]}


@action("trash")
def act_trash(ctx: Instance, file: str) -> None:
    """The owner moves a file to trash."""
    _named(ctx.state, file)["trashed"] = True


Drive.actions = [act_revoke_access, act_share, act_edit_content, act_trash]
