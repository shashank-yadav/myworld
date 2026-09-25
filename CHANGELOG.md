# Changelog

Service behavior is versioned separately by date (`toolsim versions`); this file tracks the package.

## 0.2.0 (2026-09-25)

Production hardening:
- Tool calls never leave partial writes: any failure, including bugs in a simulated tool, rolls back and
  returns a 500-style error in the service's own shape. Arguments are validated deeply (item types, objects).
- Host security: optional bearer-token auth (`--token` / `TOOLSIM_TOKEN`, required when binding beyond
  localhost), Origin validation (MCP DNS-rebinding guidance), environment files only from `--env-dir`,
  inline specs can't read files, `seed_file` confined to the environment's directory, YAML errors never
  echo file content.
- Limits: instances, snapshots (oldest evicted), fault hang/delay caps, request body size, validated ids.
- MCP calls run off the event loop, so a slow call can't stall other clients; registries are thread-safe.
- Snapshot restores check service/version (or environment) compatibility; stale snapshots return 4xx.
- Clear CLI errors instead of tracebacks; `/healthz`; JSON error bodies with a reference id.
- Rollback copies are ~3x faster on large worlds.
- CI (lint, tests on 3.11 to 3.13, frozen-version check, build) and a non-root Docker image.

Features since 0.1.0: Jira, Drive, Linear and Notion (preview); date-based service versions; multiple agents
with identities; a shared clock with whole-environment snapshots and forks; world events; reliability
faults; an issue library; importers for real tool exports.

## 0.1.0

Gmail, Calendar, Slack and GitHub simulators; environments with faults and checks.
