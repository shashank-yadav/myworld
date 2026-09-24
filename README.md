# Agent Operations

See everything your agents do, especially what they change in the real world.

Agent Operations is observability for autonomous agents like Hermes and OpenClaw. A lightweight plugin
records every LLM call, tool/MCP call, browser action, terminal command, memory event, subagent and
error. A local collector stores them in an append-only event log and derives runs, traces, an external
change feed, and cost/latency breakdowns.

```
Hermes plugin ───┐
                 ├→ Collector → Event log (SQLite, append-only) → Trace engine → Dashboard
OpenClaw plugin ─┘
```

Everything runs on your machine. Prompts and tool results never leave it.

## Quick start

```bash
uv sync
uv run aops serve --demo        # http://127.0.0.1:4319 with sample runs
```

Connect Hermes:

```bash
uv run aops install hermes      # copies a self-contained plugin to ~/.hermes/plugins/aops
hermes plugins enable aops
```

Or install the package into Hermes' venv (`pip install agent-operations`). It registers through the
`hermes_agent.plugins` entry point.

Connect OpenClaw:

```bash
openclaw plugins install ./integrations/openclaw
openclaw plugins enable aops
```

Instrument anything else with the Python SDK:

```python
from aops.sdk import Client

aops = Client(source="my-agent")
with aops.run("Book meeting with John", agent="assistant", user="shashank") as run:
    with run.span("tool", "calendar_create_event", args=event) as step:
        step.set(result=calendar.create(event))
```

## What you get

| Screen | Shows |
|---|---|
| **Runs** | One line per task: `Book meeting with John · 18 calls · 2 external changes · 1 failure · $0.31` |
| **Trace** | The causal tree (LLM → the tools it called → subagents) with a timeline. Click any step to see its inputs, outputs, latency, model, tokens, cost and outcome |
| **External changes** | `12 emails sent · 4 calendar events created · 3 files modified · 2 actions failed · 1 action outcome unknown ⚠`, and a list of what needs review |
| **Cost & latency** | Spend, tokens, p50/p95 latency and error rate by model, agent, tool, MCP server, workflow and user |

### Unknown outcomes

A write that times out is not marked failed. It is marked **⚠ outcome unknown: external side effect may
have occurred**. The same applies to ambiguous errors (connection reset, 502/503/504) and to side-effecting
steps that started but never reported back before the run ended. Definite errors (400/403/404, validation)
are marked failed, and read-only calls that time out are just failures. This is the basis for later work
on reconciliation and duplicate prevention.

### How steps are classified

`aops/classify.py` decides whether each step read, wrote, deleted, sent or executed, and against which
system (email, calendar, GitHub, filesystem, …). It uses tool names, MCP server names, arguments and
parsed shell commands (`git push`, `rm`, `curl -X POST`, `gh issue create`, `kubectl delete`, …).
Memory writes are recorded but not counted as external changes. Browser clicks are recorded as
`execute`, because they may or may not change anything. An integration can override the classification
with `attrs.effect` / `attrs.system` / `attrs.object`.

## Event format (`aops.v1`)

Integrations POST batches to `/v1/events`:

```json
{"events": [
  {"id": "uuid", "ts": 1727190000.1, "type": "span.start", "run_id": "turn-42",
   "span_id": "tool:call_9", "parent_span_id": "llm:req_3", "kind": "mcp",
   "name": "gmail_send_email", "source": "hermes", "attrs": {"args": {"to": "john@acme.com"}}},
  {"id": "uuid", "ts": 1727190030.1, "type": "span.end", "run_id": "turn-42",
   "span_id": "tool:call_9", "attrs": {"status": "timeout", "error": "ReadTimeout"}}
]}
```

`type` is one of `run.start`, `run.end`, `span.start`, `span.end`, `span.event`. `kind` is one of `llm`,
`tool`, `mcp`, `browser`, `terminal`, `memory`, `subagent`, `approval`, `message`, `agent`. Event ids make
ingestion idempotent. SQLite triggers reject every UPDATE and DELETE on the log. See `src/aops/schema.py`.

## Privacy

- The collector binds to `127.0.0.1` and stores everything in `~/.aops/events.db` (`AOPS_HOME` changes the location).
- Before anything leaves the agent process, the plugins redact obvious secrets (API keys, tokens,
  bearer headers, secret-named fields) and cap each field at 32k characters.
