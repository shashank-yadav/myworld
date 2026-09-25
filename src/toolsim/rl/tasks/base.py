"""Task families: the registry and the helpers every family uses."""


from __future__ import annotations

import random
from typing import Any, Callable

from ...env import Environment, EnvRun

NOISE = {"gmail": {"emails": 150}, "slack": {"messages": 200}, "calendar": {"density": 0.45},
         "github": {"issues": 30, "pulls": 4}, "jira": {"issues": 40}, "drive": {"files": 30}}
REPO = {"owner": "acme", "repo": "api"}

Task = dict[str, Any]
FAMILIES: dict[str, tuple[Callable[[random.Random, int], Task | None], tuple[str, ...]]] = {}


def family(name: str, *servers: str) -> Callable[[Callable[..., Task | None]], Callable[..., Task | None]]:
    def wrap(fn: Callable[..., Task | None]) -> Callable[..., Task | None]:
        FAMILIES[name] = (fn, servers)
        return fn
    return wrap


def _servers(*names: str) -> dict[str, Any]:
    return {n: {"noise": dict(NOISE[n])} for n in names}


def _world(servers: dict[str, Any], seed: int) -> EnvRun:
    return EnvRun(Environment.from_dict({"name": "gen", "rng_seed": seed, "servers": servers}))


def _call(tool: str, **arguments: Any) -> dict[str, Any]:
    return {"tool": tool, "arguments": arguments}


def _submit(answer: str) -> dict[str, Any]:
    return _call("submit", answer=answer)

def _mailbox(run: EnvRun) -> tuple[str, dict[str, Any]]:
    g = run.instances["gmail"].state
    return g["default"], g["mailboxes"][g["default"]]
