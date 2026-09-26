"""drive: Drive's query language."""

from __future__ import annotations

import re
from typing import Any

from .model import _err

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
