# toolsim

Simulated replicas of the tools AI agents use (Gmail, Google Calendar, Slack, GitHub), for
testing and training agents without touching the real systems.

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

| Service | Tools | Interface matches | Built-in traps |
|---|---|---|---|
| `gmail` | 19 | GongRzhe/Gmail-MCP-Server | Gmail search syntax, threads, system labels can't be deleted |
| `calendar` | 11 | nspady/google-calendar-mcp | time zones, free/busy of others, read-only calendars (403), delete twice (410) |
| `slack` | 8 | reference Slack MCP server | bot not in channel (`not_in_channel`), hidden private channels, duplicate reactions |
| `github` | 26 | reference GitHub MCP server | real git blob SHAs, merge conflicts, required checks, duplicate PRs, missing `sha` on update |

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
uv run toolsim serve --env envs/book-q4-meeting.yaml    # prints the MCP config to give your agent
uv run toolsim grade envs/book-q4-meeting.yaml           # after the agent finishes
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
