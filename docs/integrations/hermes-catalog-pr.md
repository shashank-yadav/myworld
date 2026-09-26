# Hermes Catalog PR

Hermes catalog entries live in the Hermes Agent repository under
`optional-mcps/<name>/manifest.yaml`. A merged manifest means Nous has reviewed
and approved the MCP for one-click installation.

The myworld starter entry is:

```text
packaging/hermes/optional-mcps/myworld-gmail/manifest.yaml
```

To submit it:

```bash
git clone https://github.com/NousResearch/hermes-agent.git /tmp/hermes-agent
cd /tmp/hermes-agent
git checkout -b add-myworld-gmail-mcp
mkdir -p optional-mcps/myworld-gmail
cp /path/to/myworld/packaging/hermes/optional-mcps/myworld-gmail/manifest.yaml \
  optional-mcps/myworld-gmail/manifest.yaml
python -m pytest tests/hermes_cli/test_mcp_catalog.py -q
git add optional-mcps/myworld-gmail/manifest.yaml
git commit -m "Add myworld Gmail MCP catalog entry"
git push -u <your-fork> add-myworld-gmail-mcp
```

PR title:

```text
Add myworld Gmail MCP catalog entry
```

PR body:

```text
Adds myworld-gmail as an optional MCP catalog entry.

myworld is a safe practice world for real agent work. This entry launches the
PyPI package over stdio with:

  uvx myworld stdio gmail

The server exposes a simulated Gmail mailbox. No real email is sent. The
default enabled tools support search/read/send/draft/label workflows while
leaving destructive cleanup tools opt-in through `hermes mcp configure`.

Validation:
- `uvx myworld stdio gmail` launches
- `hermes mcp add myworld-gmail --command uvx --args myworld stdio gmail`
  discovered 19 tools locally
- `hermes mcp test myworld-gmail` passed locally
```

