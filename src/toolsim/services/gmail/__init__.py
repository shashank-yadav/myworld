"""Gmail.

Tool names, parameters and response text follow the most widely used Gmail MCP server
(GongRzhe/Gmail-MCP-Server). The state model follows the Gmail API: messages with label IDs,
threads, system and user labels, drafts and filters.

Seed format::

    user: {email: alex@acme.com, name: Alex Rivera}
    labels: [Receipts, Clients]                      # user labels
    emails:
      - {from: "John Park <john@acme.com>", to: [alex@acme.com], subject: Q4 plan,
         body: "Can we meet next week?", date: "2026-09-19T16:02:00Z",
         labels: [INBOX, UNREAD], thread: q4,        # emails sharing a thread key share a threadId
         attachments: [{filename: plan.pdf, mimeType: application/pdf, size: 48213}]}
"""

from __future__ import annotations

from .actions import act_deliver_email, act_deliver_reply, act_set_search_lag
from .service import Gmail
from .tools import (
               batch_delete_emails,
               batch_modify_emails,
               create_filter,
               create_filter_from_template,
               create_label,
               delete_email,
               delete_filter,
               delete_label,
               download_attachment,
               draft_email,
               get_filter,
               get_or_create_label,
               list_email_labels,
               list_filters,
               modify_email,
               read_email,
               search_emails,
               send_email,
               update_label,
)

Gmail.tools = [send_email, draft_email, read_email, download_attachment, search_emails, modify_email, delete_email,
               list_email_labels, batch_modify_emails, batch_delete_emails, create_label, update_label, delete_label,
               get_or_create_label, create_filter, list_filters, get_filter, delete_filter, create_filter_from_template]
Gmail.actions = [act_deliver_email, act_set_search_lag, act_deliver_reply]

__all__ = ["Gmail"]
