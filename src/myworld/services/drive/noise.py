"""drive: generated volume, distractors and background activity (see ``myworld.noise``)."""

from __future__ import annotations

import copy
import random
import re
from typing import Any

from ...noise import CUSTOMERS, PROJECTS, TITLES, VENDORS, _pick, _rng, people


def _perturb(rng: random.Random, text: str) -> str:
    return re.sub(r"\d{3,}", lambda m: str(int(int(m.group()) * rng.uniform(0.85, 1.15))), text)


def generate(seed: dict[str, Any], cfg: dict[str, Any], rng_seed: int, now: str) -> dict[str, Any]:
    seed = copy.deepcopy(seed)
    rng = _rng(rng_seed, "drive")
    me = (seed.get("user") or {}).get("email", "alex@acme.com")
    crowd = people(rng_seed, me.split("@")[1], int(cfg.get("people", 10)))
    folders, files = seed.setdefault("folders", []), seed.setdefault("files", [])
    keys = {f["key"] for f in folders}
    for key, name in [("noise-marketing", "Marketing"), ("noise-people", "People"), ("noise-archive", "Archive"),
                      ("noise-customers", "Customers"), ("noise-design", "Design")]:
        if key not in keys:
            folders.append({"key": key, "name": name})
    places = [f["key"] for f in folders] + [None]
    seeded = [f for f in files if f.get("content") and not f.get("trashed")]
    for _ in range(int(cfg.get("files", 40))):
        kind = rng.choices(["doc", "sheet", "pdf", "slides"], [45, 25, 20, 10])[0]
        project, customer = _pick(rng, PROJECTS), _pick(rng, CUSTOMERS)
        name = {"doc": _pick(rng, [f"{project} PRD", f"Meeting notes {rng.randint(1, 28)} Sep", f"{customer} QBR notes",
                                   f"Interview loop: {_pick(rng, TITLES)}", "Onboarding checklist", f"{project} retro"]),
                "sheet": _pick(rng, [f"{project} budget tracker", "Headcount plan FY27", f"{customer} usage export",
                                     "Vendor spend 2026", "OKR scorecard"]),
                "pdf": _pick(rng, [f"{customer} MSA.pdf", f"{customer} order form.pdf", "Employee handbook.pdf",
                                   f"{_pick(rng, VENDORS)} invoice {rng.randint(1000, 9999)}.pdf"]),
                "slides": _pick(rng, [f"All-hands {_pick(rng, ['August', 'September'])}", f"{project} kickoff", f"{customer} pitch"])}[kind]
        f: dict[str, Any] = {"name": name, "parent": _pick(rng, places)}
        if kind == "pdf":
            f.update(mimeType="application/pdf", size=rng.randint(40000, 900000))
        else:
            f["type"] = kind
            f["content"] = (f"item,owner,amount\n{project},{_pick(rng, crowd)['first']},{rng.randint(1000, 90000)}\n"
                            f"{customer},{_pick(rng, crowd)['first']},{rng.randint(1000, 90000)}\n" if kind == "sheet"
                            else f"{name}. Owner: {_pick(rng, crowd)['name']}. Status: {_pick(rng, ['draft', 'final', 'in review'])}.")
        if rng.random() < 0.35 and crowd:  # someone else's file, shared with you
            f.update(owner=_pick(rng, crowd)["email"], shared_role=_pick(rng, ["reader", "reader", "commenter", "writer"]))
            f.pop("parent", None)
        if rng.random() < 0.05:
            f["trashed"] = True
        files.append(f)
    if cfg.get("distractors", True):
        for src in seeded[:4]:
            name = src["name"]
            stem, dot, ext = name.rpartition(".") if "." in name[-5:] else (name, "", "")
            files.append({**{k: v for k, v in src.items() if k in ("type", "mimeType", "parent")},
                          "name": f"{stem} {_pick(rng, ['(old)', '- draft v2', '(copy)', 'FINAL'])}{dot}{ext}",
                          "content": _perturb(rng, src["content"])})
    return seed
