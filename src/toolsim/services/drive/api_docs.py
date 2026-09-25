"""drive: the Google Docs REST API (v1): documents.get / create / batchUpdate over the same text
the Drive file and the MCP tools see.

Indexes are the Docs API's: UTF-16 code units, 1 = the start of the body, and the body always ends
with a newline that can't be deleted. ``includeTabsContent`` returns the ``tabs`` form.
"""

from __future__ import annotations

import hashlib
import re
import urllib.parse
from typing import Any

from ...api import Request, operation
from ...api.google import error, select
from ...core.instance import Instance
from ...core.tools import ToolError
from .model import TYPES, _file, _iso, _new_file

HOSTS = ("docs.googleapis.com",)
BASE = "/v1/documents"
FORMAT_ONLY = {"updateTextStyle", "updateParagraphStyle", "createParagraphBullets", "deleteParagraphBullets",
               "updateDocumentStyle", "updateSectionStyle", "createNamedRange", "deleteNamedRange",
               "updateTableCellStyle", "updateTableColumnProperties", "updateTableRowStyle", "pinTableHeaderRows",
               "mergeTableCells", "unmergeTableCells", "createHeader", "createFooter", "deleteHeader", "deleteFooter",
               "createFootnote", "replaceNamedRangeContent", "replaceImage", "deletePositionedObject"}


def op(op_id: str, method: str, path: str, **kw: Any):  # noqa: ANN201
    return operation(op_id, method, BASE + path, hosts=HOSTS, **kw)


def _u16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def _py(text: str, index: int) -> int:
    """Python offset of a Docs index (1-based UTF-16 position in the body)."""
    units = index - 1
    i = n = 0
    while i < len(text) and n < units:
        n += 2 if ord(text[i]) > 0xFFFF else 1
        i += 1
    return i


def _doc(ctx: Instance, req: Request, need: str = "reader") -> dict[str, Any]:
    fid = urllib.parse.unquote(req.params["documentId"])
    try:
        f = _file(ctx.state, fid, need=need, allow_trashed=False)
    except ToolError as e:
        if e.status == 404:
            raise error(404, "Requested entity was not found.") from None
        raise error(403, "The caller does not have permission") from None
    if f["mimeType"] != TYPES["doc"]:
        raise error(400, "This operation is not supported for this document")
    return f


def _revision(f: dict[str, Any]) -> str:
    return "ALm37B" + hashlib.sha1(f"{f['id']}{f['modifiedTime']}{f['content']}".encode()).hexdigest()[:40]


def _body(text: str) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"endIndex": 1, "sectionBreak": {"sectionStyle": {
        "columnSeparatorStyle": "NONE", "contentDirection": "LEFT_TO_RIGHT", "sectionType": "CONTINUOUS"}}}]
    pos = 1
    for line in (text + "\n").split("\n")[:-1] if text else [""]:
        run = line + "\n"
        end = pos + _u16(run)
        heading = re.match(r"^(#{1,6})\s", line)
        style = {"namedStyleType": f"HEADING_{len(heading.group(1))}" if heading else "NORMAL_TEXT",
                 "direction": "LEFT_TO_RIGHT"}
        content.append({"startIndex": pos, "endIndex": end, "paragraph": {
            "elements": [{"startIndex": pos, "endIndex": end, "textRun": {"content": run, "textStyle": {}}}],
            "paragraphStyle": style}})
        pos = end
    return {"content": content}


def _document(f: dict[str, Any], tabs: bool) -> dict[str, Any]:
    body = _body(f["content"] or "")
    style = {"background": {"color": {}}, "pageNumberStart": 1, "marginTop": {"magnitude": 72, "unit": "PT"},
             "marginBottom": {"magnitude": 72, "unit": "PT"}, "marginRight": {"magnitude": 72, "unit": "PT"},
             "marginLeft": {"magnitude": 72, "unit": "PT"},
             "pageSize": {"height": {"magnitude": 792, "unit": "PT"}, "width": {"magnitude": 612, "unit": "PT"}}}
    out: dict[str, Any] = {"title": f["name"]}
    if tabs:
        out["tabs"] = [{"tabProperties": {"tabId": "t.0", "title": "Tab 1", "index": 0},
                        "documentTab": {"body": body, "documentStyle": style}}]
    else:
        out.update(body=body, documentStyle=style,
                   namedStyles={"styles": [{"namedStyleType": n, "textStyle": {}, "paragraphStyle": {}}
                                           for n in ("NORMAL_TEXT", "TITLE", "SUBTITLE", "HEADING_1", "HEADING_2",
                                                     "HEADING_3", "HEADING_4", "HEADING_5", "HEADING_6")]})
    out.update(revisionId=_revision(f), suggestionsViewMode="SUGGESTIONS_INLINE", documentId=f["id"])
    return out


