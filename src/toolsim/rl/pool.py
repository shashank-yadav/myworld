"""Many episodes at once, across worker processes.

    from toolsim.rl import EnvPool, generate

    specs = generate(256, seed=0)
    with EnvPool(workers=8, max_steps=40) as pool:
        observations = pool.reset(specs)                 # [(obs, info), ...], one per episode
        results = pool.step([policy(o) for o, _ in observations])   # [(obs, reward, terminated, truncated, info)]
        ...
        rollouts = pool.trajectories()

Episode ``i`` lives in worker ``i % workers`` for its whole life, so its world never crosses a
process boundary; only actions and observations do. ``step`` takes one action per episode, or
``None`` to leave that episode alone this round (finished ones, or ones still waiting on the
model). Worlds are independent, so the results are the same as running the episodes one by one.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import traceback
from typing import Any

Step = tuple[dict[str, Any], float, bool, bool, dict[str, Any]]


def _worker(conn: Any, env_kwargs: dict[str, Any]) -> None:
    from .episode import ToolEnv
    envs: dict[int, ToolEnv] = {}
    while True:
        cmd, items = conn.recv()
        try:
            if cmd == "close":
                conn.send(("ok", None))
                return
            if cmd == "reset":
                out = []
                for slot, spec, seed in items:
                    envs[slot] = ToolEnv(spec, **env_kwargs)
                    out.append((slot, envs[slot].reset(seed=seed)))
            elif cmd == "step":
                out = [(slot, envs[slot].step(action, elapsed=elapsed)) for slot, action, elapsed in items]
            elif cmd == "trajectory":
                out = [(slot, envs[slot].trajectory()) for (slot,) in items]
            else:
                raise ValueError(f"unknown command {cmd!r}")
            conn.send(("ok", out))
        except Exception:  # report, don't die: the pool raises it in the parent
            conn.send(("error", traceback.format_exc()))


class EnvPool:
    def __init__(self, workers: int | None = None, **env_kwargs: Any):
        """``env_kwargs`` go to every ``ToolEnv`` (``max_steps``, ``dense``, ``step_penalty``, ``allow_wait``)."""
        self.workers = max(1, workers or min(8, os.cpu_count() or 1))
        ctx = mp.get_context("spawn")  # safe everywhere (no forked locks); workers import toolsim once
        self._conns, self._procs = [], []
        for _ in range(self.workers):
            parent, child = ctx.Pipe()
            p = ctx.Process(target=_worker, args=(child, env_kwargs), daemon=True)
            p.start()
            self._conns.append(parent)
            self._procs.append(p)
        self.size = 0

    # -- batching -----------------------------------------------------------------------------

    def _run(self, cmd: str, per_worker: dict[int, list[Any]]) -> dict[int, Any]:
        for w, items in per_worker.items():
            self._conns[w].send((cmd, items))
        out: dict[int, Any] = {}
        errors = []
        for w in per_worker:
            status, payload = self._conns[w].recv()
            if status == "error":
                errors.append(payload)
            else:
                out.update(dict(payload))
        if errors:
            raise RuntimeError("an episode failed in a worker:\n" + errors[0])
        return out

    def _split(self, items: list[tuple[int, Any]]) -> dict[int, list[Any]]:
        per: dict[int, list[Any]] = {}
        for slot, *rest in items:
            per.setdefault(slot % self.workers, []).append((slot, *rest))
        return per

    # -- the API ------------------------------------------------------------------------------

    def reset(self, specs: list[dict[str, Any]], seeds: list[int | None] | None = None
              ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """Start one episode per spec (replacing any running ones)."""
        seeds = seeds or [None] * len(specs)
        if len(seeds) != len(specs):
            raise ValueError("seeds must match specs")
        self.size = len(specs)
        out = self._run("reset", self._split([(i, s, seed) for i, (s, seed) in enumerate(zip(specs, seeds))]))
        return [out[i] for i in range(self.size)]

    def step(self, actions: list[dict[str, Any] | None], elapsed: list[float | None] | None = None
             ) -> list[Step | None]:
        """One action per episode (``None`` skips it). Returns ``None`` for skipped episodes."""
        if len(actions) != self.size:
            raise ValueError(f"expected {self.size} actions, got {len(actions)}")
        elapsed = elapsed or [None] * self.size
        items = [(i, a, e) for i, (a, e) in enumerate(zip(actions, elapsed)) if a is not None]
        out = self._run("step", self._split(items)) if items else {}
        return [out.get(i) for i in range(self.size)]

    def trajectories(self) -> list[dict[str, Any]]:
        out = self._run("trajectory", self._split([(i,) for i in range(self.size)]))
        return [out[i] for i in range(self.size)]

    def close(self) -> None:
        for conn, p in zip(self._conns, self._procs):
            if p.is_alive():
                try:
                    conn.send(("close", None))
                    conn.recv()
                except (EOFError, OSError, BrokenPipeError):
                    pass
            p.join(timeout=5)
            if p.is_alive():
                p.kill()
        self._conns, self._procs = [], []

    def __enter__(self) -> EnvPool:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def run_references(specs: list[dict[str, Any]], workers: int | None = None, retries: int = 8) -> dict[str, Any]:
    """Play every task's reference solution through a pool (retrying failed calls): a throughput
    benchmark that also checks every reward is 1.0."""
    import time
    t0 = time.perf_counter()
    steps = 0
    with EnvPool(workers=workers, max_steps=10_000) as pool:
        pool.reset(specs)
        cursor = [0] * len(specs)
        tries = [0] * len(specs)
        rewards: list[float | None] = [None] * len(specs)
        while any(r is None for r in rewards):
            actions = [s["reference"][cursor[i]] if rewards[i] is None else None for i, s in enumerate(specs)]
            results = pool.step(actions)
            for i, res in enumerate(results):
                if res is None:
                    continue
                steps += 1
                obs, reward, terminated, truncated, info = res
                if obs["is_error"] and tries[i] < retries and not terminated:
                    tries[i] += 1
                    continue
                tries[i] = 0
                cursor[i] += 1
                if terminated or truncated or cursor[i] >= len(specs[i]["reference"]):
                    rewards[i] = reward
    secs = time.perf_counter() - t0
    return {"episodes": len(specs), "steps": steps, "seconds": round(secs, 2), "workers": workers,
            "episodes_per_s": round(len(specs) / secs, 1), "steps_per_s": round(steps / secs, 1),
            "mean_reward": sum(r or 0 for r in rewards) / len(rewards) if rewards else 0.0,
            "all_solved": all(r == 1.0 for r in rewards)}
