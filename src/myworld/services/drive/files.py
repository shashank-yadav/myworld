"""drive: file tools."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from ...core.instance import Instance
from ...core.tools import tool
from .model import EXPORTS, FOLDER, TYPES, _err, _file, _grant, _iso, _line, _mail_share, _my_role, _new_file
from .query import _drive_query, _eval

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
    s = ctx.search_view("files")
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
        return f"Download URL for \"{f['name']}\" as {fmt} (valid 1 hour): https://drive.myworld.local/export/{f['id']}.{fmt}"
    if export_format:
        raise _err(400, "Export is only supported for Google Docs, Sheets and Slides.", "fileNotExportable")
    return f"Download URL for \"{f['name']}\" (valid 1 hour): https://drive.myworld.local/download/{f['id']}"


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
        if send_notification and s.get("_v3"):
            _mail_share(ctx, f, share_with, role or "reader", email_message)
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
