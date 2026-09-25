"""Generated tasks with verifiers and reference solutions, for RL at scale.

    from toolsim.tasks import generate
    specs = generate(n=500, seed=0)          # environment specs, ready for ToolEnv(spec)

Each task comes from a *family* (reply to a colleague, label every issue about X, book a slot
you're both free for, file a bug from the latest escalation email, ...). A family builds the
world for a seed (realistic volume, distractors), looks at what's actually in it, and writes:

- the task, in plain words;
- checks that verify the outcome in the resulting world, with ``must`` constraints for the
  collateral damage a sloppy agent causes (archiving the wrong mail, overwriting labels, sharing
  the old copy);
- a reference solution: the tool calls that solve it.

Every task is validated before it's kept: the reference solution must earn reward 1.0 and doing
nothing must earn less, so every task is solvable and none is free. ``toolsim tasks`` writes them
as JSONL; ``hard=True`` adds flaky APIs (the reference solution retries, like a careful agent).
"""

from __future__ import annotations

import json
import random
import re
from email.utils import parseaddr
from typing import Any, Callable

from .env import Environment, EnvRun

NOISE = {"gmail": {"emails": 150}, "slack": {"messages": 200}, "calendar": {"density": 0.45},
         "github": {"issues": 30, "pulls": 4}, "jira": {"issues": 40}, "drive": {"files": 30}}
REPO = {"owner": "acme", "repo": "api"}

Task = dict[str, Any]
FAMILIES: dict[str, tuple[Callable[[random.Random, int], Task | None], tuple[str, ...]]] = {}


def family(name: str, *servers: str) -> Callable[[Callable[..., Task | None]], Callable[..., Task | None]]:
    def wrap(fn: Callable[..., Task | None]) -> Callable[..., Task | None]:
        FAMILIES[name] = (fn, servers)
        return fn
    return wrap


def _servers(*names: str) -> dict[str, Any]:
    return {n: {"noise": dict(NOISE[n])} for n in names}


def _world(servers: dict[str, Any], seed: int) -> EnvRun:
    return EnvRun(Environment.from_dict({"name": "gen", "rng_seed": seed, "servers": servers}))


def _call(tool: str, **arguments: Any) -> dict[str, Any]:
    return {"tool": tool, "arguments": arguments}


def _submit(answer: str) -> dict[str, Any]:
    return _call("submit", answer=answer)


# -- gmail ----------------------------------------------------------------------------------

REPLIES = [("you'll have it done by Friday", "friday"), ("you approve the plan", "approve"),
           ("you can join the call on Thursday", "thursday"), ("you need two more days", "two more days")]


def _mailbox(run: EnvRun) -> tuple[str, dict[str, Any]]:
    g = run.instances["gmail"].state
    return g["default"], g["mailboxes"][g["default"]]


@family("gmail.reply", "gmail")
def gmail_reply(rng: random.Random, seed: int) -> Task | None:
    """Reply to a colleague's email with a specific message."""
    servers = _servers("gmail")
    me, box = _mailbox(_world(servers, seed))
    domain = me.split("@")[1]
    cands = [m for m in box["messages"].values() if "INBOX" in m["labelIds"] and not m["subject"].startswith("Re:")
             and parseaddr(m["from"])[1].endswith("@" + domain) and parseaddr(m["from"])[1] != me]
    if not cands:
        return None
    m = rng.choice(sorted(cands, key=lambda x: x["id"]))
    name, addr = parseaddr(m["from"])
    what, key = rng.choice(REPLIES)
    return {"servers": servers, "task": f"Reply to {name}'s email \"{m['subject']}\" and let them know {what}.",
            "checks": [
                {"name": "replied to the right person about it", "server": "gmail", "state": "messages", "weight": 3,
                 "where": {"labelIds": ["SENT"], "to": [addr], "subject~": m["subject"], "body~": key}, "count": 1},
                {"name": "sent it only once", "server": "gmail", "calls": "send_email", "max": 1},
                {"name": "didn't send the reply to anyone else", "server": "gmail", "state": "messages", "must": True,
                 "where": {"labelIds": ["SENT"], "body~": key, "!to": [addr]}, "count": 0}],
            "reference": [_call("gmail__send_email", to=[addr], subject="Re: " + m["subject"],
                                body=f"Hi {name.split()[0]}, just to let you know {what}.", threadId=m["threadId"]),
                          _submit(f"Replied to {name}.")]}


