"""calendar: world actions (triggered by environments, never by agents)."""

from __future__ import annotations

from typing import Any

from ...core.instance import Instance
from ...core.tools import action
from .model import _err, _is_instance, _mail_attendees, _mail_organizer, _new_event, _parse
from .recurrence import _busy_spans, _lookup, _occurrences, _save_instance


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
    ev = _new_event(ctx, ctx.state, cid, {"summary": summary, "start": start, "end": end,
                                          "attendees": attendees or []}, actor=cid)
    _mail_attendees(ctx, ev, "invite", "all", by=cid)
    return ev["id"]


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
    _mail_organizer(ctx, ev, a["email"], response)
    ev["updated"] = ctx.now().isoformat()


@action("cancel_event")
def act_cancel_event(ctx: Instance, summary: str) -> None:
    """The organizer cancels an event."""
    ev = _find_event(ctx, summary)
    ev["status"] = "cancelled"
    ev["updated"] = ctx.now().isoformat()
    _mail_attendees(ctx, ev, "cancel", "all", by=ev["organizer"]["email"])


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
    _mail_organizer(ctx, ev, email, mine["responseStatus"])
    if _is_instance(ev):
        _save_instance(ctx, ev)
    else:
        ev["updated"] = ctx.now().isoformat()
