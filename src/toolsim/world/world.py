"""A world: named components, a journal of everything that happened, and checkpoints.

    world = World()
    world.add("workspace", DirectoryComponent(files={"README.md": "# app"}))
    world.add("db", SQLiteComponent(sql="CREATE TABLE orders(id INTEGER PRIMARY KEY, status TEXT)"))
    world.mutate("db", "sql", statement="INSERT INTO orders(status) VALUES ('paid')")
    cp = world.checkpoint()
    ...
    other = world.branch(cp)              # an independent copy as it was at that checkpoint
    world.diff(cp)                        # what changed since, component by component
    world.evaluate([{"server": "db", "state": "tables.orders", "where": {"status": "refunded"}}])

Branching restores the nearest checkpoint at or before the target step, then replays the journal
from there. Things that happen outside the world's API (an agent editing files directly) can't be
replayed, only checkpointed: take a checkpoint after each agent step to branch anywhere.
Environment runs (``toolsim.env.EnvRun``) are worlds too, with simulated services as components
and calls, world events and time in the journal.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from collections.abc import Callable
from typing import Any

from .component import Component, diff

Entry = dict[str, Any]


def result_sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _part(snap: dict[str, Any], name: str) -> Any:
    """One component's snapshot inside a world's (or an environment run's) snapshot."""
    for key in ("components", "instances"):
        if name in (snap.get(key) or {}):
            return snap[key][name]
    return None


class World:
    def __init__(self, lock: threading.RLock | None = None, now: Callable[[], str] | None = None):
        self.components: dict[str, Component] = {}
        self.journal: list[Entry] = []
        self.checkpoints: dict[int, dict[str, Any]] = {}  # journal length -> snapshot
        self.lock = lock or threading.RLock()
        self._now = now
        self.replaying = False

    # -- composition --------------------------------------------------------------------------

    def add(self, name: str, component: Component) -> Component:
        if name in self.components:
            raise ValueError(f"component {name!r} already exists")
        self.components[name] = component
        return component

    def close(self) -> None:
        for c in self.components.values():
            c.close()

    # -- the journal ----------------------------------------------------------------------------

    def record(self, entry: Entry) -> Entry:
        with self.lock:
            entry = {"i": len(self.journal), **({"at": self._now()} if self._now else {}), **entry}
            self.journal.append(entry)
            return entry

    def mutate(self, name: str, op: str, **params: Any) -> Any:
        """A semantic change to one component, recorded so replays and branches repeat it."""
        with self.lock:
            comp = self.components.get(name)
            if comp is None:
                raise ValueError(f"no component {name!r}")
            result = comp.mutate(op, **copy.deepcopy(params))
            self.record({"kind": "mutate", "component": name, "op": op, "params": params,
                         "result_sha": result_sha(result)})
            return result

    # -- state ---------------------------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {"journal": len(self.journal),
                    "components": {n: c.snapshot() for n, c in self.components.items()}}

    def restore(self, snap: dict[str, Any]) -> None:
        with self.lock:
            for n, c in self.components.items():
                if n in snap["components"]:
                    c.restore(snap["components"][n])

    def checkpoint(self, snap: dict[str, Any] | None = None) -> int:
        """Remember the state now; returns the step (journal length) it belongs to."""
        with self.lock:
            snap = snap if snap is not None else self.snapshot()
            self.checkpoints[len(self.journal)] = snap
            return len(self.journal)

    def view(self) -> dict[str, Any]:
        return {n: c.view() for n, c in self.components.items()}

    def diff(self, since: int | dict[str, Any], until: int | dict[str, Any] | None = None) -> dict[str, list[Entry]]:
        """What changed, component by component, between two checkpoints (or snapshots), or since one."""
        a = self._snap(since)
        b = self._snap(until) if until is not None else None
        out = {}
        for n, c in self.components.items():
            pa = _part(a, n)
            before = c.view(pa) if pa is not None else {}
            pb = _part(b, n) if b is not None else None
            after = c.view(pb) if pb is not None else c.view()
            changes = diff(before, after)
            if changes:
                out[n] = changes
        return out

    def _snap(self, ref: int | dict[str, Any]) -> dict[str, Any]:
        if isinstance(ref, dict):
            return ref
        if ref not in self.checkpoints:
            raise ValueError(f"no checkpoint at step {ref} (have {sorted(self.checkpoints)})")
        return self.checkpoints[ref]

    # -- branching and replay ------------------------------------------------------------------------

    def nearest_checkpoint(self, step: int) -> int:
        before = [k for k in self.checkpoints if k <= step]
        if not before:
            raise ValueError(f"no checkpoint at or before step {step}")
        return max(before)

    def fork(self) -> World:
        """An independent copy of this world as it is now (components cloned, journal copied)."""
        with self.lock:
            other = World(now=self._now)
            for n, c in self.components.items():
                other.add(n, c.clone())
            other.journal = copy.deepcopy(self.journal)
            other.checkpoints = copy.deepcopy(self.checkpoints)
            return other

    def branch(self, step: int, apply: Callable[[World, Entry], Any] | None = None) -> World:
        """An independent world as this one was after ``step`` journal entries."""
        if step < 0 or step > len(self.journal):
            raise ValueError(f"step must be within 0..{len(self.journal)}")
        base = self.nearest_checkpoint(step)
        other = self.fork()
        other.restore(self.checkpoints[base])
        other.journal = copy.deepcopy(self.journal[:base])
        other.checkpoints = {k: v for k, v in other.checkpoints.items() if k <= base}
        other.replay(self.journal[base:step], apply)
        return other

    def replay(self, entries: list[Entry], apply: Callable[[World, Entry], Any] | None = None) -> list[dict[str, Any]]:
        """Apply journal entries again; returns where the results differ from the recording."""
        apply = apply or World.apply
        diverged = []
        self.replaying = True
        try:
            for e in entries:
                got = apply(self, e)
                if e.get("result_sha") and got is not None and got != e["result_sha"]:
                    diverged.append({"step": e["i"], "entry": e, "got": got})
        finally:
            self.replaying = False
        return diverged

    def apply(self, e: Entry) -> str | None:
        """Re-apply one entry (mutations only; environment runs handle calls, events and time)."""
        if e["kind"] == "mutate":
            return result_sha(self.mutate(e["component"], e["op"], **e["params"]))
        raise ValueError(f"can't replay a {e['kind']!r} entry in a bare world")

    # -- evaluation ---------------------------------------------------------------------------------

    def evaluate(self, checks: list[dict[str, Any]], answer: str | None = None) -> dict[str, Any]:
        """Grade the current state with the environment check language (state/answer checks)."""
        from ..env import Environment
        env = Environment(name="world", task="", servers={}, checks=checks)
        return env.grade({n: {"view": v, "state": v, "calls": []} for n, v in self.view().items()}, answer)
