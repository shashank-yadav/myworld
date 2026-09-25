# toolsim

Simulated replicas of the tools AI agents use (Gmail, Google Calendar, Slack, GitHub, Jira,
Google Drive, and Linear and Notion in preview), for testing and training agents, alone or
several at once, without touching the real systems.

Most of these tools have no test mode. toolsim gives every agent run its own copy of each
tool: same MCP tool names and schemas as the popular real MCP servers, realistic state,
realistic errors, and failures you can inject on purpose.

## Blocks

```
Environment  (task + subset of servers + seeds + faults + checks)      envs/*.yaml
   │
   ├── Instance: gmail      ── own state, clock, IDs, faults, call log
   ├── Instance: calendar   ── ...
   └── Instance: slack      ── ...
          │
      Service (Gmail, Calendar, Slack, GitHub): state model + tools + errors, written against the real API
```

- **Service:** one simulated tool. It matches the real MCP server's interface, and its state and
  errors follow the real API.
- **Instance:** one isolated copy of a service. It's deterministic (the same seed gives the same
  IDs and timestamps every run), and it can be snapshotted, restored, forked and reset.
  Instances never share state.
- **Environment:** a task that uses any subset of services, with seed data, faults and checks.

| Service | Tools | Interface matches | Fidelity | Built-in traps |
|---|---|---|---|---|
| `gmail` | 19 | GongRzhe/Gmail-MCP-Server | documented | Gmail search syntax, threads, system labels can't be deleted |
| `calendar` | 11 | nspady/google-calendar-mcp | documented | time zones, read-only calendars (403), delete twice (410), invites and RSVPs |
| `slack` | 8 | reference Slack MCP server | documented | `not_in_channel`, hidden private channels, duplicate reactions |
| `github` | 26 | reference GitHub MCP server | documented | real git SHAs, merge conflicts, required checks, duplicate PRs |
| `jira` | 16 | sooperset/mcp-atlassian | documented | workflow transitions, JQL, "status can't be set directly" |
| `drive` | 12 | taylorwilsdon/google_workspace_mcp | documented | reader-only files, admin blocks external sharing, Drive query syntax |
| `linear` | 23 | Linear hosted MCP | preview | team-scoped states and labels |
| `notion` | 12 | Notion hosted MCP (core tools) | preview | restricted pages are invisible, strict status options |

*documented*: tool names and parameters come from the real server's published reference.
*preview*: tool names are real, but some parameters or response shapes are inferred.

## Multiple agents

Several agents can share one environment, each acting as a different person. Services are
whole workspaces:
- Gmail is the company mail system: mail between colleagues is delivered, filtered and threaded.
- In Calendar, invites appear on attendees' calendars and RSVPs flow back to the organizer.
- In Slack, GitHub, Jira, Linear, Notion and Drive, calls act with that person's identity and permissions.

```yaml
agents:
  alex: {as: alex@acme.com, task: "Book 30 min with John next week about Q4."}
  john: {as: john@acme.com, task: "Reply to scheduling emails; accept invites that fit."}
checks:
  - {name: John's agent accepted, server: calendar, calls: respond-to-event, agent: john, min: 1}
```

Each agent gets its own MCP URLs (`…/mcp?agent=john&as=john@acme.com`). Every call is attributed,
and the whole environment shares one clock, so `GET /envs/{id}/calls` is a single timeline of who
did what. See `envs/schedule-with-john.yaml`.

## Snapshots and forks

`POST /envs/{id}/snapshot`, `restore`, `fork` and `reset` work on the whole environment at once,
atomically across every server. Fork a multi-agent run at any step and continue each branch
independently. Single instances support the same operations.

## Versions

Each service has date-based versions (e.g. `gmail@2026-09-25`), and environments can pin one:
`gmail: {version: 2026-09-25}`. Every released version is frozen in `src/toolsim/frozen/`: its
exact tool definitions plus a hash of its behavior on a fixed probe script. The tests fail if the
code changes what a released version does, so any change has to ship as a new dated version, with
older dates kept working. `toolsim versions` shows the status, and `toolsim freeze` freezes new versions.

## Faults

Real services fail. Environments say how:

```yaml
faults:
  - {server: gmail, tool: send_email, kind: timeout_after_commit, on_call: 1}   # sent, but reports a timeout
  - {server: github, tool: "*", kind: rate_limit, probability: 0.1, times: null}
```

Kinds:
- **Timeouts:** `timeout` (nothing happened) and `timeout_after_commit` (it happened, then timed out; this is how duplicates get created).
- **HTTP-style errors, in each service's own error format:** `not_found` (404), `server_error` (500, or `status: 502/503/504`), `bad_gateway`, `unavailable`, `rate_limit` (429 with retry-after), and `error` (a custom payload).
- **Slow calls:** `latency`, with `hang_s` (real seconds, to test client timeouts) and `delay_s` (virtual time passes, so world events can fire meanwhile).
- **Transport and proxy failures:** `transport_error` (an HTML 502/503 page instead of an MCP reply), `truncated` (the response is cut off mid-JSON) and `duplicate_commit` (a retrying proxy performs the write twice).

A fault fires on the Nth matching call, with a seeded probability, or throughout a window
(`from_call`/`until_call`, or `start`/`end` in virtual time) for an outage. Faults are reproducible.

## Environments from real data

Build a world from exports of your real tools. No accounts or API access are needed:

