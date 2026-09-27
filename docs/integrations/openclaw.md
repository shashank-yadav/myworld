# OpenClaw

OpenClaw can use myworld as an MCP server. The core OpenClaw repo is not the right place for this
integration because myworld is an optional practice environment, not a missing core API. The right
publish path is ClawHub.

## ClawHub Package

The ClawHub-ready package lives at:

```text
packaging/clawhub/openclaw-myworld
```

It declares one stdio MCP server:

```bash
uvx myworld==0.2.1 world invoice-review
```

Publish flow:

```bash
npm i -g clawhub
clawhub login
clawhub package validate ./packaging/clawhub/openclaw-myworld
clawhub package publish ./packaging/clawhub/openclaw-myworld --family code-plugin --dry-run
clawhub package publish ./packaging/clawhub/openclaw-myworld --family code-plugin --wait
```

After publication:

```bash
openclaw plugins install clawhub:@shashank-yadav/openclaw-myworld
```

## Local Stdio

For users who do not want the ClawHub package yet, the direct MCP command still works:

```bash
openclaw mcp add myworld \
  --command uvx \
  --arg myworld==0.2.1 \
  --arg world \
  --arg invoice-review \
  --include 'world_*,gmail__*,slack__*,drive__*'

openclaw mcp doctor myworld --probe
```

That gives OpenClaw one shared company world. For a tiny smoke test, use
`uvx myworld stdio gmail` and include `search_emails,read_email,send_email`.

## Hosted HTTP

```bash
openclaw mcp set myworld \
  '{"url":"https://api.myworld.dev/mcp","transport":"streamable-http","headers":{"Authorization":"Bearer <token>"}}'

openclaw mcp probe myworld
openclaw mcp tools myworld --include 'world_*,gmail__*,drive__*,slack__*'
```

The hosted endpoint should expose world-control tools (`world_create`, `world_fork`,
`world_replay`, `world_diff`, `world_grade`) plus namespaced work tools for the active world.

## Publish Checklist

- [x] Package as a ClawHub code plugin instead of an OpenClaw core PR.
- [x] Pin the Python package launched through `uvx`.
- [x] Document runtime, permissions, secrets and smoke test.
- [x] Run `clawhub package validate ./packaging/clawhub/openclaw-myworld`.
- [x] Run `clawhub package publish ./packaging/clawhub/openclaw-myworld --family code-plugin --dry-run`.
- [ ] Publish after ClawHub login/token setup.
- [ ] Add `openclaw mcp doctor --probe` to release testing.