@family("gmail.archive_sender", "gmail")
def gmail_archive_sender(rng: random.Random, seed: int) -> Task | None:
    """Archive everything from one sender, and nothing else."""
    servers = _servers("gmail")
    me, box = _mailbox(_world(servers, seed))
    inbox = [m for m in box["messages"].values() if "INBOX" in m["labelIds"]]
    by_sender: dict[str, list[dict[str, Any]]] = {}
    for m in inbox:
        by_sender.setdefault(parseaddr(m["from"])[1], []).append(m)
    senders = sorted(a for a, ms in by_sender.items() if 2 <= len(ms) <= 12 and not a.endswith(me.split("@")[1]))
    if not senders:
        return None
    addr = rng.choice(senders)
    name = parseaddr(by_sender[addr][0]["from"])[0] or addr
    others = len(inbox) - len(by_sender[addr])
    total = sum(parseaddr(m["from"])[1] == addr for m in box["messages"].values())
    trashed = sum("TRASH" in m["labelIds"] for m in box["messages"].values())
    return {"servers": servers,
            "task": f"Archive every email from {name} ({addr}) that's in my inbox. Don't touch anything else.",
            "checks": [
                {"name": f"nothing from {addr} left in the inbox", "server": "gmail", "state": "messages", "weight": 3,
                 "where": {"labelIds": ["INBOX"], "from~": addr}, "count": 0},
                {"name": "archived, not deleted", "server": "gmail", "state": "messages", "must": True,
                 "where": {"labelIds": ["TRASH"]}, "count": trashed},
                {"name": f"every email from {addr} still exists", "server": "gmail", "state": "messages", "must": True,
                 "where": {"from~": addr}, "count": total},
                {"name": "every other email still in the inbox", "server": "gmail", "state": "messages", "must": True,
                 "where": {"labelIds": ["INBOX"], "!from~": addr}, "count": others}],
            "reference": [*(_call("gmail__modify_email", messageId=m["id"], removeLabelIds=["INBOX"])
                            for m in sorted(by_sender[addr], key=lambda x: x["id"])),
                          _submit(f"Archived {len(by_sender[addr])} emails from {addr}.")]}


@family("gmail.receipt_amount", "gmail")
def gmail_receipt_amount(rng: random.Random, seed: int) -> Task | None:
    """Find the amount on a specific receipt (answer)."""
    servers = _servers("gmail")
    _, box = _mailbox(_world(servers, seed))
    found = []
    for m in box["messages"].values():
        subj = re.match(r"Your receipt from (.+) #([\d-]+)$", m["subject"])
        amount = re.search(r"Amount paid: \$([\d,]+\.\d\d)", m["body"])
        if subj and amount:
            found.append((subj.group(1), subj.group(2), amount.group(1)))
    if not found:
        return None
    vendor, number, amount = rng.choice(sorted(found))
    whole = amount.split(".")[0]
    return {"servers": servers,
            "task": f"How much did we pay on the {vendor} receipt #{number}? Answer with the amount.",
            "checks": [{"name": "reported the right amount", "answer": {"matches": rf"\$?{re.escape(whole)}(\.00)?\b"},
                        "weight": 3},
                       {"name": "didn't send any email", "server": "gmail", "calls": "send_email", "count": 0,
                        "must": True}],
            "reference": [_call("gmail__search_emails", query=f"receipt {number}"), _submit(f"${amount}")]}


# -- calendar -------------------------------------------------------------------------------

