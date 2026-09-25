"""Google Calendar.

Tool names follow the most widely used Calendar MCP server (nspady/google-calendar-mcp); event
objects follow the Google Calendar API. Times respect real time zones.

Seed format::

    user: {email: alex@acme.com, name: Alex Rivera}
    timeZone: America/Los_Angeles
    calendars:                                   # beyond the user's primary calendar
      - {id: john@acme.com, summary: John Park, accessRole: freeBusyReader,
         busy: [{start: "2026-09-22T09:00:00-07:00", end: "2026-09-22T11:00:00-07:00"}]}
    events:
      - {summary: Standup, start: "2026-09-21T09:30:00-07:00", end: "2026-09-21T09:45:00-07:00",
         attendees: [priya@acme.com], calendarId: primary,
         recurrence: ["RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR"]}          # expanded from 2026-09-25.1
    people:                                      # colleagues on the same calendar system
      - {email: priya@acme.com, name: Priya Shah, auto_respond: if_free, respond_after: 10m}
    auto_respond: {john@acme.com: accept}        # how people without agents answer invites

``auto_respond`` policies (2026-09-25.1): accept, decline, tentative, or if_free (declines when
the invite clashes with something on their calendar when they get to it).
"""

from __future__ import annotations

import copy
import datetime as dt
import re
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from ..core.instance import Instance, Service
from ..core.tools import ToolError, action, tool

V1 = "2026-09-25.1"
MAX_INSTANCES = 730  # a recurring series is expanded at most this far
POLICIES = ("accept", "decline", "tentative", "if_free")

COLORS = {str(i): c for i, c in enumerate(
    ["#a4bdfc", "#7ae7bf", "#dbadff", "#ff887c", "#fbd75b", "#ffb878", "#46d6db", "#e1e1e1", "#5484ed", "#51b749",
     "#dc2127"], start=1)}


def _err(code: int, message: str, reason: str = "") -> ToolError:
    return ToolError({"error": {"code": code, "message": message, "errors": [{"reason": reason or "invalid"}]}},
                     status=code)