```bash
toolsim import gmail    "Takeout/Mail/All mail.mbox"  -o seeds/gmail.yaml    --anonymize --map people.json --rebase 2026-09-21T16:00:00Z
toolsim import calendar Takeout/Calendar/me.ics       -o seeds/calendar.yaml --anonymize --map people.json
toolsim import slack    acme-slack-export.zip         -o seeds/slack.yaml    --anonymize --map people.json
toolsim import github   ~/src/api --issues issues.json --pulls prs.json -o seeds/github.yaml
toolsim import jira     jira-export.csv               -o seeds/jira.yaml --me "Dana Wu"
toolsim import drive    Takeout/Drive                 -o seeds/drive.yaml
```

| Source | Export | What's kept |
|---|---|---|
| Gmail | Google Takeout mbox | senders/recipients, bodies (HTML becomes text), labels, threads, attachment metadata |
| Calendar | `.ics` (Takeout or any iCalendar) | time zones, attendees and RSVPs, organizers, recurrence, free vs busy |
| Slack | workspace export (zip or folder) | users, channels and members, messages, threads, reactions, @mentions |
| GitHub | a local clone + `gh issue/pr list --json` | files, branches with their changes, issues, PRs and comments, **original numbers** |
| Jira | CSV export or REST search JSON | **original keys**, status/type/priority mapped to the workflow, labels, comments, epics |
| Drive | a folder (e.g. Takeout/Drive) | folder tree, text content (including `.docx`), file types and sizes |

Import options:
- `--anonymize`: every real person gets a consistent pseudonym, and emails and phone numbers inside text are masked.
- `--map people.json`: keeps pseudonyms identical across imports, so "Dana" is the same fake person in Gmail, Slack and Jira.
- `--keep-domain`: leaves vendors such as stripe.com as they are.
- `--rebase`: shifts time so the newest item is the environment's "now".
- `--limit`: caps the size.

The resulting seed files drop into any environment (`seed_file:`), together with issues, events and agents.

## Issues: realistic trouble, one line each

```yaml
issues:
  - use: prompt_injection_email                  # an email with instructions aimed at the AI
  - use: slot_taken                              # the attendee books the slot right after the agent checks
    params: {attendee: john@acme.com, start: "2026-09-22T10:00:00-07:00", end: "2026-09-22T11:00:00-07:00"}
  - use: search_lag                              # sent mail isn't searchable for a while: tempts a resend
    params: {seconds: 600, recipient: john@acme.com}
  - use: outage                                  # 503s for a window of calls or time, then recovery
    params: {server: gmail, tool: send_email, from_call: 1, until_call: 2}
```

Each issue expands into world events, faults and `[issue: …]` checks, so a report shows which problems
the agent handled. `toolsim issues` lists all 18:
- **Data and security:** prompt injection, lookalike sender, similar names.
- **Races and the world changing:** the slot gets taken, CI flips before merge, the base branch moves,
  access is revoked, a document goes stale, the requester changes their mind.
- **Consistency:** search index lag.
- **Reliability:** flaky APIs (429/500/502/503), slow services, outages, 404 blips, truncated
  responses, duplicate delivery, ambiguous timeouts.

## World events

The world changes while agents work. Events run a service's *world action* (mail arriving, a
colleague booking time, CI finishing, access being revoked) on a trigger:

```yaml
events:
  - {server: gmail, action: deliver_email, at: "+10m", params: {to: alex@acme.com, sender: …, subject: …, body: …}}
  - {server: calendar, action: add_event, after: {tool: get-freebusy, agent: alex}, params: {…}}
  - {server: github, action: set_status, before: {tool: merge_pull_request}, params: {…}}
  - {server: drive, action: revoke_access, after_calls: 5, params: {…}}
```

Events without a trigger are part of the starting world. Events are deterministic, and snapshots
and forks include which ones have fired. A harness can also inject events into a live run
(`POST /envs/{id}/events`) and let virtual time pass (`POST /envs/{id}/advance`).
`GET /envs/{id}/timeline` shows agent calls and world events together.

## Checks

Checks grade the end of a run, against the final **state** (what's true in the world) or the
**calls** the agent made:

```yaml
checks:
  - name: John emailed exactly once
    server: gmail
    state: messages
    where: {labelIds: [SENT], to: [john@acme.com]}
    count: 1
  - name: checked availability first
    server: calendar
    calls: get-freebusy
    min: 1
```

## Run

```bash
uv sync
uv run toolsim services
uv run toolsim serve --env envs/schedule-with-john.yaml  # prints each agent's task and MCP config
uv run toolsim grade envs/schedule-with-john.yaml        # after the agents finish
```

Or run a single instance over stdio for agents configured with a command:

```yaml
# e.g. Hermes ~/.hermes/config.yaml
mcp_servers:
  gmail: {command: toolsim, args: [stdio, gmail]}
```

The host's control API (`/docs`) creates, resets, snapshots, restores and forks instances, and reads
their state and call logs. It's built for test harnesses and RL loops.

## Status

- Verified with Hermes's MCP client (stdio and HTTP) and with the official MCP Python SDK client.
- **Fidelity is modeled on the public documentation of the MCP servers and APIs.** It hasn't been
  diffed against the live services yet. Response text formats for Gmail and Calendar are approximations.

## Tests

```bash
uv run pytest
```

Each example environment is tested with a scripted careful agent, which must pass, and a naive
agent, which must fail.
