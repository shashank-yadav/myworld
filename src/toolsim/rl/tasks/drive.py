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
    others = sorted({t for n, _, t in sheets if t != total})[:20]
    return {"servers": servers,
            "task": f"What's the total of the \"{column}\" column in the \"{name}\" spreadsheet? Answer with the number.",
            "checks": [{"name": "reported the right total", "weight": 3,
                        "answer": {"matches": pattern, "max_len": 200,
                                   "not": [rf"\b({o}|{o:,})\b" for o in others]}},
                       {"name": "changed nothing", "server": "drive", "calls": "*", "where": {"committed": True},
                        "count": 0, "must": True}],
            "reference": [_call("drive__search_drive_files", query=f"name contains '{name}'"), _submit(f"{total}")]}


@family("drive.update_cell", "drive")
def drive_update_cell(rng: random.Random, seed: int) -> Task | None:
    """Change one value in a spreadsheet, leaving the other rows alone."""
    servers = _servers("drive")
    st = _world(servers, seed).instances["drive"].state
    names = [f["name"] for f in st["files"].values()]
    cands = []
    for f in st["files"].values():
        if f["mimeType"] != "application/vnd.google-apps.spreadsheet" or f["trashed"] or f["owner"] != st["me"]:
            continue
        rows = [r.split(",") for r in (f.get("content") or "").strip().splitlines()]
        if len(rows) >= 3 and rows[0] == ["item", "owner", "amount"] and names.count(f["name"]) == 1 \
                and len({r[0] for r in rows[1:]}) == len(rows) - 1:
            cands.append((f, rows))
    if not cands:
        return None
    f, rows = rng.choice(sorted(cands, key=lambda c: c[0]["name"]))
    r = rng.randrange(1, len(rows))
    item, owner, old = rows[r]
    new = int(old) + rng.choice([-1, 1]) * rng.randint(1, 50) * 100
    return {"servers": servers,
            "task": f"In the \"{f['name']}\" spreadsheet, change the amount for {item} to {new}.",
            "checks": [
                {"name": "updated the amount", "server": "drive", "state": "files", "weight": 3,
                 "where": {"id": f["id"], "content~": f"{item},{owner},{new}"}, "count": 1},
                *({"name": f"left {row[0]} alone", "server": "drive", "state": "files", "must": True,
                   "where": {"id": f["id"], "content~": ",".join(row)}, "count": 1}
                  for n, row in enumerate(rows) if n and n != r)],
            "reference": [_call("drive__read_sheet_values", spreadsheet_id=f["id"]),
                          _call("drive__modify_sheet_values", spreadsheet_id=f["id"], range_name=f"C{r + 1}",
                                values=[[str(new)]]), _submit("Updated.")]}


@family("drive.move_file", "drive")
def drive_move_file(rng: random.Random, seed: int) -> Task | None:
    """Move one of my files into another folder (out of the old one)."""
    servers = _servers("drive")
    st = _world(servers, seed).instances["drive"].state
    me = st["me"]
    folders = sorted((f for f in st["files"].values() if f["mimeType"] == FOLDER and f["owner"] == me
                      and f["id"] != "root" and not f["trashed"]), key=lambda f: f["name"])
    names = [f["name"] for f in st["files"].values()]
    files = sorted((f for f in st["files"].values() if f["owner"] == me and f["mimeType"] != FOLDER and not f["trashed"]
                    and names.count(f["name"]) == 1 and f["parents"]), key=lambda f: f["name"])
    if not files or len(folders) < 2:
        return None
    f = rng.choice(files)
    dest = rng.choice([d for d in folders if d["id"] not in f["parents"]])
    return {"servers": servers,
            "task": f"Move \"{f['name']}\" into the \"{dest['name']}\" folder.",
            "checks": [
                {"name": "it's in the new folder", "server": "drive", "state": "files", "weight": 2,
                 "where": {"id": f["id"], "parents": [dest["id"]]}, "count": 1},
                {"name": "and no longer in the old one", "server": "drive", "state": "files",
                 "where": {"id": f["id"], "!parents": [f["parents"][0]]}, "count": 1},
                {"name": "nothing else moved", "server": "drive", "calls": "update_drive_file", "must": True,
                 "where": {"!args.file_id": f["id"]}, "count": 0}],
            "reference": [_call("drive__update_drive_file", file_id=f["id"], add_parents=dest["id"],
                                remove_parents=",".join(f["parents"])), _submit("Moved.")]}
