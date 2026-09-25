"""drive: world actions (triggered by environments, never by agents)."""

from __future__ import annotations

from typing import Any

from ...core.instance import Instance
from ...core.tools import action
from .model import ROLES, _err, _grant, _iso, _not_found
from .sheets import _new_sheet


def _named(state: dict[str, Any], file: str) -> dict[str, Any]:
    f = state["files"].get(file) or next((x for x in state["files"].values() if x["name"] == file), None)
    if f is None:
        raise _not_found(file)
    return f


@action("revoke_access")
def act_revoke_access(ctx: Instance, file: str, email: str | None = None) -> None:
    """The owner removes someone's access (default: the drive's own user), so the agent starts getting 404s."""
    email = email or ctx.state["me"]
    f = _named(ctx.state, file)
    f["permissions"] = [p for p in f["permissions"] if p.get("emailAddress", "").lower() != email.lower()
                        or p["role"] == "owner"]


@action("share")
def act_share(ctx: Instance, file: str, email: str, role: str = "reader") -> None:
    """Someone shares a file with a person."""
    if role not in ROLES:
        raise _err(400, f"invalid role {role}", "invalid")
    _grant(ctx, ctx.state, _named(ctx.state, file), "user", role, email.lower())


@action("edit_content")
def act_edit_content(ctx: Instance, file: str, content: str) -> None:
    """A collaborator edits a file (what the agent read earlier is now stale)."""
    f = _named(ctx.state, file)
    f["content"], f["size"], f["modifiedTime"] = content, len(content.encode()), _iso(ctx)
    if "sheets" in f:  # a collaborator rewrote the first sheet
        first = f["sheets"][0]
        f["sheets"][0] = {**_new_sheet(first["sheetId"], first["title"], content),
                          "rowCount": first["rowCount"], "columnCount": first["columnCount"]}


@action("trash")
def act_trash(ctx: Instance, file: str) -> None:
    """The owner moves a file to trash."""
    _named(ctx.state, file)["trashed"] = True
