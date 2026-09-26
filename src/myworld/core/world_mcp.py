"""Aggregate MCP server for one shared environment run.

This is the shape agent platforms want for real practice worlds: one MCP server backed by one
``EnvRun``. Tools are namespaced by server (``gmail__search_emails``, ``drive__read_sheet_values``)
so an agent can work across apps while all calls share one clock, journal, snapshots and grade.
"""

from __future__ import annotations

import copy
import json
import sys
from typing import Any, TextIO

from .faults import TransportFault
from .mcp import SUPPORTED_VERSIONS, _error, _ok
from .. import __version__
from ..env import EnvRun


class WorldMCP:
    def __init__(self, run: EnvRun, *, agent: str = "agent", as_: str | None = None):
        if agent not in run.env.agents:
            raise ValueError(f"unknown agent {agent!r}; known agents: {', '.join(sorted(run.env.agents))}")
        self.run = run
        self.agent = agent
        self.as_ = as_ or run.env.agents[agent].get("as")
        self.snapshots: dict[str, dict[str, Any]] = {}
        self.forks: dict[str, EnvRun] = {}
        self._snap_n = 0

    def list_tools(self) -> list[dict[str, Any]]:
        tools = [_world_tool(
            "world_task",
            "Return this world's task, current agent and available servers.",
            {},
            read_only=True,
        ), _world_tool(
            "world_grade",
            "Grade the current world state against the world's checks. Pass answer to grade a final response.",
            {"answer": {"type": "string", "description": "Optional final answer to grade."}},
            read_only=True,
        ), _world_tool(
            "world_submit",
            "Record a final answer and grade the current world state.",
            {"answer": {"type": "string", "description": "Final answer from the agent."}},
            required=["answer"],
        ), _world_tool(
            "world_snapshot",
            "Save an in-memory snapshot of the full shared world and return its snapshot_id.",
            {},
            read_only=True,
        ), _world_tool(
            "world_restore",
            "Restore the full shared world from a snapshot_id returned by world_snapshot.",
            {"snapshot_id": {"type": "string"}},
            required=["snapshot_id"],
        ), _world_tool(
            "world_diff",
            "Return state changes since a checkpoint step. Defaults to changes since the start.",
            {"since": {"type": "integer", "default": 0}, "until": {"type": "integer"}},
            read_only=True,
        ), _world_tool(
            "world_calls",
            "Return all tool calls across the shared world in chronological order.",
            {},
            read_only=True,
        ), _world_tool(
            "world_timeline",
            "Return tool calls and world events across the shared world in chronological order.",
            {},
            read_only=True,
        ), _world_tool(
            "world_fork",
            "Create an in-memory fork of the current shared world and return its fork_id.",
            {"fork_id": {"type": "string", "description": "Optional id for the fork."}},
            read_only=True,
        )]
        for server in self.run.env.agent_servers(self.agent):
            inst = self.run.instances[server]
            for t in inst.list_tools():
                nt = copy.deepcopy(t)
                nt["name"] = f"{server}__{t['name']}"
                nt["description"] = f"[{server}] {t.get('description', '')}".strip()
                tools.append(nt)
        return tools

    def call(self, name: str, args: dict[str, Any] | None = None) -> tuple[str, bool]:
        args = args or {}
        if name.startswith("world_"):
            return _json(self._world_call(name, args)), False
        if "__" not in name:
            return f"Unknown tool: {name}", True
        server, tool = name.split("__", 1)
        if server not in self.run.env.agent_servers(self.agent) or server not in self.run.instances:
            return f"Unknown server for this agent: {server}", True
        inst = self.run.instances[server]
        if tool not in inst.tools:
            return f"Unknown tool for {server}: {tool}", True
        r = inst.call(tool, args, agent=self.agent, as_=self.as_)
        return r.text, r.is_error

    def _world_call(self, name: str, args: dict[str, Any]) -> Any:
        if name == "world_task":
            spec = self.run.env.agents[self.agent]
            return {"world": self.run.env.name, "agent": self.agent, "as": self.as_,
                    "task": (spec.get("task") or self.run.env.task).strip(),
                    "servers": self.run.env.agent_servers(self.agent)}
        if name == "world_grade":
            return self.run.grade(args.get("answer"))
        if name == "world_submit":
            answer = str(args.get("answer") or "")
            self.run.answer = answer
            return self.run.grade(answer)
        if name == "world_snapshot":
            self._snap_n += 1
            sid = f"snap-{self._snap_n}"
            self.snapshots[sid] = self.run.snapshot()
            return {"snapshot_id": sid}
        if name == "world_restore":
            sid = str(args.get("snapshot_id") or "")
            if sid not in self.snapshots:
                raise ValueError(f"unknown snapshot_id {sid!r}")
            self.run.restore(self.snapshots[sid])
            return {"restored": sid}
        if name == "world_diff":
            return self.run.diff(int(args.get("since") or 0), args.get("until"))
        if name == "world_calls":
            return {"calls": self.run.calls()}
        if name == "world_timeline":
            return {"timeline": self.run.timeline()}
        if name == "world_fork":
            fid = str(args.get("fork_id") or f"{self.run.id}-fork-{len(self.forks) + 1}")
            if fid in self.forks:
                raise ValueError(f"fork {fid!r} already exists")
            self.forks[fid] = self.run.fork(fid)
            return {"fork_id": fid}
        raise ValueError(f"unknown world tool {name!r}")


def handle(world: WorldMCP, msg: Any) -> Any:
    if isinstance(msg, list):
        out = [r for r in (handle(world, m) for m in msg) if r is not None]
        return out or None
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _error(None, -32600, "Invalid Request")
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if mid is None:
        return None
    try:
        if method == "initialize":
            requested = params.get("protocolVersion")
            return _ok(mid, {
                "protocolVersion": requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "myworld", "title": f"myworld: {world.run.env.name}",
                               "version": __version__},
                "instructions": "A shared myworld practice environment. Use world_task first, then call namespaced tools.",
            })
        if method == "ping":
            return _ok(mid, {})
        if method == "tools/list":
            return _ok(mid, {"tools": world.list_tools()})
        if method == "tools/call":
            text, is_error = world.call(str(params.get("name") or ""), params.get("arguments") or {})
            return _ok(mid, {"content": [{"type": "text", "text": text}], "isError": is_error})
        if method in ("resources/list", "resources/templates/list"):
            return _ok(mid, {"resources": [], "resourceTemplates": []})
        if method == "prompts/list":
            return _ok(mid, {"prompts": []})
        return _error(mid, -32601, f"Method not found: {method}")
    except TransportFault:
        raise
    except Exception as e:
        return _error(mid, -32603, f"Internal error: {type(e).__name__}: {e}")


def serve_stdio(world: WorldMCP, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
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
                resp = handle(world, msg)
            except TransportFault as e:
                resp = _error(msg.get("id") if isinstance(msg, dict) else None, -32000,
                              f"Upstream connection failed (HTTP {e.status})")
        if resp is not None:
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()


def _world_tool(name: str, description: str, props: dict[str, Any], *, read_only: bool = False,
                required: list[str] | None = None) -> dict[str, Any]:
    schema = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        schema["required"] = required
    return {"name": name, "description": description, "inputSchema": schema,
            "annotations": {"readOnlyHint": read_only, "destructiveHint": False,
                            "idempotentHint": read_only, "openWorldHint": True}}


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)
