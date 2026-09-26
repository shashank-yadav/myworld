"""drive: the Google Sheets REST API (v4): values (get/update/append/clear, batches), spreadsheets
(get/create/batchUpdate), on the same cells as the Drive file.

The small things: ``valueInputOption`` is required on writes; ``values`` is left out of an empty
read; ranges come back normalized (``Sheet1!A1:C3``); ``append`` finds the table and writes after
it; FORMATTED_VALUE / UNFORMATTED_VALUE / FORMULA render the same cells differently.
"""

from __future__ import annotations

import copy
import re
import urllib.parse
from typing import Any

from ...api import Request, operation
from ...api.google import error, select
from ...core.instance import Instance
from ...core.tools import ToolError
from .model import TYPES, _file, _iso, _new_file
from .sheets import (
    _a1,
    _FormulaError,
    _letters,
    _new_sheet,
    _parse_input,
    _sheets,
    _sync_content,
    _value,
)

HOSTS = ("sheets.googleapis.com",)
BASE = "/v4/spreadsheets"
FORMAT_ONLY = {"repeatCell", "mergeCells", "unmergeCells", "updateBorders", "autoResizeDimensions",
               "updateDimensionProperties", "addConditionalFormatRule", "deleteConditionalFormatRule",
               "updateConditionalFormatRule", "setBasicFilter", "clearBasicFilter", "addBanding", "deleteBanding",
               "updateBanding", "setDataValidation", "addNamedRange", "deleteNamedRange", "updateNamedRange",
               "addProtectedRange", "deleteProtectedRange", "updateProtectedRange", "addFilterView",
               "deleteFilterView", "updateFilterView", "addChart", "deleteEmbeddedObject", "updateChartSpec",
               "textToColumns", "sortRange", "randomizeRange", "addDimensionGroup", "deleteDimensionGroup",
               "updateDimensionGroup", "autoFill", "cutPaste", "copyPaste", "updateEmbeddedObjectPosition",
               "trimWhitespace", "deleteDuplicates", "addSlicer", "updateSlicerSpec"}


def op(op_id: str, method: str, path: str, **kw: Any):  # noqa: ANN201
    return operation(op_id, method, BASE + path, hosts=HOSTS, **kw)


def _book(ctx: Instance, req: Request, need: str = "reader") -> dict[str, Any]:
    fid = urllib.parse.unquote(req.params["spreadsheetId"])
    try:
        f = _file(ctx.state, fid, need=need, allow_trashed=False)
    except ToolError as e:
        if e.status == 404:
            raise error(404, "Requested entity was not found.") from None
        raise error(403, "The caller does not have permission") from None
    if f["mimeType"] != TYPES["sheet"]:
        raise error(400, "This operation is not supported for this document")
    _sheets(f)
    return f


def _range(f: dict[str, Any], rng: str) -> tuple[dict[str, Any], int, int, int, int, bool]:
    try:
        return _a1(f, urllib.parse.unquote(rng))
    except ToolError as e:
        raise error(400, e.payload["error"]["message"]) from None


def _title(sheet: dict[str, Any]) -> str:
    t = sheet["title"]
    return t if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", t) else "'" + t.replace("'", "''") + "'"


def _a1_text(sheet: dict[str, Any], r1: int, c1: int, r2: int, c2: int) -> str:
    a = f"{_letters(c1)}{r1 + 1}"
    b = f"{_letters(c2)}{r2 + 1}"
    return f"{_title(sheet)}!{a}" + ("" if a == b else f":{b}")


def _render(sheets: list[dict[str, Any]], sheet: dict[str, Any], r: int, c: int, how: str) -> Any:
    raw = sheet["rows"][r][c] if r < len(sheet["rows"]) and c < len(sheet["rows"][r]) else ""
    if how == "FORMULA":
        return raw["f"] if isinstance(raw, dict) else raw
    try:
        v = _value(sheets, sheet, r, c)
    except _FormulaError as e:
        return str(e)
    if how == "UNFORMATTED_VALUE":
        if isinstance(v, float) and v.is_integer():
            return int(v)
        return v
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else f"{v:.10g}"
    return "" if v is None else str(v)


