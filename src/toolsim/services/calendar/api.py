"""calendar: the Google Calendar REST API (v3), as ``gog``, Google's client libraries and ``curl`` see it.

The REST API differs from the MCP server in ways clients depend on: ``singleEvents=false`` returns
recurring events, not their instances; ``orderBy=startTime`` needs ``singleEvents``; invitations
go out only with ``sendUpdates``; responses are Google's resources (kinds, etags, RFC 3339 times,
reminders, Meet links with ``conferenceDataVersion=1``).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import urllib.parse
from typing import Any
from zoneinfo import ZoneInfo

from ...api import Request, Response, operation
from ...api.google import b64url, error, page, select, unb64url
from ...core.instance import Instance
from .actions import _schedule_responses
from .model import (
    COLORS,
    _cal,
    _is_instance,
    _mail_attendees,
    _mail_organizer,
    _new_event,
    _on_calendar,
    _parse,
    _readable,
    _role,
    _when,
    _writable,
)
from .recurrence import _lookup, _occurrences, _recurrence, _save_instance, _view

HOSTS = ("www.googleapis.com", "calendar-json.googleapis.com")
BASE = "/calendar/v3"
FIELDS = ["kind", "etag", "id", "status", "htmlLink", "created", "updated", "summary", "description", "location",
          "colorId", "creator", "organizer", "start", "end", "endTimeUnspecified", "recurrence", "recurringEventId",
          "originalStartTime", "transparency", "visibility", "iCalUID", "sequence", "attendees", "attendeesOmitted",
          "extendedProperties", "hangoutLink", "conferenceData", "guestsCanInviteOthers", "guestsCanModify",
          "guestsCanSeeOtherGuests", "privateCopy", "locked", "reminders", "source", "workingLocationProperties",
          "outOfOfficeProperties", "focusTimeProperties", "attachments", "eventType"]
SETTABLE = ["summary", "description", "location", "colorId", "transparency", "visibility", "guestsCanInviteOthers",
            "guestsCanModify", "guestsCanSeeOtherGuests", "reminders", "extendedProperties", "source", "eventType",
            "workingLocationProperties", "outOfOfficeProperties", "focusTimeProperties", "attachments"]
CALENDAR_COLORS = {str(i): c for i, c in enumerate(
    ["#ac725e", "#d06b64", "#f83a22", "#fa573c", "#ff7537", "#ffad46", "#42d692", "#16a765", "#7bd148", "#b3dc6c",
     "#fbe983", "#fad165", "#92e1c0", "#9fe1e7", "#9fc6e7", "#4986e7", "#9a9cff", "#b99aff", "#c2c2c2", "#cabdbf",
     "#cca6ac", "#f691b2", "#cd74e6", "#a47ae2"], start=1)}


def op(op_id: str, method: str, path: str, **kw: Any):  # noqa: ANN201
    return operation(op_id, method, BASE + path, hosts=HOSTS, **kw)


def _rfc(v: str | None) -> str | None:
    if not v:
        return v
    t = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    t = t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    t = t.astimezone(dt.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def _etag(*parts: Any) -> str:
    return '"' + str(int(hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest(), 16))[:16] + '"'


def _resource(ctx: Instance, ev: dict[str, Any]) -> dict[str, Any]:
    """An event as Google's API returns it to the acting user."""
    v = _view(ctx, ev)
    out = {k: v[k] for k in FIELDS if k in v and v[k] is not None}
    out["kind"] = "calendar#event"
    out["etag"] = _etag(ev["id"], ev.get("updated"), ev.get("sequence"))
    out["created"], out["updated"] = _rfc(ev.get("created")), _rfc(ev.get("updated"))
    if out.get("transparency") == "opaque":
        out.pop("transparency")  # Google leaves the default out
    if not out.get("description"):
        out.pop("description", None)
    out.setdefault("reminders", {"useDefault": True})
    out.setdefault("eventType", "default")
    if out.get("status") == "cancelled":
        return {k: out[k] for k in ("kind", "etag", "id", "status", "recurringEventId", "originalStartTime") if k in out}
    return out


def _calendar(ctx: Instance, req: Request, key: str = "calendarId") -> tuple[str, dict[str, Any]]:
    return _cal(ctx, urllib.parse.unquote(req.params.get(key, "primary")))


