"""A tiny email/calendar MCP server for live tests. Every side effect is appended to
$FAKE_MCP_LOG so tests can compare what the agent really did with what aops recorded."""

import json
import os
import time

try:  # mcp >= 2.0
    from mcp.server import MCPServer
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer

mcp = MCPServer("acme")
LOG = os.environ.get("FAKE_MCP_LOG", "/tmp/fake_mcp.log")


def _record(action: str, **fields) -> None:
    with open(LOG, "a") as f:
        f.write(json.dumps({"ts": time.time(), "action": action, **fields}) + "\n")


@mcp.tool()
def search_emails(query: str) -> str:
    """Search the inbox. Returns matching emails."""
    return json.dumps([{"id": "m1", "from": "john@acme.com", "subject": "Q4 planning"}])


@mcp.tool()
def send_email(to: str, subject: str, body: str) -> str:
    """Send an email."""
    _record("send_email", to=to, subject=subject)
    return json.dumps({"id": "msg_1", "status": "sent"})


@mcp.tool()
def create_calendar_event(title: str, start: str) -> str:
    """Create a calendar event."""
    _record("create_calendar_event", title=title, start=start)
    return json.dumps({"id": "evt_1", "status": "confirmed"})


@mcp.tool()
def delete_calendar_event(event_id: str) -> str:
    """Delete a calendar event by id."""
    raise ValueError(f"404 Not Found: no calendar event {event_id}")


if __name__ == "__main__":
    mcp.run()