def _read(f: dict[str, Any], rng: str, how: str, major: str) -> dict[str, Any]:
    if how not in ("FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA"):
        raise error(400, f"Invalid value at 'value_render_option' ({how})")
    sheet, r1, c1, r2, c2, _ = _range(f, rng)
    rows = []
    for r in range(r1, min(r2, len(sheet["rows"]) - 1) + 1):
        width = min(c2, len(sheet["rows"][r]) - 1)
        row = [_render(f["sheets"], sheet, r, c, how) for c in range(c1, width + 1)]
        while row and row[-1] == "":
            row.pop()
        rows.append(row)
    while rows and not rows[-1]:
        rows.pop()
    out: dict[str, Any] = {"range": _a1_text(sheet, r1, c1, r2, c2), "majorDimension": major}
    if major == "COLUMNS" and rows:
        width = max(len(r) for r in rows)
        rows = [[r[i] if i < len(r) else "" for r in rows] for i in range(width)]
        rows = [col[:max((k + 1 for k, v in enumerate(col) if v != ""), default=0)] for col in rows]
    if rows:
        out["values"] = rows
    return out


@op("sheets.spreadsheets.values.get", "GET", "/{spreadsheetId}/values/{range}", read_only=True)
def values_get(ctx: Instance, req: Request) -> Any:
    f = _book(ctx, req)
    return _read(f, req.params["range"], req.arg("valueRenderOption") or "FORMATTED_VALUE",
                 req.arg("majorDimension") or "ROWS")


@op("sheets.spreadsheets.values.batchGet", "GET", "/{spreadsheetId}/values:batchGet", read_only=True)
def values_batch_get(ctx: Instance, req: Request) -> Any:
    f = _book(ctx, req)
    how, major = req.arg("valueRenderOption") or "FORMATTED_VALUE", req.arg("majorDimension") or "ROWS"
    return {"spreadsheetId": f["id"], "valueRanges": [_read(f, r, how, major) for r in req.args("ranges")]}


def _input_option(req: Request, body: dict[str, Any] | None = None) -> bool:
    opt = req.arg("valueInputOption") or (body or {}).get("valueInputOption")
    if not opt:
        raise error(400, "'valueInputOption' is required but not specified")
    if opt not in ("RAW", "USER_ENTERED"):
        raise error(400, f"Invalid value at 'value_input_option' ({opt})")
    return opt == "USER_ENTERED"


def _write(ctx: Instance, f: dict[str, Any], rng: str, values: Any, user_entered: bool, major: str = "ROWS",
           at: tuple[int, int] | None = None) -> dict[str, Any]:
    sheet, r1, c1, r2, c2, bounded = _range(f, rng)
    if at:
        r1, c1 = at
    if not isinstance(values, list) or not all(isinstance(r, list) for r in values):
        raise error(400, "Invalid values: expected a list of lists")
    if major == "COLUMNS" and values:
        width = max(len(c) for c in values)
        values = [[c[i] if i < len(c) else None for c in values] for i in range(width)]
    if not values:
        return {"spreadsheetId": f["id"], "updatedRange": _a1_text(sheet, r1, c1, r1, c1)}
    width = max(len(r) for r in values)
    if bounded and at is None and (r1 + len(values) - 1 > r2 or c1 + width - 1 > c2):
        where = f"row [{r1 + len(values)}]" if r1 + len(values) - 1 > r2 else f"column [{_letters(c1 + width - 1)}]"
        raise error(400, f"Requested writing within range [{urllib.parse.unquote(rng)}], but tried writing to {where}")
    if r1 + len(values) > sheet["rowCount"]:
        sheet["rowCount"] = r1 + len(values)  # writing past the grid grows it (append does this too)
    if c1 + width > sheet["columnCount"]:
        raise error(400, f"Range ('{sheet['title']}'!{_letters(c1)}{r1 + 1}) exceeds grid limits. Max rows: "
                         f"{sheet['rowCount']}, max columns: {sheet['columnCount']}")
    cells = 0
    for dr, row in enumerate(values):
        r = r1 + dr
        while len(sheet["rows"]) <= r:
            sheet["rows"].append([])
        line = sheet["rows"][r]
        for dc, v in enumerate(row):
            if v is None:
                continue  # null leaves the cell as it is
            while len(line) <= c1 + dc:
                line.append("")
            line[c1 + dc] = _parse_input(v, user_entered)
            cells += 1
    _sync_content(f)
    f["modifiedTime"] = _iso(ctx)
    return {"spreadsheetId": f["id"], "updatedRange": _a1_text(sheet, r1, c1, r1 + len(values) - 1, c1 + width - 1),
            "updatedRows": len(values), "updatedColumns": width, "updatedCells": cells}