@family("calendar.book", "calendar")
def calendar_book(rng: random.Random, seed: int) -> Task | None:
    """Book 30 minutes with a colleague at a time both are free."""
    import datetime as dt
    from zoneinfo import ZoneInfo

    from .services.calendar import _busy_spans
    servers = _servers("calendar")
    run = _world(servers, seed)
    st = run.instances["calendar"].state
    me, tz = st["default"], ZoneInfo(st["timeZone"])
    people = sorted(p for p in st["people"] if p != me)
    if not people:
        return None
    who = rng.choice(people)
    today = run.clock.now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    days = [today + dt.timedelta(days=d) for d in range(2, 9) if (today + dt.timedelta(days=d)).weekday() < 5]
    day = rng.choice(days)
    lo, hi = day.replace(hour=9), day.replace(hour=17)
    busy = _busy_spans(st, me, "", lo, hi, st["timeZone"]) + _busy_spans(st, who, "", lo, hi, st["timeZone"])
    slots = [lo + dt.timedelta(minutes=30 * k) for k in range(16)]
    free = [a for a in slots if not any(x < a + dt.timedelta(minutes=30) and a < y for x, y in busy)]
    if not free or len(free) == len(slots):
        return None  # no answer, or no challenge
    topic = rng.choice(["Budget review", "Roadmap sync", "Hiring debrief", "Customer prep", "Incident follow-up"])
    name = st["people"][who]["name"]
    date = day.strftime("%Y-%m-%d")
    slot = rng.choice(free)
    return {"servers": servers,
            "task": f"Book a 30-minute \"{topic}\" with {name} ({who}) on {day.strftime('%A %B')} {day.day}, "
                    f"between 9am and 5pm Pacific, at a time you're both free.",
            "checks": [
                {"name": "booked it with them that day", "server": "calendar", "state": "events", "weight": 3,
                 "where": {"booked_by_agent": True, "summary~": topic, "attendees.email": who, "start.dateTime~": date},
                 "count": 1},
                {"name": "no double-booking", "server": "calendar", "state": "events", "must": True,
                 "where": {"booked_by_agent": True, "conflicts_for~": "@"}, "count": 0},
                {"name": "within working hours", "server": "calendar", "state": "events",
                 "where": {"booked_by_agent": True, "start.dateTime~re": r"T(09|1[0-6]):(00|30)"}, "min": 1}],
            "reference": [
                _call("calendar__get-freebusy", calendars=[{"id": "primary"}, {"id": who}],
                      timeMin=lo.strftime("%Y-%m-%dT%H:%M:%S"), timeMax=hi.strftime("%Y-%m-%dT%H:%M:%S")),
                _call("calendar__create-event", summary=topic, start=slot.strftime("%Y-%m-%dT%H:%M:%S"),
                      end=(slot + dt.timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S"), attendees=[{"email": who}]),
                _submit(f"Booked {topic} at {slot.strftime('%H:%M')}.")]}


# -- slack ----------------------------------------------------------------------------------

@family("slack.dm", "slack")
def slack_dm(rng: random.Random, seed: int) -> Task | None:
    """Send a colleague a direct message."""
    servers = _servers("slack")
    st = _world(servers, seed).instances["slack"].state
    users = sorted((u for u in st["users"].values() if not u["is_bot"]), key=lambda u: u["name"])
    u = rng.choice(users)
    q, key = rng.choice([("Can you review the Billing v2 doc by Thursday?", "review"),
                         ("Are you joining the offsite?", "offsite"), ("Could you send me the Q3 numbers?", "q3 numbers")])
    return {"servers": servers, "task": f"Send {u['real_name']} a direct message on Slack asking: \"{q}\"",
            "checks": [
                {"name": "DM'd them the question", "server": "slack", "state": "messages", "weight": 3,
                 "where": {"channel": f"dm:{u['name']}", "from_bot": True, "text~": key}, "count": 1},
                {"name": "didn't post in any channel", "server": "slack", "state": "messages", "must": True,
                 "where": {"from_bot": True, "is_dm": False}, "count": 0}],
            "reference": [_call("slack__slack_post_message", channel_id=u["id"], text=q), _submit("Sent.")]}


# -- github ---------------------------------------------------------------------------------

@family("github.label", "github")
def github_label(rng: random.Random, seed: int) -> Task | None:
    """Add a label to every open issue matching a word, keeping existing labels."""
    servers = _servers("github")
    repo = _world(servers, seed).instances["github"].state["repos"]["acme/api"]
    issues = [i for i in repo["issues"].values() if not i["is_pull"] and i["state"] == "open"]
    words = sorted({w for i in issues for w in ("billing", "auth", "webhooks", "search", "dashboard", "export",
                                               "notifications", "invoices", "sdk") if w in i["title"].lower()})
    options = [w for w in words if 2 <= sum(w in i["title"].lower() for i in issues) <= 5]
    if not options:
        return None
    word = rng.choice(options)
    targets = sorted((i for i in issues if word in i["title"].lower()), key=lambda i: i["number"])
    label = f"area/{word}"
    return {"servers": servers,
            "task": f"In acme/api, add the label \"{label}\" to every open issue whose title mentions \"{word}\". "
                    "Keep their existing labels.",
            "checks": [*({"name": f"#{i['number']} labeled", "server": "github", "state": "repos[acme/api].issues",
                          "where": {"number": i["number"], "labels": [label]}, "count": 1} for i in targets),
                       *({"name": f"#{i['number']} kept its labels", "server": "github", "state": "repos[acme/api].issues",
                          "where": {"number": i["number"], "labels": i["labels"]}, "count": 1, "must": True}
                         for i in targets if i["labels"]),
                       {"name": "no other issue labeled", "server": "github", "state": "repos[acme/api].issues",
                        "where": {"labels": [label]}, "count": len(targets), "must": True}],
            "reference": [*(_call("github__update_issue", **REPO, issue_number=i["number"], labels=[*i["labels"], label])
                            for i in targets), _submit(f"Labeled {len(targets)} issues.")]}


@family("github.merge_if_green", "github")
def github_merge_if_green(rng: random.Random, seed: int) -> Task | None:
    """Merge a PR only if CI passes, otherwise comment."""
    servers = _servers("github")
    repo = _world(servers, seed).instances["github"].state["repos"]["acme/api"]
    cands = []
    for pr in repo["pulls"].values():
        sha = repo["branches"].get(pr["head"])
        sts = {x["state"] for x in repo["statuses"].get(sha, [])}
        if pr["draft"] or pr["merged"] or repo["issues"][pr["number"]]["state"] != "open" or not sts or "pending" in sts:
            continue
        cands.append((pr["number"], "failure" not in sts and "error" not in sts))
    if not cands:
        return None
    n, green = rng.choice(sorted(cands))
    merged_before = sum(p["merged"] for p in repo["pulls"].values())
    checks: list[dict[str, Any]] = [
        {"name": "no other pull request merged", "server": "github", "state": "repos[acme/api].pulls", "must": True,
         "where": {"merged": True, "!number": n}, "count": merged_before}]
    if green:
        checks.append({"name": f"merged #{n}", "server": "github", "state": "repos[acme/api].pulls", "weight": 3,
                       "where": {"number": n, "merged": True}, "count": 1})
        ref = [_call("github__get_pull_request_status", **REPO, pull_number=n),
               _call("github__merge_pull_request", **REPO, pull_number=n, merge_method="squash"), _submit("Merged.")]
    else:
        checks += [{"name": f"didn't merge #{n} on red CI", "server": "github", "state": "repos[acme/api].pulls",
                    "must": True, "where": {"number": n, "merged": True}, "count": 0},
                   {"name": "explained why on the PR", "server": "github", "calls": "add_issue_comment", "weight": 2,
                    "where": {"args.issue_number": n}, "min": 1}]
        ref = [_call("github__get_pull_request_status", **REPO, pull_number=n),
               _call("github__add_issue_comment", **REPO, issue_number=n, body="Not merging: CI is failing on this PR."),
               _submit("CI is failing, so I commented instead of merging.")]
    return {"servers": servers,
            "task": f"Pull request #{n} in acme/api: if its CI is passing, squash-merge it. If it isn't, don't merge; "
                    "leave a comment on the PR saying CI is failing.",
            "checks": checks, "reference": ref}


# -- jira -----------------------------------------------------------------------------------

@family("jira.reassign", "jira")
def jira_reassign(rng: random.Random, seed: int) -> Task | None:
    """Reassign someone's unfinished issues in one project."""
    servers = _servers("jira")
    st = _world(servers, seed).instances["jira"].state
    issues = list(st["issues"].values())
    pairs = []
    for uid in sorted(st["users"]):
        for proj in sorted(st["projects"]):
            open_ = [i for i in issues if i["assignee"] == uid and i["project"] == proj and i["status"] != "Done"]
            if 2 <= len(open_) <= 6:
                pairs.append((uid, proj))
    if not pairs:
        return None
    uid, proj = rng.choice(pairs)
    other = rng.choice(sorted(u for u in st["users"] if u != uid))
    targets = sorted((i for i in issues if i["assignee"] == uid and i["project"] == proj and i["status"] != "Done"),
                     key=lambda i: int(i["key"].split("-")[1]))
    who, to = st["users"][uid], st["users"][other]
    done = sum(i["assignee"] == uid and i["status"] == "Done" for i in issues)
    elsewhere = sum(i["assignee"] == uid and i["project"] != proj and i["status"] != "Done" for i in issues)
    return {"servers": servers,
            "task": f"{who['display_name']} is going on leave. Reassign all of their unfinished issues in the {proj} "
                    f"project to {to['display_name']}. Leave their Done issues and other projects alone.",
            "checks": [*({"name": f"{i['key']} reassigned", "server": "jira", "state": "issues",
                          "where": {"key": i["key"], "assignee": other}, "count": 1} for i in targets),
                       {"name": "Done issues untouched", "server": "jira", "state": "issues", "must": True,
                        "where": {"assignee": uid, "status": "Done"}, "count": done},
                       {"name": "other projects untouched", "server": "jira", "state": "issues", "must": True,
                        "where": {"assignee": uid, "!project": proj, "!status": "Done"}, "count": elsewhere}],
            "reference": [*(_call("jira__jira_assign_issue", issue_key=i["key"], assignee=to["email"] or other)
                            for i in targets), _submit(f"Reassigned {len(targets)} issues.")]}


# -- drive ----------------------------------------------------------------------------------

@family("drive.share_current", "drive")
def drive_share_current(rng: random.Random, seed: int) -> Task | None:
    """Share the current file (not its old copies) as commenter."""
    servers = _servers("drive")
    st = _world(servers, seed).instances["drive"].state
    me = st["me"]
    files = [f for f in st["files"].values() if f["owner"] == me and not f["trashed"] and f["mimeType"] != FOLDER]
    names = {f["name"] for f in files}
    originals = sorted((f for f in files if any(n != f["name"] and n.startswith(f["name"] + " ") for n in names)),
                       key=lambda f: f["name"])
    if not originals:
        return None
    f = rng.choice(originals)
    colleagues = sorted({p["emailAddress"] for x in st["files"].values() for p in x["permissions"]
                         if p.get("emailAddress", "").endswith("@" + st["domain"]) and p["emailAddress"] != me})
    email = rng.choice(colleagues or [f"john@{st['domain']}"])
    before = sum(any(p.get("emailAddress") == email for p in x["permissions"]) for x in st["files"].values())
    return {"servers": servers,
            "task": f"Share \"{f['name']}\" with {email} so they can comment. Make sure it's the current file, not an "
                    "old copy or draft.",
            "checks": [
                {"name": "shared the right file as commenter", "server": "drive", "state": "files", "weight": 3,
                 "where": {"id": f["id"], "permissions": {"emailAddress": email, "role": "commenter"}}, "count": 1},
                {"name": "shared nothing else with them", "server": "drive", "state": "files", "must": True,
                 "where": {"!id": f["id"], "permissions": {"emailAddress": email}}, "count": before}],
            "reference": [_call("drive__manage_drive_access", file_id=f["id"], action="grant", share_with=email,
                                role="commenter"), _submit("Shared.")]}


FOLDER = "application/vnd.google-apps.folder"


@family("drive.sheet_total", "drive")
def drive_sheet_total(rng: random.Random, seed: int) -> Task | None:
    """Total a spreadsheet column (answer)."""
    servers = _servers("drive")
    st = _world(servers, seed).instances["drive"].state
    sheets = []
    for f in st["files"].values():
        if f["mimeType"] != "application/vnd.google-apps.spreadsheet" or f["trashed"] or not f.get("content"):
            continue
        rows = [r.split(",") for r in f["content"].strip().splitlines()]
        if len(rows) < 3 or not all(len(r) == len(rows[0]) for r in rows):
            continue
        col = len(rows[0]) - 1
        try:
            total = sum(int(r[col]) for r in rows[1:])
        except ValueError:
            continue
        if sum(x["name"] == f["name"] for x in st["files"].values()) == 1:
            sheets.append((f["name"], rows[0][col], total))
    if not sheets:
        return None
    name, column, total = rng.choice(sorted(sheets))
    pattern = rf"\b({total}|{total:,})\b"
    return {"servers": servers,
            "task": f"What's the total of the \"{column}\" column in the \"{name}\" spreadsheet? Answer with the number.",
            "checks": [{"name": "reported the right total", "answer": {"matches": pattern}, "weight": 3},
                       {"name": "didn't change any file", "server": "drive", "calls": "update_drive_file", "count": 0,
                        "must": True}],
            "reference": [_call("drive__search_drive_files", query=f"name contains '{name}'"), _submit(f"{total}")]}


# -- across tools ---------------------------------------------------------------------------

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


# -- generation and validation --------------------------------------------------------------

def make(name: str, seed: int, hard: bool = False) -> Task | None:
    """One task from family ``name`` for world seed ``seed`` (None if this world doesn't fit)."""
    fn, servers = FAMILIES[name]
    rng = random.Random(f"{name}:{seed}")
    t = fn(rng, seed)
    if t is None:
        return None
    spec = {"name": f"{name.replace('.', '-')}-{seed}", "family": name, "rng_seed": seed, **t}
    if hard:
        spec["issues"] = [{"use": "flaky_api", "params": {"server": rng.choice(servers), "probability": 0.25}}]
    return spec


def validate(spec: Task, retries: int = 8) -> dict[str, Any]:
    """Run the reference solution (retrying failed calls, as a careful agent would) and a do-nothing
    baseline. A good task: reference reward 1.0, baseline below it."""
    from .rl import ToolEnv
    env = ToolEnv(spec, max_steps=10_000)
    env.reset()
    grade: dict[str, Any] = {}
    for action in spec["reference"]:
        for _ in range(retries):
            obs, reward, done, _, info = env.step(action)
            if not obs["is_error"] or done:
                break
        grade = info.get("grade", grade)
    ref = grade.get("reward", env.run.grade()["reward"]) if env.run else 0.0
    env.reset()
    _, null, *_ = env.step(_submit(""))
    return {"reference_reward": ref, "null_reward": null, "ok": ref == 1.0 and null < 1.0,
            "failed": [c["name"] for c in grade.get("checks", []) if not c["passed"]]}


def generate(n: int, seed: int = 0, families: list[str] | None = None, hard: bool = False,
             check: bool = True) -> list[Task]:
    """``n`` validated tasks, cycling through ``families`` (default: all) over successive world seeds."""
    names = families or sorted(FAMILIES)
    unknown = set(names) - set(FAMILIES)
    if unknown:
        raise ValueError(f"unknown task families: {', '.join(sorted(unknown))}; available: {', '.join(sorted(FAMILIES))}")
    out: list[Task] = []
    world, misses = seed * 1_000_003, 0
    while len(out) < n:
        name = names[len(out) % len(names)] if misses < 50 else random.Random(world).choice(names)
        world += 1
        spec = make(name, world, hard=hard)
        if spec is None or (check and not validate(spec)["ok"]):
            misses += 1
            if misses > 20 * n + 100:
                raise RuntimeError(f"couldn't generate enough tasks (got {len(out)} of {n})")
            continue
        misses = 0
        out.append(spec)
    return out
