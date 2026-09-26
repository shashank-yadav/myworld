# AutomationBench on toolsim

244 of AutomationBench's 600 public tasks ([zapier/AutomationBench](https://github.com/zapier/AutomationBench),
MIT, commit `4a8e106`), converted to toolsim environments: every task whose apps toolsim simulates
(Gmail, Google Calendar, Slack, Google Drive and Sheets, Jira, Notion). The other 356 need
Salesforce, HubSpot, Zendesk, QuickBooks and other apps toolsim doesn't simulate.

| domain | tasks |
|---|---|
| marketing | 73 |
| finance | 73 |
| hr | 69 |
| operations | 27 |
| sales | 2 |

Each line of `<domain>.jsonl` is an environment spec (`Environment.from_dict`). Service versions
are pinned, so a task behaves the same forever.

**Grading is AutomationBench's own.** The final toolsim world is converted back into its
`WorldState` and scored with its `partial_credit` (free assertions excluded, broken guards
penalized); `passed` is its `task_completed_correctly`. The grader needs its code:
`pip install` it or set `TOOLSIM_AUTOMATIONBENCH=<checkout>`.

**Checked.** For all 244 tasks, every assertion gives the same result on the converted starting
world as on AutomationBench's own. A reference solver that performs each task's asserted actions
through toolsim's tools passes 228 of them fully (the rest need exact counts or times it doesn't
derive); see `tests/test_bench.py`.

**Differences from running AutomationBench itself.**
- The agent uses toolsim's tools (MCP, or the real Google/Slack REST APIs through the gateway),
  not AutomationBench's `api_fetch`/Zapier functions. They are stricter (real request shapes,
  errors, pagination), so scores can be lower.
- Spreadsheets, Drive files and folders, and Notion pages keep AutomationBench's ids (prompts
  refer to them). Worksheet tabs keep their titles; a prompt's `ws_...` id names a tab the agent
  finds by listing the spreadsheet's tabs.
- Folders and pages known only by id get a name from it (`fld_ops_shared` → "Ops Shared").
- The Slack bot is a member of every channel; calendars are in UTC.

Rebuild: `python -m toolsim.bench.automationbench build --ab <checkout> -o datasets/automationbench`.
