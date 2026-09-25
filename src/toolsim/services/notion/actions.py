"""notion: world actions (triggered by environments, never by agents)."""

from __future__ import annotations

from typing import Any

from ...core.instance import Instance
from ...core.tools import action
from .model import ACCESS, _err, _iso, _not_found


def _titled(state: dict[str, Any], title: str) -> dict[str, Any]:
    p = next((p for p in state["pages"].values() if p["title"] == title), None)
    if p is None:
        raise _not_found(f"page titled {title!r}")
    return p


@action("edit_page")
def act_edit_page(ctx: Instance, title: str, content: str) -> None:
    """A teammate rewrites a page (what the agent read earlier is now stale)."""
    p = _titled(ctx.state, title)
    p["content"], p["last_edited_time"] = content, _iso(ctx)


@action("restrict_page")
def act_restrict_page(ctx: Instance, title: str, restricted: bool = True) -> None:
    """A page's permissions change: it disappears for everyone without access."""
    _titled(ctx.state, title)["restricted"] = restricted


@action("finish_duplicate")
def act_finish_duplicate(ctx: Instance, source: str, copy: str) -> None:
    """An asynchronous duplication completes: the copy gets the source's content (as it is now)."""
    src, dst = ctx.state["pages"].get(source), ctx.state["pages"].get(copy)
    if src is None or dst is None:
        return
    dst["content"], dst["last_edited_time"] = src["content"], _iso(ctx)


@action("set_access")
def act_set_access(ctx: Instance, title: str, access: str) -> None:
    """Someone changes your access to a page (e.g. to view-only)."""
    if access not in ACCESS:
        raise _err(f"access must be one of {', '.join(ACCESS)}")
    _titled(ctx.state, title)["access"] = access
