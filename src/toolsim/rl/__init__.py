"""Reinforcement learning on realistic tool worlds.

- ``ToolEnv``: one episode at a time, Gymnasium-style (``toolsim.rl.episode``).
- ``EnvPool``: many episodes at once across worker processes (``toolsim.rl.pool``).
- ``tasks``: generated, validated tasks with verifiers and reference solutions (``toolsim.rl.tasks``).
"""

from . import tasks
from .episode import ToolEnv
from .pool import EnvPool, run_references
from .tasks import generate, validate

__all__ = ["EnvPool", "ToolEnv", "generate", "run_references", "tasks", "validate"]
