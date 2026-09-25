# Changelog

Service behavior is versioned separately by date (`toolsim versions`); this file tracks the package.

## Unreleased

- **Working-condition fixes (found running real processes):** agents' credentials survive host
  restarts (the signing key is kept with the store or the CA); a loaded real-time run catches up to
  the wall clock; the journal records the passage of time only when something happened (2 entries
  instead of 21 for two calls); `--proxy-agents` keeps agents connected when their run moves;
  `POST /store/gc` and `DELETE /store/saved/{name}`; the coordinator's startup line is flushed.

- **World runtime, continued:** a durable copy-on-write store (`--store`: checkpoints as shared
  Merkle references, save/load across restarts, export/import); counterfactuals (branch, change,
  replay the rest, compare outcomes); machine components (`command` templates, `docker` preset);
  a coordinator for many hosts (placement, routing, moves, rebalancing). Real-time replays are
  exact: scheduled and timed events fire at their due time.

- **World runtime (`toolsim.world`):** runs are worlds of components with a journal and checkpoints.
  Branch at any step, replay with divergence reports (virtual and real time), structural diffs,
  recorded mutations, and checks on any component. New components: `directory`, `sqlite` and
  `remote` (a six-route HTTP protocol, so environments in any language plug in). Timed events can
  mutate components. HTTP: `/envs/{id}/journal|checkpoint|branch|replay|diff|mutate|components`.

- **Real clients, unmodified:** an HTTPS gateway (`toolsim serve --gateway PORT`) serves the Gmail,
  Calendar, Drive, Sheets, Docs, People, OAuth and GitHub (REST + GraphQL) APIs from the simulated
  worlds. Verified with `gog` (OpenClaw), Hermes' `google_api.py` (Google's Python client) and `gh`.
- **GitHub Actions:** CI runs are workflow runs with jobs, steps, logs (the zip `gh run view
  --log-failed` reads) and re-runs.
- **Contacts:** Google Contacts per mailbox (My Contacts, other contacts, directory).
- **Gmail `2026-09-25.3`:** Gmail's whole search language (grouping, `{a b}`, grouped operator values,
  whole words, sizes, categories, Pacific-time dates).
- **Wall-clock worlds:** `now: wallclock` starts a world now, moving seeded dates by whole weeks
  (default with the gateway, which also runs worlds in real time).

- **One company across tools** (new service versions; older ones unchanged):
  - Gmail `2026-09-25.2`: every company address has a mailbox, and seeded mail between them is in
    both (John's Sent has what he sent Alex).
  - Calendar `2026-09-25.2`: colleagues are full calendar users; invitations, updates, cancellations
    and RSVPs are emailed through any Gmail in the environment (also when a colleague books or
    cancels through a world event).
  - Drive `2026-09-25.3`: everyone at the company has a My Drive; shares email the recipient;
    `user_google_email` must be the signed-in user.
  - GitHub users have company emails, so `as: alex@acme.com` works everywhere.
  - Jira `2026-09-25.3`, GitHub `2026-09-25.3`, Linear `2026-09-25.2`: email notifications
    (assignments, comments, @mentions, reviews, status changes, merges) to the people involved.
  - Generated colleagues get their own background mail (Gmail `2026-09-25.2`).
  - Version fingerprints include the emails a probe call sends.
- **Noise and ambient activity for Linear and Notion**; generated colleagues exist in every tool.
- **Multi-tool tests** (`tests/test_multitool.py`): identity everywhere, notifications and faults, forks,
  realtime, parallel episodes. `schedule-with-john` checks no longer pass on seeded or calendar mail;
  a test keeps every bundled environment unsolved at the start.

- **Parallel episodes:** `toolsim.rl.EnvPool` (worker processes, batched reset/step with skips) and
  `toolsim bench` (reference rollouts; ~340 episodes/s on 8 workers).
- **Tasks:** 20 families; answer checks guard against stuffing (`max_len`, `not`); `calls: "*"` for
  "changed nothing"; validation also runs an answer-stuffing adversary.

- **Clock modes:** virtual (fast, deterministic; the default) and realtime (follows the wall clock, optionally
  accelerated; events fire between calls). RL episodes get a `wait` tool and `step(..., elapsed=)`.
- **Layout:** one package per tool (`services/<tool>/`: service, model, tools by area, actions, noise,
  importer) and a `toolsim.rl` package (episodes, task families).

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
- **Task generator** (`toolsim.rl.tasks`, `toolsim tasks`): 12 families, each producing tasks from the seeded
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
