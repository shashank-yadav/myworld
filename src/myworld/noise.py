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

import datetime as dt
import importlib
import random
from types import ModuleType
from typing import Any

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


REPLIES = ["On it", "Thanks!", "Looking", "Same here", "Fixed now", "Can you share a link?", ":eyes:", "Works for me"]


def _module(service: str) -> ModuleType | None:
    try:
        return importlib.import_module(f"myworld.services.{service}.noise")
    except ModuleNotFoundError:
        return None


def apply(service: str, seed: dict[str, Any], cfg: Any, rng_seed: int, now: str) -> dict[str, Any]:
    """The seed with generated volume and distractors added (``services/<tool>/noise.py``)."""
    opts = enabled(cfg)
    if opts is None:
        return seed
    mod = _module(service)
    if mod is None:
        raise ValueError(f"noise isn't available for {service} yet")
    return mod.generate(seed, opts, rng_seed, now)


def ambient(spec: dict[str, Any], servers: dict[str, str], seeds: dict[str, dict[str, Any]], rng_seed: int,
            now: str) -> list[dict[str, Any]]:
    """World events spread over ``hours``: mail keeps arriving, channels keep talking, issues get
    comments, colleagues' calendars fill up. Only generated people act, and only on generated items,
    so the task's own items stay as seeded."""
    hours = float(spec.get("hours", 8))
    rng = _rng(rng_seed, "ambient")
    events: list[dict[str, Any]] = []
    for server, service in servers.items():
        rate = spec.get(server, spec.get(service))
        if not rate:
            continue
        mod = _module(service)
        if mod is None or not hasattr(mod, "ambient"):
            raise ValueError(f"ambient activity isn't available for {service} yet")
        times = sorted(int(rng.uniform(60, hours * 3600)) for _ in range(min(500, round(float(rate) * hours))))
        events += mod.ambient(server, seeds[server], times, rng, rng_seed, now)
    return events
