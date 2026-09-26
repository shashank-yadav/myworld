"""A small GraphQL executor for simulated APIs (GitHub's v4 API).

No schema: objects are dicts carrying ``__typename``; a field is a plain value, or a callable
taking the field's arguments (for connections, nested lookups and anything computed). Queries,
mutations, variables, aliases, named and inline fragments (with interfaces), ``@include`` and
``@skip`` work as in GraphQL; asking for a field an object doesn't have fails like GitHub does
("Field 'x' doesn't exist on type 'Y'"), so gaps show up instead of silently returning nulls.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any

from graphql import (
    BooleanValueNode,
    EnumValueNode,
    FieldNode,
    FloatValueNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLError,
    InlineFragmentNode,
    IntValueNode,
    ListValueNode,
    NullValueNode,
    ObjectValueNode,
    OperationDefinitionNode,
    StringValueNode,
    VariableNode,
    parse,
)


class QueryError(Exception):
    """An error for the response's ``errors`` list (the field resolves to null)."""

    def __init__(self, message: str, type_: str | None = None):
        super().__init__(message)
        self.type = type_


def connection(items: list[Any], args: dict[str, Any], cursor_prefix: str = "Y3Vyc29y") -> dict[str, Any]:
    """A Relay connection over ``items`` with first/last/after/before."""
    def cur(i: int) -> str:
        return base64.b64encode(f"cursor:v2:{i}".encode()).decode()

    def index(c: str) -> int:
        try:
            return int(base64.b64decode(c).decode().rsplit(":", 1)[1])
        except Exception:
            raise QueryError(f"`{c}` does not appear to be a valid cursor.") from None

    lo, hi = 0, len(items)
    if args.get("after"):
        lo = index(args["after"]) + 1
    if args.get("before"):
        hi = index(args["before"])
    first, last = args.get("first"), args.get("last")
    for name, v in (("first", first), ("last", last)):
        if v is not None and (v < 0 or v > 100):
            raise QueryError(f"Requesting {v} records on the `{name}` connection exceeds the `{name}` limit of 100 "
                             "records." if v > 100 else f"`{name}` must be non-negative")
    if first is not None:
        hi = min(hi, lo + first)
    if last is not None:
        lo = max(lo, hi - last)
    window = list(range(lo, hi))
    return {"__typename": "Connection", "nodes": [items[i] for i in window],
            "edges": [{"__typename": "Edge", "node": items[i], "cursor": cur(i)} for i in window],
            "totalCount": len(items),
            "pageInfo": {"__typename": "PageInfo", "hasNextPage": hi < len(items), "hasPreviousPage": lo > 0,
                         "startCursor": cur(lo) if window else None, "endCursor": cur(hi - 1) if window else None}}


