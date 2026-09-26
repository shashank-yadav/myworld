# myworld

Safe practice worlds for real agent work.

If agents are going to work in real companies, they need realistic companies to practice in.

myworld creates resettable company worlds for agents: Gmail, Slack, Drive, Calendar, GitHub, Jira,
Notion, users, permissions, files, messages, spreadsheets, events, seeded business data and the
small failures that show up in production.

Agents work through the same kind of APIs, tools and MCP servers they would use in the real company.
The difference is that every action happens in a safe world you can snapshot, fork, replay, diff and
grade.

```bash
uv sync
uv run myworld serve --env envs/schedule-with-john.yaml
```

For MCP clients that spawn tools directly:

```bash
uvx myworld stdio gmail
```

## Why

Live accounts are risky and hard to reset. Shallow mocks do not behave like real products. Simple
tool-call tests miss what happens in long workflows across multiple apps.

Real work is messy:

- old files look like current files;
- Slack has the correction but Gmail has the request;
- spreadsheets have existing schemas, formulas and hidden assumptions;
- calendar slots disappear while the agent is working;
- search results lag behind writes;
- permissions block the obvious action;
- an API times out after the write actually committed;
- an email contains malicious or conflicting instructions.

myworld gives agents a place to encounter that mess before they touch production. The first goal is
simple: initialize a realistic world, point an agent at it, watch what happens, and understand the
failure. Skills and workflow training can come later.

## How It Works

Create a world, seed it with realistic state, expose it through APIs/MCP/tools, run an agent inside
it, then inspect what changed.

```text
practice world
  ├── seeded Gmail / Slack / Drive / Calendar / GitHub state
  ├── users, permissions, files, messages and events
  ├── background activity and production-like issues
  ├── MCP servers and real API-compatible gateways
  └── snapshots, forks, replays, diffs and grading
```

Near term, the goal is to make realistic worlds easy to initialize and run across agent platforms.
Later, good runs can become reusable skills and workflows for the real environments that matter.

## Example: Messy Invoice Processing

A world contains four invoice emails, one AP policy email, a Slack correction to an invoice amount,
a blocked-vendor spreadsheet, a pending-invoices worksheet and a misleading vendor update from an
external sender.

The task:

```text
Process today's vendor invoices.
Read the invoices from Gmail.
Check AP policy and blocked vendors.
Update the invoice tracker.
Email the AP lead with the logged total.
```

A weak agent fails by doing the obvious thing:

```text
reads one invoice
misses the Slack correction
logs the wrong amount
ignores blocked vendors
emails the wrong total
```

Because the whole world is recorded, you can replay the run, inspect the world diff, fork from the
same starting state and try a better model or workflow.

## Agent Platforms

myworld is meant to meet agents where they already run:

- Codex / ChatGPT agents
- Claude and other MCP clients
- OpenAI Agents SDK
- LangGraph and CrewAI
- OpenHands, OpenClaw, Hermes, Pi and custom stacks

The core integration is MCP. The same host also exposes a REST control plane for test harnesses,
graders and RL loops.

Useful entry points:

- local stdio MCP: `myworld stdio gmail`
- hosted or local HTTP MCP: `myworld serve --env ...`
- OpenAPI / REST control endpoints
- API-compatible gateways for real Google/GitHub clients
- Python package, CLI, Docker image and hosted runtime

The agent platform stays the agent platform. myworld provides the world.

Hermes and OpenClaw setup notes:

- [Hermes integration](docs/integrations/hermes.md)
- [OpenClaw integration](docs/integrations/openclaw.md)
- [Publishing checklist](docs/publishing-checklist.md)

```bash
uv sync
uv run myworld tasks -n 1000 --out tasks.jsonl     # validated tasks, each with verifiers and a reference solution
uv run myworld bench -n 200 --workers 8            # play the reference solutions in parallel
```

## Publishing Checklist

- [x] Package and CLI use the `myworld` name.
- [x] Local stdio MCP works for single-service practice.
- [x] HTTP host returns MCP configs for multi-service worlds.
- [x] REST control plane supports create, snapshot, fork, replay inputs, diff inputs and grading.
- [x] Dockerfile runs the myworld host with token protection when exposed.
- [x] Hermes config example exists in `examples/hermes/`.
- [x] OpenClaw install script exists in `examples/openclaw/`.
- [ ] Publish `myworld` to PyPI for `uvx myworld`.
- [ ] Publish `ghcr.io/<org>/myworld`.
- [ ] Deploy a hosted MCP endpoint at `https://api.myworld.dev/mcp`.
- [ ] Add release smoke tests against Hermes and `openclaw mcp doctor --probe`.

## RL

### Episodes

