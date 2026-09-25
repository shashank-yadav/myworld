"""Jira from a CSV export (Filters > Export > CSV (all fields)) or REST search JSON
(``/rest/api/3/search?jql=...&maxResults=100``). Issue keys are preserved; statuses and types
map onto the simulator's workflow."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .common import ImportOptions

TYPES = {"task": "Task", "bug": "Bug", "story": "Story", "epic": "Epic", "sub-task": "Subtask", "subtask": "Subtask"}
PRIORITIES = {"highest": "Highest", "blocker": "Highest", "critical": "Highest", "high": "High", "major": "High",
              "medium": "Medium", "low": "Low", "minor": "Low", "lowest": "Lowest", "trivial": "Lowest"}


def _status(name: str, category: str | None = None) -> str:
    n, c = (name or "").lower(), (category or "").lower()
    if c == "done" or any(w in n for w in ("done", "closed", "resolved", "complete", "shipped")):
        return "Done"
    if "review" in n or "qa" in n:
        return "In Review"
    if c == "indeterminate" or any(w in n for w in ("progress", "doing", "started", "active")):
        return "In Progress"
    return "To Do"


def _adf_text(node: Any) -> str:
    """Atlassian Document Format -> plain text."""
    if isinstance(node, str) or node is None:
        return node or ""
    if node.get("type") == "text":
        return node.get("text", "")
    sep = "\n" if node.get("type") in ("paragraph", "heading", "listItem", "codeBlock") else ""
    return "".join(_adf_text(c) for c in node.get("content", [])) + sep


def _rows_from_csv(path: Path) -> list[dict[str, Any]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = []
        for r in reader:
            row: dict[str, Any] = {}
            for h, v in zip(header, r):
                if h in ("Labels", "Comment", "Component/s"):
                    row.setdefault(h, []).append(v) if v else row.setdefault(h, [])
                else:
                    row.setdefault(h, v)
            rows.append({"key": row.get("Issue key"), "summary": row.get("Summary"), "type": row.get("Issue Type"),
                         "status": row.get("Status"), "category": row.get("Status Category"),
                         "priority": row.get("Priority"), "assignee": row.get("Assignee"), "reporter": row.get("Reporter"),
                         "assignee_email": None, "labels": row.get("Labels", []), "description": row.get("Description"),
                         "created": row.get("Created"), "project_name": row.get("Project name"),
                         "comments": [c.split(";", 2)[-1] if c.count(";") >= 2 else c for c in row.get("Comment", [])],
                         "comment_authors": [c.split(";", 2)[1] if c.count(";") >= 2 else None for c in row.get("Comment", [])],
                         "epic": row.get("Parent") or row.get("Custom field (Epic Link)") or None})
        return rows


def _rows_from_json(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    rows = []
    for i in data.get("issues", data if isinstance(data, list) else []):
        f = i.get("fields", {})
        person = lambda p: (p or {}).get("displayName")  # noqa: E731
        rows.append({"key": i["key"], "summary": f.get("summary"), "type": (f.get("issuetype") or {}).get("name"),
                     "status": (f.get("status") or {}).get("name"),
                     "category": ((f.get("status") or {}).get("statusCategory") or {}).get("key"),
                     "priority": (f.get("priority") or {}).get("name"), "assignee": person(f.get("assignee")),
                     "assignee_email": (f.get("assignee") or {}).get("emailAddress"),
                     "reporter": person(f.get("reporter")), "labels": f.get("labels", []),
                     "description": _adf_text(f.get("description")).strip() or None, "created": f.get("created"),
                     "project_name": (f.get("project") or {}).get("name"),
                     "comments": [_adf_text(c.get("body")).strip() for c in (f.get("comment") or {}).get("comments", [])],
                     "comment_authors": [person(c.get("author")) for c in (f.get("comment") or {}).get("comments", [])],
                     "epic": (f.get("parent") or {}).get("key")})
    return rows


def import_export(path: str | Path, opts: ImportOptions | None = None, *, me: str | None = None,
                  home_domain: str = "example.com") -> dict[str, Any]:
    opts = opts or ImportOptions()
    path = Path(path)
    rows = _rows_from_json(path) if path.suffix.lower() == ".json" else _rows_from_csv(path)
    rows = [r for r in rows if r.get("key") and r.get("summary")]
    if not rows:
        raise ValueError(f"no issues found in {path}")
    opts.home = opts.home or home_domain
    people: dict[str, dict[str, str]] = {}

    def person(name: str | None) -> str | None:
        if not name:
            return None
        email = f"{name.lower().replace(' ', '.')}@{opts.home}"
        fake_email, fake_name = opts.person(email, name)
        account = fake_email.split("@")[0].replace(".", "-")
        people[account] = {"account_id": account, "display_name": fake_name, "email": fake_email}
        return account

    projects: dict[str, dict[str, Any]] = {}
    for r in rows:
        pkey = r["key"].rsplit("-", 1)[0]
        proj = projects.setdefault(pkey, {"key": pkey, "name": r.get("project_name") or pkey, "issues": []})
        issue: dict[str, Any] = {"key": r["key"], "summary": opts.text(r["summary"]),
                                 "type": TYPES.get((r.get("type") or "").lower(), "Task"),
                                 "status": _status(r.get("status") or "", r.get("category")),
                                 "priority": PRIORITIES.get((r.get("priority") or "").lower(), "Medium"),
                                 "labels": [l.replace(" ", "-") for l in r.get("labels") or []],
                                 "reporter": person(r.get("reporter")) or None}
        if r.get("assignee"):
            issue["assignee"] = person(r["assignee"])
        if r.get("description"):
            issue["description"] = opts.text(r["description"])
        comments = [{"author": person(a) or (issue["reporter"]), "body": opts.text(b)}
                    for a, b in zip(r.get("comment_authors", []), r.get("comments", [])) if b]
        if comments:
            issue["comments"] = comments
        if r.get("epic"):
            issue["epic"] = r["epic"]
        proj["issues"].append({k: v for k, v in issue.items() if v not in (None, [], "")})
    me_id = person(me) if me else next(iter(people), "me")
    if me_id not in people:
        people[me_id] = {"account_id": me_id, "display_name": me_id, "email": f"{me_id}@{opts.domain}"}
    opts.save_map()
    return {"user": people[me_id], "users": [p for k, p in people.items() if k != me_id],
            "projects": list(projects.values())}
