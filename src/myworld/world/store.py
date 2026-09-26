"""A content-addressed store for world snapshots: durable, deduplicated, copy-on-write.

Values are stored as a Merkle tree: every mapping or list bigger than a few hundred bytes becomes
its own object named by the hash of its contents, and parents refer to children by hash. Two
checkpoints of a world share every subtree that didn't change (one new email is one new object
plus the few mappings above it), so keeping a checkpoint per step costs little, and a branch is a
reference, not a copy.

    store = Store("worlds.db")          # SQLite file (durable); Store() keeps objects in memory
    ref = store.put(snapshot)           # "sha256:..." for the whole snapshot
    same = store.get(ref)               # rebuilt on demand
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

INLINE = 256  # smaller subtrees are kept inside their parent
PREFIX = "sha256:"


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def is_ref(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {"$ref"} and str(value["$ref"]).startswith(PREFIX)


class Store:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self._mem: dict[str, str] = {}
        self._db = None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("CREATE TABLE IF NOT EXISTS objects (hash TEXT PRIMARY KEY, data TEXT NOT NULL)")
            self._db.execute("CREATE TABLE IF NOT EXISTS names (name TEXT PRIMARY KEY, ref TEXT NOT NULL)")

    # -- objects ---------------------------------------------------------------------------------

    def _write(self, h: str, text: str) -> None:
        with self._lock:
            if self._db is not None:
                self._db.execute("INSERT OR IGNORE INTO objects(hash, data) VALUES (?, ?)", (h, text))
            else:
                self._mem.setdefault(h, text)

    def _read(self, h: str) -> str:
        with self._lock:
            if self._db is not None:
                row = self._db.execute("SELECT data FROM objects WHERE hash = ?", (h,)).fetchone()
                if row is None:
                    raise KeyError(h)
                return row[0]
            return self._mem[h]

    def put(self, value: Any) -> str:
        """Store ``value``; returns the reference of its root."""
        node = self._node(value)
        if is_ref(node):
            return node["$ref"]
        text = _canon(node)
        h = PREFIX + hashlib.sha256(text.encode()).hexdigest()
        self._write(h, text)
        return h

    def _node(self, value: Any) -> Any:
        if isinstance(value, dict):
            if all(isinstance(k, str) for k in value):
                node: Any = {k: self._node(v) for k, v in value.items()}
            else:  # integer keys (issue numbers, run ids) survive the round trip
                node = {"$kd": [[k, self._node(v)] for k, v in value.items()]}
        elif isinstance(value, tuple):
            node = {"$t": [self._node(v) for v in value]}
        elif isinstance(value, list):
            node = [self._node(v) for v in value]
        else:
            return value
        text = _canon(node)
        if len(text) <= INLINE:
            return node
        h = PREFIX + hashlib.sha256(text.encode()).hexdigest()
        self._write(h, text)
        return {"$ref": h}

    def get(self, ref: str) -> Any:
        return self._expand(json.loads(self._read(ref)))

    def _expand(self, node: Any) -> Any:
        if is_ref(node):
            return self.get(node["$ref"])
        if isinstance(node, dict):
            if set(node) == {"$kd"}:
                return {(tuple(k) if isinstance(k, list) else k): self._expand(v) for k, v in node["$kd"]}
            if set(node) == {"$t"}:
                return tuple(self._expand(v) for v in node["$t"])
            return {k: self._expand(v) for k, v in node.items()}
        if isinstance(node, list):
            return [self._expand(v) for v in node]
        return node

    # -- names (durable pointers, e.g. a saved run) -----------------------------------------------------

    def name(self, name: str, ref: str) -> None:
        with self._lock:
            if self._db is not None:
                self._db.execute("INSERT OR REPLACE INTO names(name, ref) VALUES (?, ?)", (name, ref))
            else:
                self._mem["name:" + name] = ref

    def lookup(self, name: str) -> str:
        with self._lock:
            if self._db is not None:
                row = self._db.execute("SELECT ref FROM names WHERE name = ?", (name,)).fetchone()
                if row is None:
                    raise KeyError(name)
                return row[0]
            return self._mem["name:" + name]

    def names(self) -> list[str]:
        with self._lock:
            if self._db is not None:
                found = [r[0] for r in self._db.execute("SELECT name FROM names ORDER BY name")]
            else:
                found = sorted(k[5:] for k in self._mem if k.startswith("name:"))
            return [n for n in found if not n.startswith("__")]

    def _children(self, text: str) -> list[str]:
        out: list[str] = []

        def walk(node: Any) -> None:
            if is_ref(node):
                out.append(node["$ref"])
            elif isinstance(node, dict):
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(json.loads(text))
        return out

    def gc(self, live: list[str] | None = None) -> dict[str, int]:
        """Delete objects no saved name and no ``live`` reference (e.g. running checkpoints) reaches."""
        with self._lock:
            if self._db is not None:
                roots = [r[0] for r in self._db.execute("SELECT ref FROM names") if r[0].startswith(PREFIX)]
                every = [r[0] for r in self._db.execute("SELECT hash FROM objects")]
            else:
                roots = [v for k, v in self._mem.items() if k.startswith("name:") and v.startswith(PREFIX)]
                every = [k for k in self._mem if k.startswith(PREFIX)]
            seen: set[str] = set()
            stack = [*roots, *(live or [])]
            while stack:
                h = stack.pop()
                if h in seen:
                    continue
                seen.add(h)
                try:
                    stack.extend(self._children(self._read(h)))
                except KeyError:
                    continue
            dead = [h for h in every if h not in seen]
            if self._db is not None:
                self._db.executemany("DELETE FROM objects WHERE hash = ?", [(h,) for h in dead])
                if len(dead) > 1000:
                    self._db.execute("VACUUM")
            else:
                for h in dead:
                    self._mem.pop(h, None)
            return {"deleted": len(dead), "kept": len(seen & set(every))}

    def delete_name(self, name: str) -> None:
        with self._lock:
            if self._db is not None:
                self._db.execute("DELETE FROM names WHERE name = ?", (name,))
            else:
                self._mem.pop("name:" + name, None)

    def stats(self) -> dict[str, int]:
        with self._lock:
            if self._db is not None:
                n, size = self._db.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(data)), 0) FROM objects").fetchone()
                return {"objects": n, "bytes": size}
            objs = [v for k, v in self._mem.items() if k.startswith(PREFIX)]
            return {"objects": len(objs), "bytes": sum(len(v) for v in objs)}

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
