"""Run a model on a dataset of toolsim worlds and score it the way each benchmark reports.

    toolsim eval datasets/automationbench --model claude-opus-5 --limit 20 -o results/ab
    toolsim eval datasets/automationbench-runtime --only resume
    toolsim eval datasets/safety

The agent sees the task and the world's tools (``toolsim.rl.ToolEnv``: ``<server>__<tool>``,
``wait`` and ``submit``) and acts until it stops calling tools, submits, or runs out of steps.
Every run is saved with its journal (``runs/<name>.json``, ``EnvRun.export``), so any result can be
replayed exactly, forked at any step, or regraded later.

Needs the ``anthropic`` package and ANTHROPIC_API_KEY (``pip install toolsim[bench]``).
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ..rl.episode import ToolEnv

DEFAULT_MODEL = "claude-opus-5"


def load_dataset(path: str | Path) -> list[dict[str, Any]]:
    """Environment specs from a dataset folder or file: plain specs (JSONL), overlays on a base
    dataset (runtime splits), or counterfactual scenarios (YAML)."""
    from . import pairs, splits
    path = Path(path)
    if path.suffix in (".yaml", ".yml") or (path.is_dir() and any(path.glob("*.yaml"))):
        return pairs.load(path)
    if path.is_dir():
        return splits.load(path)
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def kind_of(name: str) -> str:
    """The split an item belongs to, from its name: ``ab-hr-x.resume`` -> ``resume``."""
    tail = name.rsplit(".", 1)[-1] if "." in name else ""
    return tail if tail in ("injection", "lookalike", "flaky", "outage", "timeout", "resume", "allowed", "forbidden") \
        else "base"


class Agent:
    """Claude through the Messages API: a manual tool-use loop over a ToolEnv."""

    def __init__(self, model: str = DEFAULT_MODEL, effort: str | None = None, max_tokens: int = 16000,
                 client: Any = None, thinking_time: bool = False):
        """``thinking_time``: let the model's response time pass in the world (virtual time), as it
        would for a real deployment. Off by default, so results don't depend on API latency."""
        self.thinking_time = thinking_time
        if client is None:
            try:
                import anthropic
            except ImportError as e:
                raise RuntimeError("the eval runner needs the anthropic package: pip install 'toolsim[bench]'") from e
            client = anthropic.Anthropic()
        self.client, self.model, self.effort, self.max_tokens = client, model, effort, max_tokens

    def episode(self, env: ToolEnv) -> dict[str, Any]:
        obs, _info = env.reset()
        tools = obs["tools"]
        messages: list[dict[str, Any]] = [{"role": "user", "content": obs["task"]}]
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "requests": 0}
        stop = "max_steps"
        while not env.done:
            kw: dict[str, Any] = {"model": self.model, "max_tokens": self.max_tokens, "tools": tools,
                                  "messages": messages, "thinking": {"type": "adaptive"},
                                  "cache_control": {"type": "ephemeral"}}
            if self.effort:
                kw["output_config"] = {"effort": self.effort}
            t0 = time.monotonic()
            with self.client.messages.stream(**kw) as stream:
                response = stream.get_final_message()
            elapsed = time.monotonic() - t0
            usage["requests"] += 1
            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens"):
                usage[k] += int(getattr(response.usage, k, 0) or 0)
            messages.append({"role": "assistant", "content": response.content})
            calls = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
            if response.stop_reason == "pause_turn":
                continue
            if not calls:
                stop = response.stop_reason or "end_turn"
                if stop == "end_turn" and not env.done:
                    text = " ".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", None) == "text")
                    env.step({"tool": "submit", "arguments": {"answer": text.strip()}})  # its last words are the answer
                break
            results = []
            for n, b in enumerate(calls):
                if env.done:
                    results.append({"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                                    "content": "Error: the episode is over (step limit reached)."})
                    continue
                out, _r, terminated, _t, _i = env.step({"tool": b.name, "arguments": b.input},
                                                      elapsed=elapsed if self.thinking_time and n == 0 and elapsed > 0 else None)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": out["content"] or "(empty)",
                                **({"is_error": True} if out["is_error"] else {})})
                if terminated:
                    stop = "submit"
            messages.append({"role": "user", "content": results})
        traj = env.trajectory()
        traj.update(model=self.model, effort=self.effort, stop=stop, usage=usage)
        return traj


def evaluate(specs: list[dict[str, Any]], agent: Agent, *, out: str | Path | None = None, max_steps: int = 50,
             concurrency: int = 4, on_result: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Run ``agent`` on every spec; returns per-item results and a report (see ``report``)."""
    out_dir = Path(out) if out else None
    if out_dir:
        (out_dir / "runs").mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    results: list[dict[str, Any]] = []

    def one(spec: dict[str, Any]) -> dict[str, Any]:
        env = ToolEnv(spec, max_steps=max_steps)
        try:
            traj = agent.episode(env)
            g = traj["grade"]
            row = {"name": spec["name"], "kind": kind_of(spec["name"]), "passed": bool(g["passed"]),
                   "score": g["score"], "reward": g["reward"], "violations": g["violations"],
                   "steps": len(traj["steps"]), "stop": traj["stop"], "usage": traj["usage"]}
            if out_dir:
                (out_dir / "runs" / f"{spec['name']}.json").write_text(json.dumps(
                    {"result": row, "trajectory": traj, "run": env.run.export()}, default=str))
        except Exception as e:  # an API failure is a missing result, not a score
            row = {"name": spec["name"], "kind": kind_of(spec["name"]), "error": f"{type(e).__name__}: {e}"[:500]}
        with lock:
            results.append(row)
            if out_dir:
                with open(out_dir / "results.jsonl", "a") as fh:
                    fh.write(json.dumps(row) + "\n")
        if on_result:
            on_result(row)
        return row

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        list(pool.map(one, specs))
    rep = report(results)
    if out_dir:
        (out_dir / "report.json").write_text(json.dumps(rep, indent=1))
    return {"results": sorted(results, key=lambda r: r["name"]), "report": rep}


def report(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Pass rate (AutomationBench's official metric), mean partial credit, and unauthorized action
    rate, per split; for safety pairs also pair accuracy."""
    from .pairs import summarize
    done = [r for r in results if "error" not in r]
    out: dict[str, Any] = {"items": len(results), "errors": len(results) - len(done)}
    kinds = sorted({r["kind"] for r in done})
    for k in ["all", *kinds]:
        rows = done if k == "all" else [r for r in done if r["kind"] == k]
        if rows:
            out[k] = {"n": len(rows), "pass_rate": sum(r["passed"] for r in rows) / len(rows),
                      "mean_score": sum(r["score"] for r in rows) / len(rows),
                      "unauthorized_action_rate": sum(bool(r["violations"]) for r in rows) / len(rows),
                      "mean_steps": sum(r["steps"] for r in rows) / len(rows)}
    pairs = {r["name"]: {"passed": r["passed"], "violations": r["violations"], "score": r["score"]}
             for r in done if r["kind"] in ("allowed", "forbidden")}
    if pairs:
        out["pairs"] = summarize(pairs)
    return out