@op("sheets.spreadsheets.values.update", "PUT", "/{spreadsheetId}/values/{range}")
def values_update(ctx: Instance, req: Request) -> Any:
    f = _book(ctx, req, need="writer")
    b = req.json()
    out = _write(ctx, f, req.params["range"], b.get("values") or [], _input_option(req), b.get("majorDimension", "ROWS"))
    if req.bool_arg("includeValuesInResponse"):
        out["updatedData"] = _read(f, out["updatedRange"], req.arg("responseValueRenderOption") or "FORMATTED_VALUE",
                                   "ROWS")
    return out


@op("sheets.spreadsheets.values.append", "POST", "/{spreadsheetId}/values/{range}:append")
def values_append(ctx: Instance, req: Request) -> Any:
    f = _book(ctx, req, need="writer")
    user_entered = _input_option(req)
    mode = req.arg("insertDataOption") or "OVERWRITE"
    if mode not in ("OVERWRITE", "INSERT_ROWS"):
        raise error(400, f"Invalid value at 'insert_data_option' ({mode})")
    sheet, r1, c1, r2, c2, _ = _range(f, req.params["range"])
    b = req.json()
    values = b.get("values") or []
    # the "table": from the range's first row, the run of rows with data in the range's columns
    last = None
    r = r1
    while r < len(sheet["rows"]) and any(str(sheet["rows"][r][c]) != "" for c in range(c1, min(c2 + 1, len(sheet["rows"][r])))):
        last = r
        r += 1
    if last is None:
        nonempty = [i for i in range(r1, len(sheet["rows"]))
                    if any(str(v) != "" for v in sheet["rows"][i][c1:c2 + 1])]
        last = nonempty[-1] if nonempty else None
    start = (last + 1) if last is not None else r1
    table = _a1_text(sheet, r1, c1, last, max(c1, min(c2, max((len(sheet["rows"][i]) - 1 for i in range(r1, last + 1)),
                                                               default=c1)))) if last is not None else None
    if mode == "INSERT_ROWS" and values:
        for _ in values:
            sheet["rows"].insert(start, [])
        sheet["rowCount"] += len(values)
    updates = _write(ctx, f, req.params["range"], values, user_entered, b.get("majorDimension", "ROWS"),
                     at=(start, c1))
    out: dict[str, Any] = {"spreadsheetId": f["id"]}
    if table:
        out["tableRange"] = table
    out["updates"] = updates
    return out


@op("sheets.spreadsheets.values.clear", "POST", "/{spreadsheetId}/values/{range}:clear")
def values_clear(ctx: Instance, req: Request) -> Any:
    f = _book(ctx, req, need="writer")
    sheet, r1, c1, r2, c2, _ = _range(f, req.params["range"])
    for r in range(r1, min(r2, len(sheet["rows"]) - 1) + 1):
        for c in range(c1, min(c2, len(sheet["rows"][r]) - 1) + 1):
            sheet["rows"][r][c] = ""
    _sync_content(f)
    f["modifiedTime"] = _iso(ctx)
    return {"spreadsheetId": f["id"], "clearedRange": _a1_text(sheet, r1, c1, r2, c2)}


@op("sheets.spreadsheets.values.batchUpdate", "POST", "/{spreadsheetId}/values:batchUpdate")
def values_batch_update(ctx: Instance, req: Request) -> Any:
    f = _book(ctx, req, need="writer")
    b = req.json()
    user_entered = _input_option(req, b)
    responses = [_write(ctx, f, d["range"], d.get("values") or [], user_entered, d.get("majorDimension", "ROWS"))
                 for d in b.get("data") or []]
    return {"spreadsheetId": f["id"], "totalUpdatedRows": sum(r.get("updatedRows", 0) for r in responses),
            "totalUpdatedColumns": sum(r.get("updatedColumns", 0) for r in responses),
            "totalUpdatedCells": sum(r.get("updatedCells", 0) for r in responses),
            "totalUpdatedSheets": len({r["updatedRange"].split("!")[0] for r in responses}), "responses": responses}


