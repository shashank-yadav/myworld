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

def _checked(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Fail with a clear message on a missing path, and never create files at the source path."""
    def run(path: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        from pathlib import Path
        if not Path(path).exists():
            raise ValueError(f"no such file or folder: {path}")
        return fn(path, *args, **kwargs)
    run.__signature__ = __import__("inspect").signature(fn)  # type: ignore[attr-defined]
    return run


IMPORTERS: dict[str, Callable[..., dict[str, Any]]] = {name: _checked(fn) for name, fn in {
    "gmail": gmail.import_mbox,
    "calendar": calendar.import_ics,
    "slack": slack.import_export,
    "github": github.import_repo,
    "jira": jira.import_export,
    "drive": drive.import_folder,
}.items()}

__all__ = ["IMPORTERS", "ImportOptions"]
