"""calendar: generated volume, distractors and background activity (see ``myworld.noise``)."""

from __future__ import annotations

import copy
import datetime as dt
import random
from typing import Any
from zoneinfo import ZoneInfo

from ...noise import CUSTOMERS, PROJECTS, _now, _pick, _rng, people


def generate(seed: dict[str, Any], cfg: dict[str, Any], rng_seed: int, now: str) -> dict[str, Any]:
    seed = copy.deepcopy(seed)
    rng, t_now = _rng(rng_seed, "calendar"), _now(now)
    user = (seed.get("user") or {}).get("email", "alex@acme.com")
    tz = ZoneInfo(seed.get("timeZone", "America/Los_Angeles"))
    domain = user.split("@")[1]
    existing = {p["email"] for p in seed.get("people", [])}
    crowd = [c for c in people(rng_seed, domain, int(cfg.get("people", 10))) if c["email"] not in existing]
    seed.setdefault("people", []).extend({"email": c["email"], "name": c["name"], "noise": True} for c in crowd)
    events = seed.setdefault("events", [])

    def span(e: dict[str, Any]) -> tuple[dt.datetime, dt.datetime]:
        a, b = (dt.datetime.fromisoformat(str(e[k]).replace("Z", "+00:00")) for k in ("start", "end"))
        return (a if a.tzinfo else a.replace(tzinfo=tz)), (b if b.tzinfo else b.replace(tzinfo=tz))

    mine = [span(e) for e in events if (e.get("organizer") or user) == user or user in
            [a if isinstance(a, str) else a.get("email") for a in e.get("attendees", [])]]

    def free(a: dt.datetime, b: dt.datetime) -> bool:
        return not any(x < b and a < y for x, y in mine)

    def add(summary: str, a: dt.datetime, minutes: int, organizer: str, others: list[str], **extra: Any) -> None:
        b = a + dt.timedelta(minutes=minutes)
        if not free(a, b):
            return
        mine.append((a, b))
        if organizer == user:
            attendees = others
        else:
            attendees = [{"email": user, "responseStatus": _pick(rng, ["accepted", "accepted", "needsAction", "tentative"])},
                         *others]
        events.append({"summary": summary, "start": a.isoformat(), "end": b.isoformat(), "attendees": attendees,
                       **({"organizer": organizer} if organizer != user else {}), **extra})

    today = t_now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    monday = today - dt.timedelta(days=today.weekday())
    if crowd:
        for c in crowd[:2]:  # weekly 1:1s
            d = monday + dt.timedelta(days=rng.randint(1, 4), hours=rng.choice([10, 11, 14, 15]))
            add(f"{c['first']} / {user.split('@')[0].split('.')[0].capitalize()} 1:1", d, 30, user, [c["email"]],
                recurrence=["RRULE:FREQ=WEEKLY"])
        lead = crowd[-1]
        add("Team sync", monday + dt.timedelta(days=rng.randint(0, 4), hours=13), 45, lead["email"],
            [c["email"] for c in crowd[:5]], recurrence=["RRULE:FREQ=WEEKLY"])
    density = float(cfg.get("density", 0.4))
    for day in range(-5, int(cfg.get("days_ahead", 14))):
        d = today + dt.timedelta(days=day)
        if d.weekday() >= 5:
            continue
        for _ in range(round(density * 8 * rng.uniform(0.6, 1.4))):
            a = d.replace(hour=rng.randint(9, 16), minute=rng.choice([0, 30]))
            others = [c["email"] for c in rng.sample(crowd, k=min(len(crowd), rng.randint(1, 4)))]
            org = user if rng.random() < 0.4 or not crowd else _pick(rng, crowd)["email"]
            what = _pick(rng, [f"{_pick(rng, PROJECTS)} review", f"{_pick(rng, CUSTOMERS)} call", "Interview",
                               f"{_pick(rng, PROJECTS)} planning", "Coffee chat", "Hiring debrief", "Design crit"])
            add(what, a, rng.choice([30, 30, 45, 60]), org, [e for e in others if e != org])
    if cfg.get("colleague_busy", True):  # people you can only see as free/busy get busier too
        for c in seed.get("calendars", []):
            if "@" not in c.get("id", "") or c["id"].startswith("team"):
                continue
            busy = c.setdefault("busy", [])
            for day in range(0, int(cfg.get("days_ahead", 14))):
                d = today + dt.timedelta(days=day)
                if d.weekday() < 5 and rng.random() < density:
                    a = d.replace(hour=rng.randint(9, 16), minute=rng.choice([0, 30]))
                    busy.append({"start": a.isoformat(), "end": (a + dt.timedelta(minutes=rng.choice([30, 60]))).isoformat()})
    return seed


def ambient(server: str, seed: dict[str, Any], times: list[int], rng: random.Random, rng_seed: int,
            now: str) -> list[dict[str, Any]]:
    """Background activity for one run: world events at the given offsets (seconds)."""
    events: list[dict[str, Any]] = []
    tz = ZoneInfo(seed.get("timeZone", "America/Los_Angeles"))
    crowd = [p["email"] for p in seed.get("people", []) if p.get("noise")]
    day0 = _now(now).astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    for t in times:
        if not crowd:
            break
        d = day0 + dt.timedelta(days=rng.randint(1, 7))
        a = d.replace(hour=rng.randint(9, 16), minute=rng.choice([0, 30]))
        events.append({"server": server, "action": "add_event", "at": f"+{t}s", "name": "ambient booking",
                       "params": {"calendar": _pick(rng, crowd), "summary": _pick(rng, ["Customer call", "Interview", "Focus"]),
                                  "start": a.isoformat(), "end": (a + dt.timedelta(minutes=30)).isoformat()}})
    return events
