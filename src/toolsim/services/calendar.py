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
         attendees: [priya@acme.com], calendarId: primary}
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from ..core.instance import Instance, Service
from ..core.tools import ToolError, tool

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
        user = seed.get("user") or {"email": "alex@acme.com", "name": "Alex Rivera"}
        tz = seed.get("timeZone", "America/Los_Angeles")
        state: dict[str, Any] = {"user": user, "timeZone": tz, "calendars": {}, "events": {}, "busy": {}}
        state["calendars"]["primary"] = {"id": user["email"], "summary": user["name"], "timeZone": tz,
                                         "accessRole": "owner", "primary": True}
        for c in seed.get("calendars", []):
            state["calendars"][c["id"]] = {"id": c["id"], "summary": c.get("summary", c["id"]),
                                           "timeZone": c.get("timeZone", tz), "accessRole": c.get("accessRole", "reader"),
                                           "primary": False}
            state["busy"][c["id"]] = [{"start": b["start"], "end": b["end"]} for b in c.get("busy", [])]
        for e in seed.get("events", []):
            _new_event(ctx, state, e.get("calendarId", "primary"), e)
        return state

    def fault_error(self, fault: Any) -> tuple[Any, int]:
        if fault.kind == "rate_limit":
            return {"error": {"code": 403, "message": "Rate Limit Exceeded", "errors": [{"reason": "rateLimitExceeded"}]}}, 403
        return super().fault_error(fault)


# -- helpers -----------------------------------------------------------------------------

def _cal(state: dict[str, Any], calendar_id: str | None) -> tuple[str, dict[str, Any]]:
    cid = calendar_id or "primary"
    if cid == state["user"]["email"]:
        cid = "primary"
    cal = state["calendars"].get(cid)
    if cal is None:
        raise _err(404, "Not Found", "notFound")
    return cid, cal


def _writable(cal: dict[str, Any]) -> None:
    if cal["accessRole"] not in ("owner", "writer"):
        raise _err(403, "You need to have writer access to this calendar.", "requiredAccessLevel")


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


def _new_event(ctx: Instance, state: dict[str, Any], calendar_id: str, e: dict[str, Any]) -> dict[str, Any]:
    cid, cal = _cal(state, calendar_id)
    tz = e.get("timeZone") or cal["timeZone"]
    start, end = _parse(e["start"], tz), _parse(e["end"], tz)
    if end <= start:
        raise _err(400, "The specified time range is empty.", "timeRangeEmpty")
    eid = ctx.token(26, "abcdefghijklmnopqrstuv0123456789")
    me = state["user"]["email"]
    attendees = [{"email": a if isinstance(a, str) else a["email"],
                  "responseStatus": (a.get("responseStatus") if isinstance(a, dict) else None) or "needsAction"}
                 for a in e.get("attendees") or []]
    if attendees and cid == "primary" and not any(a["email"] == me for a in attendees):
        attendees.insert(0, {"email": me, "organizer": True, "self": True, "responseStatus": "accepted"})
    now = ctx.now().isoformat()
    ev = {"kind": "calendar#event", "id": eid, "calendarId": cid, "status": "confirmed",
          "htmlLink": f"https://www.google.com/calendar/event?eid={eid}", "created": now, "updated": now,
          "summary": e.get("summary", "(No title)"), "description": e.get("description"), "location": e.get("location"),
          "creator": {"email": me, "self": True}, "organizer": {"email": cal["id"], "self": cid == "primary"},
          "start": _when(start, tz), "end": _when(end, tz), "attendees": attendees or None,
          "colorId": e.get("colorId"), "transparency": e.get("transparency", "opaque"),
          "recurrence": e.get("recurrence"), "iCalUID": f"{eid}@google.com", "sequence": 0}
    state["events"][eid] = {k: v for k, v in ev.items() if v is not None}
    return state["events"][eid]


def _event(state: dict[str, Any], calendar_id: str | None, event_id: str) -> dict[str, Any]:
    cid, _ = _cal(state, calendar_id)
    ev = state["events"].get(event_id)
    if ev is None or ev["calendarId"] != cid or ev["status"] == "cancelled":
        raise _err(404, "Not Found", "notFound")
    return ev


