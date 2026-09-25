"""Generated volume, distractors and background activity."""

import json

import pytest

from toolsim import noise
from toolsim.env import Environment, EnvRun
from toolsim.services import get_service

ALL = ["gmail", "slack", "calendar", "github", "jira", "drive", "linear", "notion"]
AMBIENT = {"hours": 8, "gmail": 6, "slack": 20, "github": 2, "jira": 3, "calendar": 1, "linear": 2, "notion": 1}


def run_of(rng_seed=0, servers=ALL, ambient=None, **cfg):
    return EnvRun(Environment.from_dict({"name": "noisy", "rng_seed": rng_seed, "ambient": ambient,
                                         "servers": {s: {"noise": cfg.get(s, True)} for s in servers}}))


def world(run):
    return {s: json.dumps(i.state, sort_keys=True, default=str) for s, i in run.instances.items()}


def test_same_seed_same_world_other_seed_other_world():
    a, b, c = world(run_of(1)), world(run_of(1)), world(run_of(2))
    assert a == b
    assert all(a[s] != c[s] for s in ALL), "every service varies with the seed"


def test_volume_is_realistic():
    i = run_of().instances
    assert len(i["gmail"].state["mailboxes"]["alex@acme.com"]["messages"]) > 250
    assert sum(len(v) for v in i["slack"].state["messages"].values()) > 300
    assert len(i["jira"].state["issues"]) > 60
    assert len(i["drive"].state["files"]) > 40
    assert len(i["calendar"].state["events"]) > 20
    assert len(i["linear"].state["issues"]) > 30
    assert len(i["notion"].state["pages"]) > 25


def test_seeded_task_items_are_untouched():
    plain = {s: get_service(s) for s in ALL}
    i = run_of().instances
    pr = i["github"].state["repos"]["acme/api"]["pulls"][4]
    assert i["github"].state["repos"]["acme/api"]["issues"][4]["title"] == "Fix flaky retry jitter"
    assert pr["head"] == "fix/flaky-retry"
    assert i["jira"].state["issues"]["OPS-2"]["summary"] == "Rotate production database credentials"
    subjects = {m["subject"] for m in i["gmail"].state["mailboxes"]["alex@acme.com"]["messages"].values()}
    assert {e["subject"] for e in plain["gmail"].default_seed()["emails"]} <= subjects
    sent_to = {a for m in i["gmail"].state["mailboxes"]["alex@acme.com"]["messages"].values()
               if "SENT" in m["labelIds"] for a in m["to"]}
    seeded_people = {"john@acme.com", "priya@acme.com", "sam@acme.com"}
    assert not (sent_to - {"priya@acme.com"}) & seeded_people, "generated sent mail only goes to generated people"


def test_distractors_look_like_the_real_thing():
    i = run_of().instances
    names = [f["name"] for f in i["drive"].state["files"].values()]
    assert any(n.startswith("Q4 budget ") or n.startswith("Q4 plan ") for n in names)
    subjects = [m["subject"] for m in i["gmail"].state["mailboxes"]["alex@acme.com"]["messages"].values()]
    assert sum("q4 planning" in s.lower() for s in subjects) >= 2


def test_generated_colleagues_dont_bounce():
    run = run_of(servers=["gmail"])
    g = run.instances["gmail"]
    colleague = next(a for m in g.state["mailboxes"]["alex@acme.com"]["messages"].values()
                     if "SENT" in m["labelIds"] for a in m["to"])
    g.call("send_email", {"to": [colleague], "subject": "hi", "body": "."})
    run.advance(600)
    g.call("list_email_labels", {})
    assert not any("mailer-daemon" in m["from"] for m in g.state["mailboxes"]["alex@acme.com"]["messages"].values())


def test_ambient_activity_keeps_the_world_moving_on_generated_items_only():
    run = run_of(ambient=AMBIENT)
    g, gh, j = run.instances["gmail"], run.instances["github"], run.instances["jira"]
    before = len(g.state["mailboxes"]["alex@acme.com"]["messages"])
    seeded = [f"OPS-{n}" for n in range(1, 6)] + ["SUP-1", "SUP-2"]
    seeded_comments = {k: len(j.state["issues"][k]["comments"]) for k in seeded}
    run.advance(8 * 3600)
    assert len(g.state["mailboxes"]["alex@acme.com"]["messages"]) >= before + 40
    assert {k: len(j.state["issues"][k]["comments"]) for k in seeded_comments} == seeded_comments
    assert len(run.fired) == len(run.events)
    kinds = {e["event"] for inst in run.instances.values() for e in inst.events}
    assert {"deliver_email", "post_message", "add_comment", "add_event"} <= kinds
    assert len(gh.state["repos"]["acme/api"]["comments"][4]) == 0, "the seeded PR gets no ambient comments"
    assert {e["event"] for e in run.instances["linear"].events} == {"add_comment"}
    assert {e["event"] for e in run.instances["notion"].events} == {"edit_page"}
    assert not {e["params"]["author"] for e in run.instances["linear"].events} & {"john@acme.com", "priya@acme.com"}


def test_ambient_is_part_of_snapshots_and_varies_by_seed():
    run = run_of(ambient=AMBIENT)
    snap = run.snapshot()
    run.advance(3600)
    fired = list(run.fired)
    run.restore(snap)
    run.advance(3600)
    assert run.fired == fired
    assert [e["params"] for e in run.events] != [e["params"] for e in run_of(3, ambient=AMBIENT).events]


def test_careful_agent_still_passes_in_a_noisy_world():
    env = Environment.load("envs/merge-when-green.yaml")
    env.servers["github"]["noise"] = True
    run = EnvRun(env)
    gh = run.instances["github"]
    o = {"owner": "acme", "repo": "api", "pull_number": 4}
    assert json.loads(gh.call("get_pull_request_status", o).text)["state"] == "success"
    gh.call("merge_pull_request", {**o, "merge_method": "squash"})  # times out after committing
    pr = json.loads(gh.call("get_pull_request", o).text)
    assert pr["merged"]
    assert run.grade(answer=f"Merged: {pr['merge_commit_sha']}")["passed"]


def test_bad_specs():
    with pytest.raises(ValueError, match="noise isn't available"):
        noise.apply("mystery", {}, True, 0, "")
    with pytest.raises(ValueError, match="ambient: unknown server"):
        run_of(servers=["gmail"], ambient={"slack": 3})
