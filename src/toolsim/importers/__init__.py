"""Build environment seeds from exports of real tools.

    toolsim import gmail   "~/Takeout/Mail/All mail.mbox"     -o seeds/gmail.yaml
    toolsim import calendar ~/Takeout/Calendar/alex@acme.com.ics -o seeds/calendar.yaml
    toolsim import slack   ~/Downloads/acme-slack-export.zip   -o seeds/slack.yaml
    toolsim import github  ~/src/api --issues issues.json --pulls prs.json -o seeds/github.yaml
    toolsim import jira    ~/Downloads/jira-export.csv         -o seeds/jira.yaml
    toolsim import drive   ~/Takeout/Drive                     -o seeds/drive.yaml

Every importer produces the seed format of its service, so the result drops straight into an
environment (``servers: {gmail: {seed_file: seeds/gmail.yaml}}``). Common options: pseudonymize
people consistently across imports (``--anonymize --map people.json``), scrub emails and phone
numbers inside text, shift time so the newest item lands at the environment's "now", and cap size.
"""

from __future__ import annotations

from typing import Any, Callable

from . import calendar, drive, github, gmail, jira, slack
from .common import ImportOptions

IMPORTERS: dict[str, Callable[..., dict[str, Any]]] = {
    "gmail": gmail.import_mbox,
    "calendar": calendar.import_ics,
    "slack": slack.import_export,
    "github": github.import_repo,
    "jira": jira.import_export,
    "drive": drive.import_folder,
}

__all__ = ["IMPORTERS", "ImportOptions"]
