# toolsim

Real-world RL environments for agents: faithful, stateful replicas of the tools people work in
(Gmail, Google Calendar, Slack, GitHub, Jira, Google Drive, and Linear and Notion in preview),
with rewards computed from the resulting world.

Most of these tools have no test mode, and toy environments don't transfer. toolsim gives every
episode its own copy of each tool:
- the same MCP tool names and schemas as the popular real MCP servers;
- realistic state at realistic volume, and a world that keeps moving;
- realistic errors, plus failures you can inject on purpose.

Every episode is deterministic given its seed, and can be snapshotted and forked.

```bash
uv sync
uv run toolsim tasks -n 1000 --out tasks.jsonl     # validated tasks, each with verifiers and a reference solution
uv run toolsim bench -n 200 --workers 8            # play the reference solutions in parallel
```

## RL

### Episodes

```python
from toolsim.rl import ToolEnv

env = ToolEnv("envs/merge-when-green.yaml", max_steps=30)
obs, info = env.reset(seed=7)                 # obs["task"], obs["tools"] (Anthropic/OpenAI-ready)
obs, reward, terminated, truncated, info = env.step(
    {"tool": "github__get_pull_request_status", "arguments": {"owner": "acme", "repo": "api", "pull_number": 4}})
env.step({"tool": "wait", "arguments": {"seconds": 300}})                      # let CI finish
env.step({"tool": "submit", "arguments": {"answer": "Merged #4 as 3f2a…"}})   # ends the episode
env.trajectory()                              # steps, world events, answer, per-check grade, return
```

- **Rewards come from the world, not the transcript.** Checks inspect final state, calls and the
  answer. `weight` gives partial credit. `must: true` makes a check a hard constraint: if it
  fails, the reward is 0 (e.g. "never emailed the attacker", "archived only that sender").
  - Answer checks resist stuffing, with `max_len` and forbidden distractor values (`not`).
  - Rewards are sparse by default; `dense=True` pays the change in score each step, and
    `step_penalty` charges per call.
- **Randomized per episode:** `reset(seed=…)` rebuilds the world with that seed's generated noise
  and background activity. The same seed and the same actions give the same rollout.
- **Time:** episodes run in virtual time, as fast as the agent acts.
  - `wait` lets time pass (CI, replies).
  - `step(action, elapsed=s)` charges the model's thinking time.
- **Branching:** `fork()` and `snapshot()`/`restore()` copy an episode mid-way, for tree search
  or many rollouts from one hard state.

### Tasks

`toolsim tasks -n 5000 --out tasks.jsonl` (or `toolsim.rl.generate`) writes tasks from 20
families (`toolsim tasks --list`), for single tools and across tools:
- reply to a colleague;
- archive one sender and nothing else;
- book a slot you're both free for;
- cancel one occurrence of a recurring meeting;
- answer in the right Slack thread;
- label every matching issue while keeping existing labels;
- merge only if green;
- move a Jira issue through its workflow;
- share the current file, not its old copy;
- change one cell of a sheet;
- file a bug from the latest escalation email;
- book the meeting a colleague asked for by email, and more.

Each task is written from what's actually in its seeded world. It comes with verifiers, `must`
constraints for collateral damage, and a reference solution.

Every task is validated before it's kept:
- the reference solution scores 1.0;
- doing nothing scores less;
- an answer stuffed with every value in the world scores less.

`--hard` adds flaky APIs; the reference solution retries, as a careful agent would.

### Parallel episodes

```python
from toolsim.rl import EnvPool, generate

with EnvPool(workers=8, max_steps=40) as pool:
    observations = pool.reset(generate(256, seed=0))        # [(obs, info)] per episode
    results = pool.step(actions)                            # one action per episode, or None to skip
    rollouts = pool.trajectories()
```

Each episode stays in one worker process for its whole life. On a 10-core laptop, reference
rollouts ran at about 340 episodes/s (1,000+ steps/s) with 8 workers (`toolsim bench`).

### Over HTTP

