# Hermes Catalog PR

Hermes catalog entries live in the Hermes Agent repository under
`optional-mcps/<name>/manifest.yaml`. A merged manifest means Nous has reviewed
and approved the MCP for one-click installation.

The myworld catalog entries are:

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
cp -R /path/to/myworld/packaging/hermes/optional-mcps/myworld-* optional-mcps/
python -m pytest tests/hermes_cli/test_mcp_catalog.py -q
git add optional-mcps/myworld-*
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

- myworld-calendar
- myworld-drive
- myworld-github
- myworld-gmail
- myworld-jira
- myworld-linear
- myworld-notion
- myworld-slack

myworld is a safe practice world for real agent work. Each entry launches the
PyPI package over stdio with:

  uvx myworld stdio <service>

These servers expose simulated company tools. No real inboxes, calendars,
repositories, Jira projects, Linear workspaces, Notion pages, Slack workspaces,
Drive files, Docs or Sheets are changed.

The default enabled tool sets are intentionally focused so users can try
realistic workflows without loading every available tool. Users can opt into
the rest with `hermes mcp configure <entry>`.

Validation:
- `uvx myworld stdio gmail` launches
- `hermes mcp add myworld-gmail --command uvx --args myworld stdio gmail`
  discovered 19 tools locally
- `hermes mcp test myworld-gmail` passed locally
```
