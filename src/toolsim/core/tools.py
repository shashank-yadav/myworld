"""Declaring a service's tools.

A tool is a plain function ``fn(ctx, **args)`` decorated with :func:`tool`. Its JSON Schema is
generated from the signature, so what agents see in ``tools/list`` always matches what the
function accepts. Parameter descriptions come from ``Annotated[type, "description"]``.
"""

from __future__ import annotations

import inspect
import types
import typing
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, get_args, get_origin, get_type_hints


class ToolError(Exception):
    """A failure the real service would report (not found, invalid argument, ...).

    ``payload`` is the service-shaped error body the agent sees, e.g. Slack's
    ``{"ok": false, "error": "channel_not_found"}`` or GitHub's ``{"message": "Not Found"}``.
    """

    def __init__(self, payload: Any, *, status: int = 400):
        super().__init__(str(payload))
        self.payload = payload
        self.status = status


@dataclass
class Tool:
    name: str
    fn: Callable[..., Any]
    description: str
    input_schema: dict[str, Any]
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    params: dict[str, inspect.Parameter] = field(default_factory=dict)
    since: str | None = None   # first service version (date) that has this tool
    until: str | None = None   # first service version that no longer has it

    def in_version(self, version: str) -> bool:
        return (self.since is None or version >= self.since) and (self.until is None or version < self.until)

    def mcp_definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {
                "readOnlyHint": self.read_only,
                "destructiveHint": self.destructive,
                "idempotentHint": self.idempotent,
                "openWorldHint": True,
            },
        }


def tool(name: str | None = None, *, read_only: bool = False, destructive: bool = False,
         idempotent: bool = False, description: str | None = None, since: str | None = None,
         until: str | None = None) -> Callable[[Callable[..., Any]], Tool]:
    def wrap(fn: Callable[..., Any]) -> Tool:
        sig = inspect.signature(fn)
        hints = get_type_hints(fn, include_extras=True)
        params = {n: p for n, p in list(sig.parameters.items())[1:]}  # skip ctx
        props, required = {}, []
        for pname, p in params.items():
            props[pname] = _schema(hints.get(pname, Any))
            if p.default is inspect.Parameter.empty:
                required.append(pname)
            elif p.default is not None:
                props[pname]["default"] = p.default
        schema: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
        if required:
            schema["required"] = required
        return Tool(
            name=name or fn.__name__, fn=fn,
            description=description or inspect.cleandoc(fn.__doc__ or ""),
            input_schema=schema, read_only=read_only, destructive=destructive,
            idempotent=idempotent or read_only, params=params, since=since, until=until,
        )
    return wrap


def _schema(tp: Any) -> dict[str, Any]:
    desc = None
    if get_origin(tp) is typing.Annotated:
        tp, *meta = get_args(tp)
        desc = next((m for m in meta if isinstance(m, str)), None)
    s = _base_schema(tp)
    if desc:
        s["description"] = desc
    return s


def _base_schema(tp: Any) -> dict[str, Any]:
    origin = get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        options = [a for a in get_args(tp) if a is not type(None)]
        return _base_schema(options[0]) if len(options) == 1 else {"anyOf": [_base_schema(a) for a in options]}
    if origin is Literal:
        values = list(get_args(tp))
        return {"type": "string" if all(isinstance(v, str) for v in values) else "integer", "enum": values}
    if origin in (list, typing.List):
        (item,) = get_args(tp) or (Any,)
        return {"type": "array", "items": _base_schema(item)}
    if origin in (dict, typing.Dict) or tp is dict:
        return {"type": "object"}
    return {str: {"type": "string"}, int: {"type": "integer"}, float: {"type": "number"},
            bool: {"type": "boolean"}}.get(tp, {})


def validate_args(t: Tool, args: dict[str, Any]) -> dict[str, Any]:
    """Light validation matching what real MCP servers enforce: unknown and missing args, basic types."""
    if not isinstance(args, dict):
        raise ToolError({"error": "arguments must be an object"})
    unknown = set(args) - set(t.params)
    if unknown:
        raise ToolError({"error": f"Unknown argument(s): {', '.join(sorted(unknown))}"})
    missing = [n for n in t.input_schema.get("required", []) if args.get(n) is None]
    if missing:
        raise ToolError({"error": f"Missing required argument(s): {', '.join(missing)}"})
    out = {}
    for n, v in args.items():
        s = t.input_schema["properties"][n]
        out[n] = _coerce(n, v, s)
    return out


def _coerce(name: str, v: Any, s: dict[str, Any]) -> Any:
    if v is None:
        return None
    typ = s.get("type")
    try:
        if typ == "integer" and not isinstance(v, bool):
            v = int(v)
        elif typ == "number":
            v = float(v)
        elif typ == "boolean" and isinstance(v, str):
            v = v.lower() in ("true", "1", "yes")
        elif typ == "array" and isinstance(v, str):
            v = [v]
    except (TypeError, ValueError):
        raise ToolError({"error": f"Invalid value for {name}: expected {typ}"}) from None
    if "enum" in s and v not in s["enum"]:
        raise ToolError({"error": f"Invalid value for {name}: must be one of {s['enum']}"})
    return v


@dataclass
class Action:
    """A change the *world* makes (mail arriving, a colleague booking a slot, CI finishing).
    Actions are never exposed to agents; environments trigger them as events."""
    name: str
    fn: Callable[..., Any]
    description: str


def action(name: str | None = None) -> Callable[[Callable[..., Any]], Action]:
    def wrap(fn: Callable[..., Any]) -> Action:
        return Action(name=name or fn.__name__, fn=fn, description=inspect.cleandoc(fn.__doc__ or ""))
    return wrap
