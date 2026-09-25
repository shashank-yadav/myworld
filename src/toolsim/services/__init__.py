"""The simulated tools. Each is a self-contained block: an environment picks the subset it needs."""

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
