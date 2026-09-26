"""Plug-in graders: score a run with someone else's grading code (e.g. a benchmark's own rubric).

    graders:
      - use: automationbench          # toolsim.bench.automationbench
        assertions: [...]

A grader is ``fn(env, spec, worlds, answer) -> {"checks": [...], "score": float, "passed": bool,
"extra": {...}}``. Its checks are listed with the environment's own; ``score`` (when the
environment has no checks of its own) and ``passed`` are the grader's verdict; ``extra`` is
merged into the grade (e.g. ``partial_credit``).
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

Grader = Callable[[Any, dict[str, Any], dict[str, dict[str, Any]], "str | None"], dict[str, Any]]

KNOWN: dict[str, str] = {
    "automationbench": "toolsim.bench.automationbench:grade",
}
_LOADED: dict[str, Grader] = {}


def register(name: str, fn: Grader) -> None:
    _LOADED[name] = fn


def grader(name: str) -> Grader:
    """The grader called ``name``; imported on first use, so a spec loads without its dependencies."""
    if name in _LOADED:
        return _LOADED[name]
    if name not in KNOWN:
        raise ValueError(f"unknown grader {name!r} (known: {', '.join(sorted({*KNOWN, *_LOADED}))})")

    def lazy(*args: Any, **kwargs: Any) -> dict[str, Any]:
        module, attr = KNOWN[name].split(":")
        fn = getattr(importlib.import_module(module), attr)
        _LOADED[name] = fn
        return fn(*args, **kwargs)
    return lazy
