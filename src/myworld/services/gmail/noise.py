"""gmail: generated volume, distractors and background activity (see ``myworld.noise``)."""

from __future__ import annotations

import copy
import datetime as dt
import random
from typing import Any

from ...noise import COMPONENTS, CUSTOMERS, FIRST, LAST, PROJECTS, TITLES, VENDORS, _iso, _now, _past, _pick, _rng, people

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
        first, last = _pick(rng, FIRST), _pick(rng, LAST)
        return {"from": f"{first} {last} <{first.lower()}@{ext}>", "labels": [],
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


def generate(seed: dict[str, Any], cfg: dict[str, Any], rng_seed: int, now: str) -> dict[str, Any]:
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
    seed["company_mail"] = _company_mail(seed, cfg, rng_seed, t_now, me, crowd)
    return seed


def _company_mail(seed: dict[str, Any], cfg: dict[str, Any], rng_seed: int, t_now: dt.datetime, me: str,
                  crowd: list[dict[str, str]]) -> dict[str, list[dict[str, Any]]]:
    """Everyone else's own background mail, for company mailboxes (read from 2026-09-25.2 on). Its
    own random stream, so the default mailbox is the same as without it."""
    rng = _rng(rng_seed, "gmail:company")
    domain, org = me.split("@")[1], me.split("@")[1].split(".")[0]
    seeded = {a.split("<")[-1].rstrip(">").strip().lower() for e in seed.get("emails", [])
              for a in [e.get("from", ""), *(e.get("to") or [])] if a}
    company = sorted({a for a in [*seeded, *seed.get("directory", [])] if a.endswith("@" + domain)} - {me})
    out: dict[str, list[dict[str, Any]]] = {}
    for addr in company:
        others = [c for c in crowd if c["email"] != addr]
        mails = out.setdefault(addr, [])
        for i in range(int(cfg.get("colleague_emails", 25))):
            m = _mail(rng, domain, org, others)
            m.pop("colleague", None)
            t = _past(rng, t_now, float(cfg.get("days", 30)))
            read = rng.random() < (0.5 if (t_now - t).days < 2 else 0.9)
            labels = [*(["INBOX"] if "SPAM" not in m["labels"] else []), *m.pop("labels"), *([] if read else ["UNREAD"])]
            mails.append({**m, "to": [addr], "date": _iso(t), "labels": labels, "thread": f"company-{i}"})
    return out


def ambient(server: str, seed: dict[str, Any], times: list[int], rng: random.Random, rng_seed: int,
            now: str) -> list[dict[str, Any]]:
    """Background activity for one run: world events at the given offsets (seconds)."""
    events: list[dict[str, Any]] = []
    me = (seed.get("user") or {}).get("email", "alex@acme.com")
    crowd = people(rng_seed, me.split("@")[1], 14)
    for t in times:
        m = _mail(rng, me.split("@")[1], me.split("@")[1].split(".")[0], crowd)
        m.pop("colleague", None)
        labels = [x for x in m.pop("labels") if x.startswith("CATEGORY_") or x in ("IMPORTANT", "SPAM")]
        events.append({"server": server, "action": "deliver_email", "at": f"+{t}s", "name": "ambient mail",
                       "params": {"sender": m["from"], "subject": m["subject"], "body": m["body"],
                                  **({"labels": labels} if labels else {})}})
    return events
