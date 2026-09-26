# Hermes

Hermes can use myworld through MCP. Start with one practice world, expose only the tools the
agent needs, then widen the surface when the workflow is stable.

## Local Stdio

Use this when Hermes and myworld run on the same machine.

```yaml
mcp_servers:
  myworld_gmail:
    command: "uvx"
    args: ["myworld", "stdio", "gmail"]
    tools:
      include: ["search_emails", "read_email", "send_email"]
      resources: false
      prompts: false
```

This starts one Gmail-shaped practice server over stdio. It is a small first test, useful for
checking that Hermes can discover tools and call them.

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

For full company worlds, add the other printed MCP servers (`drive`, `slack`, `calendar`, etc.) or
use the hosted myworld endpoint once it is deployed.

## Publish Checklist

- Ship a PyPI package so Hermes users can run `uvx myworld ...`.
- Publish a Docker image for teams that want a long-running HTTP host.
- Keep the default examples narrow and low-risk.
- Document tool filters for every example.
- Add a smoke test that runs Hermes against `tools/list` and one real task.

