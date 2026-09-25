"""Reinforcement learning on realistic tool worlds: a Gymnasium-style episode API.

    from toolsim.rl import ToolEnv

    env = ToolEnv("envs/merge-when-green.yaml", max_steps=30)
    obs, info = env.reset(seed=7)              # a fresh world; the seed also randomizes noise
    obs["task"], obs["tools"]                  # the prompt and tool definitions for the model
    obs, reward, terminated, truncated, info = env.step(
        {"tool": "github__get_pull_request_status", "arguments": {"owner": "acme", "repo": "api", "pull_number": 4}})
    ...
    obs, reward, terminated, truncated, info = env.step({"tool": "submit", "arguments": {"answer": "Merged: 3f2a…"}})
    info["grade"]                              # every check, with weights and hard constraints
    env.trajectory()                           # the whole rollout, JSON-ready

Tools are named ``<server>__<tool>`` (valid for OpenAI and Anthropic tool names), plus ``submit``,
which ends the episode with the agent's final answer, and ``wait`` (unless ``allow_wait=False``),
which lets simulated time pass so CI can finish or a reply can arrive.

Time is virtual: episodes run as fast as the agent can act, and time only moves when something
takes time (each call takes 1-3 s, ``wait``, latency faults). To count the model's own thinking
time, pass ``elapsed`` (seconds) to ``step``. The reward comes from the environment's
checks, which look at the resulting world, never at what the agent claims: the weighted share of
checks passed, or 0 if a ``must`` check (a hard constraint) failed. Rewards are sparse (given
when the episode ends) unless ``dense=True``, which pays the change in score after every step.
``step_penalty`` subtracts a little per tool call.

Everything is deterministic given the seed: the same seed and the same actions give the same
observations and reward. ``snapshot()``/``restore()`` and ``fork()`` branch an episode mid-way
(for tree search, or many rollouts from one hard state).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from ..core.faults import TransportFault
from ..env import Environment, EnvRun

SUBMIT = {"name": "submit", "description": "Finish the task and give your final answer to the user.",
          "input_schema": {"type": "object", "properties": {"answer": {"type": "string",
                                                                       "description": "Your final answer or summary"}},
                           "required": ["answer"], "additionalProperties": False}}
WAIT = {"name": "wait", "description": "Wait before doing anything else, e.g. for CI to finish or for a reply "
                                     "to arrive. Time passes and the world moves on.",
        "input_schema": {"type": "object", "properties": {"seconds": {"type": "number", "minimum": 1, "maximum": 3600,
                                                                      "description": "How long to wait (1-3600)"}},
                         "required": ["seconds"], "additionalProperties": False}}
SEP = "__"


class ToolEnv:
    def __init__(self, env: Environment | str | Path | dict[str, Any], *, agent: str | None = None,
                 max_steps: int = 50, dense: bool = False, step_penalty: float = 0.0, allow_wait: bool = True):
        if isinstance(env, (str, Path)):
            env = Environment.load(env)
        elif isinstance(env, dict):
            env = Environment.from_dict(env, base_dir=None)
        self.base = env
        self.agent = agent or next(iter(env.agents))
        if self.agent not in env.agents:
            raise ValueError(f"unknown agent {self.agent!r}; this environment has {', '.join(env.agents)}")
        self.max_steps, self.dense, self.step_penalty, self.allow_wait = max_steps, dense, step_penalty, allow_wait
        self.run: EnvRun | None = None
        self.seed: int | None = None
        self._reset_state()

    def _reset_state(self) -> None:
        self.steps: list[dict[str, Any]] = []
        self.done = False
        self._score = 0.0
        self._final: dict[str, Any] | None = None

    # -- the episode ------------------------------------------------------------------------

    def reset(self, seed: int | None = None, options: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        self.seed = self.base.rng_seed if seed is None else int(seed)
        env = dataclasses.replace(self.base, rng_seed=self.seed)
        self.run = EnvRun(env, run_id=f"{env.name}-{self.seed}")
        self._reset_state()
        self._score = self.run.grade()["score"] if self.dense else 0.0
        return self.observation(), {"seed": self.seed, "environment": env.name, "agent": self.agent}

    def observation(self) -> dict[str, Any]:
        run = self._need_run()
        return {"task": (run.env.agents[self.agent].get("task") or run.env.task).strip(), "tools": self.tools(),
                "now": run.clock.now.isoformat()}

    def tools(self) -> list[dict[str, Any]]:
        """Tool definitions in the Anthropic shape (name, description, input_schema); for OpenAI use
        ``{"type": "function", "function": {"name", "description", "parameters": input_schema}}``."""
        run = self._need_run()
        out = []
        for server in run.env.agent_servers(self.agent):
            for t in run.instances[server].list_tools():
                out.append({"name": f"{server}{SEP}{t['name']}", "description": t["description"],
                            "input_schema": t["inputSchema"]})
        return out + ([WAIT] if self.allow_wait else []) + [SUBMIT]

    def step(self, action: dict[str, Any], elapsed: float | None = None
             ) -> tuple[dict[str, Any], float, bool, bool, dict[str, Any]]:
        """``action``: ``{"tool": name, "arguments": {...}}`` (``name``/``input`` also accepted).
        ``elapsed``: seconds the agent spent before this action (model thinking), in virtual time."""
        run = self._need_run()
        if self.done:
            raise RuntimeError("the episode is over; call reset()")
        if elapsed:
            if not 0 < elapsed <= 86400:
                raise ValueError("elapsed must be between 0 and 86400 seconds")
            run.advance(float(elapsed))
        name = str(action.get("tool") or action.get("name") or "")
        args = action.get("arguments", action.get("input")) or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = None
        record: dict[str, Any] = {"n": len(self.steps) + 1, "tool": name, "arguments": args}
        terminated = False
        if name == "submit":
            run.answer = str((args or {}).get("answer", "")) if isinstance(args, dict) else ""
            content, is_error, terminated = "Submitted.", False, True
        elif not isinstance(args, dict):
            content, is_error = "Error: arguments must be a JSON object", True
        elif name == "wait" and self.allow_wait:
            try:
                seconds = float(args.get("seconds"))
            except (TypeError, ValueError):
                seconds = -1
            if not 1 <= seconds <= 3600:
                content, is_error = "Error: seconds must be a number from 1 to 3600", True
            else:
                run.advance(seconds)
                content, is_error = f"Waited {seconds:g} seconds. It is now {run.clock.now.isoformat()}.", False
        else:
            content, is_error = self._call(run, name, args, record)
        record.update(content=content, is_error=is_error, at=run.clock.now.isoformat())
        self.steps.append(record)
        truncated = not terminated and len(self.steps) >= self.max_steps
        self.done = terminated or truncated
        reward = -self.step_penalty if name != "submit" else 0.0
        info: dict[str, Any] = {"steps": len(self.steps)}
        if self.dense or self.done:
            grade = run.grade()
            reward += grade["reward"] - self._score if self.dense else grade["reward"]
            self._score = grade["reward"]
            if self.done:
                self._final = grade
                info["grade"] = grade
        record["reward"] = reward
        return {"content": content, "is_error": is_error}, reward, terminated, truncated, info

    def _call(self, run: EnvRun, name: str, args: dict[str, Any], record: dict[str, Any]) -> tuple[str, bool]:
        server, sep, tool = name.partition(SEP)
        if not sep or server not in run.env.agent_servers(self.agent):
            return f"Error: unknown tool {name!r}", True
        inst = run.instances[server]
        try:
            result = inst.call(tool, args, agent=self.agent, as_=run.env.identity_for(self.agent, server))
        except TransportFault as e:  # what an MCP client sees when the HTTP layer fails
            record["fault"] = "transport_error"
            return f"Error: HTTP {e.status} from the {server} server", True
        record["fault"] = inst.calls[-1].get("fault") if inst.calls else None
        return result.text, result.is_error

    # -- branching --------------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {"world": self._need_run().snapshot(), "steps": json.loads(json.dumps(self.steps, default=str)),
                "done": self.done, "score": self._score, "seed": self.seed}

    def restore(self, snap: dict[str, Any]) -> None:
        if self.seed != snap["seed"]:
            self.reset(seed=snap["seed"])
        self._need_run().restore(snap["world"])
        self.steps, self.done, self._score, self._final = list(snap["steps"]), snap["done"], snap["score"], None

    def fork(self) -> ToolEnv:
        """An independent copy of this episode at this point."""
        other = ToolEnv(self.base, agent=self.agent, max_steps=self.max_steps, dense=self.dense,
                        step_penalty=self.step_penalty)
        other.reset(seed=self.seed)
        other.restore(self.snapshot())
        return other

    # -- output -----------------------------------------------------------------------------

    def trajectory(self) -> dict[str, Any]:
        """The rollout: task, every step with its result and reward, world events, final grade."""
        run = self._need_run()
        return {"environment": run.env.name, "seed": self.seed, "agent": self.agent,
                "task": self.observation()["task"], "steps": self.steps,
                "events": [e for e in run.timeline() if e["kind"] == "event"],
                "answer": run.answer, "grade": self._final or run.grade(),
                "return": sum(s.get("reward", 0.0) for s in self.steps)}

    def _need_run(self) -> EnvRun:
        if self.run is None:
            raise RuntimeError("call reset() first")
        return self.run
