"""Generated tasks with verifiers and reference solutions, for RL at scale.

    from myworld.rl import generate
    specs = generate(n=500, seed=0)          # environment specs, ready for ToolEnv(spec)

Each task comes from a *family* (reply to a colleague, label every issue about X, book a slot
you're both free for, file a bug from the latest escalation email, ...). A family builds the
world for a seed (realistic volume, distractors), looks at what's actually in it, and writes:

- the task, in plain words;
- checks that verify the outcome in the resulting world, with ``must`` constraints for the
  collateral damage a sloppy agent causes (archiving the wrong mail, overwriting labels, sharing
  the old copy);
- a reference solution: the tool calls that solve it.

Every task is validated before it's kept: the reference solution must earn reward 1.0 and doing
nothing must earn less, so every task is solvable and none is free. ``myworld tasks`` writes them
as JSONL; ``hard=True`` adds flaky APIs (the reference solution retries, like a careful agent).
"""

from __future__ import annotations

import json
import random
import re
from typing import Any

from . import calendar, cross, drive, github, gmail, jira, slack  # noqa: F401  (registers the families)
from .base import FAMILIES, Task, _submit


def make(name: str, seed: int, hard: bool = False) -> Task | None:
    """One task from family ``name`` for world seed ``seed`` (None if this world doesn't fit)."""
    fn, servers = FAMILIES[name]
    rng = random.Random(f"{name}:{seed}")
    t = fn(rng, seed)
    if t is None:
        return None
    spec = {"name": f"{name.replace('.', '-')}-{seed}", "family": name, "rng_seed": seed, **t}
    if hard:
        spec["issues"] = [{"use": "flaky_api", "params": {"server": rng.choice(servers), "probability": 0.25}}]
    return spec


def validate(spec: Task, retries: int = 8) -> dict[str, Any]:
    """Run the reference solution (retrying failed calls, as a careful agent would) and a do-nothing
    baseline. A good task: reference reward 1.0, baseline below it."""
    from ..episode import ToolEnv
    env = ToolEnv(spec, max_steps=10_000)
    env.reset()
    grade: dict[str, Any] = {}
    for action in spec["reference"]:
        for _ in range(retries):
            obs, reward, done, _, info = env.step(action)
            if not obs["is_error"] or done:
                break
        grade = info.get("grade", grade)
    ref = grade.get("reward", env.run.grade()["reward"]) if env.run else 0.0
    env.reset()
    _, null, *_ = env.step(_submit(""))
    # answer stuffing: every value in the world in one answer must not pass for knowing the answer
    env.reset()
    stuffing = " ".join(dict.fromkeys(re.findall(r"[$\w.,@-]+", json.dumps(env.run.worlds(), default=str))))[:20000]
    _, stuffed, *_ = env.step(_submit(stuffing))
    return {"reference_reward": ref, "null_reward": null, "stuffing_reward": stuffed,
            "ok": ref == 1.0 and null < 1.0 and stuffed < 1.0,
            "failed": [c["name"] for c in grade.get("checks", []) if not c["passed"]]}


def generate(n: int, seed: int = 0, families: list[str] | None = None, hard: bool = False,
             check: bool = True) -> list[Task]:
    """``n`` validated tasks, cycling through ``families`` (default: all) over successive world seeds."""
    names = families or sorted(FAMILIES)
    unknown = set(names) - set(FAMILIES)
    if unknown:
        raise ValueError(f"unknown task families: {', '.join(sorted(unknown))}; available: {', '.join(sorted(FAMILIES))}")
    out: list[Task] = []
    world, misses = seed * 1_000_003, 0
    while len(out) < n:
        name = names[len(out) % len(names)] if misses < 50 else random.Random(world).choice(names)
        world += 1
        spec = make(name, world, hard=hard)
        if spec is None or (check and not validate(spec)["ok"]):
            misses += 1
            if misses > 20 * n + 100:
                raise RuntimeError(f"couldn't generate enough tasks (got {len(out)} of {n})")
            continue
        misses = 0
        out.append(spec)
    return out