def _tz(ctx: Instance, cal: dict[str, Any], req: Request) -> str:
    tz = req.arg("timeZone") or cal["timeZone"]
    try:
        ZoneInfo(tz)
    except Exception:
        raise error(400, f"Invalid time zone definition for time zone {tz!r}.", "invalid") from None
    return tz


def _time_arg(req: Request, name: str, tz: str) -> dt.datetime | None:
    v = req.arg(name)
    if not v:
        return None
    try:
        t = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise error(400, "Bad Request", "badRequest") from None
    if t.tzinfo is None:  # the REST API wants an offset
        raise error(400, "Bad Request", "badRequest")
    return t


# -- calendars ---------------------------------------------------------------------------------------

def _entry(ctx: Instance, cid: str, cal: dict[str, Any]) -> dict[str, Any]:
    me = ctx.actor
    role = _role(cal, me)
    color = "14" if cid == me else str(int(hashlib.sha1(cid.encode()).hexdigest(), 16) % 24 + 1)
    out: dict[str, Any] = {"kind": "calendar#calendarListEntry", "etag": _etag("cal", cid), "id": cid,
                           "summary": cid if cid == me else cal["summary"], "timeZone": cal["timeZone"],
                           "colorId": color, "backgroundColor": CALENDAR_COLORS[color], "foregroundColor": "#000000",
                           "selected": True, "accessRole": role,
                           "defaultReminders": [{"method": "popup", "minutes": 10}] if role in ("owner", "writer") else [],
                           "conferenceProperties": {"allowedConferenceSolutionTypes": ["hangoutsMeet"]}}
    if cid == me:
        out["notificationSettings"] = {"notifications": [{"type": t, "method": "email"} for t in
                                                         ("eventCreation", "eventChange", "eventCancellation",
                                                          "eventResponse")]}
        out["primary"] = True
    return out


def _visible_calendars(ctx: Instance) -> list[tuple[str, dict[str, Any]]]:
    me, s = ctx.actor, ctx.state
    return [(cid, c) for cid, c in s["calendars"].items()
            if not c.get("person") or cid == me or c["acl"].get(me)]


@op("calendar.calendarList.list", "GET", "/users/me/calendarList", read_only=True)
def calendar_list(ctx: Instance, req: Request) -> Any:
    items = [_entry(ctx, cid, c) for cid, c in _visible_calendars(ctx)]
    items.sort(key=lambda e: (not e.get("primary"), e["summary"].lower()))
    chunk, token = page(items, req, default=100, maximum=250)
    out: dict[str, Any] = {"kind": "calendar#calendarList", "etag": _etag("list", len(items))}
    out["nextPageToken" if token else "nextSyncToken"] = token or "CKi" + b64url(str(len(items)), pad=False)
    out["items"] = chunk
    return select(out, req.arg("fields"))


@op("calendar.calendarList.get", "GET", "/users/me/calendarList/{calendarId}", read_only=True)
def calendar_list_get(ctx: Instance, req: Request) -> Any:
    cid, cal = _calendar(ctx, req)
    if cid not in dict(_visible_calendars(ctx)):
        raise error(404, "Not Found", "notFound")
    return _entry(ctx, cid, cal)


@op("calendar.calendars.get", "GET", "/calendars/{calendarId}", read_only=True)
def calendars_get(ctx: Instance, req: Request) -> Any:
    cid, cal = _calendar(ctx, req)
    return {"kind": "calendar#calendar", "etag": _etag("c", cid), "id": cid,
            "summary": cid if cid == ctx.actor else cal["summary"], "timeZone": cal["timeZone"],
            "conferenceProperties": {"allowedConferenceSolutionTypes": ["hangoutsMeet"]}}


@op("calendar.colors.get", "GET", "/colors", read_only=True)
def colors_get(ctx: Instance, req: Request) -> Any:
    return {"kind": "calendar#colors", "updated": "2012-02-14T00:00:00.000Z",
            "calendar": {k: {"background": v, "foreground": "#1d1d1d"} for k, v in CALENDAR_COLORS.items()},
            "event": {k: {"background": v, "foreground": "#1d1d1d"} for k, v in COLORS.items()}}


def _settings(ctx: Instance) -> dict[str, str]:
    return {"timezone": ctx.state["calendars"].get(ctx.actor, {}).get("timeZone", ctx.state["timeZone"]),
            "locale": "en", "weekStart": "0", "format24HourTime": "false", "dateFieldOrder": "MDY",
            "defaultEventLength": "60", "hideWeekends": "false", "showDeclinedEvents": "true",
            "autoAddHangouts": "true", "useKeyboardShortcuts": "true", "hideInvitations": "false",
            "remindOnRespondedEventsOnly": "false"}