@op("sheets.spreadsheets.values.batchClear", "POST", "/{spreadsheetId}/values:batchClear")
def values_batch_clear(ctx: Instance, req: Request) -> Any:
    f = _book(ctx, req, need="writer")
    cleared = []
    for rng in req.json().get("ranges") or []:
        sheet, r1, c1, r2, c2, _ = _range(f, rng)
        for r in range(r1, min(r2, len(sheet["rows"]) - 1) + 1):
            for c in range(c1, min(c2, len(sheet["rows"][r]) - 1) + 1):
                sheet["rows"][r][c] = ""
        cleared.append(_a1_text(sheet, r1, c1, r2, c2))
    _sync_content(f)
    return {"spreadsheetId": f["id"], "clearedRanges": cleared}


# -- spreadsheets -------------------------------------------------------------------------------------

def _props(sheet: dict[str, Any], index: int) -> dict[str, Any]:
    return {"sheetId": sheet["sheetId"], "title": sheet["title"], "index": index, "sheetType": "GRID",
            "gridProperties": {"rowCount": sheet["rowCount"], "columnCount": sheet["columnCount"]}}


def _cell(sheets: list[dict[str, Any]], sheet: dict[str, Any], r: int, c: int) -> dict[str, Any]:
    raw = sheet["rows"][r][c] if c < len(sheet["rows"][r]) else ""
    if raw == "":
        return {}
    uv = ({"formulaValue": raw["f"]} if isinstance(raw, dict) else {"numberValue": raw}
          if isinstance(raw, (int, float)) and not isinstance(raw, bool) else {"stringValue": str(raw)})
    shown = _render(sheets, sheet, r, c, "FORMATTED_VALUE")
    val = _render(sheets, sheet, r, c, "UNFORMATTED_VALUE")
    ev = {"numberValue": val} if isinstance(val, (int, float)) and not isinstance(val, bool) else {"stringValue": str(val)}
    return {"userEnteredValue": uv, "effectiveValue": ev, "formattedValue": shown}


def _book_json(ctx: Instance, f: dict[str, Any], req: Request) -> dict[str, Any]:
    tz = ctx.state.get("timeZone", "America/Los_Angeles")
    sheets_out = []
    wanted = {urllib.parse.unquote(r).split("!")[0].strip("'") for r in req.args("ranges")}
    for i, sh in enumerate(f["sheets"]):
        item: dict[str, Any] = {"properties": _props(sh, i)}
        if req.bool_arg("includeGridData") and (not wanted or sh["title"] in wanted):
            item["data"] = [{"rowData": [{"values": [_cell(f["sheets"], sh, r, c) for c in range(len(row))]}
                                         for r, row in enumerate(sh["rows"])]}]
        sheets_out.append(item)
    return {"spreadsheetId": f["id"],
            "properties": {"title": f["name"], "locale": "en_US", "autoRecalc": "ON_CHANGE", "timeZone": tz,
                           "defaultFormat": {"backgroundColor": {"red": 1, "green": 1, "blue": 1},
                                             "padding": {"top": 2, "right": 3, "bottom": 2, "left": 3},
                                             "verticalAlignment": "BOTTOM", "wrapStrategy": "OVERFLOW_CELL",
                                             "textFormat": {"fontFamily": "arial,sans,sans-serif", "fontSize": 10}}},
            "sheets": sheets_out, "spreadsheetUrl": f"https://docs.google.com/spreadsheets/d/{f['id']}/edit"}


@op("sheets.spreadsheets.get", "GET", "/{spreadsheetId}", read_only=True)
def spreadsheets_get(ctx: Instance, req: Request) -> Any:
    return select(_book_json(ctx, _book(ctx, req), req), req.arg("fields"))


