"""github: file tools."""

from __future__ import annotations

from typing import Annotated, Any

from ...core.instance import Instance
from ...core.tools import tool
from .model import (
    API,
    _ancestors,
    _branch,
    _check_push,
    _commit,
    _commit_json,
    _content_json,
    _err,
    _iso,
    _not_found,
    _paginate,
    _pushed,
    _put_blob,
    _ref_sha,
    _repo,
    _unprocessable,
)


@tool("create_or_update_file")
def create_or_update_file(ctx: Instance,
                          owner: Annotated[str, "Repository owner (username or organization)"],
                          repo: Annotated[str, "Repository name"],
                          path: Annotated[str, "Path where to create/update the file"],
                          content: Annotated[str, "Content of the file"],
                          message: Annotated[str, "Commit message"],
                          branch: Annotated[str, "Branch to create/update the file in"],
                          sha: Annotated[str | None, "SHA of the file being replaced (required when updating existing files)"] = None) -> dict[str, Any]:
    """Create or update a single file in a GitHub repository"""
    s = ctx.state
    r = _repo(s, owner, repo)
    head = _branch(r, branch)
    _check_push(s, r, branch)
    tree = dict(r["commits"][head]["tree"])
    if path in tree:
        if not sha:
            raise _unprocessable('Invalid request.\n\n"sha" wasn\'t supplied.')
        if sha != tree[path]:
            raise _err(409, f"{path} does not match {sha}")
    elif sha:
        raise _err(404, "Not Found")
    tree[path] = _put_blob(s, content)
    new = _commit(ctx, s, r, tree, [head], message, s["viewer"])
    r["branches"][branch] = new
    r["updated_at"] = _iso(ctx)
    _pushed(ctx, r, new)
    return {"content": _content_json(r, path, tree[path], content, branch), "commit": _commit_json(r, new)}


@tool("push_files")
def push_files(ctx: Instance,
               owner: Annotated[str, "Repository owner (username or organization)"],
               repo: Annotated[str, "Repository name"],
               branch: Annotated[str, "Branch to push to (e.g., 'main' or 'master')"],
               files: Annotated[list[dict], "Array of files to push, each {path, content}"],
               message: Annotated[str, "Commit message"]) -> dict[str, Any]:
    """Push multiple files to a GitHub repository in a single commit"""
    s = ctx.state
    r = _repo(s, owner, repo)
    head = _branch(r, branch)
    _check_push(s, r, branch)
    if not files:
        raise _unprocessable("Invalid request: files must not be empty")
    tree = dict(r["commits"][head]["tree"])
    for f in files:
        if not isinstance(f, dict) or "path" not in f or "content" not in f:
            raise _unprocessable("Invalid request: each file needs path and content")
        tree[f["path"]] = _put_blob(s, f["content"])
    new = _commit(ctx, s, r, tree, [head], message, s["viewer"])
    r["branches"][branch] = new
    _pushed(ctx, r, new)
    return {"ref": f"refs/heads/{branch}", "node_id": f"REF_kwDO{new[:10]}",
            "url": f"{API}/repos/{r['full_name']}/git/refs/heads/{branch}", "object": {"sha": new, "type": "commit"}}


@tool("get_file_contents", read_only=True)
def get_file_contents(ctx: Instance,
                      owner: Annotated[str, "Repository owner (username or organization)"],
                      repo: Annotated[str, "Repository name"],
                      path: Annotated[str, "Path to the file or directory"],
                      branch: Annotated[str | None, "Branch to get contents from"] = None) -> Any:
    """Get the contents of a file or directory from a GitHub repository"""
    s = ctx.state
    r = _repo(s, owner, repo)
    ref = branch or r["default_branch"]
    tree = r["commits"][_ref_sha(r, ref)]["tree"]
    path = path.strip("/")
    if path in tree:
        return _content_json(r, path, tree[path], s["blobs"][tree[path]], ref)
    prefix = f"{path}/" if path else ""
    entries: dict[str, dict[str, Any]] = {}
    for p, sha in tree.items():
        if p.startswith(prefix):
            rest = p[len(prefix):]
            name = rest.split("/", 1)[0]
            is_dir = "/" in rest
            entries[name] = {"type": "dir" if is_dir else "file", "name": name, "path": prefix + name,
                             "sha": "" if is_dir else sha, "size": 0 if is_dir else len(s["blobs"][sha].encode()),
                             "url": f"{API}/repos/{r['full_name']}/contents/{prefix + name}?ref={ref}"}
    if not entries:
        raise _not_found()
    return sorted(entries.values(), key=lambda e: (e["type"] != "dir", e["name"]))


@tool("create_branch")
def create_branch(ctx: Instance,
                  owner: Annotated[str, "Repository owner (username or organization)"],
                  repo: Annotated[str, "Repository name"],
                  branch: Annotated[str, "Name for the new branch"],
                  from_branch: Annotated[str | None, "Optional: source branch to create from (defaults to the repository's default branch)"] = None) -> dict[str, Any]:
    """Create a new branch in a GitHub repository"""
    r = _repo(ctx.state, owner, repo)
    if branch in r["branches"]:
        raise _unprocessable("Reference already exists")
    sha = _branch(r, from_branch or r["default_branch"])
    r["branches"][branch] = sha
    return {"ref": f"refs/heads/{branch}", "node_id": f"REF_kwDO{sha[:10]}",
            "url": f"{API}/repos/{r['full_name']}/git/refs/heads/{branch}", "object": {"sha": sha, "type": "commit"}}


@tool("list_commits", read_only=True)
def list_commits(ctx: Instance,
                 owner: Annotated[str, "Repository owner (username or organization)"],
                 repo: Annotated[str, "Repository name"],
                 sha: Annotated[str | None, "SHA or branch name to start listing commits from"] = None,
                 page: Annotated[str | None, "Page number"] = None,
                 per_page: Annotated[str | None, "Results per page (max 100)"] = None) -> list[dict[str, Any]]:
    """Get list of commits of a branch in a GitHub repository"""
    r = _repo(ctx.state, owner, repo)
    return [_commit_json(r, c) for c in _paginate(_ancestors(r, _ref_sha(r, sha)), page, per_page)]