class Calendar(Service):
    name = "calendar"
    title = "Google Calendar"
    description = "Simulated Google Calendar. Behaves like the Google Calendar MCP server; nothing is really scheduled."

    versions = {"2026-09-25": "Initial release: 11 tools modeled on nspady/google-calendar-mcp.",
                V1: "Recurring events expand into instances (RRULE/EXDATE), modificationScope on updates, "
                    "per-instance deletes/responses, and colleagues who answer invites on their own."}

    def probe(self, ctx: Instance) -> None:
        c = ctx.call
        c("list-calendars", {})
        c("list-events", {"timeMin": "2026-09-21T00:00:00", "timeMax": "2026-09-28T00:00:00"})
        c("get-freebusy", {"calendars": [{"id": "primary"}, {"id": "john@acme.com"}],
                           "timeMin": "2026-09-22T08:00:00", "timeMax": "2026-09-23T18:00:00"})
        r = c("create-event", {"summary": "Q4 plan", "start": "2026-09-22T10:00:00", "end": "2026-09-22T10:30:00",
                               "attendees": [{"email": "john@acme.com"}]})
        eid = r.data["event"]["id"]
        c("update-event", {"eventId": eid, "location": "Room 2"})
        c("search-events", {"query": "q4"})
        c("create-event", {"summary": "x", "calendarId": "team@acme.com", "start": "2026-09-22T10:00:00",
                           "end": "2026-09-22T11:00:00"})
        c("list-events", {"calendarId": "john@acme.com"})
        c("delete-event", {"eventId": eid})
        c("delete-event", {"eventId": eid})
        c("get-current-time", {})
        c("list-colors", {})
        if ctx.at_least(V1):
            r = c("create-event", {"summary": "1:1", "start": "2026-09-21T15:00:00", "end": "2026-09-21T15:30:00",
                                   "attendees": [{"email": "john@acme.com"}],
                                   "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=MO,TH;COUNT=6"]})
            sid = r.data["event"]["id"]
            week = {"timeMin": "2026-09-21T00:00:00", "timeMax": "2026-10-06T00:00:00"}
            ids = [e["id"] for e in c("list-events", week).data["events"] if e.get("recurringEventId") == sid]
            c("update-event", {"eventId": sid, "modificationScope": "thisEventOnly",
                               "originalStartTime": "2026-09-24T15:00:00", "start": "2026-09-24T16:00:00",
                               "end": "2026-09-24T16:30:00"})
            c("delete-event", {"eventId": ids[2]})
            c("update-event", {"eventId": sid, "modificationScope": "thisAndFollowing",
                               "futureStartDate": "2026-10-01", "summary": "1:1 (new time)"})
            c("list-events", week)
            c("get-freebusy", {"calendars": [{"id": "primary"}], **week})
            c("create-event", {"summary": "bad", "start": "2026-09-22T10:00:00", "end": "2026-09-22T11:00:00",
                               "recurrence": ["RRULE:FREQ=HOURLYISH"]})
            c("update-event", {"eventId": sid, "modificationScope": "thisEventOnly", "summary": "x"})

    def default_seed(self) -> dict[str, Any]:
        return {
            "user": {"email": "alex@acme.com", "name": "Alex Rivera"},
            "timeZone": "America/Los_Angeles",
            "calendars": [
                {"id": "john@acme.com", "summary": "John Park", "accessRole": "freeBusyReader",
                 "busy": [{"start": "2026-09-22T09:00:00-07:00", "end": "2026-09-22T10:00:00-07:00"},
                          {"start": "2026-09-23T13:00:00-07:00", "end": "2026-09-23T17:00:00-07:00"}]},
                {"id": "team@acme.com", "summary": "Team calendar", "accessRole": "reader"},
            ],
            "events": [
                {"summary": "Standup", "start": "2026-09-21T09:30:00-07:00", "end": "2026-09-21T09:45:00-07:00",
                 "attendees": ["priya@acme.com", "john@acme.com"]},
                {"summary": "Design review", "start": "2026-09-22T11:00:00-07:00", "end": "2026-09-22T12:00:00-07:00",
                 "attendees": ["priya@acme.com"], "location": "Room 4B"},
                {"summary": "Focus time", "start": "2026-09-23T09:00:00-07:00", "end": "2026-09-23T12:00:00-07:00",
                 "transparency": "opaque"},
                {"summary": "All hands", "start": "2026-09-24T10:00:00-07:00", "end": "2026-09-24T11:00:00-07:00",
                 "calendarId": "team@acme.com"},
            ],
        }

    def initial_state(self, seed: dict[str, Any], ctx: Instance) -> dict[str, Any]:
        """A company calendar system: every person has a primary calendar (keyed by email).
        ``people: [{email, name}]`` adds colleagues whose agents can join; ``calendars`` adds
        other calendars with the default user's access role (e.g. a colleague's free/busy)."""
        user = seed.get("user") or {"email": "alex@acme.com", "name": "Alex Rivera"}
        tz = seed.get("timeZone", "America/Los_Angeles")
        state: dict[str, Any] = {"default": user["email"].lower(), "timeZone": tz, "people": {}, "calendars": {},
                                 "events": {}, "busy": {}}
        for person in [user, *seed.get("people", [])]:
            email = person["email"].lower()
            state["people"][email] = {"email": email, "name": person.get("name", email)}
            state["calendars"][email] = {"id": email, "summary": person.get("name", email),
                                         "timeZone": person.get("timeZone", tz), "acl": {email: "owner"},
                                         "domainRole": "freeBusyReader", "person": True}
        for c in seed.get("calendars", []):
            cid = c["id"].lower()
            if cid in state["calendars"]:  # a colleague who is also a person here: keep the real calendar
                state["busy"][cid] = [{"start": b["start"], "end": b["end"]} for b in c.get("busy", [])]
                continue
            state["calendars"][cid] = {"id": c["id"], "summary": c.get("summary", c["id"]), "timeZone": c.get("timeZone", tz),
                                       "acl": dict(c.get("acl") or {}), "domainRole": c.get("accessRole", "reader"),
                                       "person": "@" in c["id"] and not c["id"].startswith("team")}
            state["busy"][cid] = [{"start": b["start"], "end": b["end"]} for b in c.get("busy", [])]
        if ctx.at_least(V1):
            state["_recurring"] = True
            policies = {p["email"].lower(): p for p in [*seed.get("people", []), *seed.get("calendars", [])]
                        if p.get("auto_respond")}
            policies.update({k.lower(): {"auto_respond": v} for k, v in (seed.get("auto_respond") or {}).items()})
            state["_policies"] = {}
            for email, p in policies.items():
                if p["auto_respond"] not in POLICIES:
                    raise ValueError(f"auto_respond for {email} must be one of {', '.join(POLICIES)}")
                state["_policies"][email] = {"response": p["auto_respond"],
                                             "delay": _delay(p.get("respond_after", "10m"))}
        for e in seed.get("events", []):
            organizer = (e.get("organizer") or state["default"]).lower()
            cid = e.get("calendarId", "primary")
            _new_event(ctx, state, organizer if cid == "primary" else cid, e, actor=organizer)
        return state

    def default_actor(self, state: dict[str, Any]) -> str:
        return state["default"]

    def grading_view(self, state: dict[str, Any]) -> dict[str, Any]:
        """Events annotated with ``booked_by_agent`` and ``conflicts_for``: the people for whom the
        event (any instance of it, for a recurring series) overlaps something else they're busy
        with (accepted events or free/busy blocks)."""
        tz = state["timeZone"]
        events = []
        for e in state["events"].values():
            conflicts: set[str] = set()
            if e["status"] != "cancelled":
                for inst in _occurrences(state, e, None, None, tz):
                    s, en = _parse(inst["start"], tz), _parse(inst["end"], tz)
                    people = {inst["calendarId"], *[a["email"] for a in inst.get("attendees", [])
                                                    if a["responseStatus"] != "declined"]}
                    conflicts |= {p for p in people if _on_calendar(inst, p)
                                  and any(a < en and s < b for a, b in _busy_spans(state, p, e["id"], s, en, tz))}
            events.append({**e, "booked_by_agent": bool(e.get("_by_agent")), "conflicts_for": sorted(conflicts)})
        return {**state, "events": events}

    def resolve_actor(self, state: dict[str, Any], identity: str) -> str:
        email = identity.strip().lower()
        if email not in state["people"]:
            raise ValueError(f"no calendar user {identity} in this environment")
        return email

    def error_shape(self, status: int, message: str) -> Any:
        reasons = {404: "notFound", 500: "backendError", 502: "backendError", 503: "backendError", 504: "backendError"}
        return {"error": {"code": status, "message": message, "errors": [{"reason": reasons.get(status, "error")}]}}

    def fault_error(self, fault: Any) -> tuple[Any, int]:
        if fault.kind == "rate_limit":
            return {"error": {"code": 403, "message": "Rate Limit Exceeded", "errors": [{"reason": "rateLimitExceeded"}]}}, 403
        return super().fault_error(fault)


# -- helpers -----------------------------------------------------------------------------

def _role(cal: dict[str, Any], actor: str) -> str:
    return cal["acl"].get(actor) or cal["domainRole"]


def _cal(ctx: Instance, calendar_id: str | None) -> tuple[str, dict[str, Any]]:
    """Resolve a calendar id for the acting user ('primary' = their own)."""
    state = ctx.state
    cid = (calendar_id or "primary").lower()
    if cid == "primary":
        cid = ctx.actor
    cal = state["calendars"].get(cid)
    if cal is None:
        raise _err(404, "Not Found", "notFound")
    return cid, cal


def _writable(ctx: Instance, cal: dict[str, Any]) -> None:
    if _role(cal, ctx.actor) not in ("owner", "writer"):
        raise _err(403, "You need to have writer access to this calendar.", "requiredAccessLevel")


def _readable(ctx: Instance, cal: dict[str, Any]) -> None:
    if _role(cal, ctx.actor) == "freeBusyReader":
        raise _err(403, "Not allowed to read events on this calendar (free/busy access only).", "forbidden")


def _parse(v: Any, tz: str) -> dt.datetime:
    if isinstance(v, dict):
        v, tz = v.get("dateTime") or v.get("date"), v.get("timeZone") or tz
    try:
        t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        raise _err(400, f"Invalid time value: {v}") from None
    return t if t.tzinfo else t.replace(tzinfo=ZoneInfo(tz))


def _when(t: dt.datetime, tz: str) -> dict[str, str]:
    return {"dateTime": t.astimezone(ZoneInfo(tz)).isoformat(), "timeZone": tz}