@op("calendar.settings.list", "GET", "/users/me/settings", read_only=True)
def settings_list(ctx: Instance, req: Request) -> Any:
    items = [{"kind": "calendar#setting", "etag": _etag("s", k, v), "id": k, "value": v}
             for k, v in _settings(ctx).items()]
    return {"kind": "calendar#settings", "etag": _etag("settings"), "nextSyncToken": "CPjb", "items": items}


@op("calendar.settings.get", "GET", "/users/me/settings/{setting}", read_only=True)
def settings_get(ctx: Instance, req: Request) -> Any:
    k = req.params["setting"]
    v = _settings(ctx).get(k)
    if v is None:
        raise error(404, "Not Found", "notFound")
    return {"kind": "calendar#setting", "etag": _etag("s", k, v), "id": k, "value": v}


# -- events: reading ---------------------------------------------------------------------------------

def _matches_q(ev: dict[str, Any], q: str) -> bool:
    text = " ".join([ev.get("summary", ""), ev.get("description") or "", ev.get("location") or "",
                     *[a["email"] for a in ev.get("attendees", [])], ev["organizer"]["email"]]).lower()
    return all(w in text for w in q.lower().split())


def _updated_ms(ev: dict[str, Any]) -> int:
    return int(dt.datetime.fromisoformat(ev["updated"].replace("Z", "+00:00")).timestamp() * 1000)


@op("calendar.events.list", "GET", "/calendars/{calendarId}/events", read_only=True)
def events_list(ctx: Instance, req: Request) -> Any:
    s = ctx.state
    cid, cal = _calendar(ctx, req)
    _readable(ctx, cal)
    tz = _tz(ctx, cal, req)
    single = req.bool_arg("singleEvents")
    order = req.arg("orderBy")
    if order == "startTime" and not single:
        raise error(400, "The requested ordering is not available for the particular query.", "badRequest")
    if order not in (None, "startTime", "updated"):
        raise error(400, f"Invalid value for: {order} is not a valid value", "invalidParameter", location="orderBy")
    sync = req.arg("syncToken")
    if sync and (req.arg("timeMin") or req.arg("timeMax") or req.arg("q")):
        raise error(400, "Sync token cannot be used together with timeMin, timeMax or q.", "invalid")
    lo, hi = _time_arg(req, "timeMin", tz), _time_arg(req, "timeMax", tz)
    if lo and hi and hi <= lo:
        raise error(400, "The specified time range is empty.", "timeRangeEmpty")
    show_deleted = req.bool_arg("showDeleted") or bool(sync)
    since = int(b64url_decode_int(sync)) if sync else None
    q, uid = req.arg("q"), req.arg("iCalUID")
    items: list[dict[str, Any]] = []
    for ev in s["events"].values():
        mine = ev["calendarId"] == cid or any(a["email"] == cid for a in ev.get("attendees", []))
        if not mine or (cid in ev.get("hiddenFor", []) and not show_deleted):
            continue
        if ev["status"] == "cancelled" and not show_deleted:
            continue
        if since is not None and _updated_ms(ev) <= since:
            continue
        if uid and ev.get("iCalUID") != uid:
            continue
        if single:
            insts = _occurrences(s, ev, lo, hi, tz) if ev["status"] != "cancelled" else [ev]
            items += [i for i in insts if _on_calendar(i, cid) or show_deleted]
        elif ev["status"] == "cancelled" or _occurrences(s, ev, lo, hi, tz):
            items.append(ev)
    if q:
        items = [e for e in items if _matches_q(e, q)]
    if order == "startTime":
        items.sort(key=lambda e: _parse(e["start"], tz))
    elif order == "updated":
        items.sort(key=lambda e: e["updated"])
    chunk, token = page(items, req, default=250, maximum=2500)
    newest = max([_updated_ms(e) for e in s["events"].values()] or [0])
    out: dict[str, Any] = {"kind": "calendar#events", "etag": _etag("events", cid, newest),
                           "summary": cid if cid == ctx.actor else cal["summary"], "description": "",
                           "updated": _rfc(dt.datetime.fromtimestamp(newest / 1000, dt.timezone.utc).isoformat()),
                           "timeZone": tz, "accessRole": _role(cal, ctx.actor),
                           "defaultReminders": [{"method": "popup", "minutes": 10}]
                           if _role(cal, ctx.actor) in ("owner", "writer") else []}
    if token:
        out["nextPageToken"] = token
    else:
        out["nextSyncToken"] = b64url_int(max(newest, ctx.now().timestamp() * 1000))
    out["items"] = [_resource(ctx, e) for e in chunk]
    return select(out, req.arg("fields"))


