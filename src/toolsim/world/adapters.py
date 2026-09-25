"""Components for things that aren't simulated services.

- ``DirectoryComponent``: a directory, e.g. the agent's workspace or a repo checkout.
- ``SQLiteComponent``: a SQLite database, e.g. an app's backend.
- ``RemoteComponent``: any process, in any language, that speaks the component protocol below;
  ``serve_component`` exposes any component that way.

The component protocol (JSON over HTTP)::

    GET  /state                 -> the state as data, now
    POST /view                  {"snapshot": ...} -> the state as data at a snapshot it returned
    POST /snapshot              -> {"snapshot": <anything JSON>}
    POST /restore               {"snapshot": ...} -> {}
    POST /mutate                {"op": "...", "params": {...}} -> {"result": ...}
    GET  /mutations             -> [{"name", "doc", "params"}]
    POST /clone                 -> {"url": "<base url of an independent copy>"}   (optional)
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import sqlite3
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .component import Component

MAX_FILE = 5 * 1024 * 1024


# -- a directory -------------------------------------------------------------------------------------

class DirectoryComponent(Component):
    """Files under a directory. Snapshots hold every file (text as text, binary as base64), so a
    restore puts the directory back exactly, removing files that appeared since."""

    kind = "directory"

    def __init__(self, path: str | Path | None = None, files: dict[str, str] | None = None):
        self.owned = path is None
        self.root = Path(path) if path else Path(tempfile.mkdtemp(prefix="toolsim-dir-"))
        self.root.mkdir(parents=True, exist_ok=True)
        for rel, content in (files or {}).items():
            self._write(rel, content)

    def _safe(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if not p.is_relative_to(self.root.resolve()) or p == self.root.resolve():
            raise ValueError(f"{rel!r} is outside the directory")
        return p

    def _write(self, rel: str, content: str | bytes) -> None:
        p = self._safe(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content.encode() if isinstance(content, str) else content)

    def _files(self) -> list[Path]:
        return sorted(p for p in self.root.rglob("*") if p.is_file() and not p.is_symlink())

    def snapshot(self) -> Any:
        out = {}
        for p in self._files():
            data = p.read_bytes()[:MAX_FILE]
            rel = p.relative_to(self.root).as_posix()
            try:
                out[rel] = {"text": data.decode()}
            except UnicodeDecodeError:
                out[rel] = {"b64": base64.b64encode(data).decode()}
        return {"files": out}

    def restore(self, snap: Any) -> None:
        want = snap["files"]
        for p in self._files():
            if p.relative_to(self.root).as_posix() not in want:
                p.unlink()
        for rel, f in want.items():
            self._write(rel, f["text"] if "text" in f else base64.b64decode(f["b64"]))
        for d in sorted((p for p in self.root.rglob("*") if p.is_dir()), reverse=True):
            if not any(d.iterdir()):
                d.rmdir()

    def clone(self) -> DirectoryComponent:
        other = DirectoryComponent()
        other.restore(self.snapshot())
        return other

    def view(self, snap: Any = None) -> Any:
        files = (snap or self.snapshot())["files"]
        return {"files": {rel: f["text"] if "text" in f else
                          f"<binary {len(base64.b64decode(f['b64']))} bytes sha256:"
                          f"{hashlib.sha256(base64.b64decode(f['b64'])).hexdigest()[:12]}>"
                          for rel, f in files.items()}}

    def mutate(self, op: str, **params: Any) -> Any:
        if op == "write":
            self._write(params["path"], params.get("content", ""))
        elif op == "append":
            p = self._safe(params["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a") as fh:
                fh.write(params.get("content", ""))
        elif op == "delete":
            p = self._safe(params["path"])
            if not p.exists():
                raise ValueError(f"no file {params['path']!r}")
            p.unlink()
        elif op == "replace":
            p = self._safe(params["path"])
            text = p.read_text()
            if params["old"] not in text:
                raise ValueError(f"{params['old']!r} isn't in {params['path']}")
            p.write_text(text.replace(params["old"], params.get("new", ""), int(params.get("count", -1))))
        else:
            return super().mutate(op, **params)
        return None

    def mutations(self) -> list[dict[str, Any]]:
        return [{"name": "write", "doc": "Create or overwrite a file.", "params": ["path", "content"]},
                {"name": "append", "doc": "Append to a file.", "params": ["path", "content"]},
                {"name": "delete", "doc": "Delete a file.", "params": ["path"]},
                {"name": "replace", "doc": "Replace text in a file.", "params": ["path", "old", "new", "count"]}]

    def close(self) -> None:
        if self.owned:
            shutil.rmtree(self.root, ignore_errors=True)


# -- a SQLite database -----------------------------------------------------------------------------

def _q(name: str) -> str:
    """A table name from sqlite_master, safe inside double quotes."""
    return name.replace('"', '""')


class SQLiteComponent(Component):
    """A SQLite database. Snapshots are SQL dumps (readable, diffable); the view is every table's
    rows, in primary-key order."""

    kind = "sqlite"

    def __init__(self, path: str | Path | None = None, sql: str | list[str] | None = None):
        self.owned = path is None
        self.path = Path(path) if path else Path(tempfile.mkdtemp(prefix="toolsim-db-")) / "db.sqlite"
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        for stmt in [sql] if isinstance(sql, str) else sql or []:
            self.db.executescript(stmt)

    def snapshot(self) -> Any:
        with self._lock:
            return {"sql": "\n".join(self.db.iterdump())}

    def restore(self, snap: Any) -> None:
        with self._lock:
            tables = [r[0] for r in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            self.db.execute("PRAGMA foreign_keys=OFF")
            for t in tables:
                self.db.execute(f'DROP TABLE IF EXISTS "{_q(t)}"')
            for kind in ("view", "trigger", "index"):
                for (name,) in list(self.db.execute("SELECT name FROM sqlite_master WHERE type=? "
                                                    "AND name NOT LIKE 'sqlite_%'", (kind,))):
                    self.db.execute(f'DROP {kind.upper()} IF EXISTS "{_q(name)}"')
            self.db.executescript(snap["sql"])

    def clone(self) -> SQLiteComponent:
        other = SQLiteComponent()
        other.restore(self.snapshot())
        return other

    @staticmethod
    def _tables(db: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
        out = {}
        for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                               "ORDER BY name"):
            cur = db.execute(f'SELECT * FROM "{_q(t)}" ORDER BY rowid')  # noqa: S608  (a quoted table name)
            cols = [c[0] for c in cur.description]
            out[t] = [dict(zip(cols, row)) for row in cur.fetchall()]
        return out

    def view(self, snap: Any = None) -> Any:
        if snap is None:
            with self._lock:
                return {"tables": self._tables(self.db)}
        mem = sqlite3.connect(":memory:")
        mem.executescript(snap["sql"])
        try:
            return {"tables": self._tables(mem)}
        finally:
            mem.close()

    def mutate(self, op: str, **params: Any) -> Any:
        if op != "sql":
            return super().mutate(op, **params)
        with self._lock:
            cur = self.db.execute(params["statement"], params.get("params") or [])
            if cur.description:
                cols = [c[0] for c in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
            return {"rowcount": cur.rowcount}

    def mutations(self) -> list[dict[str, Any]]:
        return [{"name": "sql", "doc": "Run one SQL statement (with ? parameters).", "params": ["statement", "params"]}]

    def close(self) -> None:
        self.db.close()
        if self.owned:
            shutil.rmtree(self.path.parent, ignore_errors=True)


# -- a remote process -------------------------------------------------------------------------------------

class RemoteComponent(Component):
    """A component served elsewhere over the component protocol (any language)."""

    kind = "remote"

    def __init__(self, url: str, timeout: float = 30.0):
        if urlsplit(url).scheme not in ("http", "https"):
            raise ValueError("a remote component's url must be http:// or https://")
        self.url = url.rstrip("/")
        self.timeout = timeout
        self._views: dict[str, Any] = {}

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,  # noqa: S310  (http(s) only)
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310
            text = r.read()
        return json.loads(text) if text else None

    def snapshot(self) -> Any:
        return self._call("POST", "/snapshot", {})["snapshot"]

    def restore(self, snap: Any) -> None:
        self._call("POST", "/restore", {"snapshot": snap})

    def clone(self) -> RemoteComponent:
        return RemoteComponent(self._call("POST", "/clone", {})["url"], self.timeout)

    def view(self, snap: Any = None) -> Any:
        if snap is None:
            return self._call("GET", "/state")
        return self._call("POST", "/view", {"snapshot": snap})

    def mutate(self, op: str, **params: Any) -> Any:
        return self._call("POST", "/mutate", {"op": op, "params": params}).get("result")

    def mutations(self) -> list[dict[str, Any]]:
        return self._call("GET", "/mutations")


def serve_component(component: Component, host: str = "127.0.0.1", port: int = 0) -> tuple[str, ThreadingHTTPServer]:
    """Serve ``component`` over the component protocol; returns (base url, server). ``/clone``
    serves the copy on a new port."""
    lock = threading.RLock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            pass

        def _send(self, status: int, value: Any) -> None:
            body = json.dumps(value, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> Any:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self) -> None:  # noqa: N802
            url = urlsplit(self.path)
            with lock:
                if url.path == "/state":
                    self._send(200, component.view())
                elif url.path == "/mutations":
                    self._send(200, component.mutations())
                else:
                    self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            try:
                body = self._body()
                with lock:
                    if path == "/snapshot":
                        self._send(200, {"snapshot": component.snapshot()})
                    elif path == "/restore":
                        component.restore(body["snapshot"])
                        self._send(200, {})
                    elif path == "/view":
                        self._send(200, component.view(body["snapshot"]))
                    elif path == "/mutate":
                        self._send(200, {"result": component.mutate(body["op"], **(body.get("params") or {}))})
                    elif path == "/clone":
                        url, _ = serve_component(component.clone(), host, 0)
                        self._send(200, {"url": url})
                    else:
                        self._send(404, {"error": "not found"})
            except (ValueError, KeyError, TypeError, NotImplementedError) as e:
                self._send(400, {"error": str(e)})

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://{host}:{server.server_address[1]}", server


def build(spec: dict[str, Any], base_dir: Path | None) -> Component:
    """A component from an environment's ``components:`` entry."""
    kind = spec.get("type")
    path = spec.get("path")
    if path is not None:
        if base_dir is None:
            raise ValueError("components in inline specs can't name a path; leave it out for a fresh one")
        root = Path(base_dir).resolve()
        full = (root / str(path)).resolve()
        if not full.is_relative_to(root):
            raise ValueError("a component's path must be inside the environment's directory")
        path = full
    if kind == "directory":
        return DirectoryComponent(path, spec.get("files"))
    if kind == "sqlite":
        return SQLiteComponent(path, spec.get("sql"))
    if kind == "remote":
        if base_dir is None:
            raise ValueError("remote components aren't allowed in inline specs")
        return RemoteComponent(spec["url"])
    raise ValueError(f"unknown component type {kind!r} (directory, sqlite or remote)")