def _new_event(ctx: Instance, state: dict[str, Any], calendar_id: str, e: dict[str, Any], actor: str) -> dict[str, Any]:
    cal = state["calendars"][calendar_id]
    tz = e.get("timeZone") or cal["timeZone"]
    start, end = _parse(e["start"], tz), _parse(e["end"], tz)
    if end <= start:
        raise _err(400, "The specified time range is empty.", "timeRangeEmpty")
    eid = ctx.token(26, "abcdefghijklmnopqrstuv0123456789")
    attendees = [{"email": (a if isinstance(a, str) else a["email"]).lower(),
                  "responseStatus": (a.get("responseStatus") if isinstance(a, dict) else None) or "needsAction"}
                 for a in e.get("attendees") or []]
    if attendees and calendar_id == actor and not any(a["email"] == actor for a in attendees):
        attendees.insert(0, {"email": actor, "organizer": True, "responseStatus": "accepted"})
    now = ctx.now().isoformat()
    ev = {"kind": "calendar#event", "id": eid, "calendarId": calendar_id, "status": "confirmed",
          "htmlLink": f"https://www.google.com/calendar/event?eid={eid}", "created": now, "updated": now,
          "summary": e.get("summary", "(No title)"), "description": e.get("description"), "location": e.get("location"),
          "creator": {"email": actor}, "organizer": {"email": cal["id"]},
          "start": _when(start, tz), "end": _when(end, tz), "attendees": attendees or None,
          "colorId": e.get("colorId"), "transparency": e.get("transparency", "opaque"),
          "recurrence": e.get("recurrence"), "iCalUID": f"{eid}@google.com", "sequence": 0, "hiddenFor": []}
    state["events"][eid] = {k: v for k, v in ev.items() if v is not None}
    return state["events"][eid]


def _on_calendar(ev: dict[str, Any], cid: str) -> bool:
    """Is this event shown on calendar ``cid``? Its own calendar, or an attendee's."""
    if ev["status"] == "cancelled" or cid in ev.get("hiddenFor", []):
        return False
    return ev["calendarId"] == cid or any(a["email"] == cid for a in ev.get("attendees", []))


def _event(ctx: Instance, calendar_id: str | None, event_id: str) -> dict[str, Any]:
    """An event by id: a stored event, or (2026-09-25.1) an instance of a recurring series, returned
    as a copy; change instances through ``_save_instance``."""
    cid, _ = _cal(ctx, calendar_id)
    ev = _lookup(ctx.state, event_id)
    if ev is None or not _on_calendar(ev, cid):
        raise _err(404, "Not Found", "notFound")
    return ev


