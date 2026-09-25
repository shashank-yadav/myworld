"""calendar: agent-facing tools."""

from __future__ import annotations

import copy
import datetime as dt
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from ...core.instance import Instance
from ...core.tools import tool
from .actions import _schedule_responses
from .model import (
    COLORS,
    V1,
    _cal,
    _err,
    _is_instance,
    _new_event,
    _on_calendar,
    _parse,
    _range,
    _readable,
    _role,
    _when,
    _writable,
)
from .recurrence import (
    _calendar_ids,
    _event,
    _excluded,
    _key,
    _occurrences,
    _recurrence,
    _save_instance,
    _series_instance,
    _starts,
    _view,
)


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
