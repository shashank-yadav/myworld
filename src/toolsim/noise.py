"""Realistic volume, distractors and background activity, generated deterministically.

Real workspaces are noisy: hundreds of emails, busy channels, a long backlog, look-alike files.
Agents that do fine on five hand-written items often fall apart at real volume. Turn it on per
server, and let the world keep moving during an episode::

    servers:
      gmail: {noise: {emails: 300}}        # or `noise: true` for realistic defaults
      slack: {noise: {messages: 400}}
      calendar: {noise: {density: 0.5}}    # share of working hours already booked
      github: {noise: {issues: 60, pulls: 4}}
      jira: {noise: {issues: 80}}
      drive: {noise: {files: 50}}
    ambient: {hours: 8, gmail: 6, slack: 20, github: 2, jira: 3, calendar: 1}   # events per hour

Everything comes from ``rng_seed``: the same seed gives the same world, and a different seed gives
a different but equally plausible one (per-episode randomization for RL). Noise never touches what
the environment seeded: seeded items keep their ids and numbers, generated people are new, and
sent mail only goes to generated people, so checks about the seeded task keep their meaning.
Distractors are near-duplicates of seeded items ("Q4 budget (old)", an older thread with a similar
subject) that an agent has to tell apart.
"""

from __future__ import annotations

import copy
import datetime as dt
import random
import re
from typing import Any
from zoneinfo import ZoneInfo

FIRST = ["Maya", "Daniel", "Aisha", "Tom", "Lena", "Carlos", "Nina", "Omar", "Grace", "Ravi", "Hannah", "Leo",
         "Sofia", "Ben", "Mei", "Jonas", "Zara", "Ethan", "Fatima", "Lucas", "Chloe", "Arjun", "Ivy", "Noah",
         "Elena", "Kenji", "Rosa", "Owen", "Tara", "Diego", "Yuki", "Sara", "Marcus", "Anya", "Felix", "Leila"]
LAST = ["Chen", "Okafor", "Novak", "Garcia", "Kim", "Patel", "Schmidt", "Rossi", "Nguyen", "Cohen", "Silva",
        "Andersen", "Kowalski", "Haddad", "Ibrahim", "Murphy", "Tanaka", "Dubois", "Larsen", "Mendes", "Brooks",
        "Weber", "Costa", "Singh", "Olsen", "Reyes", "Fischer", "Moreau"]
TITLES = ["Software Engineer", "Senior Engineer", "Product Manager", "Product Designer", "Data Scientist",
          "Account Executive", "Customer Success Manager", "Recruiter", "Finance Manager", "SRE",
          "Engineering Manager", "Marketing Lead", "Legal Counsel", "Support Engineer", "Office Manager"]
PROJECTS = ["Billing v2", "Atlas migration", "SOC 2 audit", "mobile redesign", "Q4 roadmap", "EU data residency",
            "pricing page", "onboarding flow", "search relevance", "SSO rollout", "Kubernetes upgrade",
            "customer portal", "usage-based pricing", "data warehouse"]
CUSTOMERS = ["Globex", "Initech", "Umbrella Health", "Stark Logistics", "Wayne Retail", "Hooli", "Vandelay Imports",
             "Soylent Foods", "Tyrell Labs", "Cyberdyne", "Massive Dynamic", "Pied Piper"]
VENDORS = ["Datadog", "Snowflake", "Figma", "Zoom", "Notion", "Okta", "PagerDuty", "Vercel", "Linear", "Gong"]
COMPONENTS = ["billing", "auth", "webhooks", "search", "dashboard", "api", "export", "notifications", "rate limiter",
              "sdk", "invoices", "admin panel"]
KNOWN_NAMES = {"john park", "priya shah", "alex rivera", "sam lee"}


def enabled(cfg: Any) -> dict[str, Any] | None:
    if cfg in (None, False):
        return None
    return {} if cfg is True else dict(cfg)


# -- people ---------------------------------------------------------------------------------

def people(rng_seed: int, domain: str, n: int, exclude: set[str] = frozenset()) -> list[dict[str, str]]:
    """Generated colleagues, the same list in every service of an environment."""
    rng = random.Random(f"{rng_seed}:people")
    out, seen = [], set(KNOWN_NAMES) | {x.lower() for x in exclude}
    firsts, lasts = FIRST[:], LAST[:]
    rng.shuffle(firsts)
    rng.shuffle(lasts)
    for i in range(len(firsts)):
        first, last = firsts[i], lasts[i % len(lasts)]
        if f"{first} {last}".lower() in seen or first.lower() in seen:
            continue
        seen.add(f"{first} {last}".lower())
        out.append({"first": first, "last": last, "name": f"{first} {last}", "handle": first.lower(),
                    "login": f"{first}-{last}".lower(), "email": f"{first}.{last}@{domain}".lower(),
                    "title": TITLES[i % len(TITLES)]})
        if len(out) == n:
            break
    return out


def _rng(rng_seed: int, what: str) -> random.Random:
    return random.Random(f"{rng_seed}:{what}")