def _lookup(state: dict[str, Any], event_id: str) -> dict[str, Any] | None:
    ev = state["events"].get(event_id)
    if ev is None and state.get("_recurring") and "_" in event_id:
        sid, _, key = event_id.partition("_")
        master = state["events"].get(sid)
        try:
            orig = dt.datetime.strptime(key, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            return None
        if master is not None:
            ev = _series_instance(state, master, orig, state["timeZone"])
    return ev


def _is_instance(ev: dict[str, Any]) -> bool:
    return "recurringEventId" in ev and "_" in ev["id"]


def _save_instance(ctx: Instance, inst: dict[str, Any]) -> dict[str, Any]:
    """Store what differs between an edited instance and its series as an exception."""
    master = ctx.state["events"][inst["recurringEventId"]]
    key = inst["id"].partition("_")[2]
    base = _instance({**master, "_exceptions": {}}, _parse(inst["originalStartTime"], ctx.state["timeZone"]),
                     ctx.state["timeZone"])
    master.setdefault("_exceptions", {})[key] = {k: copy.deepcopy(inst.get(k)) for k in _OVERRIDABLE
                                                 if inst.get(k) != base.get(k)}
    master["updated"] = ctx.now().isoformat()
    return inst


def _range(time_min: str | None, time_max: str | None, tz: str) -> tuple[Any, Any]:
    lo = _parse(time_min, tz) if time_min else None
    hi = _parse(time_max, tz) if time_max else None
    if lo and hi and hi <= lo:
        raise _err(400, "The specified time range is empty.", "timeRangeEmpty")
    return lo, hi


def _in_range(ev: dict[str, Any], lo: Any, hi: Any, tz: str) -> bool:
    s, e = _parse(ev["start"], tz), _parse(ev["end"], tz)
    return (hi is None or s < hi) and (lo is None or e > lo)


def _delay(v: Any) -> float:
    m = re.fullmatch(r"\+?(\d+(?:\.\d+)?)([smhd]?)", str(v).strip())
    if not m:
        raise ValueError(f"invalid delay {v!r} (e.g. 30s, 10m, 2h)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


# -- recurring events (2026-09-25.1) ----------------------------------------------------------
# A series is one stored event with ``recurrence``; reads expand it into instances with ids
# ``<seriesId>_<original start, UTC, basic format>`` like Google's. Per-instance changes live in
# the series' private ``_exceptions`` keyed by that original start.

_DAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
_FREQS = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")
_OVERRIDABLE = ("summary", "description", "location", "colorId", "start", "end", "attendees", "status", "hiddenFor")


def _bad_recurrence(line: str) -> ToolError:
    return _err(400, f"Invalid recurrence rule: {line}", "invalid")


def _ical_time(v: str, tz: str) -> dt.datetime | dt.date:
    v = v.strip()
    try:
        if len(v) == 8:
            return dt.datetime.strptime(v, "%Y%m%d").date()
        if v.endswith("Z"):
            return dt.datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
        return dt.datetime.strptime(v, "%Y%m%dT%H%M%S").replace(tzinfo=ZoneInfo(tz))
    except ValueError:
        raise _bad_recurrence(v) from None


def _recurrence(ev: dict[str, Any], tz: str) -> tuple[dict[str, str], list[Any]] | None:
    """(RRULE parts, EXDATEs) of a series, or None if it doesn't repeat. Raises 400 on bad rules."""
    rule, exdates = None, []
    for line in ev.get("recurrence") or []:
        head, _, val = str(line).partition(":")
        name, *params = head.upper().split(";")
        if name == "RRULE":
            try:
                rule = dict(p.split("=", 1) for p in val.upper().split(";") if p)
            except ValueError:
                raise _bad_recurrence(line) from None
            if rule.get("FREQ") not in _FREQS or ("COUNT" in rule and "UNTIL" in rule):
                raise _bad_recurrence(line)
            try:
                if int(rule.get("INTERVAL", 1)) < 1 or int(rule.get("COUNT", 1)) < 1:
                    raise _bad_recurrence(line)
            except ValueError:
                raise _bad_recurrence(line) from None
            for d in filter(None, rule.get("BYDAY", "").split(",")):
                if not re.fullmatch(r"(-?[1-5])?(MO|TU|WE|TH|FR|SA|SU)", d):
                    raise _bad_recurrence(line)
            if "UNTIL" in rule:
                _ical_time(rule["UNTIL"], tz)
        elif name == "EXDATE":
            ptz = next((p.split("=", 1)[1] for p in params if p.startswith("TZID=")), tz)
            exdates += [_ical_time(v, ptz) for v in val.split(",") if v]
        elif name in ("RDATE", "EXRULE"):
            pass  # rare; accepted and ignored
        else:
            raise _bad_recurrence(line)
    return (rule, exdates) if rule else None


def _month_days(year: int, month: int, rule: dict[str, str], day: int) -> list[int]:
    last = ((dt.date(year + month // 12, month % 12 + 1, 1)) - dt.timedelta(days=1)).day
    if rule.get("BYDAY"):
        out = []
        for spec in rule["BYDAY"].split(","):
            m = re.fullmatch(r"(-?\d)?(\w\w)", spec)
            n, wd = (int(m.group(1)) if m.group(1) else None), _DAYS.index(m.group(2))
            days = [d for d in range(1, last + 1) if dt.date(year, month, d).weekday() == wd]
            if n is None:
                out += days
            elif 0 < abs(n) <= len(days):
                out.append(days[n - 1] if n > 0 else days[n])
        return sorted(set(out))
    if rule.get("BYMONTHDAY"):
        days = [int(x) for x in rule["BYMONTHDAY"].split(",")]
        return sorted({d if d > 0 else last + 1 + d for d in days if 1 <= (d if d > 0 else last + 1 + d) <= last})
    return [day] if day <= last else []


def _starts(ev: dict[str, Any], rule: dict[str, str], tz: str, stop: dt.datetime) -> list[dt.datetime]:
    """Original start times of a series, in order, up to (not including) ``stop``. COUNT counts
    from the first instance, as RFC 5545 says; UNTIL is inclusive."""
    etz = ev["start"].get("timeZone") or tz
    zone = ZoneInfo(etz)
    first = _parse(ev["start"], tz).astimezone(zone).replace(tzinfo=None)
    interval, count = int(rule.get("INTERVAL", 1)), int(rule["COUNT"]) if "COUNT" in rule else None
    until = _ical_time(rule["UNTIL"], etz) if "UNTIL" in rule else None
    if isinstance(until, dt.date) and not isinstance(until, dt.datetime):
        until = dt.datetime.combine(until, dt.time(23, 59, 59), zone)
    wanted = [_DAYS.index(d[-2:]) for d in rule.get("BYDAY", "").split(",") if d]
    out: list[dt.datetime] = []
    freq = rule["FREQ"]
    for period in range(0, 20000):
        if freq == "DAILY":
            day = first + dt.timedelta(days=period * interval)
            cands = [day] if not wanted or day.weekday() in wanted else []
        elif freq == "WEEKLY":
            monday = first - dt.timedelta(days=first.weekday()) + dt.timedelta(weeks=period * interval)
            cands = [monday + dt.timedelta(days=d) for d in sorted(wanted or [first.weekday()])]
        elif freq == "MONTHLY":
            y, m = divmod(first.month - 1 + period * interval, 12)
            cands = [first.replace(year=first.year + y, month=m + 1, day=d)
                     for d in _month_days(first.year + y, m + 1, rule, first.day)]
        else:
            y = first.year + period * interval
            cands = [first.replace(year=y)] if (first.month, first.day) != (2, 29) or y % 4 == 0 else []
        for c in cands:
            if c < first:
                continue
            t = c.replace(tzinfo=zone)
            if (until and t > until) or t >= stop or len(out) >= min(count or MAX_INSTANCES, MAX_INSTANCES):
                return out
            out.append(t)
    return out


def _key(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _excluded(t: dt.datetime, exdates: list[Any]) -> bool:
    return any(t.date() == x if not isinstance(x, dt.datetime) else t == x for x in exdates)


def _instance(ev: dict[str, Any], orig: dt.datetime, tz: str) -> dict[str, Any]:
    etz = ev["start"].get("timeZone") or tz
    length = _parse(ev["end"], tz) - _parse(ev["start"], tz)
    key = _key(orig)
    inst = {k: copy.deepcopy(v) for k, v in ev.items() if k not in ("recurrence", "_exceptions")}
    inst.update(id=f"{ev['id']}_{key}", recurringEventId=ev["id"], originalStartTime=_when(orig, etz),
                start=_when(orig, etz), end=_when(orig + length, etz))
    inst.update(copy.deepcopy((ev.get("_exceptions") or {}).get(key, {})))
    return inst


def _occurrences(state: dict[str, Any], ev: dict[str, Any], lo: Any, hi: Any, tz: str) -> list[dict[str, Any]]:
    """What an event looks like on a calendar in [lo, hi): itself, or its live instances."""
    rec = _recurrence(ev, tz) if state.get("_recurring") else None
    if rec is None:
        return [ev] if _in_range(ev, lo, hi, tz) else []
    rule, exdates = rec
    stop = hi or (max(lo, _parse(ev["start"], tz)) if lo else _parse(ev["start"], tz)) + dt.timedelta(days=366)
    out = []
    for orig in _starts(ev, rule, tz, stop):
        if _excluded(orig, exdates):
            continue
        inst = _instance(ev, orig, tz)
        if inst.get("status") != "cancelled" and _in_range(inst, lo, hi, tz):
            out.append(inst)
    return out


def _series_instance(state: dict[str, Any], master: dict[str, Any], orig: dt.datetime, tz: str) -> dict[str, Any] | None:
    rec = _recurrence(master, tz)
    if rec is None:
        return None
    if not any(t == orig for t in _starts(master, rec[0], tz, orig + dt.timedelta(seconds=1))) or _excluded(orig, rec[1]):
        return None
    return _instance(master, orig, tz)


def _busy_spans(state: dict[str, Any], person: str, exclude: str, lo: dt.datetime, hi: dt.datetime,
                tz: str) -> list[tuple[dt.datetime, dt.datetime]]:
    """When ``person`` is busy in [lo, hi) apart from event ``exclude``: events on their calendar
    they haven't declined, and free/busy blocks."""
    spans = []
    for e in state["events"].values():
        if e["id"] == exclude or e["status"] == "cancelled" or e.get("transparency") == "transparent":
            continue
        for inst in _occurrences(state, e, lo, hi, tz):
            if _on_calendar(inst, person) and not any(a["email"] == person and a["responseStatus"] == "declined"
                                                      for a in inst.get("attendees", [])):
                spans.append((_parse(inst["start"], tz), _parse(inst["end"], tz)))
    return spans + [(_parse(b["start"], tz), _parse(b["end"], tz)) for b in state["busy"].get(person, [])]


def _calendar_ids(v: Any) -> list[str]:
    if v is None:
        return ["primary"]
    return [v] if isinstance(v, str) else list(v)


def _view(ctx: Instance, ev: dict[str, Any]) -> dict[str, Any]:
    """The event as the acting user sees it (``self`` flags are relative to them)."""
    me = ctx.actor
    out = {k: v for k, v in ev.items() if k not in ("calendarId", "hiddenFor") and not k.startswith("_")}
    out["creator"] = {**ev["creator"], "self": ev["creator"]["email"] == me}
    out["organizer"] = {**ev["organizer"], "self": ev["organizer"]["email"] == me}
    if ev.get("attendees"):
        out["attendees"] = [{**a, "self": True} if a["email"] == me else dict(a) for a in ev["attendees"]]
    return out


# -- tools -------------------------------------------------------------------------------

@tool("list-calendars", read_only=True)
def list_calendars(ctx: Instance) -> dict[str, Any]:
    """List all available calendars"""
    me = ctx.actor
    return {"calendars": [{"id": c["id"], "summary": c["summary"], "timeZone": c["timeZone"],
                           "accessRole": _role(c, me), "primary": cid == me}
                          for cid, c in ctx.state["calendars"].items()
                          if not c.get("person") or cid == me or cid in ctx.state["busy"] or c["acl"].get(me)]}


@tool("list-events", read_only=True)
def list_events(ctx: Instance,
                calendarId: Annotated[str | list[str] | None, "ID of the calendar(s) to list events from. Use 'primary' for the main calendar"] = "primary",
                timeMin: Annotated[str | None, "Start time boundary (ISO 8601), e.g. 2026-09-22T00:00:00"] = None,
                timeMax: Annotated[str | None, "End time boundary (ISO 8601), e.g. 2026-09-29T00:00:00"] = None,
                timeZone: Annotated[str | None, "Timezone for interpreting times without an offset (IANA name)"] = None) -> dict[str, Any]:
    """List events from one or more calendars with date filtering"""
    s = ctx.state
    tz = timeZone or s["timeZone"]
    lo, hi = _range(timeMin, timeMax, tz)
    out = []
    for cid_in in _calendar_ids(calendarId):
        cid, cal = _cal(ctx, cid_in)
        _readable(ctx, cal)
        out += [_view(ctx, i) for e in s["events"].values() for i in _occurrences(s, e, lo, hi, tz) if _on_calendar(i, cid)]
    out.sort(key=lambda e: _parse(e["start"], tz))
    return {"events": out, "totalCount": len(out)}


@tool("get-event", read_only=True)
def get_event(ctx: Instance,
              eventId: Annotated[str, "ID of the event to retrieve"],
              calendarId: Annotated[str | None, "ID of the calendar. Use 'primary' for the main calendar"] = "primary") -> dict[str, Any]:
    """Get details of a specific event by ID"""
    _readable(ctx, _cal(ctx, calendarId)[1])
    return {"event": _view(ctx, _event(ctx, calendarId, eventId))}


@tool("search-events", read_only=True)
def search_events(ctx: Instance,
                  query: Annotated[str, "Free text search query (searches summary, description, location, attendees)"],
                  calendarId: Annotated[str | None, "ID of the calendar. Use 'primary' for the main calendar"] = "primary",
                  timeMin: Annotated[str | None, "Start time boundary (ISO 8601)"] = None,
                  timeMax: Annotated[str | None, "End time boundary (ISO 8601)"] = None,
                  timeZone: Annotated[str | None, "Timezone (IANA name)"] = None) -> dict[str, Any]:
    """Search for events in a calendar by text query"""
    s = ctx.state
    tz = timeZone or s["timeZone"]
    cid, cal = _cal(ctx, calendarId)
    _readable(ctx, cal)
    lo, hi = _range(timeMin, timeMax, tz)
    q = query.lower()
    hits = [_view(ctx, e) for m in s["events"].values() for e in _occurrences(s, m, lo, hi, tz)
            if _on_calendar(e, cid)
            and q in " ".join([e.get("summary", ""), e.get("description") or "", e.get("location") or "",
                               *[a["email"] for a in e.get("attendees", [])]]).lower()]
    hits.sort(key=lambda e: _parse(e["start"], tz))
    return {"events": hits, "totalCount": len(hits)}


@tool("create-event")
def create_event(ctx: Instance,
                 summary: Annotated[str, "Title of the event"],
                 start: Annotated[str, "Event start time (ISO 8601), e.g. 2026-09-22T10:00:00"],
                 end: Annotated[str, "Event end time (ISO 8601), e.g. 2026-09-22T10:30:00"],
                 calendarId: Annotated[str | None, "ID of the calendar. Use 'primary' for the main calendar"] = "primary",
                 description: Annotated[str | None, "Description/notes for the event"] = None,
                 timeZone: Annotated[str | None, "Timezone of the event start/end times (IANA name)"] = None,
                 location: Annotated[str | None, "Location of the event"] = None,
                 attendees: Annotated[list[dict] | None, "List of attendees, e.g. [{\"email\": \"john@acme.com\"}]"] = None,
                 colorId: Annotated[str | None, "Color ID for the event (1-11)"] = None,
                 recurrence: Annotated[list[str] | None, "Recurrence rules in RFC5545 format, e.g. [\"RRULE:FREQ=WEEKLY;COUNT=5\"]"] = None,
                 sendUpdates: Annotated[Literal["all", "externalOnly", "none"] | None, "Whether to send invitations"] = "all") -> dict[str, Any]:
    """Create a new calendar event"""
    s = ctx.state
    cid, cal = _cal(ctx, calendarId)
    _writable(ctx, cal)
    if colorId and colorId not in COLORS:
        raise _err(400, f"Invalid color id: {colorId}")
    if s.get("_recurring"):
        _recurrence({"recurrence": recurrence}, cal["timeZone"])
    ev = _new_event(ctx, s, cid, {
        "summary": summary, "start": start, "end": end, "description": description, "timeZone": timeZone,
        "location": location, "colorId": colorId, "recurrence": recurrence,
        "attendees": [a.get("email") if isinstance(a, dict) else a for a in attendees or []]}, actor=ctx.actor)
    ev["_by_agent"] = True  # graders can tell agent-made events from the world's (never shown to agents)
    if sendUpdates != "none" and ev.get("attendees"):
        ev["invitationsSent"] = [a["email"] for a in ev["attendees"] if a["email"] != ctx.actor]
    _schedule_responses(ctx, ev, [a["email"] for a in ev.get("attendees", [])])
    return {"event": _view(ctx, ev)}


def _apply_update(ev: dict[str, Any], tz: str, start: str | None, end: str | None, fields: dict[str, Any],
                  attendees: list[dict] | None) -> None:
    new_start = _parse(start, tz) if start else _parse(ev["start"], tz)
    new_end = _parse(end, tz) if end else _parse(ev["end"], tz)
    if new_end <= new_start:
        raise _err(400, "The specified time range is empty.", "timeRangeEmpty")
    ev["start"], ev["end"] = _when(new_start, tz), _when(new_end, tz)
    for k, v in fields.items():
        if v is not None:
            ev[k] = v
    if attendees is not None:
        existing = {a["email"]: a for a in ev.get("attendees", [])}
        ev["attendees"] = [existing.get(a.get("email") if isinstance(a, dict) else a,
                                        {"email": a.get("email") if isinstance(a, dict) else a, "responseStatus": "needsAction"})
                           for a in attendees]


@tool("update-event", idempotent=True, until=V1)
def update_event(ctx: Instance,
                 eventId: Annotated[str, "ID of the event to update"],
                 calendarId: Annotated[str | None, "ID of the calendar. Use 'primary' for the main calendar"] = "primary",
                 summary: Annotated[str | None, "Updated title"] = None,
                 description: Annotated[str | None, "Updated description"] = None,
                 start: Annotated[str | None, "Updated start time (ISO 8601)"] = None,
                 end: Annotated[str | None, "Updated end time (ISO 8601)"] = None,
                 timeZone: Annotated[str | None, "Timezone for start/end (IANA name)"] = None,
                 location: Annotated[str | None, "Updated location"] = None,
                 attendees: Annotated[list[dict] | None, "Updated attendee list (replaces the existing list)"] = None,
                 colorId: Annotated[str | None, "Updated color ID"] = None,
                 sendUpdates: Annotated[Literal["all", "externalOnly", "none"] | None, "Whether to send update notifications"] = "all") -> dict[str, Any]:
    """Update an existing calendar event"""
    s = ctx.state
    _, cal = _cal(ctx, calendarId)
    ev = _event(ctx, calendarId, eventId)
    _writable(ctx, s["calendars"][ev["calendarId"]])  # attendees can't edit the organizer's event
    tz = timeZone or ev["start"].get("timeZone") or cal["timeZone"]
    _apply_update(ev, tz, start, end, {"summary": summary, "description": description, "location": location,
                                       "colorId": colorId}, attendees)
    ev["updated"] = ctx.now().isoformat()
    ev["sequence"] = ev.get("sequence", 0) + 1
    return {"event": _view(ctx, ev)}


def _rrule_line(rule: dict[str, str]) -> str:
    return "RRULE:" + ";".join(f"{k}={v}" for k, v in rule.items())


def _split_series(ctx: Instance, ev: dict[str, Any], rule: dict[str, str], exdates: list[Any], split: dt.datetime,
                  tz: str) -> dict[str, Any]:
    """End a series before ``split`` and start a new one there (Google's thisAndFollowing)."""
    starts = _starts(ev, rule, tz, dt.datetime.max.replace(tzinfo=dt.timezone.utc))
    follow = [t for t in starts if t >= split and not _excluded(t, exdates)]
    if not follow:
        raise _err(400, "No instances of this recurring event on or after futureStartDate.", "invalid")
    first = follow[0]
    if first == starts[0]:
        return ev  # from the first instance on: that's the whole series
    before = sum(t < first for t in starts)
    old_rule, new_rule = dict(rule), dict(rule)
    if "COUNT" in rule:
        old_rule["COUNT"], new_rule["COUNT"] = str(before), str(int(rule["COUNT"]) - before)
    else:
        old_rule["UNTIL"] = _key(first - dt.timedelta(seconds=1))
    others = [r for r in ev["recurrence"] if not str(r).upper().startswith("RRULE")]
    new = copy.deepcopy(ev)
    etz = ev["start"].get("timeZone") or tz
    new.update(id=ctx.token(26, "abcdefghijklmnopqrstuv0123456789"), recurrence=[_rrule_line(new_rule), *others],
               start=_when(first, etz), end=_when(first + (_parse(ev["end"], tz) - _parse(ev["start"], tz)), etz),
               created=ctx.now().isoformat(), sequence=0)
    new["htmlLink"], new["iCalUID"] = f"https://www.google.com/calendar/event?eid={new['id']}", f"{new['id']}@google.com"
    cut = _key(first)
    exc = ev.get("_exceptions") or {}
    new["_exceptions"] = {k: v for k, v in exc.items() if k >= cut}
    ev["_exceptions"] = {k: v for k, v in exc.items() if k < cut}
    ev["recurrence"] = [_rrule_line(old_rule), *others]
    ev["updated"] = ctx.now().isoformat()
    ctx.state["events"][new["id"]] = new
    return new


@tool("update-event", idempotent=True, since=V1)
def update_event_v1(ctx: Instance,
                    eventId: Annotated[str, "ID of the event to update"],
                    calendarId: Annotated[str | None, "ID of the calendar. Use 'primary' for the main calendar"] = "primary",
                    summary: Annotated[str | None, "Updated title"] = None,
                    description: Annotated[str | None, "Updated description"] = None,
                    start: Annotated[str | None, "Updated start time (ISO 8601)"] = None,
                    end: Annotated[str | None, "Updated end time (ISO 8601)"] = None,
                    timeZone: Annotated[str | None, "Timezone for start/end (IANA name)"] = None,
                    location: Annotated[str | None, "Updated location"] = None,
                    attendees: Annotated[list[dict] | None, "Updated attendee list (replaces the existing list)"] = None,
                    colorId: Annotated[str | None, "Updated color ID"] = None,
                    modificationScope: Annotated[Literal["thisAndFollowing", "all", "thisEventOnly"] | None,
                                                 "Scope for recurring event modifications: 'thisEventOnly' (one instance), "
                                                 "'thisAndFollowing' (this and future instances) or 'all' (every instance). Defaults to 'all'"] = None,
                    originalStartTime: Annotated[str | None, "Original start time of the instance to modify (ISO 8601). Required when modificationScope is 'thisEventOnly'"] = None,
                    futureStartDate: Annotated[str | None, "Date from which changes apply (ISO 8601). Required when modificationScope is 'thisAndFollowing'"] = None,
                    sendUpdates: Annotated[Literal["all", "externalOnly", "none"] | None, "Whether to send update notifications"] = "all") -> dict[str, Any]:
    """Update an existing calendar event, with recurring event modification scope support"""
    s = ctx.state
    _, cal = _cal(ctx, calendarId)
    ev = _event(ctx, calendarId, eventId)
    _writable(ctx, s["calendars"][ev["calendarId"]])  # attendees can't edit the organizer's event
    tz = timeZone or ev["start"].get("timeZone") or cal["timeZone"]
    fields = {"summary": summary, "description": description, "location": location, "colorId": colorId}
    rec = None if _is_instance(ev) else _recurrence(ev, tz)
    if _is_instance(ev):
        if modificationScope not in (None, "thisEventOnly"):
            raise _err(400, f"modificationScope '{modificationScope}' needs the recurring event's id "
                            f"({ev['recurringEventId']}), not an instance id.", "invalid")
        target = ev
    elif modificationScope in ("thisEventOnly", "thisAndFollowing") and rec is None:
        raise _err(400, "modificationScope only applies to recurring events.", "invalid")
    elif modificationScope == "thisEventOnly":
        if not originalStartTime:
            raise _err(400, "originalStartTime is required when modificationScope is 'thisEventOnly'.", "required")
        target = _series_instance(s, ev, _parse(originalStartTime, tz), tz)
        if target is None:
            raise _err(404, f"No instance of this recurring event starts at {originalStartTime}.", "notFound")
    elif modificationScope == "thisAndFollowing":
        if not futureStartDate:
            raise _err(400, "futureStartDate is required when modificationScope is 'thisAndFollowing'.", "required")
        target = _split_series(ctx, ev, rec[0], rec[1], _parse(futureStartDate, tz), tz)
    else:
        target = ev
    had = {a["email"] for a in target.get("attendees", [])}
    _apply_update(target, tz, start, end, fields, attendees)
    if _is_instance(target):
        _save_instance(ctx, target)
    else:
        target["updated"] = ctx.now().isoformat()
        target["sequence"] = target.get("sequence", 0) + 1
    _schedule_responses(ctx, target, [a["email"] for a in target.get("attendees", []) if a["email"] not in had])
    return {"event": _view(ctx, target)}


@tool("delete-event", destructive=True, idempotent=False)
def delete_event(ctx: Instance,
                 eventId: Annotated[str, "ID of the event to delete"],
                 calendarId: Annotated[str | None, "ID of the calendar. Use 'primary' for the main calendar"] = "primary",
                 sendUpdates: Annotated[Literal["all", "externalOnly", "none"] | None, "Whether to send cancellation notifications"] = "all") -> dict[str, Any]:
    """Delete a calendar event"""
    s = ctx.state
    cid, cal = _cal(ctx, calendarId)
    _writable(ctx, cal)
    ev = s["events"].get(eventId)
    if ev is None and s.get("_recurring") and "_" in eventId:  # an instance: gone if its exception says so
        master = s["events"].get(eventId.partition("_")[0])
        exc = ((master or {}).get("_exceptions") or {}).get(eventId.partition("_")[2], {})
        ev = {**(master or {}), **exc} if master else None
    if ev is not None and (ev["status"] == "cancelled" or cid in ev.get("hiddenFor", [])):
        raise _err(410, "Resource has been deleted", "deleted")  # Google answers 410 Gone on a second delete
    ev = _event(ctx, calendarId, eventId)
    if ev["calendarId"] != cid:
        # an attendee removing an invite from their calendar declines it; the organizer's event stays
        for a in ev.get("attendees", []):
            if a["email"] == cid:
                a["responseStatus"] = "declined"
        ev["hiddenFor"] = [*ev.get("hiddenFor", []), cid]
    else:
        ev["status"] = "cancelled"
    if _is_instance(ev):
        _save_instance(ctx, ev)  # deleting one instance of a series leaves the rest
    else:
        ev["updated"] = ctx.now().isoformat()
    return {"success": True, "message": "Event deleted successfully"}


@tool("respond-to-event", idempotent=True)
def respond_to_event(ctx: Instance,
                     eventId: Annotated[str, "ID of the event to respond to"],
                     response: Annotated[Literal["accepted", "declined", "tentative", "needsAction"], "Your response"],
                     calendarId: Annotated[str | None, "ID of the calendar. Use 'primary' for the main calendar"] = "primary") -> dict[str, Any]:
    """Respond to an event invitation (accept, decline, maybe, or no response)"""
    ev = _event(ctx, calendarId, eventId)
    me = ctx.actor
    mine = next((a for a in ev.get("attendees", []) if a["email"] == me), None)
    if mine is None:
        raise _err(400, "You are not an attendee of this event.", "notAnAttendee")
    if mine.get("organizer"):
        raise _err(400, "The organizer cannot respond to their own event.", "organizerResponse")
    mine["responseStatus"] = response
    if _is_instance(ev):
        _save_instance(ctx, ev)
    else:
        ev["updated"] = ctx.now().isoformat()
    return {"event": _view(ctx, ev), "responseStatus": response}


@tool("get-freebusy", read_only=True)
def get_freebusy(ctx: Instance,
                 calendars: Annotated[list[dict], "Calendars to check, e.g. [{\"id\": \"primary\"}, {\"id\": \"john@acme.com\"}]"],
                 timeMin: Annotated[str, "Start of the interval (ISO 8601)"],
                 timeMax: Annotated[str, "End of the interval (ISO 8601)"],
                 timeZone: Annotated[str | None, "Timezone (IANA name)"] = None) -> dict[str, Any]:
    """Query free/busy information for calendars, including other people's"""
    s = ctx.state
    tz = timeZone or s["timeZone"]
    lo, hi = _range(timeMin, timeMax, tz)
    out: dict[str, Any] = {}
    for c in calendars:
        raw = c.get("id") if isinstance(c, dict) else c
        cid = ctx.actor if raw == "primary" else str(raw).lower()
        if cid not in s["calendars"]:
            out[raw] = {"errors": [{"domain": "global", "reason": "notFound"}], "busy": []}
            continue
        owner = cid if s["calendars"][cid].get("person") else None
        blocks = [(_parse(e["start"], tz), _parse(e["end"], tz)) for m in s["events"].values()
                  for e in _occurrences(s, m, lo, hi, tz)
                  if _on_calendar(e, cid) and e.get("transparency") != "transparent"
                  and not any(a["email"] == (owner or e["organizer"]["email"]) and a["responseStatus"] == "declined"
                              for a in e.get("attendees", []))]
        blocks += [(_parse(b["start"], tz), _parse(b["end"], tz)) for b in s["busy"].get(cid, [])]
        blocks = sorted((max(a, lo), min(b, hi)) for a, b in blocks if b > lo and a < hi)
        merged: list[list[dt.datetime]] = []
        for a, b in blocks:
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        out[raw] = {"busy": [{"start": a.astimezone(ZoneInfo(tz)).isoformat(), "end": b.astimezone(ZoneInfo(tz)).isoformat()}
                             for a, b in merged]}
    return {"timeMin": lo.isoformat(), "timeMax": hi.isoformat(), "calendars": out}


@tool("get-current-time", read_only=True)
def get_current_time(ctx: Instance, timeZone: Annotated[str | None, "Timezone (IANA name). Defaults to the calendar's"] = None) -> dict[str, Any]:
    """Get the current date and time in the calendar's timezone"""
    tz = timeZone or ctx.state["timeZone"]
    try:
        now = ctx.now().astimezone(ZoneInfo(tz))
    except Exception:
        raise _err(400, f"Invalid timezone: {tz}") from None
    return {"currentTime": now.isoformat(), "timeZone": tz, "dayOfWeek": now.strftime("%A")}


@tool("list-colors", read_only=True)
def list_colors(ctx: Instance) -> dict[str, Any]:
    """List available color IDs and their meanings for calendar events"""
    return {"event": {k: {"background": v, "foreground": "#1d1d1d"} for k, v in COLORS.items()}}


Calendar.tools = [list_calendars, list_events, get_event, search_events, create_event, update_event, update_event_v1,
                  delete_event,
                  respond_to_event, get_freebusy, get_current_time, list_colors]


# -- world actions (triggered by environments, never by agents) --------------------------

def _find_event(ctx: Instance, summary: str) -> dict[str, Any]:
    ev = next((e for e in ctx.state["events"].values() if e["status"] != "cancelled"
               and summary.lower() in e.get("summary", "").lower()), None)
    if ev is None:
        raise _err(404, f"no event matching {summary!r}")
    return ev


@action("add_event")
def act_add_event(ctx: Instance, calendar: str, summary: str, start: str, end: str,
                  attendees: list[str] | None = None) -> str:
    """Someone books time on their calendar (e.g. the slot the agent just checked)."""
    cid = calendar.lower()
    if cid not in ctx.state["calendars"]:
        raise _err(404, f"no calendar {calendar}")
    return _new_event(ctx, ctx.state, cid, {"summary": summary, "start": start, "end": end,
                                            "attendees": attendees or []}, actor=cid)["id"]


@action("add_busy")
def act_add_busy(ctx: Instance, calendar: str, start: str, end: str) -> None:
    """A calendar becomes busy (for people known only through free/busy)."""
    ctx.state["busy"].setdefault(calendar.lower(), []).append({"start": start, "end": end})


@action("respond")
def act_respond(ctx: Instance, summary: str, attendee: str, response: str) -> None:
    """A person without an agent answers an invite."""
    ev = _find_event(ctx, summary)
    a = next((a for a in ev.get("attendees", []) if a["email"] == attendee.lower()), None)
    if a is None:
        raise _err(400, f"{attendee} is not invited to {summary!r}")
    a["responseStatus"] = response
    ev["updated"] = ctx.now().isoformat()


@action("cancel_event")
def act_cancel_event(ctx: Instance, summary: str) -> None:
    """The organizer cancels an event."""
    ev = _find_event(ctx, summary)
    ev["status"] = "cancelled"
    ev["updated"] = ctx.now().isoformat()


def _schedule_responses(ctx: Instance, ev: dict[str, Any], invited: list[str]) -> None:
    """People without agents answer invites on their own, a while later (2026-09-25.1)."""
    policies = ctx.state.get("_policies") or {}
    for email in invited:
        p = policies.get(email)
        if p and email not in (ctx.actor, ev["calendarId"]):
            ctx.schedule(p["delay"], "auto_respond", {"event_id": ev["id"], "attendee": email},
                         reason=f"{email} answers the invite")


@action("auto_respond")
def act_auto_respond(ctx: Instance, event_id: str, attendee: str, response: str | None = None) -> None:
    """Someone answers an invite by their policy (or ``response``), unless they already have. The
    if_free policy looks at their calendar when they get to it, not when they were invited."""
    s, email = ctx.state, attendee.lower()
    ev = _lookup(s, event_id)
    if ev is None or ev["status"] == "cancelled":
        return
    mine = next((a for a in ev.get("attendees", []) if a["email"] == email), None)
    if mine is None or (mine["responseStatus"] != "needsAction" and not response):
        return
    policy = response or (s.get("_policies") or {}).get(email, {}).get("response", "accept")
    if policy == "if_free":
        tz = s["timeZone"]
        first = (_occurrences(s, ev, None, None, tz) or [ev])[0]
        a, b = _parse(first["start"], tz), _parse(first["end"], tz)
        busy = any(x < b and a < y for x, y in _busy_spans(s, email, ev.get("recurringEventId", ev["id"]), a, b, tz))
        policy = "decline" if busy else "accept"
    mine["responseStatus"] = {"accept": "accepted", "decline": "declined", "tentative": "tentative"}.get(policy, policy)
    if _is_instance(ev):
        _save_instance(ctx, ev)
    else:
        ev["updated"] = ctx.now().isoformat()


Calendar.actions = [act_add_event, act_add_busy, act_respond, act_cancel_event, act_auto_respond]
