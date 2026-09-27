# Hermes Registry

Hermes has two useful distribution paths for myworld:

- official Hermes catalog PRs in `optional-mcps/`, which are reviewed and merged into
  `NousResearch/hermes-agent`;
- the community Hermes Registry at `registry.hermesone.org`, which indexes MCPs, skills, agents
  and workflows from `hermesonehq/hermes-registry`.

This folder prepares the community registry entry:

```text
packaging/hermes-registry/mcp/myworld/manifest.json
```

It launches the same pinned practice-world MCP as the official catalog entry:

```bash
uvx myworld==0.2.1 world invoice-review
```

The registry is a catalog, not a package mirror. This manifest points at the already-published
PyPI package and the upstream source repo. Users get a local synthetic Gmail, Slack and Drive world
with world tools for task, grading, submit, snapshots, restore, diffs and call history.

## Submit

```bash
git clone https://github.com/hermesonehq/hermes-registry.git /tmp/hermes-registry
cp -R packaging/hermes-registry/mcp/myworld /tmp/hermes-registry/mcp/
cd /tmp/hermes-registry
pip install -r requirements.txt
python scripts/validate.py
git checkout -b add-myworld-mcp
git add mcp/myworld
git commit -m "Add myworld MCP"
git push -u <your-fork> add-myworld-mcp
```

PR title:

```text
Add myworld MCP
```

PR summary:

```text
Adds myworld, a safe multi-tool practice-world MCP for Hermes. It launches the pinned PyPI package
with `uvx myworld==0.2.1 world invoice-review`, exposing a synthetic invoice-review company world
with Gmail, Slack, Drive and world-control tools. No real accounts or workspaces are touched.
```
