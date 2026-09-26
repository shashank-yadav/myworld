# Hermes Catalog PR

Hermes catalog entries live in the Hermes Agent repository under
`optional-mcps/<name>/manifest.yaml`. A merged manifest means Nous has reviewed
and approved the MCP for one-click installation.

The primary myworld catalog entry is one shared practice world:

```text
packaging/hermes/optional-mcps/myworld/manifest.yaml
```

It launches:

```text
uvx myworld==0.2.1 world invoice-review
```

That gives Hermes one MCP server with namespaced tools such as
`gmail__search_emails`, `slack__slack_get_channel_history`,
`drive__read_sheet_values`, plus `world_task`, `world_grade`, `world_snapshot`
and `world_diff`. Gmail, Slack and Drive all share the same seeded company,
clock, call log, snapshots, forks and grader.

The repo also keeps service-specific entries for users who want a small
single-tool sandbox:

```text
packaging/hermes/optional-mcps/myworld-calendar/manifest.yaml
packaging/hermes/optional-mcps/myworld-drive/manifest.yaml
packaging/hermes/optional-mcps/myworld-github/manifest.yaml
packaging/hermes/optional-mcps/myworld-gmail/manifest.yaml
packaging/hermes/optional-mcps/myworld-jira/manifest.yaml
packaging/hermes/optional-mcps/myworld-linear/manifest.yaml
packaging/hermes/optional-mcps/myworld-notion/manifest.yaml
packaging/hermes/optional-mcps/myworld-slack/manifest.yaml
```

To submit it:

```bash
git clone https://github.com/NousResearch/hermes-agent.git /tmp/hermes-agent
cd /tmp/hermes-agent
git checkout -b add-myworld-mcp-worlds
cp -R /path/to/myworld/packaging/hermes/optional-mcps/myworld optional-mcps/
cp -R /path/to/myworld/packaging/hermes/optional-mcps/myworld-* optional-mcps/
python -m pytest tests/hermes_cli/test_mcp_catalog.py -q
git add optional-mcps/myworld optional-mcps/myworld-*
git commit -m "Add myworld MCP catalog entries"
git push -u <your-fork> add-myworld-mcp-worlds
```

PR title:

```text
Add myworld MCP catalog entries
```

PR body:

```text
Adds myworld practice-world MCP catalog entries:

- myworld
- myworld-calendar
- myworld-drive
- myworld-github
- myworld-gmail
- myworld-jira
- myworld-linear
- myworld-notion
- myworld-slack

myworld is a safe practice world for real agent work. The main entry launches a
shared invoice-review company world over stdio with:

  uvx myworld==0.2.1 world invoice-review

It exposes Gmail, Slack and Drive tools in one MCP server, with namespaced tools
and shared state, snapshots, forks, call logs and grading. No real inboxes,
Slack workspaces, Drive files, Docs or Sheets are changed.

The service-specific entries are included as smaller sandboxes for users who
want only Gmail, Drive, Slack, Calendar, GitHub, Jira, Linear or Notion.

Validation:
- `uvx --refresh --from myworld==0.2.1 myworld world invoice-review` launches and exposes Gmail, Slack and Drive
- `uvx --from myworld==0.2.1 myworld stdio gmail` launches for single-service use
- Hermes catalog test passed: `uv run --group dev pytest tests/hermes_cli/test_mcp_catalog.py -q`
- `hermes mcp add myworld-gmail --command uvx --args myworld stdio gmail`
  discovered 19 tools locally
- `hermes mcp test myworld-gmail` passed locally
```