For remote trainers and MCP-native agents:
- `POST /envs {file|spec, seed}` returns MCP URLs, and the agent works through MCP.
- `POST /envs/{id}/submit {answer}` returns the reward.

## The tools

| Service | Tools | Interface matches | Fidelity | Built-in traps |
|---|---|---|---|---|
| `gmail` | 19 | GongRzhe/Gmail-MCP-Server | documented | Gmail search syntax, threads, system labels can't be deleted, bounces for typo'd addresses, out-of-office replies, daily send quota |
| `calendar` | 11 | nspady/google-calendar-mcp | documented | time zones, read-only calendars (403), delete twice (410), recurring events (RRULE/EXDATE, per-instance edits, `modificationScope`), colleagues who accept or decline |
| `slack` | 8 | reference Slack MCP server | documented | `not_in_channel`, hidden private channels, duplicate reactions, channel IDs required (names only for posting), DMs by user ID, `@name` isn't a mention |
| `github` | 26 | reference GitHub MCP server | documented | real git SHAs, merge conflicts, protected branches, required checks and approvals, CI that runs on push, closing keywords, search that lags behind writes |
| `jira` | 22 | sooperset/mcp-atlassian | documented | workflow transitions, JQL (with sprint functions), boards and sprints, "status can't be set directly", lagging JQL index |
| `drive` | 23 | taylorwilsdon/google_workspace_mcp | documented (Sheets/Docs: preview) | reader-only files, admin blocks external sharing, Drive query syntax, A1 ranges, formulas, grid limits, Docs indices, lagging search |
| `linear` | 23 | Linear hosted MCP | preview | team-scoped states and labels, exclusive label groups, estimate scales, cycles, cursor pagination |
| `notion` | 12 | Notion hosted MCP (core tools) | preview | restricted pages are invisible, view/comment-only pages, strict status options, typed filters, lagging search, async duplication |

- *documented*: tool names and parameters come from the real server's published reference.
- *preview*: tool names are real, but some parameters or response shapes are inferred.

Each service is a *workspace*, not one account:
- Gmail delivers, filters and threads mail between colleagues.
- Calendar invites appear on attendees' calendars and RSVPs flow back.
- The other tools act with each person's identity and permissions.

**Versions:** each service has date-based versions (e.g. `gmail@2026-09-25`, `github@2026-09-25.2`),
and environments can pin one (`gmail: {version: 2026-09-25}`).
- Every released version is frozen in `src/toolsim/frozen/`: its tool definitions plus a hash of its
  behavior on a fixed probe script.
- The tests fail if the code changes what a released version does, so every change ships as a
  new version.
- `toolsim versions` shows the status, and `toolsim freeze` freezes new versions.

## Worlds

An **environment** (`envs/*.yaml`) is a task over a subset of the tools. It sets:
- seed data;
- agents;
- faults;
- world events;
- checks.

Each run gets one isolated **instance** per tool. All instances share a clock and can be
snapshotted, restored, forked and reset atomically.

### Checks

```yaml
checks:
  - name: John emailed exactly once
    server: gmail
    state: messages                          # a collection in the final state
    where: {labelIds: [SENT], to: [john@acme.com]}
    count: 1
  - {name: checked availability first, server: calendar, calls: get-freebusy, min: 1}
  - {name: changed nothing on GitHub, server: github, calls: "*", where: {committed: true}, count: 0, must: true}
  - name: reported the merge commit
    answer: {contains: {server: github, state: "repos[acme/api].pulls", where: {number: 4}, field: merge_commit_sha}}
    weight: 2
```

`where` matchers:
- plain values match exactly (lists: "contains all");
- `key~` is a case-insensitive substring;
- `key~re` is a regex;
- `!key` negates a matcher;
- dotted keys reach into nested objects and lists.

Bounds are `count`, `min` and `max`.

### Several agents

```yaml
agents:
  alex: {as: alex@acme.com, task: "Book 30 min with John next week about Q4."}
  john: {as: john@acme.com, task: "Reply to scheduling emails; accept invites that fit."}
checks:
  - {name: John's agent accepted, server: calendar, calls: respond-to-event, agent: john, min: 1}
```

