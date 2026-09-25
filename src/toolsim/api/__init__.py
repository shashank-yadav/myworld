"""REST surfaces: the real services' HTTP APIs, served from the simulated worlds.

Agents that use real clients (``gog``, ``gh``, Google's Python client, Octokit) talk to these
through the gateway (``toolsim.gateway``), which answers for ``*.googleapis.com`` and
``api.github.com``. Each service declares its operations in ``services/<tool>/api.py``:

    @operation("gmail.users.messages.list", "GET", "/gmail/v1/users/{userId}/messages", read_only=True)
    def list_messages(ctx, req): ...

An operation runs like any tool call (recorded under its id, faults, rollback, notifications,
the shared clock), gets a ``Request`` and returns a JSON-able value, a ``Response``, or raises
``ToolError`` with the service's error body and HTTP status.
"""

from __future__ import annotations

import base64
import importlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..core.tools import Tool


@dataclass
class Request:
    method: str
    path: str
    params: dict[str, str]                   # path parameters
    query: dict[str, list[str]]              # query string (repeated keys kept)
    body: Any = None                         # parsed JSON, or bytes for uploads
    headers: dict[str, str] = field(default_factory=dict)
    host: str = ""

    def arg(self, name: str, default: Any = None) -> Any:
        v = self.query.get(name)
        return v[-1] if v else default

    def args(self, name: str) -> list[str]:
        return list(self.query.get(name) or [])

    def int_arg(self, name: str, default: int, maximum: int | None = None) -> int:
        v = self.arg(name)
        if v in (None, ""):
            return default
        n = int(v)
        return min(n, maximum) if maximum else n

    def bool_arg(self, name: str, default: bool = False) -> bool:
        v = self.arg(name)
        return default if v is None else str(v).lower() in ("1", "true", "yes")

    def json(self) -> dict[str, Any]:
        return self.body if isinstance(self.body, dict) else {}


@dataclass
class Response:
    status: int = 200
    body: Any = None                          # JSON-able, or bytes
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Operation:
    id: str
    method: str
    path: str
    service: str
    hosts: tuple[str, ...]
    tool: Tool
    pattern: re.Pattern[str]


_OPS: list[Operation] = []
_LOADED: set[str] = set()


def operation(op_id: str, method: str, path: str, *, service: str | None = None, hosts: tuple[str, ...] = (),
              read_only: bool = False, destructive: bool = False) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare a REST operation. ``path`` uses ``{name}`` for a segment and ``{name+}`` for the rest."""
    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        svc = service or fn.__module__.split(".")[-2]
        regex = re.sub(r"\{(\w+)\+\}", r"(?P<\1>.+)", re.escape(path).replace(r"\{", "{").replace(r"\}", "}"))
        regex = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", regex)
        def run(ctx: Any, args: dict[str, Any]) -> Any:
            body = args.get("body")
            if isinstance(body, dict) and "__bytes__" in body:
                body = base64.b64decode(body["__bytes__"])
            return fn(ctx, Request(args.get("method", method), args.get("path", path), dict(args.get("params") or {}),
                                   dict(args.get("query") or {}), body, dict(args.get("headers") or {}),
                                   args.get("host", "")))

        t = Tool(name=op_id, fn=run, description=(fn.__doc__ or "").strip(), input_schema={"type": "object"},
                 read_only=read_only, destructive=destructive, idempotent=read_only or method in ("GET", "PUT", "DELETE"),
                 raw=True)
        _OPS.append(Operation(op_id, method.upper(), path, svc, hosts, t, re.compile(f"^{regex}$")))
        return fn
    return wrap


def load(service: str) -> None:
    if service not in _LOADED:
        _LOADED.add(service)
        try:
            importlib.import_module(f"toolsim.services.{service}.api")
        except ModuleNotFoundError as e:
            if e.name != f"toolsim.services.{service}.api":
                raise


def resolve(host: str, method: str, path: str, services: set[str]) -> tuple[Operation, dict[str, str]] | None:
    """The operation for a request, among the given services' surfaces."""
    for s in services:
        load(s)
    allowed = None
    for op in _OPS:
        if op.service not in services or (op.hosts and host not in op.hosts):
            continue
        m = op.pattern.match(path)
        if m:
            if op.method == method.upper():
                return op, {k: v for k, v in m.groupdict().items()}
            allowed = op
    if allowed is not None:
        return allowed, {"__method_not_allowed__": "1"}
    return None


def operations(service: str) -> list[Operation]:
    load(service)
    return [o for o in _OPS if o.service == service]
