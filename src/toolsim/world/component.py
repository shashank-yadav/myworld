"""Components: anything with state that a world can snapshot, restore, clone, mutate, view and diff.

A component is one piece of an agent's world: a simulated service (Gmail, GitHub...), the agent's
working directory, a SQLite database, or any process that speaks the HTTP component protocol
(``adapters.RemoteComponent``). The world runtime only needs these six things:

    snapshot()        -> JSON-able state, enough to come back to this exact point
    restore(snap)     -> go back to it
    clone()           -> an independent copy (for forks and branches)
    view(snap=None)   -> the state as data (for diffs, checks and grading), now or at a snapshot
    mutate(op, **p)   -> a named, semantic change ("write a file", "run SQL", "deliver an email")
    mutations()       -> what mutate() accepts: [{name, doc, params}]
"""

from __future__ import annotations

import json
from typing import Any


class Component:
    kind = "component"

    def snapshot(self) -> Any:
        raise NotImplementedError

    def restore(self, snap: Any) -> None:
        raise NotImplementedError

    def clone(self) -> Component:
        raise NotImplementedError(f"{self.kind} components can't be cloned")

    def view(self, snap: Any = None) -> Any:
        raise NotImplementedError

    def mutate(self, op: str, **params: Any) -> Any:
        raise ValueError(f"{self.kind}: unknown mutation {op!r}")

    def mutations(self) -> list[dict[str, Any]]:
        return []

    def close(self) -> None:
        """Release what the component holds (temporary directories, connections)."""


# -- diffs ------------------------------------------------------------------------------------------

def _short(v: Any, limit: int = 200) -> Any:
    if isinstance(v, str) and len(v) > limit:
        return v[:limit] + f"... ({len(v)} chars)"
    if isinstance(v, (dict, list)):
        text = json.dumps(v, default=str)
        if len(text) > limit:
            return f"<{type(v).__name__} of {len(v)}>"
    return v


def _keyed(items: list[Any]) -> dict[str, Any] | None:
    """Lists of records with ids are compared by id, not position."""
    if items and all(isinstance(x, dict) for x in items):
        for key in ("id", "key", "number", "resourceName", "path", "name"):
            vals = [x.get(key) for x in items]
            if all(v is not None for v in vals) and len(set(map(str, vals))) == len(vals):
                return {str(v): x for v, x in zip(vals, items)}
    return None


def diff(a: Any, b: Any, path: str = "", out: list[dict[str, Any]] | None = None,
         limit: int = 500) -> list[dict[str, Any]]:
    """The changes from ``a`` to ``b``: [{"path", "op": added|removed|changed, "before", "after"}]."""
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(a, list) and isinstance(b, list):
        ka, kb = _keyed(a), _keyed(b)
        if ka is not None and kb is not None:
            a, b = ka, kb
        else:
            a, b = dict(enumerate(a)), dict(enumerate(b))
    if isinstance(a, dict) and isinstance(b, dict):
        for k in list(a) + [k for k in b if k not in a]:
            sub = f"{path}.{k}" if path else str(k)
            if str(k).startswith("_"):
                continue  # internal bookkeeping, not part of the world
            if k not in b:
                out.append({"path": sub, "op": "removed", "before": _short(a[k])})
            elif k not in a:
                out.append({"path": sub, "op": "added", "after": _short(b[k])})
            else:
                diff(a[k], b[k], sub, out, limit)
        return out
    if a != b:
        out.append({"path": path, "op": "changed", "before": _short(a), "after": _short(b)})
    return out


# -- simulated services ---------------------------------------------------------------------------------

class ServiceComponent(Component):
    """A toolsim service instance (its calls, time and world actions stay with the instance)."""

    kind = "service"

    def __init__(self, instance: Any):
        self.instance = instance

    def snapshot(self) -> Any:
        return self.instance.snapshot()

    def restore(self, snap: Any) -> None:
        self.instance.restore(snap)

    def view(self, snap: Any = None) -> Any:
        state = snap["state"] if snap is not None else self.instance.state
        return self.instance.service.grading_view(state)

    def mutate(self, op: str, **params: Any) -> Any:
        as_ = params.pop("as", None)
        return self.instance.apply_action(op, params, as_=as_, source="mutation")

    def mutations(self) -> list[dict[str, Any]]:
        return [{"name": a.name, "doc": a.description} for a in self.instance.service.actions]
