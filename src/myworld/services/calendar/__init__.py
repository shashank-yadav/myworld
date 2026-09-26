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

from .actions import act_add_busy, act_add_event, act_auto_respond, act_cancel_event, act_respond
from .service import Calendar
from .tools import (
                  create_event,
                  delete_event,
                  get_current_time,
                  get_event,
                  get_freebusy,
                  list_calendars,
                  list_colors,
                  list_events,
                  respond_to_event,
                  search_events,
                  update_event,
                  update_event_v1,
)

Calendar.tools = [list_calendars, list_events, get_event, search_events, create_event, update_event, update_event_v1,
                  delete_event,
                  respond_to_event, get_freebusy, get_current_time, list_colors]
Calendar.actions = [act_add_event, act_add_busy, act_respond, act_cancel_event, act_auto_respond]

__all__ = ["Calendar"]
