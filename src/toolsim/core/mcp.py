"""The Model Context Protocol, server side: just enough to be a drop-in replacement for the real
MCP server an agent is configured with.

Implements ``initialize``, ``ping``, ``tools/list`` and ``tools/call`` (plus empty
``resources/list`` / ``prompts/list``), over stdio (newline-delimited JSON-RPC) or HTTP
(the "streamable HTTP" transport, answering with plain JSON).
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from .faults import TransportFault
from .instance import Instance

SUPPORTED_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]


def handle(instance: Instance, msg: Any, *, agent: str | None = None, as_: str | None = None) -> Any:
    """Handle one JSON-RPC message (or a batch). Returns the response, or None for notifications.
    ``agent``/``as_`` identify the connected agent and who it acts as (multi-agent environments)."""
    if isinstance(msg, list):
        out = [r for r in (handle(instance, m, agent=agent, as_=as_) for m in msg) if r is not None]
        return out or None
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _error(None, -32600, "Invalid Request")
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if mid is None:  # notification (notifications/initialized, cancelled, ...)
        return None
    try:
        if method == "initialize":
            requested = params.get("protocolVersion")
            return _ok(mid, {
                "protocolVersion": requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": instance.service.name, "title": instance.service.title,
                               "version": instance.version},
                "instructions": instance.service.description,
            })
        if method == "ping":
            return _ok(mid, {})
        if method == "tools/list":
            return _ok(mid, {"tools": instance.list_tools()})
        if method == "tools/call":
            name = params.get("name")
            if name not in instance.tools:
                return _error(mid, -32602, f"Unknown tool: {name}")
            r = instance.call(name, params.get("arguments") or {}, agent=agent, as_=as_)
            return _ok(mid, {"content": [{"type": "text", "text": r.text}], "isError": r.is_error})
        if method in ("resources/list", "resources/templates/list"):
            return _ok(mid, {"resources": [], "resourceTemplates": []})
        if method == "prompts/list":
            return _ok(mid, {"prompts": []})
        return _error(mid, -32601, f"Method not found: {method}")
    except TransportFault:
        raise  # not an MCP answer at all: the transport layer decides what the client sees
    except Exception as e:  # a bug in a fake must look like a server error, not kill the transport
        return _error(mid, -32603, f"Internal error: {type(e).__name__}: {e}")


def _ok(mid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def serve_stdio(instance: Instance, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout, *,
                agent: str | None = None, as_: str | None = None) -> None:
    """For agents configured with a command (``command: toolsim, args: [stdio, gmail]``)."""
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            resp: Any = _error(None, -32700, "Parse error")
        else:
            try:
                resp = handle(instance, msg, agent=agent, as_=as_)
            except TransportFault as e:  # over stdio, a dead upstream looks like a JSON-RPC server error
                resp = _error(msg.get("id") if isinstance(msg, dict) else None, -32000,
                              f"Upstream connection failed (HTTP {e.status})")
        if resp is not None:
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()