def b64url_int(n: float) -> str:
    return "CPD" + b64url(str(int(n)), pad=False)


def b64url_decode_int(token: str) -> int:
    try:
        return int(unb64url(token.removeprefix("CPD")).decode())
    except (ValueError, UnicodeDecodeError):
        raise error(410, "Sync token is no longer valid, a full sync is required.", "fullSyncRequired") from None


def _get_event(ctx: Instance, req: Request) -> tuple[str, dict[str, Any]]:
    cid, cal = _calendar(ctx, req)
    ev = _lookup(ctx.state, urllib.parse.unquote(req.params["eventId"]))
    if ev is None or not (ev["calendarId"] == cid or any(a["email"] == cid for a in ev.get("attendees", []))):
        raise error(404, "Not Found", "notFound")
    return cid, ev


@op("calendar.events.get", "GET", "/calendars/{calendarId}/events/{eventId}", read_only=True)
def events_get(ctx: Instance, req: Request) -> Any:
    cid, ev = _get_event(ctx, req)
    _readable(ctx, ctx.state["calendars"][cid])
    if cid in ev.get("hiddenFor", []):
        return _resource(ctx, {**ev, "status": "cancelled"})
    return select(_resource(ctx, ev), req.arg("fields"))


@op("calendar.events.instances", "GET", "/calendars/{calendarId}/events/{eventId}/instances", read_only=True)
def events_instances(ctx: Instance, req: Request) -> Any:
    cid, ev = _get_event(ctx, req)
    cal = ctx.state["calendars"][cid]
    tz = _tz(ctx, cal, req)
    if _recurrence(ev, tz) is None:
        raise error(400, "The requested event is not a recurring event.", "invalid")
    lo, hi = _time_arg(req, "timeMin", tz), _time_arg(req, "timeMax", tz)
    insts = _occurrences(ctx.state, ev, lo, hi, tz)
    chunk, token = page(insts, req, default=250, maximum=2500)
    out: dict[str, Any] = {"kind": "calendar#events", "etag": _etag("inst", ev["id"], ev["updated"]),
                           "summary": cid if cid == ctx.actor else cal["summary"], "timeZone": tz,
                           "accessRole": _role(cal, ctx.actor), "items": [_resource(ctx, i) for i in chunk]}
    if token:
        out["nextPageToken"] = token
    return out


# -- events: writing ---------------------------------------------------------------------------------

def _send_updates(req: Request) -> str:
    """The REST default is not to email anyone (the MCP server defaults to 'all')."""
    v = req.arg("sendUpdates") or ("all" if req.bool_arg("sendNotifications") else "none")
    if v not in ("all", "externalOnly", "none"):
        raise error(400, f"Invalid value for: {v} is not a valid value", "invalidParameter", location="sendUpdates")
    return v


def _check_times(b: dict[str, Any], need: bool) -> None:
    for k in ("start", "end"):
        if need and not b.get(k):
            raise error(400, f"Missing {k} time.", "required")
        w = b.get(k)
        if w is not None and not (isinstance(w, dict) and (w.get("dateTime") or w.get("date"))):
            raise error(400, f"Missing {k} time.", "required")


def _set_all_day(ev: dict[str, Any], b: dict[str, Any]) -> None:
    for k in ("start", "end"):
        if isinstance(b.get(k), dict) and b[k].get("date") and not b[k].get("dateTime"):
            ev[k] = {"date": b[k]["date"]}


def _attendees(b: dict[str, Any]) -> list[dict[str, Any]] | None:
    if "attendees" not in b:
        return None
    out = []
    for a in b.get("attendees") or []:
        email = (a.get("email") if isinstance(a, dict) else a) or ""
        if "@" not in email:
            raise error(400, "Invalid attendee email.", "invalid")
        out.append(a if isinstance(a, dict) else {"email": email})
    return out