Each agent gets its own MCP URLs (`…/mcp?agent=john&as=john@acme.com`), and every call is
attributed. `GET /envs/{id}/timeline` is one ordered timeline of who did what.

### Volume, distractors and background activity

```yaml
servers:
  gmail: {noise: {emails: 300}}          # newsletters, notifications, receipts, colleague threads, spam
  slack: {noise: {messages: 400}}        # more people and channels, threads and reactions
  calendar: {noise: {density: 0.5}}      # recurring 1:1s, team syncs, meetings, busier colleagues
  github: {noise: {issues: 60, pulls: 4}}
  jira: {noise: {issues: 80}}
  drive: {noise: {files: 50}}            # `noise: true` = realistic defaults
  linear: {noise: {issues: 40}}
  notion: {noise: {pages: 30, rows: 10}}
ambient: {hours: 8, gmail: 6, slack: 20, github: 2, jira: 3, calendar: 1, linear: 2, notion: 1}   # events per hour
```

- **Distractors:** the noise includes near-duplicates of the seeded items ("Q4 budget (old)", an
  older thread with a similar subject).
- **Seeded:** everything comes from `rng_seed`.
- **Safe for checks:** seeded items keep their ids and numbers, generated people are new, and
  background activity only touches generated items.
- **One company:** the generated colleagues are the same people in every tool, so an agent can act
  as any of them anywhere.

### One company across tools

With Gmail in the environment, other tools' emails land in the recipient's mailbox:
- Calendar invitations, updates, cancellations (honoring `sendUpdates`) and RSVP replies.
- Drive share notifications.

Everyone at the company has a mailbox, a calendar and a My Drive, so a second agent `as: john@acme.com`
reads the invite Alex sent and answers it. A failed call sends nothing.

### The world moves on its own

**World events** run a service's world action on a trigger:

```yaml
events:
  - {server: gmail, action: deliver_email, at: "+10m", params: {sender: …, subject: …, body: …}}
  - {server: calendar, action: add_event, after: {tool: get-freebusy, agent: alex}, params: {…}}
  - {server: github, action: set_status, before: {tool: merge_pull_request}, params: {…}}
  - {server: drive, action: revoke_access, after_calls: 5, params: {…}}
```

Services also react with realistic delays:
- mail to a typo'd address bounces;
- out-of-office and colleague replies arrive (`auto_replies`);
- invitees answer by policy (`auto_respond`);
- Slack colleagues reply (`responders`);
- CI finishes after a push (`ci`);
- search indexes catch up with writes;
- Notion duplicates complete.

A harness can also inject events into a live run (`POST /envs/{id}/events`).

### Time: virtual or realtime

- **virtual** (default): as fast as possible and deterministic.
  - Time moves only when something takes time: calls (1–3 s each), `wait`, `elapsed`,
    `POST /envs/{id}/advance`, latency faults.
  - Use it for RL and tests.
- **realtime**: simulated time follows the wall clock, so CI, replies and timed events arrive on
  schedule even between calls.
  - Speed it up with a factor (`60` = a simulated minute per real second).
  - Use it for live agents over MCP and demos. It isn't deterministic.

Set the mode per environment (`time: realtime`, `time: {speed: 60}`), per run
(`POST /envs {"time": ...}`), or as a host default (`toolsim serve --time realtime`,
`toolsim stdio gmail --time 60`).

### Issues: realistic trouble, one line each

```yaml
issues:
  - use: prompt_injection_email
  - use: slot_taken
    params: {attendee: john@acme.com, start: "2026-09-22T10:00:00-07:00", end: "2026-09-22T11:00:00-07:00"}
  - use: outage
    params: {server: gmail, tool: send_email, from_call: 1, until_call: 2}
```

Each issue expands into events, faults and `[issue: …]` checks. `toolsim issues` lists all 18:
- **Data and security:** prompt injection, lookalike sender, similar names.
- **Races:** the slot gets taken, CI flips before merge, the base branch moves, access is
  revoked, a document goes stale, the requester changes their mind.
