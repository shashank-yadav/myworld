"""drive: state model, constants and helpers shared by the tools."""

from __future__ import annotations

from typing import Any

from ...core.instance import Instance
from ...core.tools import ToolError

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
V2 = "2026-09-25.2"
V3 = "2026-09-25.3"
SEARCH_LAG = {"files": 60}  # seconds until Drive search sees new, renamed or edited files


def _err(code: int, message: str, reason: str) -> ToolError:
    return ToolError({"error": {"code": code, "message": message, "errors": [{"reason": reason, "message": message}]}},
                     status=code)


def _not_found(fid: str) -> ToolError:
    return _err(404, f"File not found: {fid}.", "notFound")


def _forbidden(message: str = "The user does not have sufficient permissions for this file.") -> ToolError:
    return _err(403, message, "insufficientFilePermissions")

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


def _mail_share(ctx: Instance, f: dict[str, Any], to: str, role: str, message: str | None) -> None:
    """Google Drive's share notification (2026-09-25.3): it reaches their mailbox if Gmail is here."""
    me = ctx.state["me"]
    name = me.split("@")[0].replace(".", " ").title()
    kind = {"application/vnd.google-apps.document": "Document", "application/vnd.google-apps.spreadsheet": "Spreadsheet",
            "application/vnd.google-apps.presentation": "Presentation", FOLDER: "Folder"}.get(f["mimeType"], "File")
    verb = {"reader": "view", "commenter": "comment on", "writer": "edit"}.get(role, "view")
    ctx.notify(to, f"{name} (via Google Drive) <drive-shares-dm-noreply@google.com>",
               f"{kind} shared with you: \"{f['name']}\"",
               f"{name} ({me}) has invited you to {verb} the following {kind.lower()}:\n\n{f['name']}\n"
               + (f"\n{message}\n" if message else "") + f"\nOpen: {f['webViewLink']}")