def _conference(ctx: Instance, ev: dict[str, Any], b: dict[str, Any], req: Request) -> None:
    want = (b.get("conferenceData") or {}).get("createRequest")
    if not want or req.arg("conferenceDataVersion") not in ("1",):
        return
    code = "-".join(ctx.token(n, "abcdefghijklmnopqrstuvwxyz") for n in (3, 4, 3))
    uri = f"https://meet.google.com/{code}"
    ev["hangoutLink"] = uri
    ev["conferenceData"] = {
        "createRequest": {"requestId": want.get("requestId", ""),
                          "conferenceSolutionKey": want.get("conferenceSolutionKey") or {"type": "hangoutsMeet"},
                          "status": {"statusCode": "success"}},
        "entryPoints": [{"entryPointType": "video", "uri": uri, "label": uri.removeprefix("https://")},
                        {"entryPointType": "more", "uri": f"https://tel.meet/{code}?pin=1234567890123",
                         "pin": "1234567890123"}],
        "conferenceSolution": {"key": {"type": "hangoutsMeet"}, "name": "Google Meet",
                               "iconUri": "https://fonts.gstatic.com/s/i/productlogos/meet_2020q4/v6/web-512dp/"
                                          "logo_meet_2020q4_color_2x_web_512dp.png"},
        "conferenceId": code}


@op("calendar.events.insert", "POST", "/calendars/{calendarId}/events")
def events_insert(ctx: Instance, req: Request) -> Any:
    s = ctx.state
    cid, cal = _calendar(ctx, req)
    _writable(ctx, cal)
    b = req.json()
    _check_times(b, True)
    send = _send_updates(req)
    tz = (b["start"].get("timeZone") if isinstance(b.get("start"), dict) else None) or cal["timeZone"]
    if s.get("_recurring"):
        _recurrence({"recurrence": b.get("recurrence")}, tz)
    if b.get("colorId") and str(b["colorId"]) not in COLORS:
        raise error(400, "Invalid color id.", "invalid")
    if b.get("id"):
        if b["id"] in s["events"]:
            raise error(409, "The requested identifier already exists.", "duplicate")
        if not (5 <= len(b["id"]) <= 1024 and all(c in "abcdefghijklmnopqrstuv0123456789" for c in b["id"])):
            raise error(400, "Invalid resource id value.", "invalid")
    atts = _attendees(b) or []
    ev = _new_event(ctx, s, cid, {"summary": b.get("summary"), "start": b["start"], "end": b["end"],
                                  "description": b.get("description"), "timeZone": tz, "location": b.get("location"),
                                  "colorId": b.get("colorId"), "recurrence": b.get("recurrence"),
                                  "transparency": b.get("transparency", "opaque"),
                                  "attendees": [a["email"] for a in atts]}, actor=ctx.actor)
    if not b.get("summary"):
        ev.pop("summary", None)
    if b.get("id"):
        s["events"][b["id"]] = s["events"].pop(ev["id"])
        ev.update(id=b["id"], htmlLink=f"https://www.google.com/calendar/event?eid={b['id']}", iCalUID=f"{b['id']}@google.com")
    for a, given in zip([x for x in ev.get("attendees", []) if not x.get("organizer")], atts):
        for k in ("optional", "displayName", "comment", "additionalGuests"):
            if k in given:
                a[k] = given[k]
    _set_all_day(ev, b)
    for k in SETTABLE:
        if k in b and k not in ("summary", "description", "location", "colorId", "transparency"):
            ev[k] = b[k]
    _conference(ctx, ev, b, req)
    ev["_by_agent"] = True
    _schedule_responses(ctx, ev, [a["email"] for a in ev.get("attendees", [])])
    _mail_attendees(ctx, ev, "invite", send)
    return _resource(ctx, ev)


def _own_response_only(ctx: Instance, ev: dict[str, Any], b: dict[str, Any]) -> bool:
    """An attendee may change their own response (and nothing else) on someone else's event."""
    if set(b) - {"attendees", "reminders"}:
        return False
    theirs = {a["email"]: a for a in ev.get("attendees", [])}
    for a in b.get("attendees") or []:
        cur = theirs.get(a.get("email", "").lower())
        if cur is None:
            return False
        if a["email"].lower() != ctx.actor and a.get("responseStatus", cur["responseStatus"]) != cur["responseStatus"]:
            return False
    return True