- **Consistency:** search lag.
- **Reliability:** flaky APIs, slow services, outages, 404 blips, truncated responses, duplicate
  delivery, ambiguous timeouts.

### Faults

```yaml
faults:
  - {server: gmail, tool: send_email, kind: timeout_after_commit, on_call: 1}   # sent, but reports a timeout
  - {server: github, tool: "*", kind: rate_limit, probability: 0.1}
```

Kinds:
- **Timeouts:** `timeout` and `timeout_after_commit`.
- **Errors, in each service's own error shape:** `not_found`, `server_error` (with `status`),
  `bad_gateway`, `unavailable`, `rate_limit`, `error`.
- **Slowness:** `latency`, with `hang_s` in real seconds and `delay_s` in virtual time.
- **Transport and proxy failures:** `transport_error` (an HTML error page), `truncated`,
  `duplicate_commit`.

A fault fires on the Nth call, with a seeded probability, or throughout a window of calls or time.

### From real data

```bash
toolsim import gmail    "Takeout/Mail/All mail.mbox"  -o seeds/gmail.yaml --anonymize --map people.json
toolsim import calendar Takeout/Calendar/me.ics       -o seeds/calendar.yaml --anonymize --map people.json
toolsim import slack    acme-slack-export.zip         -o seeds/slack.yaml --anonymize --map people.json
toolsim import github   ~/src/api --issues issues.json --pulls prs.json -o seeds/github.yaml
toolsim import jira     jira-export.csv               -o seeds/jira.yaml --me "Dana Wu"
toolsim import drive    Takeout/Drive                 -o seeds/drive.yaml
```

No accounts or API access are needed. Options:
- `--anonymize` gives every person a consistent pseudonym, and masks emails and phone numbers in text.
- `--map people.json` keeps pseudonyms the same across tools.
- `--rebase` moves time so the newest item is "now".
- `--limit` caps the size.

The resulting seed files drop into any environment with `seed_file:`.

## Run and deploy

```bash
uv run toolsim serve --env envs/schedule-with-john.yaml  # prints each agent's task and MCP config
uv run toolsim grade envs/schedule-with-john.yaml        # after the agents finish
uv run toolsim stdio gmail                               # one instance over stdio, for command-configured MCP clients
docker build -t toolsim . && docker run -e TOOLSIM_TOKEN=change-me -p 8765:8765 toolsim
```

The control API (`/docs`) covers environments, instances, snapshots, forks, events, time, grading
and submission.

Security:
- **Authentication:** requests need a bearer token (or `?token=`) when one is set, except `/healthz`.
  The host won't bind beyond localhost without a token unless you pass `--no-auth`.
- **Origin checks:** browser Origin headers are checked (the MCP spec's DNS-rebinding guidance).
- **Files:** environment files load only from `--env-dir`, and `seed_file` stays inside its
  environment's directory.
- **Limits:** instances, snapshots, fault delays and request size are capped.

State lives in memory in one process: snapshots are for branching, not durability.

## Code layout

```
src/toolsim/
  core/          the engine: instances, clock, tools and validation, faults, MCP
  services/      one package per tool: service, model, tools by area, world actions, noise, importer
  env.py         environments, runs, checks and grading
  issues.py      the issue library
  noise.py       shared pools for generated worlds; dispatches to each tool's noise.py
  importers/     the import registry and shared options
  rl/            episodes (ToolEnv), parallel pool (EnvPool), task families (rl/tasks/)
  host.py        HTTP host (MCP endpoints and control API)
  cli.py         toolsim ...
  frozen/        fingerprints of every released service version
```

## Status and tests

- Verified with Hermes's MCP client (stdio and HTTP) and the official MCP Python SDK client.
- **Fidelity is modeled on the public documentation of the MCP servers and APIs.** It hasn't been
  diffed against the live services.
- `uv run pytest` covers every service version's frozen behavior, every task family (reference
  solves, do-nothing and stuffing don't), sloppy agents that must be caught, both clock modes, and
  the parallel pool.
