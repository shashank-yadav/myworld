# Publishing Checklist

This is the release checklist for making myworld easy to use from Hermes, OpenClaw and other MCP
clients.

## Package

- [x] `pyproject.toml` uses `name = "myworld"`.
- [x] The console command is `myworld`.
- [x] Source imports use `myworld`.
- [x] Docker starts `myworld serve`.
- [ ] Publish the package to PyPI.
- [ ] Verify `uvx myworld --help` from a clean machine.

## MCP

- [x] Local stdio MCP works through `myworld stdio <service>`.
- [x] HTTP MCP endpoints are available from `myworld serve`.
- [x] Multi-service worlds print per-agent MCP configs when started with `--env`.
- [ ] Add a hosted aggregate MCP endpoint with world tools such as `world_create`, `world_fork`,
      `world_replay`, `world_diff` and `world_grade`.
- [ ] Add release smoke tests for `initialize`, `tools/list`, one Gmail call, one Drive call and
      `grade`.

## Hermes

- [x] Add `docs/integrations/hermes.md`.
- [x] Add `examples/hermes/mcp.yaml`.
- [ ] Run Hermes against the stdio example and capture the exact setup output.
- [ ] Run Hermes against a multi-service world over HTTP.
- [ ] Publish a short example task showing the agent failure and replay.

## OpenClaw

- [x] Add `docs/integrations/openclaw.md`.
- [x] Add `examples/openclaw/install.sh`.
- [ ] Run `openclaw mcp doctor myworld-gmail --probe` from a clean install.
- [ ] Run the hosted config with `openclaw mcp set ...`.
- [ ] Add an OpenClaw smoke run to release notes.

## Hosted Runtime

- [ ] Publish `ghcr.io/<org>/myworld`.
- [ ] Deploy `https://api.myworld.dev`.
- [ ] Put `/healthz` behind monitoring.
- [ ] Require bearer tokens for non-localhost hosts.
- [ ] Document retention for stored worlds, snapshots and journals.

## Release Gate

- [ ] `uv sync --frozen`
- [ ] `uv run pytest -q`
- [ ] `uvx ruff check src tests`
- [ ] `uv build`
- [ ] `uv run myworld versions`
- [ ] Load `examples/worlds/invoice-review.yaml`

