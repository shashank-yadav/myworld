"""Parallel episodes: same results as one-by-one, batching with skips, errors surfaced."""

import pytest

from myworld.rl import EnvPool, ToolEnv, run_references, tasks


@pytest.fixture(scope="module")
def specs():
    return tasks.generate(8, seed=11)


def test_pool_matches_sequential_episodes(specs):
    with EnvPool(workers=2) as pool:
        obs = pool.reset(specs)
        assert [o["task"] for o, _ in obs] == [s["task"] for s in specs]
        first = [s["reference"][0] for s in specs]
        results = pool.step(first)
    for spec, (o, *_rest) in zip(specs, results):
        env = ToolEnv(spec)
        env.reset()
        assert env.step(spec["reference"][0])[0] == o


def test_skips_and_trajectories(specs):
    with EnvPool(workers=3) as pool:
        pool.reset(specs[:3])
        out = pool.step([specs[0]["reference"][0], None, None])
        assert out[0] is not None and out[1] is None and out[2] is None
        trajs = pool.trajectories()
        assert [len(t["steps"]) for t in trajs] == [1, 0, 0]
        with pytest.raises(ValueError, match="expected 3 actions"):
            pool.step([None])


def test_worker_errors_are_raised_not_hidden(specs):
    with EnvPool(workers=1) as pool:
        pool.reset(specs[:1])
        pool.step([{"tool": "submit", "arguments": {"answer": "x"}}])
        with pytest.raises(RuntimeError, match="episode is over"):
            pool.step([{"tool": "submit", "arguments": {"answer": "x"}}])


def test_reference_benchmark(specs):
    report = run_references(specs, workers=2)
    assert report["all_solved"] and report["episodes"] == 8 and report["steps_per_s"] > 0
