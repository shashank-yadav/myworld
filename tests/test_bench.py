"""Plug-in graders, and AutomationBench tasks on toolsim worlds.

The AutomationBench tests need its code: TOOLSIM_AUTOMATIONBENCH=<checkout of
https://github.com/zapier/AutomationBench>; they're skipped otherwise.
"""

import copy
import os
from pathlib import Path

import pytest

from toolsim import graders
from toolsim.env import Environment, EnvRun

AB = os.environ.get("TOOLSIM_AUTOMATIONBENCH")


def test_plugin_grader_scores_the_run():
    def count_sent(env, spec, worlds, answer):
        box = worlds["gmail"]["state"]["mailboxes"]["alex@acme.com"]
        n = sum("SENT" in m["labelIds"] and m["subject"] == spec["subject"] for m in box["messages"].values())
        return {"checks": [{"name": "sent it", "passed": n == 1}], "score": float(n == 1), "passed": n == 1,
                "extra": {"sent": n}}
    graders.register("count-sent", count_sent)
    env = Environment.from_dict({"name": "g", "task": "t", "servers": {"gmail": {}},
                                 "graders": [{"use": "count-sent", "subject": "Hello"}]})
    assert env.to_dict()["graders"] == [{"use": "count-sent", "subject": "Hello"}]
    run = EnvRun(env)
    assert run.grade()["score"] == 0.0
    run.instances["gmail"].call("send_email", {"to": ["john@acme.com"], "subject": "Hello", "body": "hi"})
    g = run.grade()
    assert g["passed"] and g["score"] == 1.0 and g["sent"] == 1 and g["checks"][0]["name"] == "sent it"


def test_unknown_grader_fails_at_load():
    with pytest.raises(ValueError, match="unknown grader"):
        Environment.from_dict({"name": "g", "task": "t", "servers": {"gmail": {}}, "graders": [{"use": "nope"}]})


def test_seeds_can_fix_ids():
    env = Environment.from_dict({"name": "ids", "task": "t", "servers": {
        "drive": {"seed": {"user": {"email": "me@co.com"}, "folders": [{"key": "f", "id": "fld_ops", "name": "Ops"}],
                           "files": [{"id": "ss_budget", "name": "Budget", "type": "sheet", "parent": "f",
                                      "sheets": {"Q1": "a,b\n1,2\n"}}]}},
        "notion": {"seed": {"user": {"name": "Me", "email": "me@co.com"},
                            "pages": [{"key": "p", "id": "pg_ops", "title": "Ops log", "content": ""}]}}}})
    run = EnvRun(env)
    got = run.instances["drive"].call("read_sheet_values", {"spreadsheet_id": "ss_budget", "range_name": "Q1!A1:B2"})
    assert not got.is_error and "1" in got.text
    assert run.instances["drive"].state["files"]["ss_budget"]["parents"] == ["fld_ops"]
    made = run.instances["notion"].call("notion-create-pages", {"pages": [{"properties": {"title": "Entry"}}],
                                                                "parent": {"page_id": "pg_ops"}})
    assert not made.is_error


# -- AutomationBench -------------------------------------------------------------------------------

needs_ab = pytest.mark.skipif(not AB, reason="set TOOLSIM_AUTOMATIONBENCH to an AutomationBench checkout")


@pytest.fixture(scope="module")
def ab_tasks():
    from toolsim.bench import automationbench as ab
    return [t for t in ab.load_tasks() if ab.supported(t)[0]]


@needs_ab
def test_every_assertion_agrees_on_the_starting_world(ab_tasks):
    """Converted to toolsim and back, the world satisfies exactly the assertions it did before."""
    from automationbench.rubric.registry import AssertionRegistry
    from automationbench.schema.world import WorldState
    from toolsim.bench import automationbench as ab
    assert len(ab_tasks) >= 240
    for t in ab_tasks:
        spec = ab.convert(t)
        run = EnvRun(Environment.from_dict(spec, base_dir=None))
        d = ab._world_dict(spec["graders"][0], run.worlds(), {s: run.env.seed_for(s) or {} for s in run.env.servers})
        d.pop("_sheets_updated", None)
        ours = WorldState(**{k: v for k, v in d.items() if k in WorldState.model_fields})
        theirs = WorldState(**copy.deepcopy(t["info"]["initial_state"]))
        for a in t["info"]["assertions"]:
            assert AssertionRegistry.check(ours, a) == AssertionRegistry.check(theirs, a), (t["info"]["task_name"], a)


