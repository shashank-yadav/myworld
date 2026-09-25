"""Shared import options: pseudonymization, scrubbing, time rebasing, limits."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FIRST = ["Alex", "Sam", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Jamie", "Avery", "Quinn", "Drew", "Rowan",
         "Parker", "Reese", "Skyler", "Emerson", "Hayden", "Kendall", "Logan", "Blake", "Cameron", "Dakota", "Finley",
         "Harper", "Jessie", "Kai", "Lane", "Micah", "Noel", "Peyton", "Sage", "Tatum"]
LAST = ["Rivera", "Chen", "Patel", "Kim", "Okafor", "Novak", "Silva", "Haddad", "Larsen", "Moreau", "Tanaka", "Walsh",
        "Ibrahim", "Kowalski", "Mendes", "Nakamura", "Osei", "Petrov", "Quinn", "Rossi", "Sato", "Torres", "Ueda",
        "Varga", "Weber", "Yilmaz", "Zhou", "Andersen", "Becker", "Costa", "Dubois", "Eriksen"]
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)")


@dataclass
class ImportOptions:
    anonymize: bool = False                 # replace real people with consistent pseudonyms
    domain: str = "acme.com"                # pseudonymized company domain
    keep_domains: tuple[str, ...] = ()      # external domains to leave as-is (e.g. "stripe.com")
    scrub_text: bool = True                 # when anonymizing, mask emails/phones inside bodies too
    rebase_to: dt.datetime | None = None    # shift all times so the newest item lands here
    limit: int | None = None                # keep at most N newest items per collection
    map_path: Path | None = None            # persist the pseudonym map to keep imports consistent
    people: dict[str, dict[str, str]] = field(default_factory=dict)  # real email -> {email, name}
    _shift: dt.timedelta = dt.timedelta(0)

    def __post_init__(self) -> None:
        if self.map_path and Path(self.map_path).exists():
            self.people.update(json.loads(Path(self.map_path).read_text()))

    def save_map(self) -> None:
        if self.map_path and self.anonymize:
            Path(self.map_path).write_text(json.dumps(self.people, indent=1, sort_keys=True))

    # -- people ------------------------------------------------------------------------------

    def person(self, email: str, name: str | None = None) -> tuple[str, str]:
        """(email, name) for a real person, pseudonymized when anonymizing. Deterministic."""
        email = (email or "").strip().lower()
        if not self.anonymize or not email:
            return email, (name or email.split("@")[0])
        domain = email.rsplit("@", 1)[-1]
        if domain in self.keep_domains:
            return email, name or email.split("@")[0]
        if email not in self.people:
            h = int(hashlib.sha256(email.encode()).hexdigest(), 16)
            first, last = FIRST[h % len(FIRST)], LAST[(h // len(FIRST)) % len(LAST)]
            used = {p["email"] for p in self.people.values()}
            base = f"{first}.{last}".lower()
            fake_domain = self.domain if domain == self._home_domain() else f"external{h % 97}.example"
            candidate, n = f"{base}@{fake_domain}", 2
            while candidate in used:
                candidate, n = f"{base}{n}@{fake_domain}", n + 1
            self.people[email] = {"email": candidate, "name": f"{first} {last}"}
        p = self.people[email]
        return p["email"], p["name"]

    home: str | None = None  # the importing user's real domain (their colleagues map onto ``domain``)

    def _home_domain(self) -> str:
        return (self.home or "").lower()

    def address(self, raw: str) -> str:
        """'John Park <john@real.com>' -> 'Name <email>' (pseudonymized if enabled)."""
        from email.utils import parseaddr
        name, email = parseaddr(raw)
        if not email:
            return raw
        e, n = self.person(email, name or None)
        return f"{n} <{e}>" if (name or self.anonymize) else e

    def text(self, s: str | None) -> str | None:
        if s is None or not (self.anonymize and self.scrub_text):
            return s
        s = EMAIL_RE.sub(lambda m: self.person(m.group(0))[0], s)
        for real, fake in sorted(self.people.items(), key=lambda kv: -len(kv[0])):
            real_name = real.split("@")[0].replace(".", " ")
            if len(real_name) > 3:
                s = re.sub(re.escape(real_name), fake["name"], s, flags=re.I)
        return PHONE_RE.sub("+1 555 0100", s)

    # -- time --------------------------------------------------------------------------------

    def plan_shift(self, times: list[dt.datetime]) -> None:
        if self.rebase_to and times:
            self._shift = self.rebase_to - max(times)

    def time(self, t: dt.datetime) -> dt.datetime:
        return t + self._shift

    def newest(self, items: list[Any], key: Any) -> list[Any]:
        items = sorted(items, key=key)
        return items[-self.limit:] if self.limit else items


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