@op("docs.documents.get", "GET", "/{documentId}", read_only=True)
def documents_get(ctx: Instance, req: Request) -> Any:
    return select(_document(_doc(ctx, req), req.bool_arg("includeTabsContent")), req.arg("fields"))


@operation("docs.documents.create", "POST", BASE, hosts=HOSTS)
def documents_create(ctx: Instance, req: Request) -> Any:
    s = ctx.state
    root = _file(s, "root", need="writer")
    f = _new_file(ctx, s, req.json().get("title") or "Untitled document", TYPES["doc"], root["id"], owner=s["me"],
                  content="")
    return _document(f, False)


@op("docs.documents.batchUpdate", "POST", "/{documentId}:batchUpdate")
def documents_batch_update(ctx: Instance, req: Request) -> Any:
    f = _doc(ctx, req, need="writer")
    b = req.json()
    want = (b.get("writeControl") or {}).get("requiredRevisionId")
    if want and want != _revision(f):
        raise error(400, "The document was modified after the specified revision. Try again with the latest revision.")
    text = f["content"] or ""
    replies: list[dict[str, Any]] = []
    for i, request in enumerate(b.get("requests") or []):
        if not isinstance(request, dict) or len(request) != 1:
            raise error(400, f"Invalid requests[{i}]: exactly one kind of request is required")
        kind, r = next(iter(request.items()))
        where = f"requests[{i}].{kind}"
        end = _u16(text) + 2  # the body's text plus its final newline, from index 1
        if kind == "insertText":
            ins = r.get("text", "")
            if r.get("endOfSegmentLocation") is not None:
                at = len(text)
            else:
                idx = int((r.get("location") or {}).get("index", 0))
                if idx < 1:
                    raise error(400, f"Invalid {where}: Index {idx} must be greater than or equal to 1.")
                if idx >= end:
                    raise error(400, f"Invalid {where}: Index {idx} must be less than the end index of the "
                                     f"referenced segment, {end}.")
                at = _py(text, idx)
            text = text[:at] + ins + text[at:]
            replies.append({})
        elif kind == "deleteContentRange":
            rng = r.get("range") or {}
            a, z = int(rng.get("startIndex", 0)), int(rng.get("endIndex", 0))
            if z <= a:
                raise error(400, f"Invalid {where}: The range should not be empty.")
            if a < 1:
                raise error(400, f"Invalid {where}: Index {a} must be greater than or equal to 1.")
            if z >= end:
                raise error(400, f"Invalid {where}: The range cannot include the newline character at the end of "
                                 "the segment.")
            text = text[:_py(text, a)] + text[_py(text, z):]
            replies.append({})
        elif kind == "replaceAllText":
            find = (r.get("containsText") or {}).get("text", "")
            if not find:
                raise error(400, f"Invalid {where}: The find text must not be empty.")
            flags = 0 if (r.get("containsText") or {}).get("matchCase") else re.I
            repl = r.get("replaceText", "")
            text, n = re.subn(re.escape(find), lambda _, repl=repl: repl, text, flags=flags)
            replies.append({"replaceAllText": {"occurrencesChanged": n}} if n else {"replaceAllText": {}})
        elif kind == "insertPageBreak":
            idx = int((r.get("location") or {}).get("index", end - 1))
            at = _py(text, idx) if r.get("endOfSegmentLocation") is None else len(text)
            text = text[:at] + "\n" + text[at:]
            replies.append({})
        elif kind in FORMAT_ONLY or kind in ("insertTable", "insertInlineImage", "insertTableRow",
                                            "insertTableColumn", "deleteTableRow", "deleteTableColumn"):
            replies.append({})  # structure and styling: accepted, not modeled (the text is what's graded)
        else:
            raise error(400, f"Invalid JSON payload received. Unknown name \"{kind}\" at 'requests[{i}]': "
                             "Cannot find field.")
    f["content"], f["size"], f["modifiedTime"] = text, len(text.encode()), _iso(ctx)
    return {"replies": replies, "writeControl": {"requiredRevisionId": _revision(f)}, "documentId": f["id"]}