def _update(ctx: Instance, req: Request, replace: bool) -> Any:
    s = ctx.state
    cid, ev = _get_event(ctx, req)
    b = req.json()
    send = _send_updates(req)
    organizer_cal = s["calendars"][ev["calendarId"]]
    if _role(organizer_cal, ctx.actor) not in ("owner", "writer"):
        if not _own_response_only(ctx, ev, b):
            raise error(403, "You need to have writer access to this calendar.", "requiredAccessLevel")
        mine = next((a for a in b.get("attendees") or [] if a.get("email", "").lower() == ctx.actor), None)
        if mine and mine.get("responseStatus"):
            att = next(a for a in ev["attendees"] if a["email"] == ctx.actor)
            if att["responseStatus"] != mine["responseStatus"]:
                att["responseStatus"] = mine["responseStatus"]
                _mail_organizer(ctx, ev, ctx.actor, mine["responseStatus"])
        _finish_update(ctx, ev)
        return _resource(ctx, ev)
    _writable(ctx, organizer_cal)
    if replace:
        _check_times(b, True)
    else:
        _check_times({k: b[k] for k in ("start", "end") if k in b}, False)
    tz = ev["start"].get("timeZone") or organizer_cal["timeZone"]
    if "recurrence" in b and s.get("_recurring"):
        _recurrence({"recurrence": b["recurrence"]}, tz)
    before = (ev["start"], ev["end"], ev.get("summary"), ev.get("location"))
    had = {a["email"] for a in ev.get("attendees", [])}
    new_start = _parse(b["start"], tz) if b.get("start") else _parse(ev["start"], tz)
    new_end = _parse(b["end"], tz) if b.get("end") else _parse(ev["end"], tz)
    if new_end <= new_start:
        raise error(400, "The specified time range is empty.", "timeRangeEmpty")
    if b.get("start") or b.get("end"):
        stz = (b.get("start") or {}).get("timeZone") or tz
        ev["start"], ev["end"] = _when(new_start, stz), _when(new_end, stz)
        _set_all_day(ev, b)
    if replace:  # PUT replaces the whole resource: what isn't sent is cleared
        for k in SETTABLE:
            if k not in b and k != "summary":
                ev.pop(k, None)
        if "recurrence" not in b and not _is_instance(ev):
            ev.pop("recurrence", None)
    for k in SETTABLE:
        if k in b:
            if b[k] is None:
                ev.pop(k, None)
            else:
                ev[k] = b[k]
    if "recurrence" in b and not _is_instance(ev):
        ev["recurrence"] = b["recurrence"]
    atts = _attendees(b)
    if atts is not None or replace:
        existing = {a["email"]: a for a in ev.get("attendees", [])}
        new_list = []
        for a in atts or []:
            email = a["email"].lower()
            cur = dict(existing.get(email, {"email": email, "responseStatus": "needsAction"}))
            for k in ("optional", "displayName", "comment"):
                if k in a:
                    cur[k] = a[k]
            if email == ctx.actor and a.get("responseStatus"):
                cur["responseStatus"] = a["responseStatus"]
            new_list.append(cur)
        org = existing.get(ev["organizer"]["email"])
        if org and org["email"] not in {a["email"] for a in new_list} and new_list:
            new_list.insert(0, org)
        ev["attendees"] = new_list
        if not new_list:
            ev.pop("attendees", None)
    _conference(ctx, ev, b, req)
    added = {a["email"] for a in ev.get("attendees", [])} - had
    if (ev["start"], ev["end"], ev.get("summary"), ev.get("location")) != before:
        _mail_attendees(ctx, ev, "update", send, only=had)
    _mail_attendees(ctx, ev, "invite", send, only=added)
    _schedule_responses(ctx, ev, sorted(added))
    ev["sequence"] = ev.get("sequence", 0) + (0 if _is_instance(ev) else 1)
    _finish_update(ctx, ev)
    return _resource(ctx, ev)


def _finish_update(ctx: Instance, ev: dict[str, Any]) -> None:
    if _is_instance(ev):
        _save_instance(ctx, ev)
    else:
        ev["updated"] = ctx.now().isoformat()


@op("calendar.events.patch", "PATCH", "/calendars/{calendarId}/events/{eventId}")
def events_patch(ctx: Instance, req: Request) -> Any:
    return _update(ctx, req, replace=False)


@op("calendar.events.update", "PUT", "/calendars/{calendarId}/events/{eventId}")
def events_update(ctx: Instance, req: Request) -> Any:
    return _update(ctx, req, replace=True)


