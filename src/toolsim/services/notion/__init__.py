"""Notion.

Tool names follow Notion's hosted MCP server (mcp.notion.com). Pages hold Notion-flavored
Markdown; databases ("data sources") have typed schemas and their rows are pages with
properties. Pages the user can't access behave as if they don't exist, as in Notion.

Not simulated in this version: file uploads and attachments, views, Notion AI search over
connected sources, custom agents, skills, and SQL queries over data sources.

Seed format::

    user: {name: Alex Rivera, email: alex@acme.com}
    teams: [{name: Engineering}]
    pages:
      - {key: wiki, title: Engineering Wiki, team: Engineering, content: "# Welcome"}
      - {key: runbook, title: On-call runbook, parent: wiki, content: "## Paging\\n..."}
      - {title: Compensation 2026, restricted: true}
    databases:
      - key: tasks
        title: Tasks
        parent: wiki
        properties: {Name: title, Status: {status: [Not started, In progress, Done]}, Owner: people,
                     Due: date, Tags: {multi_select: [infra, billing]}, Points: number, Shipped: checkbox}
        rows: [{Name: Rotate DB credentials, Status: In progress, Owner: john@acme.com, Due: 2026-09-26}]

From 2026-09-25.1, like Notion: pages carry your access level (``access: full | edit | comment |
view``, inherited by child pages and database rows), so view-only pages reject edits and
comment-only pages reject everything but comments; search is eventually consistent (new and
edited pages reach it about two minutes later; fetch is immediate); data source queries take
typed filters (``{"Points": {">=": 3}}``, ``{"Due": {"before": "2026-10-01"}}``,
``{"Owner": {"is_empty": true}}``, contains, does_not_equal) with cursor pagination; and
duplicating a page finishes asynchronously.
"""

from __future__ import annotations

from .actions import act_edit_page, act_finish_duplicate, act_restrict_page, act_set_access
from .service import Notion
from .tools import (
                notion_create_comment,
                notion_create_database,
                notion_create_pages,
                notion_duplicate_page,
                notion_fetch,
                notion_get_comments,
                notion_get_teams,
                notion_get_users,
                notion_move_pages,
                notion_query_data_sources,
                notion_query_data_sources_v1,
                notion_search,
                notion_update_page,
)

Notion.tools = [notion_search, notion_fetch, notion_create_pages, notion_update_page, notion_move_pages,
                notion_duplicate_page, notion_create_database, notion_query_data_sources, notion_query_data_sources_v1,
                notion_create_comment,
                notion_get_comments, notion_get_users, notion_get_teams]
Notion.actions = [act_edit_page, act_restrict_page, act_finish_duplicate, act_set_access]

__all__ = ["Notion"]