def _range(state: dict[str, Any], time_min: str | None, time_max: str | None, tz: str) -> tuple[Any, Any]:
    lo = _parse(time_min, tz) if time_min else None
    hi = _parse(time_max, tz) if time_max else None
    if lo and hi and hi <= lo:
        raise _err(400, "The specified time range is empty.", "timeRangeEmpty")
    return lo, hi


def _in_range(ev: dict[str, Any], lo: Any, hi: Any, tz: str) -> bool:
    s, e = _parse(ev["start"], tz), _parse(ev["end"], tz)
    return (hi is None or s < hi) and (lo is None or e > lo)


def _calendar_ids(v: Any) -> list[str]:
    if v is None:
        return ["primary"]
    return [v] if isinstance(v, str) else list(v)


def _view(ev: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in ev.items() if k != "calendarId"}


# -- tools -------------------------------------------------------------------------------

@tool("list-calendars", read_only=True)
def list_calendars(ctx: Instance) -> dict[str, Any]:
    """List all available calendars"""
    return {"calendars": [{"id": c["id"], "summary": c["summary"], "timeZone": c["timeZone"],
                           "accessRole": c["accessRole"], "primary": c["primary"]} for c in ctx.state["calendars"].values()]}


@tool("list-events", read_only=True)
def list_events(ctx: Instance,
                calendarId: Annotated[str | list[str] | None, "ID of the calendar(s) to list events from. Use 'primary' for the main calendar"] = "primary",
                timeMin: Annotated[str | None, "Start time boundary (ISO 8601), e.g. 2026-09-22T00:00:00"] = None,
                timeMax: Annotated[str | None, "End time boundary (ISO 8601), e.g. 2026-09-29T00:00:00"] = None,
                timeZone: Annotated[str | None, "Timezone for interpreting times without an offset (IANA name)"] = None) -> dict[str, Any]:
    """List events from one or more calendars with date filtering"""
    s = ctx.state
    tz = timeZone or s["timeZone"]
    lo, hi = _range(s, timeMin, timeMax, tz)
    out = []
    for cid_in in _calendar_ids(calendarId):
        cid, cal = _cal(s, cid_in)
        if cal["accessRole"] == "freeBusyReader":
            raise _err(403, "Not allowed to read events on this calendar (free/busy access only).", "forbidden")
        out += [_view(e) for e in s["events"].values()
                if e["calendarId"] == cid and e["status"] != "cancelled" and _in_range(e, lo, hi, tz)]
    out.sort(key=lambda e: _parse(e["start"], tz))
    return {"events": out, "totalCount": len(out)}


