"""Jira.

Tools and parameters follow the most widely used Jira MCP server (sooperset/mcp-atlassian),
which returns simplified issue JSON. Issues move through a real workflow (only listed
transitions are allowed), and ``jira_search`` understands a practical subset of JQL.

Seed format::

    user: {account_id: alex, display_name: Alex Rivera, email: alex@acme.com}
    users: [{account_id: john, display_name: John Park, email: john@acme.com}]
    projects:
      - key: OPS
        name: Operations
        issues:
          - {summary: Rotate prod DB credentials, type: Task, status: In Progress, assignee: john,
             priority: High, labels: [security], comments: [{author: alex, body: Due Friday}]}
    boards:                                     # agile boards and sprints (2026-09-25.1)
      - name: OPS board
        project: OPS
        type: scrum                             # or kanban (no sprints)
        sprints:
          - {name: OPS Sprint 7, state: active, start: "2026-09-14", end: "2026-09-28",
             goal: Pass the audit, issues: [OPS-2]}

From 2026-09-25.1: boards and sprints tools, ``sprint`` in JQL (``sprint in openSprints()``,
``closedSprints()``, ``futureSprints()``, a sprint id or name), and issues join sprints with
``jira_update_issue`` fields ``{"sprint": <id>}`` (or customfield_10020). Like Jira, one sprint is
active per board, only active sprints can be completed, and completing one sends unfinished
issues back to the backlog (or to ``move_incomplete_to``).
"""

from __future__ import annotations

from .actions import act_add_comment, act_assign, act_move_to_sprint, act_set_status
from .service import Jira
from .tools import (
              jira_add_comment,
              jira_add_worklog,
              jira_assign_issue,
              jira_batch_create_issues,
              jira_create_issue,
              jira_create_issue_link,
              jira_create_sprint,
              jira_delete_issue,
              jira_get_agile_boards,
              jira_get_all_projects,
              jira_get_board_issues,
              jira_get_issue,
              jira_get_project_issues,
              jira_get_sprint_issues,
              jira_get_sprints_from_board,
              jira_get_transitions,
              jira_get_user_profile,
              jira_link_to_epic,
              jira_search,
              jira_transition_issue,
              jira_update_issue,
              jira_update_sprint,
)

Jira.tools = [jira_search, jira_get_issue, jira_create_issue, jira_batch_create_issues, jira_update_issue,
              jira_delete_issue, jira_assign_issue, jira_transition_issue, jira_get_transitions, jira_add_comment,
              jira_add_worklog, jira_get_all_projects, jira_get_project_issues, jira_create_issue_link, jira_link_to_epic,
              jira_get_user_profile, jira_get_agile_boards, jira_get_board_issues, jira_get_sprints_from_board,
              jira_get_sprint_issues, jira_create_sprint, jira_update_sprint]
Jira.actions = [act_set_status, act_add_comment, act_assign, act_move_to_sprint]

__all__ = ["Jira"]