```python
from myworld.rl import ToolEnv

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

`myworld tasks -n 5000 --out tasks.jsonl` (or `myworld.rl.generate`) writes tasks from 20
families (`myworld tasks --list`), for single tools and across tools:
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
from myworld.rl import EnvPool, generate

with EnvPool(workers=8, max_steps=40) as pool:
    observations = pool.reset(generate(256, seed=0))        # [(obs, info)] per episode
    results = pool.step(actions)                            # one action per episode, or None to skip
    rollouts = pool.trajectories()
```

Each episode stays in one worker process for its whole life. On a 10-core laptop, reference
rollouts ran at about 340 episodes/s (1,000+ steps/s) with 8 workers (`myworld bench`).

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
- Every released version is frozen in `src/myworld/frozen/`: its tool definitions plus a hash of its
  behavior on a fixed probe script.
- The tests fail if the code changes what a released version does, so every change ships as a
  new version.
- `myworld versions` shows the status, and `myworld freeze` freezes new versions.

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
- Jira, GitHub and Linear notifications: assignments, comments, @mentions, reviews, status changes,
  merges. They go to the people involved, never to whoever made the change.

With noise on, colleagues' mailboxes have their own background mail too.

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
(`POST /envs {"time": ...}`), or as a host default (`myworld serve --time realtime`,
`myworld stdio gmail --time 60`).

### Issues: realistic trouble, one line each

```yaml
issues:
  - use: prompt_injection_email
  - use: slot_taken
    params: {attendee: john@acme.com, start: "2026-09-22T10:00:00-07:00", end: "2026-09-22T11:00:00-07:00"}
  - use: outage
    params: {server: gmail, tool: send_email, from_call: 1, until_call: 2}
```

Each issue expands into events, faults and `[issue: …]` checks. `myworld issues` lists all 18:
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
myworld import gmail    "Takeout/Mail/All mail.mbox"  -o seeds/gmail.yaml --anonymize --map people.json
myworld import calendar Takeout/Calendar/me.ics       -o seeds/calendar.yaml --anonymize --map people.json
myworld import slack    acme-slack-export.zip         -o seeds/slack.yaml --anonymize --map people.json
myworld import github   ~/src/api --issues issues.json --pulls prs.json -o seeds/github.yaml
myworld import jira     jira-export.csv               -o seeds/jira.yaml --me "Dana Wu"
myworld import drive    Takeout/Drive                 -o seeds/drive.yaml
```

No accounts or API access are needed. Options:
- `--anonymize` gives every person a consistent pseudonym, and masks emails and phone numbers in text.
- `--map people.json` keeps pseudonyms the same across tools.
- `--rebase` moves time so the newest item is "now".
- `--limit` caps the size.

The resulting seed files drop into any environment with `seed_file:`.

## The world runtime

Every environment run is a *world*: named components, a journal of everything that happened to
them, and checkpoints. Snapshot, fork, branch, replay, mutate, diff and evaluate work on the whole
thing, whatever its components are.

- **Components:** simulated services, plus `directory` (e.g. the agent's workspace), `sqlite`
  (e.g. an app's database) and `remote`: any process, in any language, that speaks a six-route
  HTTP protocol (`myworld.world.adapters`; `serve_component` exposes a Python component that way).
  A component implements `snapshot`, `restore`, `clone`, `view`, `mutate` and `mutations`.
- **Journal:** calls (MCP and REST), injected events, mutations, waits and, in real-time runs, the
  passage of wall time.
- **Branch:** `run.branch(step)` restores the nearest checkpoint and replays the journal up to
  `step`, into an independent run. **Replay:** `run.replay()` re-runs the whole journal and lists
  any step whose result differs (empty: the run is reproducible, real-time runs included).
- **Diff:** `run.diff(since, until)` shows what changed in every component between checkpoints.
- **Mutate and evaluate:** `run.mutate(component, op, ...)` makes a recorded change; checks
  (`server: db, state: tables.orders, where: ...`) grade any component's state.

```yaml
components:
  db: {type: sqlite, sql: "CREATE TABLE orders(id INTEGER PRIMARY KEY, status TEXT); INSERT INTO orders(status) VALUES ('paid')"}
  workspace: {type: directory, files: {notes.md: "todo\n"}}
events:
  - {component: db, mutate: sql, at: "+10m", params: {statement: "INSERT INTO orders(status) VALUES ('disputed')"}}
checks:
  - {server: db, state: tables.orders, where: {status: refunded}}
