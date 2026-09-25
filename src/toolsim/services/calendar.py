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
from ..core.tools import ToolError, action, tool

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

    versions = {"2026-09-25": "Initial release: 11 tools modeled on nspady/google-calendar-mcp."}

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
        for e in seed.get("events", []):
            organizer = (e.get("organizer") or state["default"]).lower()
            cid = e.get("calendarId", "primary")
            _new_event(ctx, state, organizer if cid == "primary" else cid, e, actor=organizer)
        return state

    def default_actor(self, state: dict[str, Any]) -> str:
        return state["default"]

    def grading_view(self, state: dict[str, Any]) -> dict[str, Any]:
        """Events annotated with ``booked_by_agent`` and ``conflicts_for``: the people for whom the
        event overlaps something else they're busy with (accepted events or free/busy blocks)."""
        tz = state["timeZone"]
        live = [e for e in state["events"].values() if e["status"] != "cancelled"]

        def busy_for(person: str, exclude: str) -> list[tuple[dt.datetime, dt.datetime]]:
            spans = [(_parse(e["start"], tz), _parse(e["end"], tz)) for e in live if e["id"] != exclude
                     and e.get("transparency") != "transparent" and _on_calendar(e, person)
                     and not any(a["email"] == person and a["responseStatus"] == "declined" for a in e.get("attendees", []))]
            return spans + [(_parse(b["start"], tz), _parse(b["end"], tz)) for b in state["busy"].get(person, [])]

        events = []
        for e in state["events"].values():
            s, en = _parse(e["start"], tz), _parse(e["end"], tz)
            people = {e["calendarId"], *[a["email"] for a in e.get("attendees", [])]}
            conflicts = sorted(p for p in people if e["status"] != "cancelled"
                               and any(a < en and s < b for a, b in busy_for(p, e["id"])))
            events.append({**e, "booked_by_agent": bool(e.get("_by_agent")), "conflicts_for": conflicts})
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
    cid, cal = _cal(ctx, calendar_id)
    ev = ctx.state["events"].get(event_id)
    if ev is None or not _on_calendar(ev, cid):
        raise _err(404, "Not Found", "notFound")
    return ev


def _range(time_min: str | None, time_max: str | None, tz: str) -> tuple[Any, Any]:
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
        out += [_view(ctx, e) for e in s["events"].values() if _on_calendar(e, cid) and _in_range(e, lo, hi, tz)]
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
    hits = [_view(ctx, e) for e in s["events"].values()
            if _on_calendar(e, cid) and _in_range(e, lo, hi, tz)
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
    ev = _new_event(ctx, s, cid, {
        "summary": summary, "start": start, "end": end, "description": description, "timeZone": timeZone,
        "location": location, "colorId": colorId, "recurrence": recurrence,
        "attendees": [a.get("email") if isinstance(a, dict) else a for a in attendees or []]}, actor=ctx.actor)
    ev["_by_agent"] = True  # graders can tell agent-made events from the world's (never shown to agents)
    if sendUpdates != "none" and ev.get("attendees"):
        ev["invitationsSent"] = [a["email"] for a in ev["attendees"] if a["email"] != ctx.actor]
    return {"event": _view(ctx, ev)}


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
    _, cal = _cal(ctx, calendarId)
    ev = _event(ctx, calendarId, eventId)
    _writable(ctx, s["calendars"][ev["calendarId"]])  # attendees can't edit the organizer's event
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
    return {"event": _view(ctx, ev)}


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
    if ev is not None and (ev["status"] == "cancelled" or cid in ev.get("hiddenFor", [])):
        raise _err(410, "Resource has been deleted", "deleted")  # Google answers 410 Gone on a second delete
    ev = _event(ctx, calendarId, eventId)
    if ev["calendarId"] != cid:
        # an attendee removing an invite from their calendar declines it; the organizer's event stays
        for a in ev.get("attendees", []):
            if a["email"] == cid:
                a["responseStatus"] = "declined"
        ev["hiddenFor"].append(cid)
    else:
        ev["status"] = "cancelled"
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
        blocks = [(_parse(e["start"], tz), _parse(e["end"], tz)) for e in s["events"].values()
                  if _on_calendar(e, cid) and e.get("transparency") != "transparent" and _in_range(e, lo, hi, tz)
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


Calendar.tools = [list_calendars, list_events, get_event, search_events, create_event, update_event, delete_event,
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


Calendar.actions = [act_add_event, act_add_busy, act_respond, act_cancel_event]
