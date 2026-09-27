# myworld for OpenClaw

myworld gives OpenClaw agents a safe company to practice in before they touch real accounts.

This ClawHub package installs one MCP server:

```bash
uvx myworld==0.2.1 world invoice-review
```

The server exposes a seeded invoice-review world with Gmail, Slack and Drive data plus world tools
for reading the task, grading, submitting, snapshots, restores, diffs and call history. The data is
local and synthetic. No real inbox, workspace or Drive is used.

## Install

```bash
openclaw plugins install clawhub:@shashank-yadav/openclaw-myworld
```

Local development install:

```bash
openclaw plugins install --link ./packaging/clawhub/openclaw-myworld
```

## Example

Ask your OpenClaw agent:

```text
Use the myworld tools. Read the current world task, process the invoices, update the tracker and
submit when done.
```

The world intentionally contains ordinary production mess:

- invoice details split between Gmail and Slack;
- a spreadsheet with existing rows and blocked vendors;
- old or irrelevant messages in search results;
- world-control tools for replaying what happened after the run.

## Runtime And Permissions

- Runtime: stdio MCP.
- Command: `uvx myworld==0.2.1 world invoice-review`.
- Network: `uvx` may download the pinned `myworld` PyPI package if it is not already cached.
- Secrets: none.
- Real user data: none.
- Writes: only inside the synthetic practice world process.

## Smoke Test

```bash
uvx --from myworld==0.2.1 myworld world invoice-review
```

An MCP client should be able to initialize the server and call `world_task`. In OpenClaw, run:

```bash
openclaw mcp doctor myworld --probe
openclaw mcp tools myworld --include 'world_*,gmail__*,drive__*,slack__*'
```

## Publish

```bash
npm i -g clawhub
clawhub login
clawhub package validate ./packaging/clawhub/openclaw-myworld
clawhub package publish ./packaging/clawhub/openclaw-myworld --family code-plugin --dry-run
clawhub package publish ./packaging/clawhub/openclaw-myworld --family code-plugin --wait
```
