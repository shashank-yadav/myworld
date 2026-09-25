"""Calendar task families."""

from __future__ import annotations

import datetime as dt
import random

from .base import Task, _call, _servers, _submit, _world, family


@family("calendar.book", "calendar")
def calendar_book(rng: random.Random, seed: int) -> Task | None:
    """Book 30 minutes with a colleague at a time both are free."""
    import datetime as dt
    from zoneinfo import ZoneInfo

    from ...services.calendar.recurrence import _busy_spans
    servers = _servers("calendar")
    run = _world(servers, seed)
    st = run.instances["calendar"].state
    me, tz = st["default"], ZoneInfo(st["timeZone"])
    people = sorted(p for p in st["people"] if p != me)
    if not people:
        return None
    who = rng.choice(people)
    today = run.clock.now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    days = [today + dt.timedelta(days=d) for d in range(2, 9) if (today + dt.timedelta(days=d)).weekday() < 5]
    day = rng.choice(days)
    lo, hi = day.replace(hour=9), day.replace(hour=17)
    busy = _busy_spans(st, me, "", lo, hi, st["timeZone"]) + _busy_spans(st, who, "", lo, hi, st["timeZone"])
    slots = [lo + dt.timedelta(minutes=30 * k) for k in range(16)]
    free = [a for a in slots if not any(x < a + dt.timedelta(minutes=30) and a < y for x, y in busy)]
    if not free or len(free) == len(slots):
        return None  # no answer, or no challenge
    topic = rng.choice(["Budget review", "Roadmap sync", "Hiring debrief", "Customer prep", "Incident follow-up"])
    name = st["people"][who]["name"]
    date = day.strftime("%Y-%m-%d")
    slot = rng.choice(free)
    return {"servers": servers,
            "task": f"Book a 30-minute \"{topic}\" with {name} ({who}) on {day.strftime('%A %B')} {day.day}, "
                    f"between 9am and 5pm Pacific, at a time you're both free.",
            "checks": [
                {"name": "booked it with them that day", "server": "calendar", "state": "events", "weight": 3,
                 "where": {"booked_by_agent": True, "summary~": topic, "attendees.email": who, "start.dateTime~": date},
                 "count": 1},
                {"name": "no double-booking", "server": "calendar", "state": "events", "must": True,
                 "where": {"booked_by_agent": True, "conflicts_for~": "@"}, "count": 0},
                {"name": "within working hours", "server": "calendar", "state": "events",
                 "where": {"booked_by_agent": True, "start.dateTime~re": r"T(09|1[0-6]):(00|30)"}, "min": 1}],
            "reference": [
                _call("calendar__get-freebusy", calendars=[{"id": "primary"}, {"id": who}],
                      timeMin=lo.strftime("%Y-%m-%dT%H:%M:%S"), timeMax=hi.strftime("%Y-%m-%dT%H:%M:%S")),
                _call("calendar__create-event", summary=topic, start=slot.strftime("%Y-%m-%dT%H:%M:%S"),
                      end=(slot + dt.timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S"), attendees=[{"email": who}]),
                _submit(f"Booked {topic} at {slot.strftime('%H:%M')}.")]}


@family("calendar.cancel_occurrence", "calendar")
def calendar_cancel_occurrence(rng: random.Random, seed: int) -> Task | None:
    """Cancel one occurrence of a recurring meeting, keeping the series."""
    from zoneinfo import ZoneInfo

    from ...services.calendar.recurrence import _key, _occurrences
    servers = _servers("calendar")
    run = _world(servers, seed)
    st = run.instances["calendar"].state
    tz = st["timeZone"]
    me = st["default"]
    now = run.clock.now
    series = sorted((e for e in st["events"].values() if e.get("recurrence") and e["calendarId"] == me
                     and e["status"] == "confirmed"), key=lambda e: e["id"])
    if not series:
        return None
    ev = rng.choice(series)
    upcoming = _occurrences(st, ev, now + dt.timedelta(days=1), now + dt.timedelta(days=22), tz)
    if len(upcoming) < 2:
        return None
    k = rng.randrange(len(upcoming) - 1)
    target, after = upcoming[k], upcoming[k + 1]
    start = dt.datetime.fromisoformat(target["start"]["dateTime"]).astimezone(ZoneInfo(tz))
    key, key2 = _key(dt.datetime.fromisoformat(target["originalStartTime"]["dateTime"])), \
        _key(dt.datetime.fromisoformat(after["originalStartTime"]["dateTime"]))
    return {"servers": servers,
            "task": f"Cancel the \"{ev['summary']}\" on {start.strftime('%A %B')} {start.day} only. "
                    "Keep the rest of the series.",
            "checks": [
                {"name": "that occurrence is cancelled", "server": "calendar", "state": "events", "weight": 3,
                 "where": {"id": ev["id"], f"_exceptions.{key}.status": "cancelled"}, "count": 1},
                {"name": "the series still exists", "server": "calendar", "state": "events", "must": True,
                 "where": {"id": ev["id"], "status": "confirmed"}, "count": 1},
                {"name": "the next occurrence still happens", "server": "calendar", "state": "events", "must": True,
                 "where": {"id": ev["id"], f"!_exceptions.{key2}.status": "cancelled"}, "count": 1}],
            "reference": [_call("calendar__delete-event", eventId=target["id"]), _submit("Cancelled that one.")]}
