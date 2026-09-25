"""calendar: recurring events (RRULE/EXDATE expansion, instances, exceptions)."""

from __future__ import annotations

import copy
import datetime as dt
import re
from typing import Any
from zoneinfo import ZoneInfo

from ...core.instance import Instance
from ...core.tools import ToolError
from .model import MAX_INSTANCES, _cal, _err, _in_range, _on_calendar, _parse, _when

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
