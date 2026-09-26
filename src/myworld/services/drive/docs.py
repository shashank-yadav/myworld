"""drive: Docs tools."""

from __future__ import annotations

import re
from typing import Annotated, Any

from ...core.instance import Instance
from ...core.tools import tool
from .files import EMAIL
from .model import TYPES, V1, _err, _file, _iso, _my_role, _new_file


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
    s = ctx.search_view("files")
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
