"""The real clients against the gateway: gog (OpenClaw), gh, and Hermes' google_api.py.

Skipped unless MYWORLD_CLIENTS_BIN points at a directory with ``gog`` and/or ``gh`` (on macOS, Go
binaries built to honor SSL_CERT_FILE), and, for Hermes, MYWORLD_HERMES_API=<python> <google_api.py>
with pysocks installed next to Google's client libraries.
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from myworld.env import Environment
from myworld.host import Host, HostConfig

BIN = os.environ.get("MYWORLD_CLIENTS_BIN")
HERMES = os.environ.get("MYWORLD_HERMES_API")
pytest.importorskip("cryptography")

SPEC = {"name": "clients", "now": "2026-09-21T16:00:00Z",
        "servers": {"gmail": {}, "calendar": {}, "drive": {}, "github": {}}}


def have(name: str) -> bool:
    return bool(BIN) and (Path(BIN) / name).exists()


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    host = Host(HostConfig(gateway_port=0, ca_dir=tmp_path_factory.mktemp("ca"), gateway_passthrough=False))
    run = host.start_env(Environment.from_dict(SPEC), "clients")
    cred = host.credentials("http://unused", run=run, agent="agent")
    home = Path(tempfile.mkdtemp())
    hermes_home = home / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "google_token.json").write_text(json.dumps({**cred["google_authorized_user"],
                                                               "type": "authorized_user"}))
    env = {**os.environ, **cred["env"], "HOME": str(home), "HERMES_HOME": str(hermes_home),
           "GOG_KEYRING_BACKEND": "file", "GOG_KEYRING_PASSWORD": "x", "GOG_TIMEZONE": "America/Los_Angeles",
           "GH_CONFIG_DIR": str(home / "gh"), "NO_COLOR": "1", "PATH": f"{BIN}:{os.environ['PATH']}" if BIN else
           os.environ["PATH"]}

    def sh(cmd: str) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, shell=True, env=env, capture_output=True, text=True, timeout=60)  # noqa: S602  (fixed test commands)
    yield run, sh
    host.gateway.stop()
    shutil.rmtree(home, ignore_errors=True)


@pytest.mark.skipif(not have("gog"), reason="set MYWORLD_CLIENTS_BIN to a directory with gog")
def test_gog_workspace(world):
    run, sh = world
    out = sh("gog gmail search 'is:unread' --max 10 --json")
    assert out.returncode == 0, out.stderr
    assert "Q4 planning" in {t["subject"] for t in json.loads(out.stdout)["threads"]}
    assert sh("gog gmail send --to john@acme.com --subject 'From gog' --body 'hi' --no-input").returncode == 0
    john = run.instances["gmail"].state["mailboxes"]["john@acme.com"]["messages"].values()
    assert any(m["subject"] == "From gog" for m in john), "delivered to John's mailbox"
    created = sh("gog calendar create primary --summary 'gog sync' --from 2026-09-22T15:00:00-07:00 "
                 "--to 2026-09-22T15:30:00-07:00 --attendees john@acme.com")
    assert created.returncode == 0, created.stderr
    assert any(e.get("summary") == "gog sync" for e in run.instances["calendar"].state["events"].values())
    sheet = next(f["id"] for f in run.instances["drive"].state["files"].values() if f["name"] == "Q4 budget")
    assert sh(f"gog sheets update {sheet} 'Sheet1!A5:B5' --values-json '[[\"ops\",\"1200\"]]' "
              "--input USER_ENTERED").returncode == 0
    assert "ops" in sh(f"gog sheets get {sheet} 'Sheet1!A1:C9' --json").stdout
    assert "John Park" in sh("gog contacts list --max 20").stdout
    calls = [c["tool"] for c in run.calls()]
    assert "gmail.users.messages.send" in calls and "sheets.spreadsheets.values.update" in calls


@pytest.mark.skipif(not have("gh"), reason="set MYWORLD_CLIENTS_BIN to a directory with gh")
def test_gh_github(world):
    run, sh = world
    assert "Logged in to github.com account alex-rivera" in sh("gh auth status").stdout + sh("gh auth status").stderr
    prs = json.loads(sh("gh pr list --repo acme/api --json number,title,state").stdout)
    assert {"number": 4, "title": "Fix flaky retry jitter", "state": "OPEN"} in prs
    assert sh("gh pr diff 4 --repo acme/api").stdout.startswith("diff --git a/src/retry.py")
    checks = sh("gh pr checks 4 --repo acme/api")
    assert checks.returncode == 0 and checks.stdout.startswith("ci\tpass")
    made = sh("gh issue create --repo acme/api --title 'From gh' --body 'details'")
    assert made.stdout.strip().startswith("https://github.com/acme/api/issues/")
    assert "From gh" in {i["title"] for i in run.instances["github"].state["repos"]["acme/api"]["issues"].values()}
    assert "completed\tsuccess" in sh("gh run list --repo acme/api").stdout


@pytest.mark.skipif(not HERMES, reason="set MYWORLD_HERMES_API to '<python> <google_api.py>'")
def test_hermes_google_api(world):
    run, sh = world
    found = sh(f'{HERMES} gmail search "is:unread" --max 10')
    assert found.returncode == 0, found.stderr
    assert "Q4 planning" in {m["subject"] for m in json.loads(found.stdout)}
    events = json.loads(sh(f"{HERMES} calendar list --start 2026-09-21T00:00:00Z --end 2026-09-24T00:00:00Z").stdout)
    assert "Design review" in {e["summary"] for e in events}
    sheet = next(f["id"] for f in run.instances["drive"].state["files"].values() if f["name"] == "Q4 budget")
    assert json.loads(sh(f"{HERMES} sheets get {sheet} 'Sheet1!A1:C3'").stdout)[0] == ["team", "q3", "q4"]
    assert json.loads(sh(f"{HERMES} contacts list --max 20").stdout)
