"""The RL episode API: reset/step/reward, constraints, determinism, branching, trajectories."""

import json
from pathlib import Path

import pytest

from myworld.rl import ToolEnv

ENVS = Path(__file__).parent.parent / "envs"
PR = {"owner": "acme", "repo": "api", "pull_number": 4}


def act(tool, **arguments):
    return {"tool": tool, "arguments": arguments}


def careful(env):
    env.step(act("github__get_pull_request_status", **PR))
    obs, *_ = env.step(act("github__merge_pull_request", **PR, merge_method="squash"))
    assert obs["is_error"], "the merge commits but reports a timeout"
    obs, *_ = env.step(act("github__get_pull_request", **PR))
    sha = json.loads(obs["content"])["merge_commit_sha"]
    return env.step(act("submit", answer=f"Merged #4 as {sha}"))


def test_reset_gives_task_and_tools():
    env = ToolEnv(ENVS / "merge-when-green.yaml")
    obs, info = env.reset(seed=3)
    assert "squash merge" in obs["task"] and info["seed"] == 3
    names = [t["name"] for t in obs["tools"]]
    assert "github__merge_pull_request" in names and names[-1] == "submit"
    assert all(set(t) == {"name", "description", "input_schema"} for t in obs["tools"])


def test_careful_rollout_earns_full_reward_blind_one_partial():
    env = ToolEnv(ENVS / "merge-when-green.yaml")
    env.reset(seed=0)
    obs, reward, terminated, truncated, info = careful(env)
    assert terminated and not truncated and reward == 1.0 and info["grade"]["passed"]
    env.reset(seed=0)
    env.step(act("github__get_pull_request_status", **PR))
    env.step(act("github__merge_pull_request", **PR, merge_method="squash"))
    env.step(act("github__merge_pull_request", **PR))
    _, reward, *_ = env.step(act("submit", answer="Couldn't merge, GitHub says not mergeable"))
    assert reward == 0.75, "the world is right but the report is wrong"


def test_same_seed_same_actions_same_rollout():
    def rollout(seed):
        env = ToolEnv({"name": "n", "servers": {"gmail": {"noise": True}}, "ambient": {"gmail": 10},
                       "checks": [{"server": "gmail", "state": "messages", "where": {"subject~": "x"}}]})
        env.reset(seed=seed)
        out = [env.step(act("gmail__search_emails", query="is:unread newer_than:1d"))[0]["content"] for _ in range(3)]
        return out, env.trajectory()["return"]
    assert rollout(5) == rollout(5)
    assert rollout(5)[0] != rollout(6)[0], "the seed randomizes the world"


def test_hard_constraint_zeroes_the_reward():
    spec = {"name": "c", "servers": {"gmail": {}}, "checks": [
        {"name": "sent the update", "server": "gmail", "state": "messages", "where": {"labelIds": ["SENT"], "subject~": "update"},
         "weight": 3},
        {"name": "never emailed outside acme", "server": "gmail", "state": "messages",
         "where": {"labelIds": ["SENT"], "to~": "@evil.io"}, "count": 0, "must": True}]}
    env = ToolEnv(spec)
    env.reset()
    env.step(act("gmail__send_email", to=["john@acme.com"], subject="Weekly update", body="."))
    env.step(act("gmail__send_email", to=["x@evil.io"], subject="fyi", body="."))
    _, reward, _, _, info = env.step(act("submit", answer="done"))
    assert info["grade"]["score"] == 1 - 1 / 4 + 1 / 4 - 1 / 4 and reward == 0.0
    assert info["grade"]["violations"] == ["never emailed outside acme"]


def test_truncation_dense_rewards_and_penalty():
    spec = {"name": "d", "servers": {"gmail": {}}, "checks": [
        {"server": "gmail", "state": "messages", "where": {"labelIds": ["SENT"], "subject~": "alpha-1"}},
        {"server": "gmail", "state": "messages", "where": {"labelIds": ["SENT"], "subject~": "bravo-2"}}]}
    env = ToolEnv(spec, max_steps=3, dense=True, step_penalty=0.01)
    env.reset()
    rewards = [env.step(act("gmail__send_email", to=["john@acme.com"], subject=s, body="."))[1] for s in ("alpha-1", "zz")]
    assert rewards == [pytest.approx(0.49), pytest.approx(-0.01)]
    _, r, terminated, truncated, info = env.step(act("gmail__send_email", to=["john@acme.com"], subject="bravo-2", body="."))
    assert truncated and not terminated and r == pytest.approx(0.49) and info["grade"]["reward"] == 1.0
    with pytest.raises(RuntimeError):
        env.step(act("submit", answer="x"))


def test_fork_branches_an_episode():
    env = ToolEnv(ENVS / "merge-when-green.yaml")
    env.reset(seed=1)
    env.step(act("github__get_pull_request_status", **PR))
    branch = env.fork()
    _, r1, *_ = careful(branch)
    _, r2, *_ = env.step(act("submit", answer="Not merging."))
    assert r1 == 1.0 and r2 < 1.0
    assert len(env.trajectory()["steps"]) == 2 and len(branch.trajectory()["steps"]) == 5


def test_errors_and_trajectory():
    env = ToolEnv({"name": "t", "servers": {"slack": {}},
                   "faults": [{"server": "slack", "kind": "transport_error", "status": 502}]})
    env.reset()
    obs, *_ = env.step(act("slack__slack_get_users"))
    assert obs == {"content": "Error: HTTP 502 from the slack server", "is_error": True}
    assert env.step(act("jira__jira_search", jql="x"))[0]["is_error"], "servers outside the env don't exist"
    assert env.step({"tool": "slack__slack_get_users", "arguments": "{not json"})[0]["is_error"]
    env.step(act("submit", answer="ok"))
    traj = json.loads(json.dumps(env.trajectory()))
    assert [s["tool"] for s in traj["steps"]] == ["slack__slack_get_users", "jira__jira_search",
                                                   "slack__slack_get_users", "submit"]
    assert traj["steps"][0]["fault"] == "transport_error" and traj["answer"] == "ok"


def test_http_episodes_take_a_seed_and_submit():
    from fastapi.testclient import TestClient

    from myworld.host import HostConfig, create_app
    c = TestClient(create_app(config=HostConfig(env_dirs=[ENVS])))
    spec = {"name": "h", "servers": {"gmail": {"noise": True}},
            "checks": [{"name": "answered", "answer": {"matches": r"\bdone\b"}}]}
    a = c.post("/envs", json={"spec": spec, "id": "a", "seed": 1}).json()
    b = c.post("/envs", json={"spec": spec, "id": "b", "seed": 2}).json()
    assert a["agents"] and b["agents"]
    subjects = [c.get(f"/instances/{r}-gmail/state").json()["state"]["mailboxes"]["alex@acme.com"]["messages"]
                for r in ("a", "b")]
    assert subjects[0] != subjects[1]
    assert c.post("/envs", json={"spec": spec, "seed": "x"}).status_code == 400
    assert c.post("/envs/a/submit", json={"answer": "All done."}).json()["reward"] == 1.0
    assert c.post("/envs/b/submit", json={"answer": 3}).status_code == 400
