"""jira: JQL parsing and evaluation."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from ...core.tools import ToolError
from .model import PRIORITIES, STATUSES, _err, _resolve_user

_TOKEN = re.compile(r'\s*(?:(\()|(\))|(,)|(!=|>=|<=|!~|=|~|>|<)|"([^"]*)"|\'([^\']*)\'|([A-Za-z0-9_.@\-+/:]+(?:\(\))?))')
_KEYWORDS = {"and", "or", "not", "in", "is", "order", "by", "asc", "desc", "empty", "null"}


def _tokenize(jql: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    jql = jql.strip()
    while pos < len(jql):
        m = _TOKEN.match(jql, pos)
        if not m or m.end() == pos:
            raise _err(f"Error in the JQL Query: The character '{jql[pos]}' is a reserved JQL character.")
        pos = m.end()
        lp, rp, comma, op, dq, sq, word = m.groups()
        if lp:
            out.append(("(", "("))
        elif rp:
            out.append((")", ")"))
        elif comma:
            out.append((",", ","))
        elif op:
            out.append(("op", op))
        elif dq is not None or sq is not None:
            out.append(("str", dq if dq is not None else sq))
        else:
            out.append(("kw" if word.lower() in _KEYWORDS else "word", word))
    return out


class _JQL:
    def __init__(self, state: dict[str, Any], jql: str, now: dt.datetime):
        self.state, self.now = state, now
        self.toks = _tokenize(jql)
        self.i = 0
        self.order: list[tuple[str, bool]] = []
        self.tree = self._or() if self._peek() and not self._is_kw("order") else None
        if self._is_kw("order"):
            self.i += 1
            self._expect_kw("by")
            while self._peek():
                field = self._next()[1].lower()
                desc = False
                if self._is_kw("asc") or self._is_kw("desc"):
                    desc = self._next()[1].lower() == "desc"
                self.order.append((field, desc))
                if self._peek() and self._peek()[0] == ",":
                    self.i += 1
        if self._peek():
            raise _err(f"Error in the JQL Query: Expecting end of query but got '{self._peek()[1]}'.")

    def _peek(self) -> tuple[str, str] | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _next(self) -> tuple[str, str]:
        t = self._peek()
        if t is None:
            raise _err("Error in the JQL Query: The JQL query is incomplete.")
        self.i += 1
        return t

    def _is_kw(self, w: str) -> bool:
        t = self._peek()
        return bool(t and t[0] == "kw" and t[1].lower() == w)

    def _expect_kw(self, w: str) -> None:
        if not self._is_kw(w):
            raise _err(f"Error in the JQL Query: Expecting '{w}'.")
        self.i += 1

    def _or(self) -> Any:
        node = self._and()
        while self._is_kw("or"):
            self.i += 1
            node = ("or", node, self._and())
        return node

    def _and(self) -> Any:
        node = self._not()
        while self._is_kw("and"):
            self.i += 1
            node = ("and", node, self._not())
        return node

    def _not(self) -> Any:
        if self._is_kw("not"):
            self.i += 1
            return ("not", self._not())
        if self._peek() and self._peek()[0] == "(":
            self.i += 1
            node = self._or()
            if not self._peek() or self._next()[0] != ")":
                raise _err("Error in the JQL Query: Expecting ')'.")
            return node
        return self._clause()

    def _value(self) -> Any:
        t = self._next()
        if t[0] == "kw" and t[1].lower() in ("empty", "null"):
            return None
        return t[1]

    def _clause(self) -> Any:
        field = self._next()
        if field[0] not in ("word", "str"):
            raise _err(f"Error in the JQL Query: Expecting a field name but got '{field[1]}'.")
        name = field[1].lower()
        if name not in _FIELDS or (name == "sprint" and not self.state.get("_v1")):
            raise _err(f"Error in the JQL Query: Field '{field[1]}' does not exist or you do not have permission to view it.")
        if self._is_kw("not"):
            self.i += 1
            self._expect_kw("in")
            return ("not in", name, self._func() or self._list())
        if self._is_kw("in"):
            self.i += 1
            return ("in", name, self._func() or self._list())
        if self._is_kw("is"):
            self.i += 1
            neg = self._is_kw("not")
            if neg:
                self.i += 1
            self._value()
            return ("is not empty" if neg else "is empty", name, None)
        op = self._next()
        if op[0] != "op":
            raise _err(f"Error in the JQL Query: Expecting operator but got '{op[1]}'.")
        return (op[1], name, self._value())

    def _func(self) -> list[Any] | None:
        """``in openSprints()`` and friends: a function instead of a (list)."""
        t = self._peek()
        if t and t[0] == "word" and t[1].endswith("()"):
            self.i += 1
            return [t[1]]
        return None

    def _list(self) -> list[Any]:
        if self._next()[0] != "(":
            raise _err("Error in the JQL Query: Expecting '('.")
        vals = []
        while True:
            vals.append(self._value())
            t = self._next()
            if t[0] == ")":
                return vals
            if t[0] != ",":
                raise _err("Error in the JQL Query: Expecting ',' or ')'.")

    # evaluation
    def matches(self, i: dict[str, Any]) -> bool:
        return self.tree is None or self._eval(self.tree, i)

    def _eval(self, node: Any, i: dict[str, Any]) -> bool:
        op = node[0]
        if op == "and":
            return self._eval(node[1], i) and self._eval(node[2], i)
        if op == "or":
            return self._eval(node[1], i) or self._eval(node[2], i)
        if op == "not":
            return not self._eval(node[1], i)
        _, field, value = node
        have = self._field(i, field)
        if op == "is empty":
            return have in (None, [], "")
        if op == "is not empty":
            return have not in (None, [], "")
        vals = [self._norm_value(field, v) for v in (value if isinstance(value, list) else [value])]
        vals = [x for v in vals for x in (v if isinstance(v, tuple) else [v])]  # functions expand to many
        if op in ("in", "="):
            return any(self._eq(have, v) for v in vals)
        if op in ("not in", "!="):
            return not any(self._eq(have, v) for v in vals)
        if op in ("~", "!~"):
            text = " ".join(have) if isinstance(have, list) else str(have or "")
            hit = all(w in text.lower() for w in str(value).lower().replace("*", "").split())
            return hit if op == "~" else not hit
        if field in ("created", "updated", "duedate", "resolved", "resolutiondate"):
            if have is None:
                return False
            a = _to_dt(have)
            b = self._date(value)
            return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[op]
        if field == "priority":
            order = {p.lower(): n for n, p in enumerate(reversed(PRIORITIES))}
            a, b = order.get(str(have).lower(), -1), order.get(str(value).lower(), -1)
            return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[op]
        raise _err(f"Error in the JQL Query: The operator '{op}' is not supported by the '{field}' field.")

    def _field(self, i: dict[str, Any], field: str) -> Any:
        return {
            "project": i["project"], "key": i["key"], "issuekey": i["key"], "id": i["id"],
            "summary": i["summary"], "description": i["description"],
            "text": f"{i['summary']} {i['description'] or ''} {' '.join(c['body'] for c in i['comments'])}",
            "status": i["status"], "statuscategory": {"new": "To Do", "indeterminate": "In Progress", "done": "Done"}[STATUSES[i["status"]]],
            "type": i["issue_type"], "issuetype": i["issue_type"], "priority": i["priority"],
            "assignee": i["assignee"], "reporter": i["reporter"], "labels": i["labels"], "label": i["labels"],
            "component": i["components"], "resolution": i["resolution"], "created": i["created"], "updated": i["updated"],
            "duedate": i["duedate"], "resolved": i["resolutiondate"], "resolutiondate": i["resolutiondate"],
            "parent": i["parent"] or i["epic"], "\"epic link\"": i["epic"], "epic link": i["epic"],
            "watcher": i["watchers"],
            "sprint": [*i.get("closed_sprints", []), *([i["sprint"]] if i.get("sprint") else [])],
        }.get(field)

    def _norm_value(self, field: str, v: Any) -> Any:
        if isinstance(v, str) and v.lower() == "currentuser()":
            return self.state["me"]
        if field == "sprint" and v is not None:
            sprints = self.state["sprints"].values()
            fn = {"opensprints()": ("active", "future"), "closedsprints()": ("closed",),
                  "futuresprints()": ("future",)}.get(str(v).lower())
            if fn:
                return tuple(sp["id"] for sp in sprints if sp["state"] in fn)
            if str(v).lower().endswith("()"):
                raise _err(f"Error in the JQL Query: Unable to find JQL function '{v}'.")
            hit = next((sp["id"] for sp in sprints if sp["id"] == str(v) or sp["name"].lower() == str(v).lower()), None)
            if hit is None:
                raise _err(f"Error in the JQL Query: The value '{v}' does not exist for the field 'sprint'.")
            return hit
        if field in ("assignee", "reporter", "watcher") and isinstance(v, str):
            try:
                return _resolve_user(self.state, v)
            except ToolError:
                return v
        if field == "resolution" and isinstance(v, str) and v.lower() == "unresolved":
            return None
        return v

    @staticmethod
    def _eq(have: Any, v: Any) -> bool:
        if isinstance(have, list):
            return any(str(h).lower() == str(v).lower() for h in have)
        if have is None or v is None:
            return have is v
        return str(have).lower() == str(v).lower()

    def _date(self, v: Any) -> dt.datetime:
        s = str(v).strip().lower()
        if s in ("now()", "now"):
            return self.now
        if s.startswith("startofday"):
            return self.now.replace(hour=0, minute=0, second=0, microsecond=0)
        m = re.fullmatch(r"([-+]?\d+)([wdhm])", s)
        if m:
            n, unit = int(m.group(1)), m.group(2)
            return self.now + dt.timedelta(**{{"w": "weeks", "d": "days", "h": "hours", "m": "minutes"}[unit]: n})
        try:
            t = dt.datetime.fromisoformat(s.replace("/", "-"))
        except ValueError:
            raise _err(f"Error in the JQL Query: Date value '{v}' for field is invalid.") from None
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)

    def sort(self, issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for field, desc in reversed(self.order or [("created", True)]):
            def key(i: dict[str, Any], f: str = field) -> Any:
                if f == "priority":
                    return PRIORITIES[::-1].index(i["priority"])
                if f in ("key", "issuekey"):
                    return (i["project"], int(i["key"].split("-")[1]))
                v = self._field(i, f)
                return (v is None, str(v or ""))
            issues.sort(key=key, reverse=desc)
        return issues


_FIELDS = {"project", "key", "issuekey", "id", "summary", "description", "text", "status", "statuscategory", "type",
           "issuetype", "priority", "assignee", "reporter", "labels", "label", "component", "resolution", "created",
           "updated", "duedate", "resolved", "resolutiondate", "parent", "\"epic link\"", "epic link", "watcher", "sprint"}


def _to_dt(s: str) -> dt.datetime:
    return dt.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc) if "T" in s \
        else dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc)
