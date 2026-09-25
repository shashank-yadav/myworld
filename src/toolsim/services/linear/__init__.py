"""Linear.

Tool names follow Linear's official hosted MCP server (mcp.linear.app). Parameters and objects
follow Linear's API: issues have team-scoped identifiers (ENG-12), each team has its own
workflow states, priorities are 0 (none) to 4 (low), and most references accept a name, a
key, an email or an ID.

Seed format::

    viewer: {name: Alex Rivera, email: alex@acme.com}
    users: [{name: John Park, email: john@acme.com}]
    teams:
      - key: ENG
        name: Engineering
        labels: [Bug, Feature]
        projects: [{name: Billing v2, state: started, lead: john@acme.com}]
        issues:
          - {title: Duplicate charges on retry, state: In Progress, priority: 1, assignee: john@acme.com,
             labels: [Bug], project: Billing v2, cycle: current, comments: [{author: alex@acme.com, body: Customer escalated}]}
        estimates: fibonacci                  # 2026-09-25.1: fibonacci | exponential | linear | tshirt | none
        label_groups: {Type: [Bug, Feature]}  # 2026-09-25.1: one label per group

From 2026-09-25.1, like Linear: issues join cycles (by number, name, or current/next; completed
cycles are closed), estimates must be on the team's scale (or are rejected when a team doesn't
use estimates), labels in a group are mutually exclusive and a group can't be applied itself, and
list_issues pages with a cursor.
"""

from __future__ import annotations

from .actions import act_add_comment, act_set_state
from .issues import (
                create_comment,
                create_issue,
                create_issue_v1,
                get_issue,
                list_comments,
                list_issues,
                list_issues_v1,
                update_issue,
                update_issue_v1,
)
from .projects import create_project, get_project, list_project_labels, list_projects, update_project
from .service import Linear
from .teams import create_issue_label, get_issue_status, get_team, list_cycles, list_issue_labels, list_issue_statuses, list_teams
from .workspace import get_document, get_user, list_documents, list_users, search_documentation

Linear.tools = [list_comments, create_comment, list_cycles, get_document, list_documents, get_issue, list_issues,
                list_issues_v1, create_issue_v1, update_issue_v1,
                create_issue, update_issue, list_issue_statuses, get_issue_status, list_issue_labels, create_issue_label,
                list_projects, get_project, create_project, update_project, list_project_labels, list_teams, get_team,
                list_users, get_user, search_documentation]
Linear.actions = [act_set_state, act_add_comment]

__all__ = ["Linear"]