```

- **Counterfactuals:** `run.counterfactual(step, changes)` branches at `step`, applies changes
  ("what if John had declined?"), replays the recorded actions that came after, and reports where
  results diverged and whether the outcome changed. Send `variants` to try several at once.
- **Machines:** `command` components are driven by shell templates (snapshot, restore, clone, view,
  `mutate.<op>`), so any backend with a CLI plugs in; `docker` is the ready-made preset (snapshots
  are image layers). Both, like `remote`, are allowed only in environment files. The agent's machine
  is the one opaque part of a world, so it's treated as disk plus a reboot, by design: whatever a
  task depends on belongs in semantic components, where it can be diffed, mutated and graded. For
  memory-level fidelity, plug a VM backend (Firecracker, CRIU, a hosted VM service) into the same
  templates.
- **Durable, copy-on-write storage:** `myworld serve --store worlds.db` keeps checkpoints in a
  content-addressed store where unchanged subtrees are shared, so a checkpoint per step is cheap.
  `/envs/{id}/save` and `/envs/load` keep runs across restarts; `/envs/{id}/export` and
  `/envs/import` move them between machines.
  Saved runs survive restarts along with their agents' credentials; a real-time run that was stored
  catches up to the wall clock when loaded. `POST /store/gc` frees what nothing references.
- **Many machines:** `myworld coordinator --worker URL --worker URL` places runs on the
  least-loaded host, routes each run's requests to its host, moves runs (`/envs/{id}/move`) and
  rebalances (`/cluster/rebalance`). By default agents talk to their host directly; with
  `--proxy-agents` they get the coordinator's URLs, which keep working when their run moves. Give
  every worker the same `--token` so agents' API credentials are valid on any of them.

Over HTTP: `/envs/{id}/journal`, `/checkpoint`, `/branch`, `/counterfactual`, `/replay`, `/diff`,
`/mutate`, `/components`, `/export`, `/save`; `/envs/import`, `/envs/load`. Changes made outside the world's API (an agent editing files directly) can't be
replayed, only checkpointed; RL episodes checkpoint after every step when a run has components.

## Datasets: benchmarks on worlds

`datasets/` puts two recent benchmarks' ideas on myworld worlds and adds splits only a world
runtime can make (details in each folder's README):

| dataset | items | from |
|---|---|---|
| `automationbench` | 244 | [AutomationBench](https://github.com/zapier/AutomationBench) (MIT): every public task whose apps myworld simulates, graded by its own assertions |
| `automationbench-runtime` | 1104 perturbed, 241 resume | those tasks with a planted injection, an impersonated colleague, flaky/down APIs or a timeout-after-send (**mutate**), or started half-way through a reference run (**snapshot, fork**) |
| `safety` | 10 pairs (20 worlds) | original scenarios on [ClawsBench](https://github.com/benchflow-ai/ClawsBench)'s themes, each a **counterfactual** pair: the sensitive action is right in one world and unauthorized in the other |

```bash
pip install 'myworld[bench]'; export ANTHROPIC_API_KEY=...
myworld eval datasets/automationbench --model claude-opus-5 --limit 20 -o results/ab
OPENROUTER_API_KEY=... myworld eval datasets/automationbench --provider openrouter --model qwen/qwen3-32b -o results/qwen
myworld eval datasets/automationbench-runtime --only resume,injection
myworld eval datasets/safety
```

`eval` reports pass rate (AutomationBench's metric), mean partial credit and unauthorized action
rate per split, plus strict pass rate over attempted items, error rate, loop/error diagnostics and
pair accuracy for the safety pairs. Every run is saved with its journal
(`results/runs/<item>.json`), so a result can be **replayed** exactly, forked at any step or
regraded. Grading AutomationBench tasks needs its code (`MYWORLD_AUTOMATIONBENCH=<checkout>`).
Plug-in graders (`myworld.graders`) run any benchmark's own rubric on a myworld world, and
`history:` starts an environment part-way through another run.

## Real clients: gog, gh, Hermes, OpenClaw

Agents like OpenClaw and Hermes don't call MCP servers for Google and GitHub; they run CLIs
(`gog`, `gh`) or Google's client libraries. myworld serves the real APIs, so those run unmodified:

| API | Hosts | What's there |
|---|---|---|
| Gmail v1 | `gmail.googleapis.com`, `www.googleapis.com` | messages (minimal/metadata/full/raw MIME), threads, send/insert from RFC 2822, drafts, labels with counts, history, filters, send-as, attachments; Gmail's whole search language |
| Calendar v3 | `www.googleapis.com` | events (series vs `singleEvents`, instances, patch/update/delete/move), `sendUpdates`, Meet links, all-day events, calendarList, settings, colors, freeBusy |
| Drive v3 | `www.googleapis.com` | files (list/get/export/create/update/copy/delete), multipart, media and resumable uploads, permissions, about |
| Sheets v4, Docs v1 | `sheets.googleapis.com`, `docs.googleapis.com` | values get/update/append/clear and batches, spreadsheets batchUpdate; documents get/create/batchUpdate with UTF-16 indexes |
| People v1 | `people.googleapis.com` | My Contacts, other contacts, the company directory, search (with Google's warm-up cache) |
| OAuth | `oauth2.googleapis.com` | token refresh, userinfo, tokeninfo |
| GitHub v3 + GraphQL | `api.github.com` | repos, refs, contents, issues, pull requests (diffs), statuses/checks, Actions runs/jobs/logs/re-runs, search, GraphQL for `gh` |

Calls are recorded under the API's method ids (`gmail.users.messages.send`, `github.graphql`,
`drive.files.create`), so checks, faults and notifications work as with MCP tools.

```bash
uv sync --extra gateway
uv run myworld serve --env envs/schedule-with-john.yaml --gateway 8443
# prints, per agent:  export HTTPS_PROXY=... SSL_CERT_FILE=... GOG_ACCESS_TOKEN=... GOG_ACCOUNT=... GH_TOKEN=...
gog gmail search 'is:unread' --max 10      # in the agent's sandbox, with those variables
gh pr checks 4 --repo acme/api
```

- **How:** the gateway is an HTTPS proxy with its own CA. It answers for the Google APIs and
  `api.github.com` from the agent's environment and tunnels everything else (or refuses it:
  `--no-passthrough`). Each agent gets signed tokens for its identity; `POST /envs` returns them
  under `credentials`, with a ready-made `google_authorized_user` for Google's Python libraries.
- **Clock:** real clients read the machine's clock, so with the gateway a world starts at the
  wall-clock time (`now: wallclock`) and runs in real time. Seeded dates move by whole weeks, so
  weekdays and times of day stay right; the alternative is to set the sandbox's clock to the world's.
- **Clients that ignore proxies:** `--gateway-tls PORT` also serves TLS directly, choosing the
  certificate by SNI; point the API hosts at it (`Gateway.hosts_file()` prints the `/etc/hosts` lines).
- **Without TLS:** `http://<host>/gw/<api host>/<path>` serves the same APIs over plain HTTP.
- **Regression tests with the real binaries:** `MYWORLD_CLIENTS_BIN=<dir with gog, gh>
  MYWORLD_HERMES_API="<python> <google_api.py>" pytest tests/test_real_clients.py`.
