"""GitHub.

Tools and parameters follow the reference GitHub MCP server (modelcontextprotocol/servers,
src/github), which returns raw GitHub REST API JSON. The model is git-like: branches point at
commits, commits hold full file trees, blob SHAs are real git blob hashes. Pull requests know
their merge base, so conflicts, "no commits between" and required status checks behave like
GitHub.

Seed format::

    viewer: alex-rivera                     # the authenticated user (owner of the token)
    users: [{login: john-park, name: John Park}]
    repos:
      - name: acme/api
        description: Acme public API
        private: true
        files: {README.md: "# Acme API", src/retry.py: "..."}
        branches:                           # branched from the default branch, with changes
          fix/flaky-retry: {files: {src/retry.py: "..."}, author: john-park}
        labels: [bug, p1, question]
        protected: {main: {required_checks: [ci], required_approvals: 1, dismiss_stale_reviews: true}}
        statuses: {fix/flaky-retry: [{context: ci, state: success}]}
        ci: {contexts: [ci], duration: 4m, fail_if: [{path: tests/, "content~": "assert False"}], flaky: 0.0}
        issues: [{title: Retries hammer the API, labels: [bug], author: john-park,
                  comments: [{author: priya-shah, body: Seeing this too}]}]
        pulls: [{title: Fix flaky retry, head: fix/flaky-retry, base: main, author: john-park}]

From 2026-09-25.1, like GitHub: protected branches reject direct pushes (changes go through a
pull request), ``required_approvals`` blocks merging until enough reviewers approve the current
head (with ``dismiss_stale_reviews``), merging into the default branch closes issues named with
closing keywords ("Fixes #12"), and repos with ``ci`` run it on every push: statuses go pending,
then pass or fail ``duration`` later (fail_if rules match files in the pushed tree; ``flaky`` is
a seeded failure probability).
"""

from __future__ import annotations

from .actions import act_add_comment, act_push_commit, act_set_issue_state, act_set_status
from .files import create_branch, create_or_update_file, get_file_contents, list_commits, push_files
from .issues import add_issue_comment, create_issue, get_issue, list_issues, update_issue
from .pulls import (
                create_pull_request,
                create_pull_request_review,
                get_pull_request,
                get_pull_request_comments,
                get_pull_request_files,
                get_pull_request_reviews,
                get_pull_request_status,
                list_pull_requests,
                merge_pull_request,
                update_pull_request_branch,
)
from .repos import create_repository, fork_repository, search_repositories
from .search import search_code, search_issues, search_users
from .service import GitHub

GitHub.tools = [create_or_update_file, push_files, search_repositories, create_repository, get_file_contents,
                create_issue, create_pull_request, fork_repository, create_branch, list_issues, update_issue,
                add_issue_comment, search_code, search_issues, search_users, list_commits, get_issue, get_pull_request,
                list_pull_requests, create_pull_request_review, merge_pull_request, get_pull_request_files,
                get_pull_request_status, update_pull_request_branch, get_pull_request_comments, get_pull_request_reviews]
GitHub.actions = [act_set_status, act_push_commit, act_add_comment, act_set_issue_state]

__all__ = ["GitHub"]
