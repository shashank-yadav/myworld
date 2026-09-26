"""jira: world actions (triggered by environments, never by agents)."""

from __future__ import annotations

from ...core.instance import Instance
from ...core.tools import action
from .model import STATUSES, _comment, _err, _get, _iso, _resolve_user, _set_sprint


@action("set_status")
def act_set_status(ctx: Instance, issue_key: str, status: str) -> None:
    """Someone moves an issue (bypassing the agent's view of the workflow)."""
    if status not in STATUSES:
        raise _err(f"unknown status {status}")
    i = _get(ctx.state, issue_key)
    i["status"] = status
    done = STATUSES[status] == "done"
    i["resolution"], i["resolutiondate"] = ("Done", _iso(ctx)) if done else (None, None)
    i["updated"] = _iso(ctx)


@action("add_comment")
def act_add_comment(ctx: Instance, issue_key: str, author: str, body: str) -> None:
    """A colleague comments on an issue."""
    _comment(ctx, _get(ctx.state, issue_key), _resolve_user(ctx.state, author), body)


@action("assign")
def act_assign(ctx: Instance, issue_key: str, assignee: str | None) -> None:
    """Someone reassigns an issue."""
    i = _get(ctx.state, issue_key)
    i["assignee"] = _resolve_user(ctx.state, assignee)
    i["updated"] = _iso(ctx)


@action("move_to_sprint")
def act_move_to_sprint(ctx: Instance, issue_key: str, sprint: str | None) -> None:
    """A teammate pulls an issue into a sprint (by id or name) or back to the backlog (None)."""
    s = ctx.state
    sid = None
    if sprint:
        sid = next((sp["id"] for sp in s["sprints"].values() if sprint in (sp["id"], sp["name"])), None)
        if sid is None:
            raise _err(f"no sprint {sprint}")
    i = _get(s, issue_key)
    _set_sprint(s, i, sid)
    i["updated"] = _iso(ctx)