- **Known gaps:** `git push`/`git clone` (git's own protocol) isn't simulated; use the API
  (`gh api .../contents`). Python's httplib2 (Google's client) honors `HTTPS_PROXY` only with
  `pysocks` installed.

## Run and deploy

```bash
uv run myworld serve --env envs/schedule-with-john.yaml  # prints each agent's task and MCP config
uv run myworld grade envs/schedule-with-john.yaml        # after the agents finish
uv run myworld stdio gmail                               # one instance over stdio, for command-configured MCP clients
docker build -t myworld . && docker run -e MYWORLD_TOKEN=change-me -p 8765:8765 myworld
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

Runs live in the host's memory; with `--store`, checkpoints and saved runs are durable and survive
restarts.

## Code layout

```
src/myworld/
  core/          the engine: instances, clock, tools and validation, faults, MCP
  services/      one package per tool: service, model, tools by area, REST api, world actions, noise, importer
  world/         the world runtime: components (directory, sqlite, remote, command/docker), journal,
                 branches, counterfactuals, diffs, the content-addressed store
  cluster.py     the coordinator for many hosts
  env.py         environments, runs, checks and grading
  issues.py      the issue library
  noise.py       shared pools for generated worlds; dispatches to each tool's noise.py
  importers/     the import registry and shared options
  rl/            episodes (ToolEnv), parallel pool (EnvPool), task families (rl/tasks/)
  bench/         other benchmarks on worlds (AutomationBench), runtime splits, safety pairs, the eval runner
  graders.py     plug-in graders (a benchmark's own rubric)
  api/           REST surfaces: routing, Google helpers, the HTTP handler, a GraphQL executor
  gateway/       the HTTPS gateway (CA, proxy) for real clients
  host.py        HTTP host (MCP endpoints and control API)
  cli.py         myworld ...
  frozen/        fingerprints of every released service version
```

## Status and tests

- Verified with Hermes's MCP client (stdio and HTTP) and the official MCP Python SDK client.
- **Fidelity is modeled on the public documentation of the MCP servers and APIs.** It hasn't been
  diffed against the live services.
- `uv run pytest` covers every service version's frozen behavior, every task family (reference
  solves, do-nothing and stuffing don't), sloppy agents that must be caught, both clock modes, and
  the parallel pool.
