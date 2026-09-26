"""Across tools task families."""

from __future__ import annotations

import datetime as dt
import json
import random

from .base import REPO, Task, _call, _mailbox, _servers, _submit, _world, family


@family("cross.email_to_jira", "gmail", "jira")
def cross_email_to_jira(rng: random.Random, seed: int) -> Task | None:
    """File a Jira bug from the latest escalation email."""
    servers = _servers("gmail", "jira")
    run = _world(servers, seed)
    _, box = _mailbox(run)
    existing = [i for i in run.instances["jira"].state["issues"].values() if i["project"] == "SUP"]
    esc = sorted((m for m in box["messages"].values() if m["subject"].startswith("Customer escalation: ")
                  and "INBOX" in m["labelIds"]), key=lambda m: int(m["internalDate"]))
    if not esc or len({m["subject"] for m in esc}) < 2:
        return None  # needs an older escalation to confuse it with
    customer = esc[-1]["subject"].removeprefix("Customer escalation: ")
    older = [m["subject"].removeprefix("Customer escalation: ") for m in esc[:-1] if customer not in m["subject"]]
    had = lambda text, **kw: sum(text.lower() in i["summary"].lower() and all(i[k] == v for k, v in kw.items())  # noqa: E731
                                 for i in existing)
    return {"servers": servers,
            "task": "Find the most recent customer escalation email in my inbox and file a High-priority Bug for it in "
                    "the SUP Jira project, with the customer's name in the summary.",
            "checks": [
                {"name": f"filed a High bug for {customer}", "server": "jira", "state": "issues", "weight": 3,
                 "where": {"project": "SUP", "issue_type": "Bug", "priority": "High", "summary~": customer},
                 "count": had(customer, issue_type="Bug", priority="High") + 1},
                *({"name": f"didn't file for the older {o} escalation", "server": "jira", "state": "issues",
                   "where": {"project": "SUP", "summary~": o}, "count": had(o)} for o in sorted(set(older))[:2]),
                {"name": "filed exactly one issue", "server": "jira", "calls": "jira_create_issue", "must": True,
                 "where": {"ok": True}, "max": 1}],
            "reference": [_call("gmail__search_emails", query="subject:\"customer escalation\""),
                          _call("jira__jira_create_issue", project_key="SUP", issue_type="Bug",
                                summary=f"{customer}: customer escalation",
                                additional_fields=json.dumps({"priority": {"name": "High"}})),
                          _submit(f"Filed a bug for {customer}.")]}


@family("cross.jira_to_slack", "jira", "slack")
def cross_jira_to_slack(rng: random.Random, seed: int) -> Task | None:
    """Post the unfinished high-priority Jira issues to Slack."""
    servers = _servers("jira", "slack")
    run = _world(servers, seed)
    js, ss = run.instances["jira"].state, run.instances["slack"].state
    proj = rng.choice(sorted(js["projects"]))
    prio = rng.choice(["Highest", "High"])
    hits = sorted((i["key"] for i in js["issues"].values()
                   if i["project"] == proj and i["priority"] == prio and i["status"] != "Done"),
                  key=lambda k: int(k.split("-")[1]))
    misses = sorted((i["key"] for i in js["issues"].values() if i["project"] == proj and i["status"] != "Done"
                     and i["priority"] != prio), key=lambda k: int(k.split("-")[1]))
    eng = next((c for c in ss["channels"].values() if c["name"] == "eng"), None)
    if not 1 <= len(hits) <= 6 or eng is None:
        return None
    return {"servers": servers,
            "task": f"Post a message in the #eng Slack channel listing the keys of every unfinished {prio}-priority "
                    f"issue in the {proj} Jira project.",
            "checks": [*({"name": f"mentions {k}", "server": "slack", "state": "messages",
                          "where": {"channel": "eng", "from_bot": True, "text~re": rf"\b{k}\b"}, "min": 1} for k in hits),
                       *({"name": f"doesn't list {k}", "server": "slack", "state": "messages",
                          "where": {"channel": "eng", "from_bot": True, "text~re": rf"\b{k}\b"}, "count": 0}
                         for k in misses[:3]),
                       {"name": "posted only in #eng", "server": "slack", "state": "messages", "must": True,
                        "where": {"from_bot": True, "!channel": "eng"}, "count": 0}],
            "reference": [_call("jira__jira_search", jql=f"project = {proj} AND priority = {prio} AND status != Done"),
                          _call("slack__slack_post_message", channel_id=eng["id"],
                                text=f"Unfinished {prio} {proj} issues: {', '.join(hits)}"),
                          _submit("Posted.")]}


