"""Slack.

Tools, parameters and responses follow the reference Slack MCP server
(modelcontextprotocol/servers, src/slack), which returns raw Slack Web API JSON. The server
acts as a bot user, so Slack's membership rules apply: the bot can only read and post in
channels it has joined (``not_in_channel`` otherwise), a very common agent failure.

Seed format::

    team: {name: Acme, domain: acme}
    bot: {name: acme-assistant}
    users:
      - {name: john, real_name: John Park, email: john@acme.com, title: Engineering Manager, tz: America/Los_Angeles}
    channels:
      - name: general
        members: [bot, john, alex]          # user names; "bot" is the MCP server's bot user
        topic: Company-wide announcements
        messages:
          - {user: john, text: "Deploy is done", reactions: {tada: [alex]},
             replies: [{user: alex, text: "Nice!"}]}
    responders:                               # colleagues who answer (2026-09-25.1)
      - {user: john, when: {mention: true}, reply: "Looking now", delay: 2m}
      - {user: priya, when: {dm: true, "text~": deploy}, reply: "It's out", times: 2}

From 2026-09-25.1, like the real API: posting to a user ID (U...) opens a DM (a D... channel);
only slack_post_message accepts channel names, every other tool needs the channel ID
(``channel_not_found`` otherwise); and responders reply a while later. A responder's ``when`` can
have ``channel`` (name), ``dm``, ``mention`` (a real ``<@U123>`` mention: a plain "@john"
notifies nobody) and ``text~``; with no ``when`` it answers DMs and mentions. It replies in the
thread (``thread: false`` for top level; DMs reply top level) up to ``times`` times (default 1).
"""

from __future__ import annotations

from .actions import act_archive_channel, act_post_message, act_set_member
from .service import Slack
from .tools import (
               slack_add_reaction,
               slack_get_channel_history,
               slack_get_thread_replies,
               slack_get_user_profile,
               slack_get_users,
               slack_list_channels,
               slack_post_message,
               slack_reply_to_thread,
)

Slack.tools = [slack_list_channels, slack_post_message, slack_reply_to_thread, slack_add_reaction,
               slack_get_channel_history, slack_get_thread_replies, slack_get_users, slack_get_user_profile]
Slack.actions = [act_post_message, act_set_member, act_archive_channel]

__all__ = ["Slack"]
