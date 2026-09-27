# Hermes

Hermes can use myworld through MCP. Start with one practice world, expose only the tools the
agent needs, then widen the surface when the workflow is stable.

## Distribution Paths

Hermes has two catalog-style paths:

- Official Hermes catalog: `NousResearch/hermes-agent` accepts PRs under `optional-mcps/`.
- Community Hermes Registry: `hermesonehq/hermes-registry` accepts entries under `mcp/<name>/`.

This repo carries both package shapes:

```text
packaging/hermes/optional-mcps/myworld/manifest.yaml
packaging/hermes-registry/mcp/myworld/manifest.json
```

Both launch the same pinned local MCP server:

```bash
uvx myworld==0.2.1 world invoice-review
```

## Local Stdio

Use this when Hermes and myworld run on the same machine.

```yaml
mcp_servers:
  myworld:
    command: "uvx"
    args: ["myworld", "world", "invoice-review"]
    tools:
      include: ["world_*", "gmail__*", "slack__*", "drive__*"]
      resources: false
      prompts: false
```

This starts one shared invoice-review company world over stdio. Hermes sees one MCP server with
namespaced Gmail, Slack and Drive tools, plus world tools for task, grading, snapshots and diffs.

For a tiny single-service smoke test, use `uvx myworld stdio gmail`.

## Hosted Or Multi-Service Worlds

Use the HTTP host when a task needs several apps in the same world.

```bash
MYWORLD_TOKEN=dev-token myworld serve --env examples/worlds/invoice-review.yaml --port 8765
```

The host prints each agent's MCP config. A Hermes config for the default agent looks like this:

```yaml
mcp_servers:
  myworld:
    url: "http://127.0.0.1:8765/instances/invoice-review-gmail/mcp?agent=agent&as=alex@acme.com"
    headers:
      Authorization: "Bearer dev-token"
    tools:
      include: ["search_emails", "read_email", "send_email"]
      resources: false
      prompts: false
```

For full company worlds over HTTP, add the other printed MCP servers (`drive`, `slack`, `calendar`,
etc.) or use the hosted myworld endpoint once it is deployed. For local stdio, prefer
`myworld world <env.yaml>` when you want one shared multi-tool world.

## Publish Checklist

- Ship a PyPI package so Hermes users can run `uvx myworld ...`.
- Keep the official Hermes optional-MCP PR assets in `packaging/hermes/`.
- Keep the community Hermes Registry entry in `packaging/hermes-registry/`.
- Publish a Docker image for teams that want a long-running HTTP host.
- Keep the default examples narrow and low-risk.
- Document tool filters for every example.
- Add a smoke test that runs Hermes against `tools/list` and one real task.
