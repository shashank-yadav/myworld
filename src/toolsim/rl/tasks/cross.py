"""Across tools task families."""

from __future__ import annotations

import json
import random

from .base import Task, _call, _mailbox, _servers, _submit, _world, family


@family("cross.email_to_jira", "gmail", "jira")
def cross_email_to_jira(rng: random.Random, seed: int) -> Task | None:
    """File a Jira bug from the latest escalation email."""
    servers = _servers("gmail", "jira")
    run = _world(servers, seed)
    _, box = _mailbox(run)
    existing = [i for i in run.instances["jira"].state["issues"].values() if i["project"] == "SUP"]
    esc = sorted((m for m in box["messages"].values() if m["subject"].startswith("Customer escalation: ")
                  and "INBOX" in m["labelIds"]), key=lambda m: int(m["internalDate"]))
    if not esc or len({m["subject"] for m in esc}) < 2:
        return None  # needs an older escalation to confuse it with
    customer = esc[-1]["subject"].removeprefix("Customer escalation: ")
    older = [m["subject"].removeprefix("Customer escalation: ") for m in esc[:-1] if customer not in m["subject"]]
    had = lambda text, **kw: sum(text.lower() in i["summary"].lower() and all(i[k] == v for k, v in kw.items())  # noqa: E731
                                 for i in existing)
    return {"servers": servers,
            "task": "Find the most recent customer escalation email in my inbox and file a High-priority Bug for it in "
                    "the SUP Jira project, with the customer's name in the summary.",
            "checks": [
                {"name": f"filed a High bug for {customer}", "server": "jira", "state": "issues", "weight": 3,
                 "where": {"project": "SUP", "issue_type": "Bug", "priority": "High", "summary~": customer},
                 "count": had(customer, issue_type="Bug", priority="High") + 1},
                *({"name": f"didn't file for the older {o} escalation", "server": "jira", "state": "issues",
                   "where": {"project": "SUP", "summary~": o}, "count": had(o)} for o in sorted(set(older))[:2]),
                {"name": "filed exactly one issue", "server": "jira", "calls": "jira_create_issue", "must": True,
                 "where": {"ok": True}, "max": 1}],
            "reference": [_call("gmail__search_emails", query="subject:\"customer escalation\""),
                          _call("jira__jira_create_issue", project_key="SUP", issue_type="Bug",
                                summary=f"{customer}: customer escalation",
                                additional_fields=json.dumps({"priority": {"name": "High"}})),
                          _submit(f"Filed a bug for {customer}.")]}


@family("cross.jira_to_slack", "jira", "slack")
def cross_jira_to_slack(rng: random.Random, seed: int) -> Task | None:
    """Post the unfinished high-priority Jira issues to Slack."""
    servers = _servers("jira", "slack")
    run = _world(servers, seed)
    js, ss = run.instances["jira"].state, run.instances["slack"].state
    proj = rng.choice(sorted(js["projects"]))
    prio = rng.choice(["Highest", "High"])
    hits = sorted((i["key"] for i in js["issues"].values()
                   if i["project"] == proj and i["priority"] == prio and i["status"] != "Done"),
                  key=lambda k: int(k.split("-")[1]))
    misses = sorted((i["key"] for i in js["issues"].values() if i["project"] == proj and i["status"] != "Done"
                     and i["priority"] != prio), key=lambda k: int(k.split("-")[1]))
    eng = next((c for c in ss["channels"].values() if c["name"] == "eng"), None)
    if not 1 <= len(hits) <= 6 or eng is None:
        return None
    return {"servers": servers,
            "task": f"Post a message in the #eng Slack channel listing the keys of every unfinished {prio}-priority "
                    f"issue in the {proj} Jira project.",
            "checks": [*({"name": f"mentions {k}", "server": "slack", "state": "messages",
                          "where": {"channel": "eng", "from_bot": True, "text~re": rf"\b{k}\b"}, "min": 1} for k in hits),
                       *({"name": f"doesn't list {k}", "server": "slack", "state": "messages",
                          "where": {"channel": "eng", "from_bot": True, "text~re": rf"\b{k}\b"}, "count": 0}
                         for k in misses[:3]),
                       {"name": "posted only in #eng", "server": "slack", "state": "messages", "must": True,
                        "where": {"from_bot": True, "!channel": "eng"}, "count": 0}],
            "reference": [_call("jira__jira_search", jql=f"project = {proj} AND priority = {prio} AND status != Done"),
                          _call("slack__slack_post_message", channel_id=eng["id"],
                                text=f"Unfinished {prio} {proj} issues: {', '.join(hits)}"),
                          _submit("Posted.")]}
