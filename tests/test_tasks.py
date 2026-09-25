"""Generated tasks: every family is solvable, none is free, and sloppy agents are caught."""

import json

import pytest
from fastapi.testclient import TestClient

from toolsim.rl import tasks
from toolsim.host import HostConfig, create_app
from toolsim.rl import ToolEnv


def run(spec, actions, answer=""):
    env = ToolEnv(spec)
    env.reset()
    for a in actions:
        env.step(a)
    return env.step({"tool": "submit", "arguments": {"answer": answer}})[4]["grade"]


def first(family, **kw):
    return next(s for s in (tasks.make(family, n, **kw) for n in range(1, 40)) if s)


@pytest.mark.parametrize("family", sorted(tasks.FAMILIES))
def test_every_family_is_solvable_and_not_free(family):
    made = [s for s in (tasks.make(family, n) for n in range(1, 9)) if s]
    assert len(made) >= 4, "the family fits most worlds"
    for spec in made:
        v = tasks.validate(spec)
        assert v["ok"], (spec["name"], v)
        json.dumps(spec)  # exportable


def test_generation_is_deterministic_and_balanced():
    a = tasks.generate(24, seed=5)
    assert a == tasks.generate(24, seed=5)
    assert {t["family"] for t in a} == set(tasks.FAMILIES)
    assert a != tasks.generate(24, seed=6)
    with pytest.raises(ValueError, match="unknown task families"):
        tasks.generate(1, families=["nope"])


def test_hard_tasks_add_flaky_apis_and_stay_solvable():
    for spec in tasks.generate(12, seed=2, hard=True):
        assert spec["issues"][0]["use"] == "flaky_api"
        assert tasks.validate(spec)["ok"]


def test_sloppy_archiving_is_caught():
    spec = first("gmail.archive_sender")
    ref = spec["reference"][:-1]
    env = ToolEnv(spec)
    env.reset()
    box = env.run.instances["gmail"].state["mailboxes"]["alex@acme.com"]["messages"]
    bystander = next(m for m in box.values() if "INBOX" in m["labelIds"]
                     and all(m["id"] != a["arguments"]["messageId"] for a in ref))
    grade = run(spec, ref + [{"tool": "gmail__modify_email",
                              "arguments": {"messageId": bystander["id"], "removeLabelIds": ["INBOX"]}}])
    assert grade["reward"] == 0 and grade["violations"] == ["every other email still in the inbox"]
    deleted = run(spec, [{"tool": "gmail__delete_email", "arguments": {"messageId": a["arguments"]["messageId"]}}
                         for a in ref])
    assert deleted["reward"] == 0, "trashing isn't archiving"


def test_overwriting_labels_is_caught():
    spec = first("github.label")
    sloppy = [{**a, "arguments": {**a["arguments"], "labels": a["arguments"]["labels"][-1:]}}
              for a in spec["reference"][:-1]]
    had_labels = any(len(a["arguments"]["labels"]) > 1 for a in spec["reference"][:-1])
    grade = run(spec, sloppy)
    assert grade["score"] > 0.5 and (grade["reward"] == 0) == had_labels


def test_sharing_the_old_copy_is_caught():
    spec = first("drive.share_current")
    good = spec["reference"][0]["arguments"]
    env = ToolEnv(spec)
    env.reset()
    files = env.run.instances["drive"].state["files"]
    name = files[good["file_id"]]["name"]
    old = next(f["id"] for f in files.values() if f["name"].startswith(name + " "))
    grade = run(spec, [{"tool": "drive__manage_drive_access", "arguments": {**good, "file_id": old}}])
    assert grade["reward"] == 0


def test_double_booking_and_wrong_escalation_are_caught():
    spec = first("calendar.book")
    ref = spec["reference"][1]["arguments"]
    env = ToolEnv(spec)
    env.reset()
    st = env.run.instances["calendar"].state
    date = ref["start"][:10]
    who = ref["attendees"][0]["email"]
    from toolsim.services.calendar.model import _parse
    from toolsim.services.calendar.recurrence import _busy_spans
    import datetime as dt
    day = _parse(date + "T09:00:00", st["timeZone"])
    spans = _busy_spans(st, "alex@acme.com", "", day, day + dt.timedelta(hours=8), st["timeZone"])
    if spans:
        a = spans[0][0].astimezone(day.tzinfo)
        clash = {**ref, "start": a.strftime("%Y-%m-%dT%H:%M:%S"),
                 "end": (a + dt.timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S")}
        assert run(spec, [{"tool": "calendar__create-event", "arguments": clash}])["reward"] == 0
    assert who in json.dumps(spec["checks"])
    spec = first("cross.email_to_jira")
    wrong = json.loads(json.dumps(spec["reference"][1]))
    older = next(c["name"].split("older ")[1].split(" escalation")[0] for c in spec["checks"] if "older" in c["name"])
    wrong["arguments"]["summary"] = f"{older}: customer escalation"
    assert run(spec, [wrong])["reward"] < 0.5


def test_tasks_run_over_http():
    spec = tasks.generate(1, seed=9, families=["slack.dm"])[0]
    c = TestClient(create_app(config=HostConfig()))
    r = c.post("/envs", json={"spec": spec, "id": "t"}).json()
    assert "Send" in r["agents"]["agent"]["task"]
    assert c.post("/envs/t/submit", json={"answer": "done"}).json()["reward"] < 1


def test_cancelling_the_whole_series_is_caught():
    spec = first("calendar.cancel_occurrence")
    series = spec["reference"][0]["arguments"]["eventId"].split("_")[0]
    assert run(spec, [{"tool": "calendar__delete-event", "arguments": {"eventId": series}}])["reward"] == 0


def test_posting_at_top_level_instead_of_the_thread_is_caught():
    spec = first("slack.thread_reply")
    ref = spec["reference"][0]["arguments"]
    grade = run(spec, [{"tool": "slack__slack_post_message", "arguments": {"channel_id": ref["channel_id"],
                                                                          "text": ref["text"]}}])
    assert grade["reward"] == 0


def test_copying_instead_of_moving_is_partial_and_moving_others_is_caught():
    spec = first("drive.move_file")
    ref = spec["reference"][0]["arguments"]
    added_only = run(spec, [{"tool": "drive__update_drive_file", "arguments": {"file_id": ref["file_id"],
                                                                              "add_parents": ref["add_parents"]}}])
    assert 0 < added_only["reward"] < 1


def test_answer_stuffing_needs_the_guards():
    spec = first("gmail.receipt_amount")
    assert tasks.validate(spec)["stuffing_reward"] < 1
    unguarded = json.loads(json.dumps(spec))
    for c in unguarded["checks"]:
        if "answer" in c:
            c["answer"] = {"matches": c["answer"]["matches"]}
    assert tasks.validate(unguarded)["stuffing_reward"] == 1.0, "without max_len/not, stuffing would win"
