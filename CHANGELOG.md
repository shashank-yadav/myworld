# Changelog

Service behavior is versioned separately by date (`toolsim versions`); this file tracks the package.

## Unreleased

Realism gaps closed as new service versions (`2026-09-25.1`; the `2026-09-25` versions are unchanged):
- **Gmail:** bounces for unknown addresses and typo'd domains, out-of-office and rule-based auto-replies
  (once per sender), daily send quota (429).
- **Calendar:** recurring events expand into instances (RRULE DAILY/WEEKLY/MONTHLY/YEARLY, INTERVAL, COUNT,
  UNTIL, BYDAY incl. -1FR, BYMONTHDAY, EXDATE; wall-clock across DST), instance ids, `modificationScope`
  (thisEventOnly / thisAndFollowing / all), per-instance deletes and RSVPs, colleagues who answer invites.
- **Slack:** DMs by posting to a user ID, channel IDs required outside `slack_post_message`, responders
  that answer DMs and real `<@U…>` mentions.
- **GitHub:** protected branches reject direct pushes, required approving reviews (stale ones dismissed),
  closing keywords close issues on merge, simulated CI on every push (pending, then pass/fail).
- **Jira:** 6 agile tools (boards, sprints, sprint issues, create/update sprint), `sprint` in JQL with
  `openSprints()` / `closedSprints()` / `futureSprints()`, Jira's sprint rules.
- **Drive:** Sheets (cells, A1 ranges, USER_ENTERED vs RAW, formulas, grid limits) and Docs tools.
- **Task generator** (`toolsim.tasks`, `toolsim tasks`): 12 families, each producing tasks from the seeded
  world with verifiers, `must` constraints and a reference solution; every task is validated (reference 1.0,
  do-nothing below). Checks gain `!key` (negation) and `key~re` (regex). Calendar grading is ~30x faster.
- **Eventually consistent search** (`2026-09-25.2` for GitHub, Jira, Drive; Notion `2026-09-25.1`): search
  indexes lag behind writes (new items missing, edits stale, deletes lingering) while get/list stay immediate.
  Configurable per seed with `search_lag`.
- **Linear `2026-09-25.1`:** cycles on issues, team estimate scales, exclusive label groups, cursor pagination.
- **Notion `2026-09-25.1`:** page access levels (view/comment/edit, inherited), typed data source filters with
  cursors, asynchronous page duplication.
- **Noise and ambient activity** (`toolsim.noise`): seeded, realistic volume and distractors for Gmail,
  Slack, Calendar, GitHub, Jira and Drive, and background activity during a run, for per-episode variety.
- **RL episodes** (`toolsim.rl.ToolEnv`): Gymnasium-style reset/step with a `submit` tool, rewards from
  checks with `weight` (partial credit) and `must` (hard constraints), `answer` checks (including values
  looked up in the final state), dense rewards, step penalties, fork/snapshot, trajectory export.
  Over HTTP: `seed` on `POST /envs`, and `POST /envs/{id}/submit`.
- Core: `ctx.schedule` for delayed world reactions (applied at their due time, included in snapshots);
  `@tool(since=…)` compares same-day revisions correctly.

## 0.2.0 (2026-09-25)

Production hardening:
- Tool calls never leave partial writes: any failure, including bugs in a simulated tool, rolls back and
  returns a 500-style error in the service's own shape. Arguments are validated deeply (item types, objects).
- Host security: optional bearer-token auth (`--token` / `TOOLSIM_TOKEN`, required when binding beyond
  localhost), Origin validation (MCP DNS-rebinding guidance), environment files only from `--env-dir`,
  inline specs can't read files, `seed_file` confined to the environment's directory, YAML errors never
  echo file content.
- Limits: instances, snapshots (oldest evicted), fault hang/delay caps, request body size, validated ids.
- MCP calls run off the event loop, so a slow call can't stall other clients; registries are thread-safe.
- Snapshot restores check service/version (or environment) compatibility; stale snapshots return 4xx.
- Clear CLI errors instead of tracebacks; `/healthz`; JSON error bodies with a reference id.
- Rollback copies are ~3x faster on large worlds.
- CI (lint, tests on 3.11 to 3.13, frozen-version check, build) and a non-root Docker image.

Features since 0.1.0: Jira, Drive, Linear and Notion (preview); date-based service versions; multiple agents
with identities; a shared clock with whole-environment snapshots and forks; world events; reliability
faults; an issue library; importers for real tool exports.

## 0.1.0

Gmail, Calendar, Slack and GitHub simulators; environments with faults and checks.