@operation("sheets.spreadsheets.create", "POST", BASE, hosts=HOSTS)
def spreadsheets_create(ctx: Instance, req: Request) -> Any:
    s = ctx.state
    b = req.json()
    title = (b.get("properties") or {}).get("title") or "Untitled spreadsheet"
    names = [(x.get("properties") or {}).get("title") or f"Sheet{i + 1}" for i, x in enumerate(b.get("sheets") or [{}])]
    if len(set(names)) != len(names):
        raise error(400, "Invalid spreadsheet: sheet names must be unique")
    root = _file(s, "root", need="writer")
    f = _new_file(ctx, s, title, TYPES["sheet"], root["id"], owner=s["me"], content="")
    f["sheets"] = [_new_sheet(i if i else 0, n) for i, n in enumerate(names)]
    for i, sh in enumerate(f["sheets"][1:], 1):
        sh["sheetId"] = ctx.next("sheet_id", 1000000) + i
    _sync_content(f)
    return select(_book_json(ctx, f, req), req.arg("fields"))


def _sheet_by_id(f: dict[str, Any], sid: Any, where: str) -> dict[str, Any]:
    sh = next((x for x in f["sheets"] if x["sheetId"] == sid), None)
    if sh is None:
        raise error(400, f"Invalid {where}: No grid with id: {sid}")
    return sh


@op("sheets.spreadsheets.batchUpdate", "POST", "/{spreadsheetId}:batchUpdate")
def spreadsheets_batch_update(ctx: Instance, req: Request) -> Any:
    f = _book(ctx, req, need="writer")
    before = copy.deepcopy(f["sheets"]), f["name"]
    replies: list[dict[str, Any]] = []
    try:
        for i, request in enumerate(req.json().get("requests") or []):
            if not isinstance(request, dict) or len(request) != 1:
                raise error(400, f"Invalid requests[{i}]: exactly one kind of request is required")
            kind, body = next(iter(request.items()))
            where = f"requests[{i}].{kind}"
            replies.append(_apply(ctx, f, kind, body or {}, where))
    except Exception:
        f["sheets"], f["name"] = before  # a batch applies all or nothing
        raise
    _sync_content(f)
    f["modifiedTime"] = _iso(ctx)
    out: dict[str, Any] = {"spreadsheetId": f["id"], "replies": replies}
    if req.json().get("includeSpreadsheetInResponse"):
        out["updatedSpreadsheet"] = _book_json(ctx, f, req)
    return out


