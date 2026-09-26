"""linear: world actions (triggered by environments, never by agents)."""

from __future__ import annotations

from ...core.instance import Instance
from ...core.tools import action
from .model import _comment, _find_issue, _find_state, _find_user, _iso, _set_state_times


@action("set_state")
def act_set_state(ctx: Instance, issue: str, state: str) -> None:
    """Someone moves an issue to another workflow state."""
    s = ctx.state
    i = _find_issue(s, issue)
    st = _find_state(s, i["teamId"], state)
    i["stateId"] = st["id"]
    _set_state_times(ctx, i, st)
    i["updatedAt"] = _iso(ctx)


@action("add_comment")
def act_add_comment(ctx: Instance, issue: str, author: str, body: str) -> None:
    """A colleague comments on an issue."""
    s = ctx.state
    _comment(ctx, s, _find_issue(s, issue)["id"], body, _find_user(s, author)["id"])
