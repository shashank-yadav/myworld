"""GitHub from a local clone, plus optional issues and pull requests exported with the GitHub CLI:

    gh issue list --state all --limit 200 --json number,title,body,state,labels,author,assignees,comments > issues.json
    gh pr list --state all --limit 100 --json number,title,body,state,headRefName,baseRefName,author,isDraft,comments > prs.json

Files come from the default branch; other local branches become branches with their changes.
Issue and pull request numbers are preserved.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from ...importers.common import ImportOptions

MAX_FILE_BYTES = 200_000
MAX_FILES = 2000


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def _text_at(repo: Path, ref: str, path: str) -> str | None:
    raw = subprocess.run(["git", "-C", str(repo), "show", f"{ref}:{path}"], capture_output=True).stdout
    if len(raw) > MAX_FILE_BYTES or b"\0" in raw[:8000]:
        return None  # large or binary: not simulated
    return raw.decode("utf-8", errors="replace")


def _login(opts: ImportOptions, login: str | None, name: str | None = None) -> str:
    if not login:
        return "ghost"
    if not opts.anonymize:
        return login
    _, fake = opts.person(f"{login.lower()}@users.noreply.github.com", name)
    return re.sub(r"[^a-z0-9]+", "-", fake.lower()).strip("-")


def import_repo(path: str | Path, opts: ImportOptions | None = None, *, issues: str | Path | None = None,
                pulls: str | Path | None = None, name: str | None = None, viewer: str | None = None) -> dict[str, Any]:
    opts = opts or ImportOptions()
    repo = Path(path)
    default = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if not name:
        remote = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"], capture_output=True,
                                text=True).stdout.strip()
        m = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?$", remote)
        name = f"{m.group(1)}/{m.group(2)}" if m else f"{viewer or 'me'}/{repo.resolve().name}"
    owner, repo_name = name.split("/", 1)
    if opts.anonymize:
        owner = "acme" if owner != (viewer or "") else _login(opts, owner)
    files = {}
    for p in _git(repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines()[:MAX_FILES]:
        content = _text_at(repo, "HEAD", p)
        if content is not None:
            files[p] = content
    branches = {}
    for b in _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").split():
        if b == default:
            continue
        changed = _git(repo, "diff", "--name-only", f"{default}...{b}").split()
        bfiles = {p: c for p in changed if (c := _text_at(repo, b, p)) is not None}
        if bfiles:
            msg = _git(repo, "log", "-1", "--format=%s", b).strip()
            author = _git(repo, "log", "-1", "--format=%an", b).strip()
            branches[b] = {"files": bfiles, "message": opts.text(msg), "author": _login(opts, author.replace(" ", "-").lower(), author)}
    seed_repo: dict[str, Any] = {"name": f"{owner}/{repo_name}", "default_branch": default, "files": files,
                                 "branches": branches, "private": True}
    users: dict[str, str] = {}

    def who(a: dict[str, Any] | None) -> str:
        login = _login(opts, (a or {}).get("login"), (a or {}).get("name"))
        users[login] = login
        return login

    labels: set[str] = set()
    if issues:
        seed_repo["issues"] = []
        for i in json.loads(Path(issues).read_text()):
            ls = [l["name"] if isinstance(l, dict) else l for l in i.get("labels", [])]
            labels.update(ls)
            seed_repo["issues"].append({"number": i["number"], "title": opts.text(i["title"]), "body": opts.text(i.get("body")),
                                        "state": str(i.get("state", "open")).lower(), "labels": ls, "author": who(i.get("author")),
                                        "assignees": [who(a) for a in i.get("assignees", [])],
                                        "comments": [{"author": who(c.get("author")), "body": opts.text(c.get("body", ""))}
                                                     for c in i.get("comments", [])]})
    if pulls:
        seed_repo["pulls"] = []
        for p in json.loads(Path(pulls).read_text()):
            head, base = p.get("headRefName"), p.get("baseRefName", default)
            if head not in branches or base not in (default, *branches):
                continue  # its branch isn't in this clone: can't be simulated faithfully
            seed_repo["pulls"].append({"number": p["number"], "title": opts.text(p["title"]), "body": opts.text(p.get("body")),
                                       "head": head, "base": base, "author": who(p.get("author")),
                                       "draft": bool(p.get("isDraft")), "state": "closed" if p.get("state", "").upper() in
                                       ("CLOSED", "MERGED") else "open",
                                       "comments": [{"author": who(c.get("author")), "body": opts.text(c.get("body", ""))}
                                                    for c in p.get("comments", [])]})
    seed_repo["labels"] = sorted(labels)
    me = _login(opts, viewer) if viewer else next(iter(users), "me")
    opts.save_map()
    return {"viewer": me, "users": [{"login": u, "name": u} for u in sorted(set(users) | {me})], "repos": [seed_repo]}
