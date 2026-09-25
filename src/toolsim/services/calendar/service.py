"""The calendar service: identity, versions, seed format, state, errors and the version probe."""

from __future__ import annotations

import datetime as dt
from typing import Any

from ...core.instance import Instance, Service
from .model import GRADING_DAYS, POLICIES, V1, V2, _delay, _new_event, _on_calendar, _parse
from .recurrence import _occurrences


class Calendar(Service):
    name = "calendar"
    title = "Google Calendar"
    description = "Simulated Google Calendar. Behaves like the Google Calendar MCP server; nothing is really scheduled."

    versions = {"2026-09-25": "Initial release: 11 tools modeled on nspady/google-calendar-mcp.",
                V1: "Recurring events expand into instances (RRULE/EXDATE), modificationScope on updates, "
                    "per-instance deletes/responses, and colleagues who answer invites on their own.",
                V2: "A company calendar system: colleagues known by free/busy or as attendees are full calendar "
                    "users (agents can act as them), and invitations, updates and cancellations are emailed "
                    "to attendees when Gmail is in the same environment."}

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
        if ctx.at_least(V2):
            c("list-events", {"timeMin": "2026-09-21T00:00:00", "timeMax": "2026-09-26T00:00:00"}, as_="priya@acme.com")
            r = c("create-event", {"summary": "Pairing", "start": "2026-09-24T15:00:00", "end": "2026-09-24T16:00:00",
                                   "attendees": [{"email": "alex@acme.com"}]}, as_="john@acme.com")
            c("respond-to-event", {"eventId": r.data["event"]["id"], "response": "accepted"})

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
        domains = {state["default"].split("@")[1], *seed.get("domains", [])}

        def add_person(email: str, name: str | None = None) -> None:
            state["people"][email] = {"email": email, "name": name or email.split("@")[0].replace(".", " ").title()}
            state["calendars"][email] = {"id": email, "summary": state["people"][email]["name"], "timeZone": tz,
                                         "acl": {email: "owner"}, "domainRole": "freeBusyReader", "person": True}

        for c in seed.get("calendars", []):
            cid = c["id"].lower()
            if ctx.at_least(V2) and cid not in state["calendars"] and "@" in cid and not cid.startswith("team") \
                    and cid.split("@")[1] in domains:
                add_person(cid, c.get("summary"))  # a colleague: a full calendar user, whose busy blocks stay
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
        if ctx.at_least(V2):  # colleagues invited to seeded events are calendar users too (seeding sends no mail)
            state["_v2"] = True
            for ev in list(state["events"].values()):
                for a in ev.get("attendees", []):
                    if a["email"] not in state["people"] and a["email"].split("@")[1] in domains:
                        add_person(a["email"])
        return state

    def default_actor(self, state: dict[str, Any]) -> str:
        return state["default"]

    def grading_view(self, state: dict[str, Any]) -> dict[str, Any]:
        """Events annotated with ``booked_by_agent`` and ``conflicts_for``: the people for whom the
        event (any instance of it, for a recurring series) overlaps something else they're busy
        with (accepted events or free/busy blocks)."""
        tz = state["timeZone"]
        expanded: dict[str, list[tuple[dt.datetime, dt.datetime, dict[str, Any]]]] = {}
        busy: dict[str, list[tuple[dt.datetime, dt.datetime, str | None]]] = {}
        for e in state["events"].values():  # expand each event once (a series up to GRADING_DAYS ahead)
            if e["status"] == "cancelled":
                continue
            start = _parse(e["start"], tz)
            spans = [(_parse(i["start"], tz), _parse(i["end"], tz), i)
                     for i in _occurrences(state, e, None, start + dt.timedelta(days=GRADING_DAYS), tz)]
            expanded[e["id"]] = spans
            if e.get("transparency") == "transparent":
                continue
            for a, b, inst in spans:
                for p in {inst["calendarId"], *[x["email"] for x in inst.get("attendees", [])]}:
                    if _on_calendar(inst, p) and not any(x["email"] == p and x["responseStatus"] == "declined"
                                                         for x in inst.get("attendees", [])):
                        busy.setdefault(p, []).append((a, b, e["id"]))
        for p, blocks in state["busy"].items():
            busy.setdefault(p, []).extend((_parse(x["start"], tz), _parse(x["end"], tz), None) for x in blocks)
        events = []
        for e in state["events"].values():
            conflicts: set[str] = set()
            for s0, en, inst in expanded.get(e["id"], []):
                people = {inst["calendarId"], *[a["email"] for a in inst.get("attendees", [])
                                                if a["responseStatus"] != "declined"]}
                conflicts |= {p for p in people if _on_calendar(inst, p)
                              and any(a < en and s0 < b and eid != e["id"] for a, b, eid in busy.get(p, []))}
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