@op("calendar.events.delete", "DELETE", "/calendars/{calendarId}/events/{eventId}", destructive=True)
def events_delete(ctx: Instance, req: Request) -> Any:
    s = ctx.state
    cid, cal = _calendar(ctx, req)
    ev = _lookup(s, urllib.parse.unquote(req.params["eventId"]))
    if ev is None or not (ev["calendarId"] == cid or any(a["email"] == cid for a in ev.get("attendees", []))):
        raise error(404, "Not Found", "notFound")
    if ev["status"] == "cancelled" or cid in ev.get("hiddenFor", []):
        raise error(410, "Resource has been deleted", "deleted")
    send = _send_updates(req)
    if ev["calendarId"] != cid:  # an attendee removing it from their calendar declines it
        for a in ev.get("attendees", []):
            if a["email"] == cid and a["responseStatus"] != "declined":
                a["responseStatus"] = "declined"
                _mail_organizer(ctx, ev, cid, "declined")
        ev["hiddenFor"] = [*ev.get("hiddenFor", []), cid]
    else:
        _writable(ctx, cal)
        ev["status"] = "cancelled"
        _mail_attendees(ctx, ev, "cancel", send)
    _finish_update(ctx, ev)
    return Response(204)


@op("calendar.events.move", "POST", "/calendars/{calendarId}/events/{eventId}/move")
def events_move(ctx: Instance, req: Request) -> Any:
    s = ctx.state
    cid, ev = _get_event(ctx, req)
    _writable(ctx, s["calendars"][ev["calendarId"]])
    dest, dcal = _cal(ctx, req.arg("destination") or "")
    _writable(ctx, dcal)
    if _is_instance(ev):
        raise error(400, "Cannot change the organizer of an instance.", "cannotChangeOrganizerOfInstance")
    ev["calendarId"] = dest
    ev["organizer"] = {"email": dest}
    ev["updated"] = ctx.now().isoformat()
    return _resource(ctx, ev)


@op("calendar.freebusy.query", "POST", "/freeBusy", read_only=True)
def freebusy(ctx: Instance, req: Request) -> Any:
    s = ctx.state
    b = req.json()
    if not b.get("timeMin") or not b.get("timeMax"):
        raise error(400, "Missing timeMin or timeMax.", "required")
    tz = b.get("timeZone") or "UTC"
    lo, hi = _parse(b["timeMin"], s["timeZone"]), _parse(b["timeMax"], s["timeZone"])
    if hi <= lo:
        raise error(400, "The specified time range is empty.", "timeRangeEmpty")
    if hi - lo > dt.timedelta(days=62):
        raise error(400, "The requested time range is too long.", "timeRangeTooLong")
    cals: dict[str, Any] = {}
    for item in b.get("items") or []:
        raw = item.get("id", "")
        cid = ctx.actor if raw == "primary" else raw.lower()
        if cid not in s["calendars"]:
            cals[raw] = {"errors": [{"domain": "global", "reason": "notFound"}], "busy": []}
            continue
        owner = cid if s["calendars"][cid].get("person") else None
        blocks = [(_parse(e["start"], s["timeZone"]), _parse(e["end"], s["timeZone"])) for m in s["events"].values()
                  for e in _occurrences(s, m, lo, hi, s["timeZone"])
                  if _on_calendar(e, cid) and e.get("transparency") != "transparent"
                  and not any(a["email"] == (owner or e["organizer"]["email"]) and a["responseStatus"] == "declined"
                              for a in e.get("attendees", []))]
        blocks += [(_parse(x["start"], s["timeZone"]), _parse(x["end"], s["timeZone"])) for x in s["busy"].get(cid, [])]
        blocks = sorted((max(a, lo), min(z, hi)) for a, z in blocks if z > lo and a < hi)
        merged: list[list[dt.datetime]] = []
        for a, z in blocks:
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], z)
            else:
                merged.append([a, z])
        fmt = (lambda t: t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")) if tz == "UTC" else \
            (lambda t: t.astimezone(ZoneInfo(tz)).isoformat())
        cals[raw] = {"busy": [{"start": fmt(a), "end": fmt(z)} for a, z in merged]}
    fmt0 = lambda t: t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")  # noqa: E731
    return {"kind": "calendar#freeBusy", "timeMin": fmt0(lo), "timeMax": fmt0(hi), "calendars": cals}
