"""drive: Sheets tools (cells, A1 ranges, formulas)."""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Annotated, Any, Literal

from ...core.instance import Instance
from ...core.tools import tool
from .files import EMAIL
from .model import TYPES, V1, _err, _file, _iso, _my_role, _new_file

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
    s = ctx.search_view("files")
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
