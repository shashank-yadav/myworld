"""Drive task families."""

from __future__ import annotations

import random

from .base import Task, _call, _servers, _submit, _world, family

FOLDER = "application/vnd.google-apps.folder"

@family("drive.share_current", "drive")
def drive_share_current(rng: random.Random, seed: int) -> Task | None:
    """Share the current file (not its old copies) as commenter."""
    servers = _servers("drive")
    st = _world(servers, seed).instances["drive"].state
    me = st["me"]
    files = [f for f in st["files"].values() if f["owner"] == me and not f["trashed"] and f["mimeType"] != FOLDER]
    names = {f["name"] for f in files}
    originals = sorted((f for f in files if any(n != f["name"] and n.startswith(f["name"] + " ") for n in names)),
                       key=lambda f: f["name"])
    if not originals:
        return None
    f = rng.choice(originals)
    colleagues = sorted({p["emailAddress"] for x in st["files"].values() for p in x["permissions"]
                         if p.get("emailAddress", "").endswith("@" + st["domain"]) and p["emailAddress"] != me})
    email = rng.choice(colleagues or [f"john@{st['domain']}"])
    before = sum(any(p.get("emailAddress") == email for p in x["permissions"]) for x in st["files"].values())
    return {"servers": servers,
            "task": f"Share \"{f['name']}\" with {email} so they can comment. Make sure it's the current file, not an "
                    "old copy or draft.",
            "checks": [
                {"name": "shared the right file as commenter", "server": "drive", "state": "files", "weight": 3,
                 "where": {"id": f["id"], "permissions": {"emailAddress": email, "role": "commenter"}}, "count": 1},
                {"name": "shared nothing else with them", "server": "drive", "state": "files", "must": True,
                 "where": {"!id": f["id"], "permissions": {"emailAddress": email}}, "count": before}],
            "reference": [_call("drive__manage_drive_access", file_id=f["id"], action="grant", share_with=email,
                                role="commenter"), _submit("Shared.")]}




@family("drive.sheet_total", "drive")
def drive_sheet_total(rng: random.Random, seed: int) -> Task | None:
    """Total a spreadsheet column (answer)."""
    servers = _servers("drive")
    st = _world(servers, seed).instances["drive"].state
    sheets = []
    for f in st["files"].values():
        if f["mimeType"] != "application/vnd.google-apps.spreadsheet" or f["trashed"] or not f.get("content"):
            continue
        rows = [r.split(",") for r in f["content"].strip().splitlines()]
        if len(rows) < 3 or not all(len(r) == len(rows[0]) for r in rows):
            continue
        col = len(rows[0]) - 1
        try:
            total = sum(int(r[col]) for r in rows[1:])
        except ValueError:
            continue
        if sum(x["name"] == f["name"] for x in st["files"].values()) == 1:
            sheets.append((f["name"], rows[0][col], total))
    if not sheets:
        return None
    name, column, total = rng.choice(sorted(sheets))
    pattern = rf"\b({total}|{total:,})\b"
    return {"servers": servers,
            "task": f"What's the total of the \"{column}\" column in the \"{name}\" spreadsheet? Answer with the number.",
            "checks": [{"name": "reported the right total", "answer": {"matches": pattern}, "weight": 3},
                       {"name": "didn't change any file", "server": "drive", "calls": "update_drive_file", "count": 0,
                        "must": True}],
            "reference": [_call("drive__search_drive_files", query=f"name contains '{name}'"), _submit(f"{total}")]}
