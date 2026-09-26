"""The simulated tools. Each is a self-contained package; an environment picks the subset it needs.

    services/<tool>/
        __init__.py   seed format (docstring), and the tool and world-action lists
        service.py    identity, versions, seed -> state, errors, the version probe
        model.py      state model, constants and helpers
        <area>.py     agent-facing tools, grouped (e.g. github: files, repos, issues, search, pulls)
        actions.py    world actions: what environments make happen (never callable by agents)
        noise.py      generated volume, distractors and background activity
        importer.py   build a seed from a real export (where one exists)

Adding a tool means adding one package and listing it here.
"""

from ..core.instance import Service
from .calendar import Calendar
from .drive import Drive
from .github import GitHub
from .gmail import Gmail
from .jira import Jira
from .linear import Linear
from .notion import Notion
from .slack import Slack

SERVICES: dict[str, type[Service]] = {s.name: s for s in (Gmail, Calendar, Slack, GitHub, Jira, Linear, Notion, Drive)}


def get_service(name: str) -> Service:
    try:
        return SERVICES[name]()
    except KeyError:
        raise ValueError(f"unknown service {name!r}; available: {', '.join(sorted(SERVICES))}") from None
