"""Google Calendar from an .ics export (Google Takeout Calendar/*.ics, or any iCalendar file)."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ...importers.common import ImportOptions

PARTSTAT = {"ACCEPTED": "accepted", "DECLINED": "declined", "TENTATIVE": "tentative", "NEEDS-ACTION": "needsAction"}


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        elif line:
            lines.append(line)
    return lines


def _prop(line: str) -> tuple[str, dict[str, str], str]:
    head, _, value = line.partition(":")
    name, *params = head.split(";")
    return name.upper(), {k.upper(): v.strip('"') for k, _, v in (p.partition("=") for p in params)}, value


def _unescape(v: str) -> str:
    out, i = [], 0
    while i < len(v):
        if v[i] == "\\" and i + 1 < len(v):
            out.append({"n": "\n", "N": "\n"}.get(v[i + 1], v[i + 1]))
            i += 2
        else:
            out.append(v[i])
            i += 1
    return "".join(out)


def _time(value: str, params: dict[str, str], default_tz: str) -> dt.datetime:
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        d = dt.datetime.strptime(value[:8], "%Y%m%d")
        return d.replace(tzinfo=ZoneInfo(default_tz))
    t = dt.datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return t.replace(tzinfo=dt.timezone.utc)
    return t.replace(tzinfo=ZoneInfo(params.get("TZID", default_tz)))


def import_ics(path: str | Path, opts: ImportOptions | None = None, *, owner: str | None = None,
               owner_name: str | None = None) -> dict[str, Any]:
    opts = opts or ImportOptions()
    lines = _unfold(Path(path).read_text(errors="replace"))
    tz = next((_prop(l)[2] for l in lines if l.upper().startswith("X-WR-TIMEZONE")), "UTC")
    cal_name = next((_prop(l)[2] for l in lines if l.upper().startswith("X-WR-CALNAME")), None)
    events, cur = [], None
    for line in lines:
        name, params, value = _prop(line)
        if name == "BEGIN" and value.upper() == "VEVENT":
            cur = {"attendees": [], "recurrence": []}
        elif name == "END" and value.upper() == "VEVENT" and cur is not None:
            if "start" in cur and cur.get("status") != "CANCELLED":
                events.append(cur)
            cur = None
        elif cur is not None:
            if name in ("DTSTART", "DTEND"):
                cur["start" if name == "DTSTART" else "end"] = _time(value, params, tz)
            elif name in ("SUMMARY", "DESCRIPTION", "LOCATION"):
                cur[name.lower()] = _unescape(value)
            elif name == "STATUS":
                cur["status"] = value.upper()
            elif name == "TRANSP":
                cur["transparency"] = "transparent" if value.upper() == "TRANSPARENT" else "opaque"
            elif name == "ATTENDEE":
                cur["attendees"].append((value.removeprefix("mailto:").removeprefix("MAILTO:"), params.get("CN"),
                                         PARTSTAT.get(params.get("PARTSTAT", "NEEDS-ACTION").upper(), "needsAction")))
            elif name == "ORGANIZER":
                cur["organizer"] = value.removeprefix("mailto:").removeprefix("MAILTO:")
            elif name in ("RRULE", "EXDATE", "RDATE"):
                cur["recurrence"].append(f"{name}:{value}")
    if not events:
        raise ValueError(f"no events found in {path}")
    for e in events:
        e.setdefault("end", e["start"] + dt.timedelta(hours=1))
    if owner is None and cal_name and "@" in cal_name:
        owner = cal_name
    if owner is None:
        counts: dict[str, int] = {}
        for e in events:
            for a, _, _ in e["attendees"]:
                counts[a.lower()] = counts.get(a.lower(), 0) + 1
        owner = max(counts, key=counts.get) if counts else "me@example.com"
    opts.home = opts.home or owner.split("@")[-1]
    events = opts.newest(events, key=lambda e: e["start"])
    opts.plan_shift([e["start"] for e in events])
    me, my_name = opts.person(owner, owner_name)
    out = []
    for e in events:
        ev: dict[str, Any] = {"summary": opts.text(e.get("summary")) or "(No title)",
                              "start": opts.time(e["start"]).isoformat(), "end": opts.time(e["end"]).isoformat()}
        if e.get("description"):
            ev["description"] = opts.text(e["description"])
        if e.get("location"):
            ev["location"] = opts.text(e["location"])
        if e.get("transparency") == "transparent":
            ev["transparency"] = "transparent"
        if e["recurrence"]:
            ev["recurrence"] = e["recurrence"]
        invited_by_other = bool(e.get("organizer")) and e["organizer"].lower() != owner.lower()
        # the owner stays on the attendee list of other people's invites (that's how it shows on their calendar);
        # on their own events the simulator adds them as organizer
        attendees = [{"email": opts.person(a, cn)[0], "responseStatus": status} for a, cn, status in e["attendees"]
                     if invited_by_other or a.lower() != owner.lower()]
        if attendees:
            ev["attendees"] = attendees
        if invited_by_other:
            ev["organizer"] = opts.person(e["organizer"])[0]
        out.append(ev)
    people = sorted({ev["organizer"] for ev in out if ev.get("organizer")})
    opts.save_map()
    seed: dict[str, Any] = {"user": {"email": me, "name": my_name}, "timeZone": tz, "events": out}
    if people:
        seed["people"] = [{"email": p, "name": opts.people.get(p, {}).get("name", p.split("@")[0])} for p in people]
    return seed