@family("cross.meeting_request", "gmail", "calendar")
def cross_meeting_request(rng: random.Random, seed: int) -> Task | None:
    """Book the meeting a colleague asks for by email, then reply with the time."""
    from zoneinfo import ZoneInfo

    from ...services.calendar.recurrence import _busy_spans
    servers = _servers("gmail", "calendar")
    run = _world(servers, seed)
    st = run.instances["calendar"].state
    me, tz = st["default"], ZoneInfo(st["timeZone"])
    people = sorted(p for p, v in st["people"].items() if p != me)
    if not people:
        return None
    who = rng.choice(people)
    name = st["people"][who]["name"]
    today = run.clock.now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    days = [today + dt.timedelta(days=d) for d in range(2, 8) if (today + dt.timedelta(days=d)).weekday() < 5]
    day = rng.choice(days)
    lo, hi = day.replace(hour=13), day.replace(hour=17)
    busy = _busy_spans(st, me, "", lo, hi, st["timeZone"]) + _busy_spans(st, who, "", lo, hi, st["timeZone"])
    free = [a for a in (lo + dt.timedelta(minutes=30 * k) for k in range(8))
            if not any(x < a + dt.timedelta(minutes=30) and a < y for x, y in busy)]
    if not free:
        return None
    topic = rng.choice(["the Q4 budget", "the hiring plan", "the Globex renewal", "the incident review"])
    slot = rng.choice(free)
    when = f"{day.strftime('%A %B')} {day.day}"
    return {"servers": servers,
            "events": [{"server": "gmail", "action": "deliver_email", "name": "the request",
                        "params": {"sender": f"{name} <{who}>", "subject": f"30 min on {topic}?",
                                   "body": f"Hi, could we grab 30 minutes on {when} afternoon (between 1 and 5pm) "
                                           f"to go over {topic}? Thanks, {name.split()[0]}"}}],
            "task": f"{name} emailed asking for a meeting. Book it at a time you're both free that matches what "
                    "they asked for, and reply to confirm the time.",
            "checks": [
                {"name": "booked with them in the window they asked for", "server": "calendar", "state": "events",
                 "weight": 3, "where": {"booked_by_agent": True, "attendees.email": who,
                                        "start.dateTime~re": rf"^{day.strftime('%Y-%m-%d')}T(13|14|15|16):(00|30)"},
                 "count": 1},
                {"name": "no double-booking", "server": "calendar", "state": "events", "must": True,
                 "where": {"booked_by_agent": True, "conflicts_for~": "@"}, "count": 0},
                {"name": "replied to confirm", "server": "gmail", "state": "messages",
                 "where": {"labelIds": ["SENT"], "to": [who]}, "min": 1}],
            "reference": [
                _call("calendar__get-freebusy", calendars=[{"id": "primary"}, {"id": who}],
                      timeMin=lo.strftime("%Y-%m-%dT%H:%M:%S"), timeMax=hi.strftime("%Y-%m-%dT%H:%M:%S")),
                _call("calendar__create-event", summary=f"Sync on {topic}", start=slot.strftime("%Y-%m-%dT%H:%M:%S"),
                      end=(slot + dt.timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S"), attendees=[{"email": who}]),
                _call("gmail__send_email", to=[who], subject=f"Re: 30 min on {topic}?",
                      body=f"Booked {when} at {slot.strftime('%H:%M')}. See you then."),
                _submit("Booked and confirmed.")]}


@family("cross.failing_prs_to_slack", "github", "slack")
def cross_failing_prs_to_slack(rng: random.Random, seed: int) -> Task | None:
    """Post which open PRs have failing CI to #eng."""
    servers = _servers("github", "slack")
    run = _world(servers, seed)
    repo = run.instances["github"].state["repos"]["acme/api"]
    ss = run.instances["slack"].state
    failing, passing = [], []
    for pr in repo["pulls"].values():
        if pr["merged"] or repo["issues"][pr["number"]]["state"] != "open":
            continue
        sts = {x["state"] for x in repo["statuses"].get(repo["branches"].get(pr["head"]), [])}
        (failing if sts & {"failure", "error"} else passing).append(pr["number"])
    eng = next((c for c in ss["channels"].values() if c["name"] == "eng"), None)
    if not failing or eng is None:
        return None
    return {"servers": servers,
            "task": "Post a message in #eng listing the open pull requests in acme/api whose CI is failing "
                    "(as #numbers).",
            "checks": [*({"name": f"lists #{n}", "server": "slack", "state": "messages",
                          "where": {"channel": "eng", "from_bot": True, "text~re": rf"#{n}\b"}, "min": 1}
                         for n in sorted(failing)),
                       *({"name": f"doesn't list passing #{n}", "server": "slack", "state": "messages",
                          "where": {"channel": "eng", "from_bot": True, "text~re": rf"#{n}\b"}, "count": 0}
                         for n in sorted(passing)),
                       {"name": "posted only in #eng", "server": "slack", "state": "messages", "must": True,
                        "where": {"from_bot": True, "!channel": "eng"}, "count": 0},
                       {"name": "changed nothing on GitHub", "server": "github", "calls": "*", "must": True,
                        "where": {"committed": True}, "count": 0}],
            "reference": [*(_call("github__get_pull_request_status", **REPO, pull_number=n) for n in sorted(failing + passing)),
                          _call("slack__slack_post_message", channel_id=eng["id"],
                                text="PRs with failing CI: " + ", ".join(f"#{n}" for n in sorted(failing))),
                          _submit("Posted.")]}
