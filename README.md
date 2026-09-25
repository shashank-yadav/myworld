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
- `timeout`: nothing happens.
- `timeout_after_commit`: the action happens, then the caller sees a timeout. This is how duplicates get created.
- `server_error`
- `rate_limit`
- `error`: a custom payload.
- `latency`

Faults are seeded and reproducible.

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
