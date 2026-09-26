"""notion: generated volume, distractors and background activity (see ``myworld.noise``)."""

from __future__ import annotations

import copy
import random
from typing import Any

from ...noise import COMPONENTS, CUSTOMERS, PROJECTS, VENDORS, _pick, _rng, people


def generate(seed: dict[str, Any], cfg: dict[str, Any], rng_seed: int, now: str) -> dict[str, Any]:
    seed = copy.deepcopy(seed)
    rng = _rng(rng_seed, "notion")
    me = seed.get("user") or {"name": "Alex Rivera", "email": "alex@acme.com"}
    domain = me["email"].split("@")[1]
    users = seed.setdefault("users", [])
    have = {u["email"].lower() for u in users} | {me["email"].lower()}
    crowd = [c for c in people(rng_seed, domain, int(cfg.get("people", 10))) if c["email"] not in have]
    users.extend({"name": c["name"], "email": c["email"], "noise": True} for c in crowd)
    pages = seed.setdefault("pages", [])
    teams = [t["name"] for t in seed.get("teams", [])]
    seeded = [p["title"] for p in pages]
    for k in range(int(cfg.get("pages", 30))):
        comp, owner = _pick(rng, COMPONENTS), _pick(rng, crowd or [{"name": me["name"]}])
        title = _pick(rng, [f"{comp.capitalize()} design notes", f"Meeting notes: {_pick(rng, PROJECTS)}",
                            f"{_pick(rng, CUSTOMERS)} account plan", f"Vendor review: {_pick(rng, VENDORS)}",
                            f"Retro: {comp} incident", f"{owner['name'].split()[0]}'s onboarding checklist"])
        if cfg.get("distractors", True) and seeded and k < 2:
            title = f"{_pick(rng, ['Old', 'Draft', 'Archive'])}: {_pick(rng, seeded)}"
        if title in {p["title"] for p in pages}:
            continue  # titles name pages in world actions: keep them unique
        page = {"noise": True, "title": title,
                "content": f"Owner: {owner['name']}\n\n## Notes\n- {comp} follow-ups pending\n- next review in two weeks"}
        if teams:
            page["team"] = _pick(rng, teams)
        pages.append(page)
    for d in seed.get("databases", []):
        people_cols = [k for k, v in d.get("properties", {}).items() if v == "people"]
        title_col = next((k for k, v in d.get("properties", {}).items() if v == "title"), None)
        if not title_col:
            continue
        for _ in range(int(cfg.get("rows", 10))):
            row = {title_col: f"{_pick(rng, ['Fix', 'Review', 'Document', 'Upgrade'])} {_pick(rng, COMPONENTS)}"}
            for col in people_cols:
                row[col] = _pick(rng, crowd)["email"] if crowd else me["email"]
            d.setdefault("rows", []).append(row)
    return seed


def ambient(server: str, seed: dict[str, Any], times: list[int], rng: random.Random, rng_seed: int,
            now: str) -> list[dict[str, Any]]:
    """Background activity: generated colleagues keep editing generated pages."""
    pages = [p for p in seed.get("pages", []) if p.get("noise")]
    authors = [u["name"] for u in seed.get("users", []) if u.get("noise")]
    if not pages or not authors:
        return []
    events, content = [], {p["title"]: p["content"] for p in pages}
    for t in times:
        title = _pick(rng, pages)["title"]
        content[title] += f"\n- {_pick(rng, authors)}: {_pick(rng, ['updated owners', 'added a risk', 'moved the date'])}"
        events.append({"server": server, "action": "edit_page", "at": f"+{t}s", "name": "ambient edit",
                       "params": {"title": title, "content": content[title]}})
    return events