@needs_ab
def test_reference_solutions_pass_through_real_tools(ab_tasks):
    """Doing what each task asserts, through toolsim's tools, passes AutomationBench's own grader."""
    from toolsim.bench import automationbench as ab
    full = 0
    for t in ab_tasks:
        run = EnvRun(Environment.from_dict(ab.convert(t), base_dir=None))
        assert run.grade()["score"] < 1.0  # nothing is solved at the start
        made = ab.oracle(run)
        assert all(m["ok"] for m in made), [m for m in made if not m["ok"]]
        g = run.grade()
        assert g["partial_credit"] == g["score"]
        full += g["passed"]
    assert full >= 228  # the rest need exact counts or times the reference solver doesn't derive


def test_history_starts_a_run_part_way():
    spec = {"name": "h", "task": "t", "servers": {"gmail": {}}}
    run = EnvRun(Environment.from_dict(spec))
    run.instances["gmail"].call("send_email", {"to": ["john@acme.com"], "subject": "One", "body": "1"})
    run.instances["gmail"].call("send_email", {"to": ["john@acme.com"], "subject": "Two", "body": "2"})
    later = EnvRun(Environment.from_dict({**spec, "history": run.journal[:1]}))
    sent = lambda r: sorted(m["subject"] for m in r.instances["gmail"].state["mailboxes"]["alex@acme.com"]["messages"].values()  # noqa: E731
                            if "SENT" in m["labelIds"] and m["subject"] in ("One", "Two"))
    assert sent(later) == ["One"] and later.journal == []
    later.reset()
    assert sent(later) == ["One"]
    bad = copy.deepcopy(run.journal[:1])
    bad[0]["result_sha"] = "0" * 16
    with pytest.raises(ValueError, match="doesn't replay"):
        EnvRun(Environment.from_dict({**spec, "history": bad}))


@needs_ab
def test_runtime_splits(tmp_path):
    from toolsim.bench import automationbench as ab
    from toolsim.bench import splits
    src = tmp_path / "base"
    src.mkdir()
    lines = (Path(__file__).parent.parent / "datasets/automationbench/hr.jsonl").read_text().splitlines()[:12]
    (src / "hr.jsonl").write_text("\n".join(lines) + "\n")
    summary = splits.build(src, tmp_path / "runtime", ab.oracle)
    assert summary["total"]["perturbed"] >= 40 and summary["total"]["resume"] >= 10
    specs = {s["name"]: s for s in splits.load(tmp_path / "runtime")}
    bases = {s["name"]: s for s in splits.load(src)}
    for name, spec in specs.items():
        base = bases[name.rsplit(".", 1)[0]]
        if name.endswith((".injection", ".lookalike")):  # doing the task doesn't trip the trap
            run = EnvRun(Environment.from_dict(spec, base_dir=None))
            ab.oracle(run)
            g = run.grade()
            assert not g["violations"], (name, g["violations"])
        if name.endswith(".resume"):
            full = EnvRun(Environment.from_dict(base, base_dir=None))
            ab.oracle(full)
            done = len(spec["history"])
            rest = EnvRun(Environment.from_dict(spec, base_dir=None))
            assert rest.grade()["score"] < full.grade()["score"]
            assert rest._replay(full.journal[done:]) == []  # finishing the reference path
            g = rest.grade()
            assert g["score"] == full.grade()["score"] and not g["violations"]
            if any(c["name"].startswith("didn't email") for c in spec["checks"]):
                again = EnvRun(Environment.from_dict(spec, base_dir=None))
                ab.oracle(again)  # starting over redoes what was done
                assert again.grade()["reward"] == 0.0
