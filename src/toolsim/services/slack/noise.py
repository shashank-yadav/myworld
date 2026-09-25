"""slack: generated volume, distractors and background activity (see ``toolsim.noise``)."""

from __future__ import annotations

import copy
import datetime as dt
import random
from typing import Any

from ...noise import COMPONENTS, CUSTOMERS, PROJECTS, REPLIES, TITLES, _iso, _now, _past, _pick, _rng, people

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


def generate(seed: dict[str, Any], cfg: dict[str, Any], rng_seed: int, now: str) -> dict[str, Any]:
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


def ambient(server: str, seed: dict[str, Any], times: list[int], rng: random.Random, rng_seed: int,
            now: str) -> list[dict[str, Any]]:
    """Background activity for one run: world events at the given offsets (seconds)."""
    events: list[dict[str, Any]] = []
    chans = [c for c in seed.get("channels", []) if not c.get("private") and not c.get("archived")]
    generated = {u["name"] for u in seed.get("users", []) if u.get("noise")}
    for t in times:
        ch = _pick(rng, chans)
        who = [m for m in ch.get("members", []) if m in generated] or sorted(generated)
        if not who:
            continue
        text = _pick(rng, CHATTER.get(ch["name"], CHATTER["_default"])).format(
            c=_pick(rng, COMPONENTS), p=_pick(rng, PROJECTS), cu=_pick(rng, CUSTOMERS), t=_pick(rng, TITLES), n="team")
        events.append({"server": server, "action": "post_message", "at": f"+{t}s", "name": "ambient chatter",
                       "params": {"channel": ch["name"], "user": _pick(rng, who), "text": text}})
    return events