def _now(now: str) -> dt.datetime:
    t = dt.datetime.fromisoformat(str(now).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _past(rng: random.Random, now: dt.datetime, days: float) -> dt.datetime:
    """A time in the last ``days``, denser toward now, mostly in working hours."""
    t = now - dt.timedelta(days=days * rng.random() ** 1.6, minutes=10)
    if rng.random() < 0.8:
        t = t.replace(hour=rng.randint(15, 23), minute=rng.randint(0, 59), second=rng.randint(0, 59))  # 8am-5pm Pacific
        if t > now - dt.timedelta(minutes=10):
            t -= dt.timedelta(days=1)
    return t


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pick(rng: random.Random, xs: list[Any]) -> Any:
    return xs[rng.randrange(len(xs))]


# -- gmail ----------------------------------------------------------------------------------

NEWSLETTERS = [("Lenny's Newsletter", "lenny@substack.com"), ("The Pragmatic Engineer", "pragmaticengineer@substack.com"),
               ("TLDR", "dan@tldrnewsletter.com"), ("Morning Brew", "crew@morningbrew.com"),
               ("Product Hunt Daily", "hello@producthunt.com"), ("Stratechery", "email@stratechery.com")]


def _mail(rng: random.Random, domain: str, org: str, colleagues: list[dict[str, str]]) -> dict[str, Any]:
    """One inbound email of a realistic kind (without date/thread)."""
    kind = rng.choices(["newsletter", "github", "jira", "invite", "receipt", "security", "colleague", "cold",
                        "social", "vendor", "spam"], [14, 12, 10, 8, 10, 3, 18, 7, 6, 8, 4])[0]
    c = _pick(rng, colleagues) if colleagues else {"name": "Maya Chen", "email": f"maya.chen@{domain}", "first": "Maya"}
    project, customer, vendor = _pick(rng, PROJECTS), _pick(rng, CUSTOMERS), _pick(rng, VENDORS)
    n = rng.randint(100, 999)
    if kind == "newsletter":
        name, addr = _pick(rng, NEWSLETTERS)
        return {"from": f"{name} <{addr}>", "labels": ["CATEGORY_PROMOTIONS" if rng.random() < 0.4 else "CATEGORY_UPDATES"],
                "subject": _pick(rng, ["How the best teams run planning", "This week: pricing experiments that worked",
                                       "The state of AI agents in production", "Why your roadmap keeps slipping",
                                       "5 lessons from scaling to 1,000 customers", "Inside a $100M ARR sales playbook"]),
                "body": "Read online. Here's what's in this issue: three essays, one interview, and a job board. "
                        "Unsubscribe at any time."}
    if kind == "github":
        comp = _pick(rng, COMPONENTS)
        return {"from": "GitHub <notifications@github.com>", "labels": ["CATEGORY_UPDATES"],
                "subject": f"[{org}/api] {comp.capitalize()} {_pick(rng, ['returns 500 on retry', 'times out under load', 'drops events', 'ignores locale'])} (Issue #{n})",
                "body": f"{c.get('login', 'maya-chen')} commented: Reproduced on staging, looks related to the recent {comp} change.\n\n"
                        "Reply to this email directly or view it on GitHub."}
    if kind == "jira":
        return {"from": "Jira <jira@" + org + ".atlassian.net>", "labels": ["CATEGORY_UPDATES"],
                "subject": f"[JIRA] (OPS-{n}) {_pick(rng, ['Rotate', 'Audit', 'Migrate', 'Document'])} {_pick(rng, COMPONENTS)} {_pick(rng, ['credentials', 'alerts', 'runbook', 'dashboards'])}",
                "body": f"{c['name']} changed the status to In Progress.\n\nView issue in Jira."}
    if kind == "invite":
        day = _pick(rng, ["Mon", "Tue", "Wed", "Thu", "Fri"])
        return {"from": "Google Calendar <calendar-notification@google.com>", "labels": [],
                "subject": f"Invitation: {project} sync @ {day} {rng.randint(9, 16)}:{_pick(rng, ['00', '30'])} (PDT) ({c['email']})",
                "body": f"{c['name']} has invited you to this event. Join with Google Meet. Reply for {c['email']}: Yes / No / Maybe"}
    if kind == "receipt":
        what = _pick(rng, [("Stripe <receipts@stripe.com>", f"Your receipt from {vendor} #{n}-{rng.randint(1000, 9999)}",
                            f"Amount paid: ${rng.randint(12, 900)}.00"),
                           ("AWS Billing <aws-billing@amazon.com>", "Amazon Web Services Billing Statement Available",
                            f"Your statement for account {rng.randint(10**11, 10**12 - 1)} is ${rng.randint(2000, 40000):,}.{rng.randint(10, 99)}."),
                           ("Uber Receipts <noreply@uber.com>", "Your Thursday evening trip with Uber", f"Total ${rng.randint(9, 60)}.{rng.randint(10, 99)}"),
                           ("Expensify <concierge@expensify.com>", "Report approved: September expenses",
                            f"{c['name']} approved your report ({rng.randint(3, 14)} expenses).")])
        return {"from": what[0], "subject": what[1], "body": what[2], "labels": ["CATEGORY_UPDATES"],
                **({"attachments": [{"filename": f"receipt-{n}.pdf", "mimeType": "application/pdf",
                                     "size": rng.randint(20000, 90000)}]} if rng.random() < 0.5 else {})}
    if kind == "security":
        return {"from": "Google <no-reply@accounts.google.com>", "labels": ["CATEGORY_UPDATES"],
                "subject": _pick(rng, ["Security alert", "New sign-in on Mac", "2-Step Verification settings changed"]),
                "body": "We noticed a new sign-in to your account. If this was you, you don't need to do anything."}
    if kind == "cold":
        ext = _pick(rng, ["growthly.io", "scalebright.com", "talentpeak.co", "dataflux.ai"])
        return {"from": f"{_pick(rng, FIRST)} {_pick(rng, LAST)} <hello@{ext}>", "labels": [],
                "subject": _pick(rng, ["Quick question", "Partnership opportunity", f"Exciting {_pick(rng, TITLES)} role",
                                       "15 minutes next week?", "Re: following up"]),
                "body": "Hope you're well! I'll keep this short: we help teams like yours cut costs by 30%. "
                        "Worth a quick chat next week?"}
    if kind == "social":
        return {"from": "LinkedIn <messages-noreply@linkedin.com>", "labels": ["CATEGORY_SOCIAL"],
                "subject": _pick(rng, [f"You appeared in {rng.randint(3, 40)} searches this week",
                                       f"{c['name']} and {rng.randint(2, 9)} others commented", "People you may know"]),
                "body": "See who's looking at your profile."}
    if kind == "vendor":
        return {"from": f"{vendor} <no-reply@{vendor.lower()}.com>", "labels": ["CATEGORY_UPDATES"],
                "subject": _pick(rng, [f"Your {vendor} renewal is coming up", f"Scheduled maintenance for {vendor}",
                                       f"What's new in {vendor} this month", f"Action required: update your {vendor} billing details"]),
                "body": f"Hi, this is a notice about your {vendor} account. Log in to the dashboard for details."}
    if kind == "spam":
        return {"from": f"Account Team <support@{_pick(rng, ['secure-verify.net', 'acct-alerts.co', 'mail-center.biz'])}>",
                "labels": ["SPAM"], "subject": "Your mailbox will be suspended",
                "body": "Verify your account within 24 hours to avoid suspension: click here."}
    subject = _pick(rng, [f"{project} status update", f"Customer escalation: {customer}", f"{project} timeline",
                          f"Can you review the {project} doc?", f"Notes from the {customer} QBR",
                          f"Budget approval: {vendor} renewal", "Offsite planning", f"Hiring: {_pick(rng, TITLES)} loop feedback"])
    return {"from": f"{c['name']} <{c['email']}>", "labels": ["IMPORTANT"] if rng.random() < 0.4 else [],
            "subject": subject, "colleague": c,
            "body": _pick(rng, [f"Quick update on {project}: we're on track but need a decision on scope by Thursday.",
                                f"{customer} flagged latency on their dashboard again. Can we get someone on it this week?",
                                "Sharing the draft here, comments welcome before Friday.",
                                f"Finance needs sign-off on the {vendor} renewal before month end.",
                                "Following up on this. Any thoughts?"])}


def gmail(seed: dict[str, Any], cfg: dict[str, Any], rng_seed: int, now: str) -> dict[str, Any]:
    seed = copy.deepcopy(seed)
    rng, t_now = _rng(rng_seed, "gmail"), _now(now)
    me = (seed.get("user") or {}).get("email", "alex@acme.com")
    domain, org = me.split("@")[1], me.split("@")[1].split(".")[0]
    crowd = people(rng_seed, domain, int(cfg.get("people", 14)))
    emails, days = seed.setdefault("emails", []), float(cfg.get("days", 30))
    seeded = [e for e in emails if "SENT" not in (e.get("labels") or [])]
    for i in range(int(cfg.get("emails", 250))):
        m = _mail(rng, domain, org, crowd)
        c = m.pop("colleague", None)
        t = _past(rng, t_now, days)
        age = (t_now - t).days
        unread = rng.random() < (0.6 if age < 2 else 0.3 if age < 7 else 0.08)
        labels = [*(["INBOX"] if "SPAM" not in m["labels"] else []), *m.pop("labels"), *(["UNREAD"] if unread else [])]
        thread = f"noise-{i}"
        emails.append({**m, "to": [me], "date": _iso(t), "labels": labels, "thread": thread})
        if c and rng.random() < 0.5:  # a conversation: you answered, maybe they did again
            for k in range(rng.randint(1, 3)):
                t += dt.timedelta(minutes=rng.randint(20, 600))
                if t >= t_now:
                    break
                mine = k % 2 == 0
                emails.append({"from": me if mine else m["from"], "to": [c["email"]] if mine else [me],
                               "subject": "Re: " + m["subject"], "date": _iso(t), "thread": thread,
                               "labels": ["SENT"] if mine else ["INBOX"],
                               "body": _pick(rng, ["Thanks, will take a look today.", "Sounds good to me.",
                                                   "Can we push this to next week?", "Looping in finance on this.",
                                                   "Done, see the updated doc."])})
    if cfg.get("distractors", True):
        for e in seeded[:4]:  # an older, different conversation that looks like the one that matters
            c = _pick(rng, crowd)
            emails.append({"from": f"{c['name']} <{c['email']}>", "to": [me], "labels": ["INBOX"],
                           "subject": f"{e.get('subject', '')} ({_pick(rng, ['old thread', 'finance', 'draft', 'v1'])})",
                           "date": _iso(_past(rng, t_now, days) - dt.timedelta(days=days / 2)),
                           "body": f"Starting a separate thread on this: {str(e.get('body', ''))[:80]}"})
    seed["directory"] = sorted({*seed.get("directory", []), *(c["email"] for c in crowd)})
    return seed


# -- slack ----------------------------------------------------------------------------------

CHATTER = {
    "eng": ["Deploying {c} to staging now", "Anyone seen flaky tests in {c}?", "PR up for the {p} change, reviews welcome",
            "Heads up: {c} migration runs tonight", "Who owns the {c} alerts?", "CI is red on main, looking"],
    "design": ["New mocks for {p} are in Figma", "Can someone review the {p} flow?", "Updated the icon set"],
    "sales": ["Closed {cu}! :tada:", "{cu} wants a security review before signing", "Pipeline review at 2pm",
              "Anyone have a case study for {cu}?"],
    "support-escalations": ["{cu} reports {c} errors since this morning", "Ticket from {cu}: {c} export stuck",
                            "Is the {c} outage resolved? {cu} is asking"],
    "incidents": ["Resolved: elevated {c} latency", "Investigating {c} error spike", "Postmortem for last week's {c} incident is up"],
    "announcements": ["Reminder: all-hands Thursday at 10", "Welcome our new {t}!", "Office closed Monday for the holiday",
                      "Open enrollment ends Friday"],
    "social": ["Lunch at the taco place?", "Who's in for the offsite hike?", "Happy birthday {n}! :birthday:", "Coffee run, anyone?"],
    "product": ["{p} beta feedback so far is positive", "Roadmap review moved to Wednesday", "Metrics for {p} are in the dashboard"],
    "_default": ["Thanks!", "+1", "Following up on this", "Can we sync on {p}?", "Looking into it", "Sounds good :thumbsup:"],
}
REPLIES = ["On it", "Thanks!", "Looking", "Same here", "Fixed now", "Can you share a link?", ":eyes:", "Works for me"]


def slack(seed: dict[str, Any], cfg: dict[str, Any], rng_seed: int, now: str) -> dict[str, Any]:
    seed = copy.deepcopy(seed)
    rng, t_now = _rng(rng_seed, "slack"), _now(now)
    users = seed.setdefault("users", [])
    domain = next((u["email"].split("@")[1] for u in users if u.get("email")), "acme.com")
    existing = {u["name"] for u in users}
    crowd = [c for c in people(rng_seed, domain, int(cfg.get("people", 14)), existing) if c["handle"] not in existing]
    for c in crowd:
        users.append({"name": c["handle"], "real_name": c["name"], "email": c["email"], "title": c["title"], "noise": True})
    names = [u["name"] for u in users]
    channels = seed.setdefault("channels", [])
    have = {c["name"] for c in channels}
    for name in cfg.get("channels", ["eng", "design", "sales", "support-escalations", "incidents", "announcements",
                                     "social", "product"]):
        if name not in have:
            bot = name in ("eng", "incidents", "product", "announcements", "support-escalations")
            members = rng.sample(names, k=min(len(names), rng.randint(5, len(names))))
            channels.append({"name": name, "members": (["bot"] if bot else []) + members, "messages": []})
    open_channels = [c for c in channels if not c.get("private") and not c.get("archived")]
    for _ in range(int(cfg.get("messages", 300))):
        ch = _pick(rng, open_channels)
        members = [m for m in ch.get("members", []) if m != "bot"] or names
        tpl = _pick(rng, CHATTER.get(ch["name"], CHATTER["_default"]))
        text = tpl.format(c=_pick(rng, COMPONENTS), p=_pick(rng, PROJECTS), cu=_pick(rng, CUSTOMERS),
                          t=_pick(rng, TITLES), n=_pick(rng, crowd)["first"] if crowd else "team")
        t = _past(rng, t_now, float(cfg.get("days", 14)))
        msg: dict[str, Any] = {"user": _pick(rng, members), "text": text, "at": _iso(t)}
        if rng.random() < 0.2:
            msg["replies"] = [{"user": _pick(rng, members), "text": _pick(rng, REPLIES),
                               "at": _iso(min(t + dt.timedelta(minutes=rng.randint(1, 90) * (j + 1)), t_now - dt.timedelta(minutes=1)))}
                              for j in range(rng.randint(1, 4))]
        if rng.random() < 0.15:
            msg["reactions"] = {_pick(rng, ["eyes", "thumbsup", "tada", "white_check_mark"]): rng.sample(members, k=min(2, len(members)))}
        ch.setdefault("messages", []).insert(0, msg)
    return seed


# -- calendar -------------------------------------------------------------------------------

def calendar(seed: dict[str, Any], cfg: dict[str, Any], rng_seed: int, now: str) -> dict[str, Any]:
    seed = copy.deepcopy(seed)
    rng, t_now = _rng(rng_seed, "calendar"), _now(now)
    user = (seed.get("user") or {}).get("email", "alex@acme.com")
    tz = ZoneInfo(seed.get("timeZone", "America/Los_Angeles"))
    domain = user.split("@")[1]
    existing = {p["email"] for p in seed.get("people", [])}
    crowd = [c for c in people(rng_seed, domain, int(cfg.get("people", 10))) if c["email"] not in existing]
    seed.setdefault("people", []).extend({"email": c["email"], "name": c["name"], "noise": True} for c in crowd)
    events = seed.setdefault("events", [])

    def span(e: dict[str, Any]) -> tuple[dt.datetime, dt.datetime]:
        a, b = (dt.datetime.fromisoformat(str(e[k]).replace("Z", "+00:00")) for k in ("start", "end"))
        return (a if a.tzinfo else a.replace(tzinfo=tz)), (b if b.tzinfo else b.replace(tzinfo=tz))

    mine = [span(e) for e in events if (e.get("organizer") or user) == user or user in
            [a if isinstance(a, str) else a.get("email") for a in e.get("attendees", [])]]

    def free(a: dt.datetime, b: dt.datetime) -> bool:
        return not any(x < b and a < y for x, y in mine)

    def add(summary: str, a: dt.datetime, minutes: int, organizer: str, others: list[str], **extra: Any) -> None:
        b = a + dt.timedelta(minutes=minutes)
        if not free(a, b):
            return
        mine.append((a, b))
        if organizer == user:
            attendees = others
        else:
            attendees = [{"email": user, "responseStatus": _pick(rng, ["accepted", "accepted", "needsAction", "tentative"])},
                         *others]
        events.append({"summary": summary, "start": a.isoformat(), "end": b.isoformat(), "attendees": attendees,
                       **({"organizer": organizer} if organizer != user else {}), **extra})

    today = t_now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    monday = today - dt.timedelta(days=today.weekday())
    if crowd:
        for c in crowd[:2]:  # weekly 1:1s
            d = monday + dt.timedelta(days=rng.randint(1, 4), hours=rng.choice([10, 11, 14, 15]))
            add(f"{c['first']} / {user.split('@')[0].split('.')[0].capitalize()} 1:1", d, 30, user, [c["email"]],
                recurrence=["RRULE:FREQ=WEEKLY"])
        lead = crowd[-1]
        add("Team sync", monday + dt.timedelta(days=rng.randint(0, 4), hours=13), 45, lead["email"],
            [c["email"] for c in crowd[:5]], recurrence=["RRULE:FREQ=WEEKLY"])
    density = float(cfg.get("density", 0.4))
    for day in range(-5, int(cfg.get("days_ahead", 14))):
        d = today + dt.timedelta(days=day)
        if d.weekday() >= 5:
            continue
        for _ in range(round(density * 8 * rng.uniform(0.6, 1.4))):
            a = d.replace(hour=rng.randint(9, 16), minute=rng.choice([0, 30]))
            others = [c["email"] for c in rng.sample(crowd, k=min(len(crowd), rng.randint(1, 4)))]
            org = user if rng.random() < 0.4 or not crowd else _pick(rng, crowd)["email"]
            what = _pick(rng, [f"{_pick(rng, PROJECTS)} review", f"{_pick(rng, CUSTOMERS)} call", "Interview",
                               f"{_pick(rng, PROJECTS)} planning", "Coffee chat", "Hiring debrief", "Design crit"])
            add(what, a, rng.choice([30, 30, 45, 60]), org, [e for e in others if e != org])
    if cfg.get("colleague_busy", True):  # people you can only see as free/busy get busier too
        for c in seed.get("calendars", []):
            if "@" not in c.get("id", "") or c["id"].startswith("team"):
                continue
            busy = c.setdefault("busy", [])
            for day in range(0, int(cfg.get("days_ahead", 14))):
                d = today + dt.timedelta(days=day)
                if d.weekday() < 5 and rng.random() < density:
                    a = d.replace(hour=rng.randint(9, 16), minute=rng.choice([0, 30]))
                    busy.append({"start": a.isoformat(), "end": (a + dt.timedelta(minutes=rng.choice([30, 60]))).isoformat()})
    return seed


# -- github ---------------------------------------------------------------------------------

def github(seed: dict[str, Any], cfg: dict[str, Any], rng_seed: int, now: str) -> dict[str, Any]:
    seed = copy.deepcopy(seed)
    rng = _rng(rng_seed, "github")
    users = seed.setdefault("users", [])
    crowd = people(rng_seed, "users.noreply.github.com", int(cfg.get("people", 10)), {u.get("name", "") for u in users})
    known = {u["login"] for u in users}
    users.extend({"login": c["login"], "name": c["name"], "noise": True} for c in crowd if c["login"] not in known)
    logins = [c["login"] for c in crowd] + ["dependabot[bot]"]
    users.append({"login": "dependabot[bot]", "name": "dependabot[bot]"})
    repos = seed.get("repos") or []
    if not repos:
        return seed
    total = int(cfg.get("issues", 40))
    for idx, repo in enumerate(repos):
        n_issues = total * 2 // 3 if idx == 0 else total // (3 * max(1, len(repos) - 1))
        issues, pulls = repo.setdefault("issues", []), repo.setdefault("pulls", [])
        number = len(issues) + len(pulls) + max([x.get("number", 0) for x in issues + pulls] or [0])
        labels = set(repo.get("labels", [])) | {"bug", "enhancement", "question", "dependencies", "good first issue",
                                                "needs-triage", "docs"}
        repo["labels"] = sorted(labels)
        seeded_titles = [i["title"] for i in issues]
        for k in range(n_issues):
            comp = _pick(rng, COMPONENTS)
            kind = rng.choices(["bug", "feature", "question", "deps", "docs"], [40, 25, 10, 15, 10])[0]
            title, lab = {
                "bug": (f"{comp.capitalize()} {_pick(rng, ['returns 500 when payload is empty', 'times out for large accounts', 'double-sends webhooks', 'ignores the locale header', 'leaks memory under load'])}", ["bug"]),
                "feature": (f"Add {_pick(rng, ['pagination to', 'filtering to', 'retries to', 'audit log for', 'bulk export in'])} {comp}", ["enhancement"]),
                "question": (f"How do I configure {comp} for {_pick(rng, CUSTOMERS)}?", ["question"]),
                "deps": (f"Bump {_pick(rng, ['requests', 'urllib3', 'pydantic', 'fastapi', 'cryptography'])} from 1.{rng.randint(0, 9)}.{rng.randint(0, 9)} to 1.{rng.randint(10, 19)}.0", ["dependencies"]),
                "docs": (f"Document {comp} limits", ["docs"]),
            }[kind]
            if cfg.get("distractors", True) and seeded_titles and k < 2:
                title, lab = f"{_pick(rng, seeded_titles)} ({_pick(rng, ['again', 'follow-up', 'staging'])})", ["bug"]
            number += 1
            issues.append({"number": number, "noise": True, "title": title, "created": _iso(_past(rng, _now(now), 120)), "labels": lab + (["needs-triage"] if rng.random() < 0.3 else []),
                           "author": "dependabot[bot]" if kind == "deps" else _pick(rng, logins[:-1]),
                           "state": "closed" if rng.random() < 0.3 else "open",
                           "body": f"Seen on {_pick(rng, ['staging', 'production', 'local'])}. Steps to reproduce in the thread.",
                           "comments": [{"author": _pick(rng, logins[:-1]), "body": _pick(rng, REPLIES + ["Can't reproduce on main.", "PR incoming."])}
                                        for _ in range(rng.choices([0, 1, 2, 3], [40, 30, 20, 10])[0])]})
        if idx == 0:
            for k in range(int(cfg.get("pulls", 3))):
                branch = f"{_pick(rng, ['feat', 'fix', 'chore'])}/{_pick(rng, COMPONENTS).replace(' ', '-')}-{k}"
                repo.setdefault("branches", {})[branch] = {"files": {f"src/{branch.split('/')[1]}.py": f"# {branch}\n"},
                                                           "author": _pick(rng, logins[:-1]), "message": f"Work on {branch}"}
                number += 1
                comp = branch.split("/")[1].rsplit("-", 1)[0].replace("-", " ")
                title = {"feat": f"Add {comp} filters", "fix": f"Fix {comp} retry edge case", "chore": f"Refactor {comp} module"}
                pulls.append({"number": number, "title": title[branch.split("/")[0]],
                              "created": _iso(_past(rng, _now(now), 10)),
                              "head": branch, "base": repo.get("default_branch", "main"), "author": _pick(rng, logins[:-1]),
                              "draft": rng.random() < 0.25})
                repo.setdefault("statuses", {})[branch] = [{"context": "ci", "state": _pick(rng, ["success", "success", "failure", "pending"])}]
    return seed


# -- jira -----------------------------------------------------------------------------------

def jira(seed: dict[str, Any], cfg: dict[str, Any], rng_seed: int, now: str) -> dict[str, Any]:
    seed = copy.deepcopy(seed)
    rng, t_now = _rng(rng_seed, "jira"), _now(now)
    me = seed.get("user") or {"account_id": "alex", "email": "alex@acme.com"}
    domain = (me.get("email") or "alex@acme.com").split("@")[1]
    users = seed.setdefault("users", [])
    have = {u["account_id"] for u in users} | {me["account_id"]}
    crowd = [c for c in people(rng_seed, domain, int(cfg.get("people", 10))) if c["handle"] not in have]
    users.extend({"account_id": c["handle"], "display_name": c["name"], "email": c["email"], "noise": True} for c in crowd)
    assignees = [None, me["account_id"], *(u["account_id"] for u in users)]
    projects = seed.get("projects") or []
    total = int(cfg.get("issues", 60))
    for p in projects:
        issues = p.setdefault("issues", [])
        keyed = any(i.get("key") for i in issues)
        n = max([int(str(i["key"]).rsplit("-", 1)[1]) for i in issues if i.get("key")] or [len(issues)])
        seeded = [i["summary"] for i in issues]
        for k in range(total // max(1, len(projects))):
            comp = _pick(rng, COMPONENTS)
            summary = _pick(rng, [f"{comp.capitalize()} alerts are noisy", f"Migrate {comp} to the new cluster",
                                  f"{_pick(rng, CUSTOMERS)} reports slow {comp}", f"Write runbook for {comp}",
                                  f"Spike: evaluate {_pick(rng, VENDORS)}", f"Clean up unused {comp} flags",
                                  f"{comp.capitalize()} error rate above SLO"])
            if cfg.get("distractors", True) and seeded and k < 2:
                summary = f"{_pick(rng, ['Follow-up', 'Investigate', 'Old'])}: {_pick(rng, seeded)}"
            n += 1
            created = t_now - dt.timedelta(days=rng.uniform(1, 90))
            issue = {"noise": True, "summary": summary, "type": rng.choices(["Task", "Bug", "Story"], [45, 35, 20])[0],
                     "status": rng.choices(["To Do", "In Progress", "In Review", "Done"], [40, 20, 10, 30])[0],
                     "priority": rng.choices(["Highest", "High", "Medium", "Low", "Lowest"], [5, 20, 50, 20, 5])[0],
                     "assignee": _pick(rng, assignees), "labels": rng.sample(["backend", "infra", "tech-debt", "customer"], k=rng.randint(0, 2)),
                     "created": created.strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
                     "comments": [{"author": _pick(rng, assignees[1:]), "body": _pick(rng, REPLIES)} for _ in range(rng.randint(0, 2))]}
            if keyed:
                issue["key"] = f"{p['key']}-{n}"
            issues.append(issue)
    return seed


# -- drive ----------------------------------------------------------------------------------

def _perturb(rng: random.Random, text: str) -> str:
    return re.sub(r"\d{3,}", lambda m: str(int(int(m.group()) * rng.uniform(0.85, 1.15))), text)


def drive(seed: dict[str, Any], cfg: dict[str, Any], rng_seed: int, now: str) -> dict[str, Any]:
    seed = copy.deepcopy(seed)
    rng = _rng(rng_seed, "drive")
    me = (seed.get("user") or {}).get("email", "alex@acme.com")
    crowd = people(rng_seed, me.split("@")[1], int(cfg.get("people", 10)))
    folders, files = seed.setdefault("folders", []), seed.setdefault("files", [])
    keys = {f["key"] for f in folders}
    for key, name in [("noise-marketing", "Marketing"), ("noise-people", "People"), ("noise-archive", "Archive"),
                      ("noise-customers", "Customers"), ("noise-design", "Design")]:
        if key not in keys:
            folders.append({"key": key, "name": name})
    places = [f["key"] for f in folders] + [None]
    seeded = [f for f in files if f.get("content") and not f.get("trashed")]
    for _ in range(int(cfg.get("files", 40))):
        kind = rng.choices(["doc", "sheet", "pdf", "slides"], [45, 25, 20, 10])[0]
        project, customer = _pick(rng, PROJECTS), _pick(rng, CUSTOMERS)
        name = {"doc": _pick(rng, [f"{project} PRD", f"Meeting notes {rng.randint(1, 28)} Sep", f"{customer} QBR notes",
                                   f"Interview loop: {_pick(rng, TITLES)}", "Onboarding checklist", f"{project} retro"]),
                "sheet": _pick(rng, [f"{project} budget tracker", "Headcount plan FY27", f"{customer} usage export",
                                     "Vendor spend 2026", "OKR scorecard"]),
                "pdf": _pick(rng, [f"{customer} MSA.pdf", f"{customer} order form.pdf", "Employee handbook.pdf",
                                   f"{_pick(rng, VENDORS)} invoice {rng.randint(1000, 9999)}.pdf"]),
                "slides": _pick(rng, [f"All-hands {_pick(rng, ['August', 'September'])}", f"{project} kickoff", f"{customer} pitch"])}[kind]
        f: dict[str, Any] = {"name": name, "parent": _pick(rng, places)}
        if kind == "pdf":
            f.update(mimeType="application/pdf", size=rng.randint(40000, 900000))
        else:
            f["type"] = kind
            f["content"] = (f"item,owner,amount\n{project},{_pick(rng, crowd)['first']},{rng.randint(1000, 90000)}\n"
                            f"{customer},{_pick(rng, crowd)['first']},{rng.randint(1000, 90000)}\n" if kind == "sheet"
                            else f"{name}. Owner: {_pick(rng, crowd)['name']}. Status: {_pick(rng, ['draft', 'final', 'in review'])}.")
        if rng.random() < 0.35 and crowd:  # someone else's file, shared with you
            f.update(owner=_pick(rng, crowd)["email"], shared_role=_pick(rng, ["reader", "reader", "commenter", "writer"]))
            f.pop("parent", None)
        if rng.random() < 0.05:
            f["trashed"] = True
        files.append(f)
    if cfg.get("distractors", True):
        for src in seeded[:4]:
            name = src["name"]
            stem, dot, ext = name.rpartition(".") if "." in name[-5:] else (name, "", "")
            files.append({**{k: v for k, v in src.items() if k in ("type", "mimeType", "parent")},
                          "name": f"{stem} {_pick(rng, ['(old)', '- draft v2', '(copy)', 'FINAL'])}{dot}{ext}",
                          "content": _perturb(rng, src["content"])})
    return seed


GENERATORS = {"gmail": gmail, "slack": slack, "calendar": calendar, "github": github, "jira": jira, "drive": drive}


def apply(service: str, seed: dict[str, Any], cfg: Any, rng_seed: int, now: str) -> dict[str, Any]:
    opts = enabled(cfg)
    if opts is None:
        return seed
    if service not in GENERATORS:
        raise ValueError(f"noise isn't available for {service} yet (available: {', '.join(GENERATORS)})")
    return GENERATORS[service](seed, opts, rng_seed, now)


# -- ambient activity ---------------------------------------------------------------------------

def ambient(spec: dict[str, Any], servers: dict[str, str], seeds: dict[str, dict[str, Any]], rng_seed: int,
            now: str) -> list[dict[str, Any]]:
    """World events spread over ``hours``: mail keeps arriving, channels keep talking, issues get
    comments, colleagues' calendars fill up. Only generated people act, and only on generated items,
    so the task's own items stay as seeded."""
    hours = float(spec.get("hours", 8))
    rng = _rng(rng_seed, "ambient")
    events: list[dict[str, Any]] = []

    def times(per_hour: float) -> list[int]:
        return sorted(int(rng.uniform(60, hours * 3600)) for _ in range(min(500, round(per_hour * hours))))

    for server, service in servers.items():
        rate = spec.get(server, spec.get(service))
        if not rate:
            continue
        seed = seeds[server]
        if service == "gmail":
            me = (seed.get("user") or {}).get("email", "alex@acme.com")
            crowd = people(rng_seed, me.split("@")[1], 14)
            for t in times(float(rate)):
                m = _mail(rng, me.split("@")[1], me.split("@")[1].split(".")[0], crowd)
                m.pop("colleague", None)
                labels = [x for x in m.pop("labels") if x.startswith("CATEGORY_") or x in ("IMPORTANT", "SPAM")]
                events.append({"server": server, "action": "deliver_email", "at": f"+{t}s", "name": "ambient mail",
                               "params": {"sender": m["from"], "subject": m["subject"], "body": m["body"],
                                          **({"labels": labels} if labels else {})}})
        elif service == "slack":
            chans = [c for c in seed.get("channels", []) if not c.get("private") and not c.get("archived")]
            generated = {u["name"] for u in seed.get("users", []) if u.get("noise")}
            for t in times(float(rate)):
                ch = _pick(rng, chans)
                who = [m for m in ch.get("members", []) if m in generated] or sorted(generated)
                if not who:
                    continue
                text = _pick(rng, CHATTER.get(ch["name"], CHATTER["_default"])).format(
                    c=_pick(rng, COMPONENTS), p=_pick(rng, PROJECTS), cu=_pick(rng, CUSTOMERS), t=_pick(rng, TITLES), n="team")
                events.append({"server": server, "action": "post_message", "at": f"+{t}s", "name": "ambient chatter",
                               "params": {"channel": ch["name"], "user": _pick(rng, who), "text": text}})
        elif service == "github":
            targets = [(r["name"], i["number"]) for r in seed.get("repos", []) for i in r.get("issues", [])
                       if i.get("noise") and i.get("state", "open") == "open"]
            authors = [u["login"] for u in seed.get("users", []) if u.get("noise")]
            for t in times(float(rate)):
                if targets and authors:
                    repo, n = _pick(rng, targets)
                    events.append({"server": server, "action": "add_comment", "at": f"+{t}s", "name": "ambient comment",
                                   "params": {"repo": repo, "number": n, "author": _pick(rng, authors),
                                              "body": _pick(rng, REPLIES + ["Still seeing this.", "Any update?"])}})
        elif service == "jira":
            targets = [i.get("key") or f"{p['key']}-{n}" for p in seed.get("projects", [])  # unkeyed: numbered in order
                       for n, i in enumerate(p.get("issues", []), 1) if i.get("noise")]
            authors = [u["account_id"] for u in seed.get("users", []) if u.get("noise")]
            for t in times(float(rate)):
                if targets and authors:
                    events.append({"server": server, "action": "add_comment", "at": f"+{t}s", "name": "ambient comment",
                                   "params": {"issue_key": _pick(rng, targets), "author": _pick(rng, authors),
                                              "body": _pick(rng, REPLIES + ["Bumping priority, customer asked again."])}})
        elif service == "calendar":
            me = (seed.get("user") or {}).get("email", "alex@acme.com")
            tz = ZoneInfo(seed.get("timeZone", "America/Los_Angeles"))
            crowd = [p["email"] for p in seed.get("people", []) if p.get("noise")]
            day0 = _now(now).astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
            for t in times(float(rate)):
                if not crowd:
                    break
                d = day0 + dt.timedelta(days=rng.randint(1, 7))
                a = d.replace(hour=rng.randint(9, 16), minute=rng.choice([0, 30]))
                events.append({"server": server, "action": "add_event", "at": f"+{t}s", "name": "ambient booking",
                               "params": {"calendar": _pick(rng, crowd), "summary": _pick(rng, ["Customer call", "Interview", "Focus"]),
                                          "start": a.isoformat(), "end": (a + dt.timedelta(minutes=30)).isoformat()}})
        else:
            raise ValueError(f"ambient activity isn't available for {service} yet")
    return events
