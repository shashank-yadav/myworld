"""Fixtures for live tests against a real Hermes Agent and a local model.

Opt in with ``AOPS_LIVE=1``. Needs ``hermes`` on PATH and Ollama serving the model named by
``AOPS_LIVE_MODEL`` (default qwen3:4b-instruct) with at least 64k context. Every test gets
the same isolated HERMES_HOME, so the user's own Hermes config is never touched.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).parent
MODEL = os.environ.get("AOPS_LIVE_MODEL", "qwen3:4b-instruct")
OLLAMA = os.environ.get("AOPS_LIVE_OLLAMA", "http://127.0.0.1:11434")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _hermes_python() -> str:
    """The interpreter inside Hermes' venv (it ships the `mcp` package the fake server needs)."""
    if os.environ.get("AOPS_LIVE_MCP_PYTHON"):
        return os.environ["AOPS_LIVE_MCP_PYTHON"]
    wrapper = Path(shutil.which("hermes") or "").read_text()
    m = re.search(r'exec "([^"]+/python[0-9.]*)"', wrapper)
    return m.group(1) if m else sys.executable


def _wait_http(url: str, timeout: float = 15) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"{url} did not come up")


class Live:
    def __init__(self, root: Path):
        self.root = root
        self.hermes_home = root / "hermes"
        self.aops_home = root / "aops"
        self.work = root / "work"
        self.mcp_log = root / "mcp_side_effects.jsonl"
        self.http_log = root / "http_side_effects.log"
        self.port = _free_port()
        self.http_port = _free_port()
        self.endpoint = f"http://127.0.0.1:{self.port}"
        self.procs: list[subprocess.Popen] = []
        for d in (self.hermes_home, self.aops_home, self.work):
            d.mkdir(parents=True, exist_ok=True)

    # -- environment -------------------------------------------------------------------

    def env(self, **extra: str) -> dict[str, str]:
        env = {**os.environ, "HERMES_HOME": str(self.hermes_home), "AOPS_HOME": str(self.aops_home),
               "AOPS_ENDPOINT": self.endpoint}
        env.update(extra)
        return env

    def start_collector(self) -> None:
        self.collector = subprocess.Popen(
            [sys.executable, "-m", "aops.cli", "--db", str(self.root / "events.db"), "serve", "--port", str(self.port)],
            env=self.env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(self.collector)
        _wait_http(f"{self.endpoint}/api/health")

    def configure_hermes(self) -> None:
        def hermes(*args: str) -> None:
            subprocess.run(["hermes", *args], env=self.env(), check=True, capture_output=True, timeout=120)

        for key, value in [("model.provider", "ollama"), ("model.default", MODEL), ("model.base_url", f"{OLLAMA}/v1"),
                           ("model.ollama_num_ctx", "65536"), ("tools.tool_search.enabled", "off")]:
            hermes("config", "set", key, value)
        with open(self.hermes_home / "config.yaml", "a") as f:
            f.write("\nmcp_servers:\n  acme:\n"
                    f"    command: {json.dumps(_hermes_python())}\n"
                    f"    args: [{json.dumps(str(HERE / 'fake_mcp_server.py'))}]\n"
                    f"    env:\n      FAKE_MCP_LOG: {json.dumps(str(self.mcp_log))}\n")
        from aops.cli import main as aops_cli
        aops_cli(["install", "hermes", "--dest", str(self.hermes_home / "plugins" / "aops")])
        hermes("plugins", "enable", "aops")

    def start_slow_http(self) -> None:
        self.procs.append(subprocess.Popen(
            [sys.executable, str(HERE / "slow_http_server.py"), str(self.http_port), str(self.http_log)]))

    def stop(self) -> None:
        for p in self.procs:
            p.send_signal(signal.SIGTERM)
        for p in self.procs:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()

    # -- driving Hermes ----------------------------------------------------------------

    def hermes_cmd(self, prompt: str, toolsets: str | None, yolo: bool, args: tuple[str, ...]) -> list[str]:
        # toolsets=None enables everything. MCP tools need that: Hermes validates -t names before
        # background MCP discovery has registered the mcp-<server> toolsets.
        return ["hermes", "chat", "-Q", *(["--yolo"] if yolo else []), *(["-t", toolsets] if toolsets else []),
                *args, "-q", prompt]

    def run(self, prompt: str, *, toolsets: str | None = "terminal,file", yolo: bool = True, args: tuple[str, ...] = (),
            env: dict[str, str] | None = None, timeout: float = 600) -> tuple[str, str | None]:
        """Run one Hermes turn to completion. Returns (output, session_id)."""
        p = subprocess.run(self.hermes_cmd(prompt, toolsets, yolo, args), cwd=self.work, env=self.env(**(env or {})),
                           capture_output=True, text=True, timeout=timeout)
        out = p.stdout + p.stderr
        m = re.search(r"session_id:\s*(\S+)", out)
        return out, (m.group(1) if m else None)

    def start(self, prompt: str, *, toolsets: str = "terminal,file", yolo: bool = True,
              env: dict[str, str] | None = None) -> subprocess.Popen:
        p = subprocess.Popen(self.hermes_cmd(prompt, toolsets, yolo, ()), cwd=self.work, env=self.env(**(env or {})),
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        self.procs.append(p)
        return p

    # -- reading what aops recorded ----------------------------------------------------

    def api(self, path: str) -> Any:
        with urllib.request.urlopen(f"{self.endpoint}{path}", timeout=10) as r:
            return json.load(r)

    def runs(self) -> list[dict[str, Any]]:
        return self.api("/api/runs?limit=500")["runs"]

    def run_detail(self, run_id: str) -> dict[str, Any]:
        return self.api(f"/api/runs/{urllib.parse.quote(run_id, safe='')}")

    def raw_events(self, run_id: str) -> list[dict[str, Any]]:
        return self.api(f"/api/runs/{urllib.parse.quote(run_id, safe='')}/events")["events"]

    def session_runs(self, session_id: str | None, *, wait: float = 10) -> list[dict[str, Any]]:
        """Top-level runs for a Hermes session (waits briefly for the plugin to flush)."""
        assert session_id, "Hermes did not print a session id"
        deadline = time.time() + wait
        while True:
            found = [r for r in self.runs() if r["session_id"] == session_id and not r["parent_run_id"]]
            if found or time.time() > deadline:
                return found
            time.sleep(0.5)

    def the_run(self, session_id: str | None) -> dict[str, Any]:
        runs = [r for r in self.session_runs(session_id) if r["summary"]["calls"]]
        assert runs, f"no run recorded for session {session_id}"
        return self.run_detail(runs[0]["run_id"])

    @staticmethod
    def spans(detail: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []

        def walk(nodes: list[dict[str, Any]]) -> None:
            for n in nodes:
                out.append(n)
                walk(n["children"])
        walk(detail["roots"])
        return out

    @staticmethod
    def command(span: dict[str, Any]) -> str:
        args = span["attrs"].get("args") or {}
        return str(args.get("command") or "") if isinstance(args, dict) else ""


def _ollama_ready() -> bool:
    try:
        tags = json.load(urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=2))
        return any(m["name"].split(":latest")[0] == MODEL for m in tags.get("models", []))
    except OSError:
        return False


@pytest.fixture(scope="session")
def live(tmp_path_factory: pytest.TempPathFactory):
    if os.environ.get("AOPS_LIVE") != "1":
        pytest.skip("live Hermes tests are opt-in: AOPS_LIVE=1")
    if not shutil.which("hermes"):
        pytest.skip("hermes is not installed")
    if not _ollama_ready():
        pytest.skip(f"Ollama at {OLLAMA} is not serving {MODEL}")
    lv = Live(tmp_path_factory.mktemp("live"))
    try:
        lv.start_collector()
        lv.configure_hermes()
        lv.start_slow_http()
        yield lv
    finally:
        lv.stop()