@tool("get-event", read_only=True)
def get_event(ctx: Instance,
              eventId: Annotated[str, "ID of the event to retrieve"],
              calendarId: Annotated[str | None, "ID of the calendar. Use 'primary' for the main calendar"] = "primary") -> dict[str, Any]:
    """Get details of a specific event by ID"""
    return {"event": _view(_event(ctx.state, calendarId, eventId))}


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
    cid, _ = _cal(s, calendarId)
    lo, hi = _range(s, timeMin, timeMax, tz)
    q = query.lower()
    hits = [_view(e) for e in s["events"].values()
            if e["calendarId"] == cid and e["status"] != "cancelled" and _in_range(e, lo, hi, tz)
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
    _, cal = _cal(s, calendarId)
    _writable(cal)
    if colorId and colorId not in COLORS:
        raise _err(400, f"Invalid color id: {colorId}")
    ev = _new_event(ctx, s, calendarId or "primary", {
        "summary": summary, "start": start, "end": end, "description": description, "timeZone": timeZone,
        "location": location, "colorId": colorId, "recurrence": recurrence,
        "attendees": [a.get("email") if isinstance(a, dict) else a for a in attendees or []]})
    if sendUpdates != "none" and ev.get("attendees"):
        ev["invitationsSent"] = [a["email"] for a in ev["attendees"] if not a.get("self")]
    return {"event": _view(ev)}


@tool("update-event", idempotent=True)
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
    _, cal = _cal(s, calendarId)
    _writable(cal)
    ev = _event(s, calendarId, eventId)
    tz = timeZone or ev["start"].get("timeZone") or cal["timeZone"]
    new_start = _parse(start, tz) if start else _parse(ev["start"], tz)
    new_end = _parse(end, tz) if end else _parse(ev["end"], tz)
    if new_end <= new_start:
        raise _err(400, "The specified time range is empty.", "timeRangeEmpty")
    ev["start"], ev["end"] = _when(new_start, tz), _when(new_end, tz)
    for k, v in (("summary", summary), ("description", description), ("location", location), ("colorId", colorId)):
        if v is not None:
            ev[k] = v
    if attendees is not None:
        existing = {a["email"]: a for a in ev.get("attendees", [])}
        ev["attendees"] = [existing.get(a.get("email") if isinstance(a, dict) else a,
                                        {"email": a.get("email") if isinstance(a, dict) else a, "responseStatus": "needsAction"})
                           for a in attendees]
    ev["updated"] = ctx.now().isoformat()
    ev["sequence"] = ev.get("sequence", 0) + 1
    return {"event": _view(ev)}


@tool("delete-event", destructive=True, idempotent=False)
def delete_event(ctx: Instance,
                 eventId: Annotated[str, "ID of the event to delete"],
                 calendarId: Annotated[str | None, "ID of the calendar. Use 'primary' for the main calendar"] = "primary",
                 sendUpdates: Annotated[Literal["all", "externalOnly", "none"] | None, "Whether to send cancellation notifications"] = "all") -> dict[str, Any]:
    """Delete a calendar event"""
    s = ctx.state
    _, cal = _cal(s, calendarId)
    _writable(cal)
    ev = s["events"].get(eventId)
    if ev is not None and ev["status"] == "cancelled":
        raise _err(410, "Resource has been deleted", "deleted")  # Google answers 410 Gone on a second delete
    ev = _event(s, calendarId, eventId)
    ev["status"] = "cancelled"
    ev["updated"] = ctx.now().isoformat()
    return {"success": True, "message": "Event deleted successfully"}


@tool("respond-to-event", idempotent=True)
def respond_to_event(ctx: Instance,
                     eventId: Annotated[str, "ID of the event to respond to"],
                     response: Annotated[Literal["accepted", "declined", "tentative", "needsAction"], "Your response"],
                     calendarId: Annotated[str | None, "ID of the calendar. Use 'primary' for the main calendar"] = "primary") -> dict[str, Any]:
    """Respond to an event invitation (accept, decline, maybe, or no response)"""
    s = ctx.state
    ev = _event(s, calendarId, eventId)
    me = s["user"]["email"]
    mine = next((a for a in ev.get("attendees", []) if a["email"] == me), None)
    if mine is None:
        raise _err(400, "You are not an attendee of this event.", "notAnAttendee")
    if mine.get("organizer"):
        raise _err(400, "The organizer cannot respond to their own event.", "organizerResponse")
    mine["responseStatus"] = response
    ev["updated"] = ctx.now().isoformat()
    return {"event": _view(ev), "responseStatus": response}


@tool("get-freebusy", read_only=True)
def get_freebusy(ctx: Instance,
                 calendars: Annotated[list[dict], "Calendars to check, e.g. [{\"id\": \"primary\"}, {\"id\": \"john@acme.com\"}]"],
                 timeMin: Annotated[str, "Start of the interval (ISO 8601)"],
                 timeMax: Annotated[str, "End of the interval (ISO 8601)"],
                 timeZone: Annotated[str | None, "Timezone (IANA name)"] = None) -> dict[str, Any]:
    """Query free/busy information for calendars, including other people's"""
    s = ctx.state
    tz = timeZone or s["timeZone"]
    lo, hi = _range(s, timeMin, timeMax, tz)
    out: dict[str, Any] = {}
    for c in calendars:
        raw = c.get("id") if isinstance(c, dict) else c
        cid = "primary" if raw in ("primary", s["user"]["email"]) else raw
        if cid not in s["calendars"]:
            out[raw] = {"errors": [{"domain": "global", "reason": "notFound"}], "busy": []}
            continue
        blocks = [(_parse(e["start"], tz), _parse(e["end"], tz)) for e in s["events"].values()
                  if e["calendarId"] == cid and e["status"] != "cancelled" and e.get("transparency") != "transparent"
                  and _in_range(e, lo, hi, tz)
                  and not any(a.get("self") and a["responseStatus"] == "declined" for a in e.get("attendees", []))]
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


Calendar.tools = [list_calendars, list_events, get_event, search_events, create_event, update_event, delete_event,
                  respond_to_event, get_freebusy, get_current_time, list_colors]