- `AOPS_CAPTURE_CONTENT=0` records sizes instead of prompts, arguments and results.
- If the collector is down, events spool to `~/.aops/spool/` and are sent when it comes back.
- Cloud upload does not exist yet. When it ships, it will be opt-in.

## Cost

Claude models are priced from Anthropic list prices. An integration can report cost directly
(`attrs.cost_usd` or `usage.cost`, which OpenRouter provides). Any other model needs an entry in
`~/.aops/pricing.json` (USD per 1M tokens) and is shown as unpriced until it has one:

```json
{"gpt-5": {"input": 1.25, "output": 10.0, "cache_read": 0.125}}
```

## Layout

```
src/aops/
  schema.py        event format + validation
  store.py         append-only SQLite event log
  classify.py      read/write/delete/send classification, ambiguous-error detection
  trace.py         runs, span trees, outcomes, change feed, breakdowns
  pricing.py       usage normalization + cost
  server.py        collector + query API (FastAPI)
  sdk.py           stdlib-only client: batching, redaction, disk spool
  integrations/hermes.py   Hermes observer-hook plugin
  dashboard/index.html     the UI (single file, no build step)
  cli.py           aops serve | demo | install | tail
integrations/openclaw/     OpenClaw plugin (TypeScript)
```

## Tests

```bash
uv run pytest                              # unit tests
cd integrations/openclaw && npm test       # OpenClaw plugin
```

### Live tests against a real Hermes

`tests/live` drives a real Hermes Agent on a local model and checks what aops recorded against
ground truth: files on disk, a fake email/calendar MCP server's log, and a local HTTP server that
accepts a POST and then stalls. The tests run in an isolated `HERMES_HOME`, so your own Hermes
config is untouched.

```bash
brew install ollama
OLLAMA_CONTEXT_LENGTH=65536 OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0 ollama serve &
ollama pull qwen3:4b-instruct
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --skip-setup --skip-browser
AOPS_LIVE=1 uv run pytest tests/live -v     # ~8 minutes on an M2 Pro
```

| Test | What happens for real | What aops must show |
|---|---|---|
| file/git/delete | the agent writes a file, commits it, deletes another | writes and a delete as external changes, each under the LLM call that asked for it |
| MCP | the agent sends an email, creates an event and deletes a missing event via MCP | email sent, event created (both confirmed by the server log), delete failed with a 404 |
| POST timeout | the server records the invoice, then curl times out (exit 28) | **outcome unknown**, although the agent tells the user "no invoice was sent" |
| interrupt | the agent is killed during `sleep 20 && touch file` | the write is **outcome unknown** and the run is interrupted |
| multi-turn | two turns in one session via `--resume` | two runs, same session |
| collector down | the collector is unreachable | events spool to disk and are delivered by the next process |
| secrets | the command contains an API key | the key is redacted everywhere, including the run title |
| capture off | `AOPS_CAPTURE_CONTENT=0` | no prompt or output text is stored, but structure and timing are |
| concurrency | two agents run at once | no steps attributed to the wrong run |
| provider error | a model that doesn't exist | a failed LLM request with the provider's error |
| guardrail | `rm -rf` without `--yolo` | **blocked**, not failed and not a change |

## Status

- **Hermes**: tested live against Hermes Agent (main @ `130b8f2`) running Qwen3 4B Instruct on local
  Ollama. The live suite passes, and manual runs also covered memory writes, subagent delegation (child
  runs linked to the parent), background LLM calls, and a model stuck in a loop. Hermes needs 64k context;
  on a 16 GB Mac use `OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0`. Small models do better with
  `tools.tool_search.enabled: off`.
- **OpenClaw**: written against OpenClaw's `api.on(...)` plugin hooks and tested with a fake plugin
  API. The hook payload field names have **not** been checked against a live OpenClaw version yet.
  All field reads are defensive, but check them first.

## Not building (yet)

No workflow engine, agent framework, memory system, evals, replay, rollback or generic MCP gateway. The
focus is showing exactly what autonomous agents did. Next: alerts → approval policies → duplicate-action
prevention → reconciliation → resuming failed runs.

The previous project in this repo (agent-commerce) is archived on the `archive/agent-commerce` branch
(tag `archive/agent-commerce-v0`).
