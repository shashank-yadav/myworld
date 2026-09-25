"""github: world actions (triggered by environments, never by agents)."""

from __future__ import annotations

from typing import Any

from ...core.instance import Instance
from ...core.tools import action
from .issues import _issue
from .model import _branch, _comment, _commit, _iso, _not_found, _pushed, _put_blob, _repo, _unprocessable, _user


def _repo_named(state: dict[str, Any], full_name: str) -> dict[str, Any]:
    owner, _, name = full_name.partition("/")
    return _repo(state, owner, name)


@action("set_status")
def act_set_status(ctx: Instance, repo: str, ref: str, context: str, state: str, description: str = "") -> None:
    """CI reports a status on a branch or commit (e.g. flips from success to failure)."""
    if state not in ("success", "failure", "error", "pending"):
        raise _unprocessable(f"invalid state {state}")
    r = _repo_named(ctx.state, repo)
    sha = r["branches"].get(ref, ref)
    sts = [x for x in r["statuses"].get(sha, []) if x["context"] != context]
    r["statuses"][sha] = sts + [{"context": context, "state": state, "description": description}]


@action("push_commit")
def act_push_commit(ctx: Instance, repo: str, branch: str, files: dict, author: str, message: str = "Update") -> str:
    """A teammate pushes to a branch (e.g. main moves under an open PR, causing a conflict)."""
    s = ctx.state
    r = s["repos"].get(repo) or (_ for _ in ()).throw(_not_found())
    head = _branch(r, branch)
    tree = dict(r["commits"][head]["tree"])
    tree.update({p: _put_blob(s, c) for p, c in files.items()})
    r["branches"][branch] = _commit(ctx, s, r, tree, [head], message, author)
    _pushed(ctx, r, r["branches"][branch])
    return r["branches"][branch]


@action("add_comment")
def act_add_comment(ctx: Instance, repo: str, number: int, author: str, body: str) -> None:
    """A teammate comments on an issue or pull request."""
    r = _repo_named(ctx.state, repo)
    _issue(r, number)
    _user(ctx, ctx.state, author)
    _comment(ctx, r, number, author, body)


@action("set_issue_state")
def act_set_issue_state(ctx: Instance, repo: str, number: int, state: str) -> None:
    """Someone opens or closes an issue."""
    r = _repo_named(ctx.state, repo)
    i = _issue(r, number)
    i["state"] = state
    i["closed_at"] = _iso(ctx) if state == "closed" else None
