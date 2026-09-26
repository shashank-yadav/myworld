# OpenClaw

OpenClaw can save myworld as an MCP server and probe it before an agent uses it. The clean path is
to publish both a local stdio command and a hosted `streamable-http` endpoint.

## Local Stdio

```bash
openclaw mcp add myworld-gmail \
  --command uvx \
  --arg myworld \
  --arg stdio \
  --arg gmail \
  --include 'search_emails,read_email,send_email'

openclaw mcp doctor myworld-gmail --probe
```

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

- Provide a copy-paste `openclaw mcp add` command for local use.
- Provide a copy-paste `openclaw mcp set` command for the hosted endpoint.
- Add `openclaw mcp doctor --probe` to release testing.
- Keep examples scoped with `--include` filters.
- Add a short "known messy failures" page so users can see why practice worlds matter.

