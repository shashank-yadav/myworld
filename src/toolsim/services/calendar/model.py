"""calendar: state model, constants and helpers shared by the tools."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any
from zoneinfo import ZoneInfo

from ...core.instance import Instance
from ...core.tools import ToolError

V1 = "2026-09-25.1"
V2 = "2026-09-25.2"
MAX_INSTANCES = 730  # a recurring series is expanded at most this far
GRADING_DAYS = 180  # graders look for conflicts this far into a recurring series
POLICIES = ("accept", "decline", "tentative", "if_free")

COLORS = {str(i): c for i, c in enumerate(
    ["#a4bdfc", "#7ae7bf", "#dbadff", "#ff887c", "#fbd75b", "#ffb878", "#46d6db", "#e1e1e1", "#5484ed", "#51b749",
     "#dc2127"], start=1)}


def _err(code: int, message: str, reason: str = "") -> ToolError:
    return ToolError({"error": {"code": code, "message": message, "errors": [{"reason": reason or "invalid"}]}},
                     status=code)

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


def _is_instance(ev: dict[str, Any]) -> bool:
    return "recurringEventId" in ev and "_" in ev["id"]


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


# -- email notifications (2026-09-25.2) -------------------------------------------------------

def _when_text(ev: dict[str, Any], tz: str) -> str:
    """Google's style: 'Tue Sep 22, 2026 10am - 10:30am (PDT)'."""
    zone = ZoneInfo(tz)
    s, e = _parse(ev["start"], tz).astimezone(zone), _parse(ev["end"], tz).astimezone(zone)

    def hm(t: dt.datetime) -> str:
        h = t.hour % 12 or 12
        return f"{h}{'' if t.minute == 0 else f':{t.minute:02d}'}{'am' if t.hour < 12 else 'pm'}"
    return f"{s.strftime('%a %b')} {s.day}, {s.year} {hm(s)} - {hm(e)} ({s.tzname()})"


def _person(state: dict[str, Any], email: str) -> str:
    name = (state["people"].get(email) or {}).get("name")
    return f"{name} <{email}>" if name else email


def _mail_attendees(ctx: Instance, ev: dict[str, Any], kind: str, send_updates: str | None,
                    only: set[str] | None = None) -> None:
    """Invitation / update / cancellation emails to the attendees, as Google Calendar sends them."""
    s = ctx.state
    if not s.get("_v2") or send_updates == "none" or ev.get("status") == "cancelled" and kind != "cancel":
        return
    domains = {s["default"].split("@")[1]}
    organizer = ev["organizer"]["email"]
    when = _when_text(ev, s["timeZone"])
    title = {"invite": "Invitation", "update": "Updated invitation", "cancel": "Canceled event"}[kind]
    for a in ev.get("attendees", []):
        to = a["email"]
        if to in (organizer, ctx.actor) or (only is not None and to not in only):
            continue
        if send_updates == "externalOnly" and to.split("@")[1] in domains:
            continue
        body = {"invite": f"{_person(s, organizer).split(' <')[0]} has invited you to this event.",
                "update": "This event has been changed.",
                "cancel": "This event has been canceled and removed from your calendar."}[kind]
        ctx.notify(to, _person(s, organizer), f"{title}: {ev.get('summary', '(No title)')} @ {when} ({to})",
                   f"{body}\n\n{ev.get('summary', '(No title)')}\nWhen: {when}\n"
                   + (f"Where: {ev['location']}\n" if ev.get("location") else "")
                   + "Who: " + ", ".join(x["email"] for x in ev.get("attendees", []))
                   + (f"\n\nReply for {to}: Yes / No / Maybe" if kind != "cancel" else "")
                   + f"\nView event: {ev.get('htmlLink', '')}")


def _mail_organizer(ctx: Instance, ev: dict[str, Any], attendee: str, response: str) -> None:
    """The organizer hears back when someone answers (Accepted: / Declined: / Tentatively Accepted:)."""
    s = ctx.state
    organizer = ev["organizer"]["email"]
    if not s.get("_v2") or organizer == attendee or response == "needsAction":
        return
    verb = {"accepted": "Accepted", "declined": "Declined", "tentative": "Tentatively Accepted"}[response]
    who = _person(s, attendee)
    ctx.notify(organizer, who, f"{verb}: {ev.get('summary', '(No title)')} @ {_when_text(ev, s['timeZone'])}",
               f"{who.split(' <')[0]} has {verb.lower()} this invitation.")