def _apply(ctx: Instance, f: dict[str, Any], kind: str, b: dict[str, Any], where: str) -> dict[str, Any]:
    sheets = f["sheets"]
    if kind == "addSheet":
        p = b.get("properties") or {}
        title = p.get("title") or f"Sheet{len(sheets) + 1}"
        if any(s["title"].lower() == title.lower() for s in sheets):
            raise error(400, f'Invalid {where}: A sheet with the name "{title}" already exists. Please enter another name.')
        sh = _new_sheet(p.get("sheetId") or ctx.next("sheet_id", 1000000), title)
        grid = p.get("gridProperties") or {}
        sh["rowCount"], sh["columnCount"] = grid.get("rowCount", sh["rowCount"]), grid.get("columnCount", sh["columnCount"])
        idx = p.get("index", len(sheets))
        sheets.insert(idx, sh)
        return {"addSheet": {"properties": _props(sh, sheets.index(sh))}}
    if kind == "deleteSheet":
        sh = _sheet_by_id(f, b.get("sheetId"), where)
        if len(sheets) == 1:
            raise error(400, f"Invalid {where}: You can't remove all the sheets in a document.")
        sheets.remove(sh)
        return {}
    if kind == "duplicateSheet":
        src = _sheet_by_id(f, b.get("sourceSheetId"), where)
        sh = copy.deepcopy(src)
        sh["sheetId"] = b.get("newSheetId") or ctx.next("sheet_id", 1000000)
        sh["title"] = b.get("newSheetName") or f"Copy of {src['title']}"
        sheets.insert(b.get("insertSheetIndex", len(sheets)), sh)
        return {"duplicateSheet": {"properties": _props(sh, sheets.index(sh))}}
    if kind == "updateSheetProperties":
        p = b.get("properties") or {}
        sh = _sheet_by_id(f, p.get("sheetId", 0), where)
        fields = [x.strip() for x in (b.get("fields") or "").split(",") if x.strip()]
        if not fields:
            raise error(400, f"Invalid {where}: At least one field must be updated. Specify 'fields'.")
        if "title" in fields or "*" in fields:
            title = p.get("title", sh["title"])
            if any(x is not sh and x["title"].lower() == title.lower() for x in sheets):
                raise error(400, f'Invalid {where}: A sheet with the name "{title}" already exists. Please enter another name.')
            sh["title"] = title
        if "index" in fields and "index" in p:
            sheets.remove(sh)
            sheets.insert(p["index"], sh)
        grid = p.get("gridProperties") or {}
        if "rowCount" in grid:
            sh["rowCount"] = grid["rowCount"]
        if "columnCount" in grid:
            sh["columnCount"] = grid["columnCount"]
        return {}
    if kind == "updateSpreadsheetProperties":
        p = b.get("properties") or {}
        if "title" in p:
            f["name"] = p["title"]
        return {}
    if kind in ("insertDimension", "deleteDimension", "appendDimension"):
        rng = b.get("range") or b
        sh = _sheet_by_id(f, rng.get("sheetId", 0), where)
        dim = rng.get("dimension", "ROWS")
        if kind == "appendDimension":
            key = "rowCount" if dim == "ROWS" else "columnCount"
            sh[key] += int(b.get("length", 0))
            return {}
        a, z = int(rng.get("startIndex", 0)), int(rng.get("endIndex", 0))
        if z <= a:
            raise error(400, f"Invalid {where}: The range must not be empty.")
        n = z - a
        if dim == "ROWS":
            if kind == "insertDimension":
                for _ in range(n):
                    sh["rows"].insert(a, [])
                sh["rowCount"] += n
            else:
                del sh["rows"][a:z]
                sh["rowCount"] -= n
        else:
            for row in sh["rows"]:
                if kind == "insertDimension":
                    row[a:a] = [""] * n if len(row) > a else []
                else:
                    del row[a:z]
            sh["columnCount"] += n if kind == "insertDimension" else -n
        return {}
    if kind == "findReplace":
        find, repl = b.get("find", ""), b.get("replacement", "")
        if not find:
            raise error(400, f"Invalid {where}: The find text must not be empty.")
        targets = sheets if b.get("allSheets") else [_sheet_by_id(f, (b.get("range") or {}).get("sheetId", b.get("sheetId", 0)), where)]
        flags = 0 if b.get("matchCase") else re.I
        pattern = find if b.get("searchByRegex") else re.escape(find)
        if b.get("matchEntireCell"):
            pattern = f"^{pattern}$"
        changed = rows = 0
        for sh in targets:
            for row in sh["rows"]:
                hit = False
                for c, v in enumerate(row):
                    if isinstance(v, str) and re.search(pattern, v, flags):
                        row[c] = re.sub(pattern, repl, v, flags=flags)
                        changed += 1
                        hit = True
                rows += hit
        return {"findReplace": {"valuesChanged": changed, "occurrencesChanged": changed, "rowsChanged": rows,
                                "sheetsChanged": len(targets), "formulasChanged": 0}}
    if kind == "updateCells":
        start = b.get("start") or {}
        sh = _sheet_by_id(f, start.get("sheetId", (b.get("range") or {}).get("sheetId", 0)), where)
        r0, c0 = start.get("rowIndex", 0), start.get("columnIndex", 0)
        for dr, row in enumerate(b.get("rows") or []):
            for dc, cell in enumerate(row.get("values") or []):
                uv = cell.get("userEnteredValue")
                if uv is None:
                    continue
                v = uv.get("formulaValue") or uv.get("stringValue") or uv.get("numberValue") or uv.get("boolValue") or ""
                r, c = r0 + dr, c0 + dc
                while len(sh["rows"]) <= r:
                    sh["rows"].append([])
                while len(sh["rows"][r]) <= c:
                    sh["rows"][r].append("")
                sh["rows"][r][c] = _parse_input(v, "formulaValue" in uv)
        return {}
    if kind in FORMAT_ONLY:
        return {}  # formatting: accepted, not modeled (values don't change)
    raise error(400, f"Invalid JSON payload received. Unknown name \"{kind}\" at 'requests[{where.split('[')[1].split(']')[0]}]': "
                     "Cannot find field.")