class Executor:
    def __init__(self, root_query: dict[str, Any], root_mutation: dict[str, Any] | None = None,
                 interfaces: dict[str, set[str]] | None = None):
        self.query = root_query
        self.mutation = root_mutation or {}
        self.interfaces = interfaces or {}

    def execute(self, source: str, variables: dict[str, Any] | None = None,
                operation_name: str | None = None) -> dict[str, Any]:
        try:
            doc = parse(source)
        except GraphQLError as e:
            loc = e.locations[0] if e.locations else None
            return {"errors": [{"message": f"Parse error on {e.message}",
                                "locations": [{"line": loc.line, "column": loc.column}] if loc else []}]}
        ops = [d for d in doc.definitions if isinstance(d, OperationDefinitionNode)]
        self.fragments = {d.name.value: d for d in doc.definitions if isinstance(d, FragmentDefinitionNode)}
        op = next((o for o in ops if o.name and o.name.value == operation_name), None) if operation_name else \
            (ops[0] if len(ops) == 1 else None)
        if op is None:
            return {"errors": [{"message": "An operation name is required" if len(ops) > 1 else "No operation found"}]}
        self.vars = dict(variables or {})
        for vd in op.variable_definitions or []:
            name = vd.variable.variable.name.value if hasattr(vd.variable, "variable") else vd.variable.name.value
            if name not in self.vars and vd.default_value is not None:
                self.vars[name] = self._value(vd.default_value)
        self.errors: list[dict[str, Any]] = []
        root = self.mutation if op.operation.value == "mutation" else self.query
        data = self._select({"__typename": "Mutation" if root is self.mutation else "Query", **root},
                            op.selection_set.selections, [])
        out: dict[str, Any] = {"data": data}
        if self.errors:
            out["errors"] = self.errors
        return out

    # -- values ------------------------------------------------------------------------------------

    def _value(self, node: Any) -> Any:
        if isinstance(node, VariableNode):
            return self.vars.get(node.name.value)
        if isinstance(node, IntValueNode):
            return int(node.value)
        if isinstance(node, FloatValueNode):
            return float(node.value)
        if isinstance(node, (StringValueNode, EnumValueNode)):
            return node.value
        if isinstance(node, BooleanValueNode):
            return node.value
        if isinstance(node, NullValueNode):
            return None
        if isinstance(node, ListValueNode):
            return [self._value(v) for v in node.values]
        if isinstance(node, ObjectValueNode):
            return {f.name.value: self._value(f.value) for f in node.fields}
        return None

    def _skip(self, node: Any) -> bool:
        for d in node.directives or []:
            args = {a.name.value: self._value(a.value) for a in d.arguments or []}
            if d.name.value == "skip" and args.get("if"):
                return True
            if d.name.value == "include" and not args.get("if"):
                return True
        return False

    def _applies(self, obj: dict[str, Any], type_name: str | None) -> bool:
        if not type_name:
            return True
        t = obj.get("__typename")
        return t == type_name or t in self.interfaces.get(type_name, set())

    # -- selections --------------------------------------------------------------------------------

    def _fields(self, obj: dict[str, Any], selections: Any) -> list[FieldNode]:
        out: list[FieldNode] = []
        for sel in selections:
            if self._skip(sel):
                continue
            if isinstance(sel, FieldNode):
                out.append(sel)
            elif isinstance(sel, InlineFragmentNode):
                cond = sel.type_condition.name.value if sel.type_condition else None
                if self._applies(obj, cond):
                    out += self._fields(obj, sel.selection_set.selections)
            elif isinstance(sel, FragmentSpreadNode):
                frag = self.fragments.get(sel.name.value)
                if frag is None:
                    raise QueryError(f"Fragment {sel.name.value} was used, but not defined")
                if self._applies(obj, frag.type_condition.name.value):
                    out += self._fields(obj, frag.selection_set.selections)
        return out

    def _select(self, obj: dict[str, Any], selections: Any, path: list[Any]) -> dict[str, Any] | None:
        out: dict[str, Any] = {}
        for f in self._fields(obj, selections):
            key = f.alias.value if f.alias else f.name.value
            try:
                out[key] = self._field(obj, f, path + [key])
            except QueryError as e:
                err: dict[str, Any] = {"message": str(e), "path": path + [key],
                                       "locations": [{"line": f.loc.start_token.line,
                                                      "column": f.loc.start_token.column}] if f.loc else []}
                if e.type:
                    err["type"] = e.type
                self.errors.append(err)
                out[key] = None
        return out

    def _field(self, obj: dict[str, Any], f: FieldNode, path: list[Any]) -> Any:
        name = f.name.value
        if name == "__typename":
            return obj.get("__typename")
        if name not in obj:
            raise QueryError(f"Field '{name}' doesn't exist on type '{obj.get('__typename', 'Object')}'",
                             "undefinedField")
        value = obj[name]
        if callable(value):
            args = {a.name.value: self._value(a.value) for a in f.arguments or []}
            value = value(args)
        return self._complete(value, f, path)

    def _complete(self, value: Any, f: FieldNode, path: list[Any]) -> Any:
        if value is None:
            return None
        if isinstance(value, list):
            return [self._complete(v, f, path + [i]) for i, v in enumerate(value)]
        if isinstance(value, dict) and f.selection_set is not None:
            return self._select(value, f.selection_set.selections, path)
        return value


Resolver = Callable[[dict[str, Any]], Any]
