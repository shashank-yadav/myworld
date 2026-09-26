"""Date-based versions and their frozen fingerprints.

Every released version of every service is frozen in ``src/myworld/frozen/<service>@<date>.json``:
its exact tool definitions plus a hash of how it behaves on a fixed probe (a scripted series of
calls against the default world). Tests recompute both for every version. If the code changed
what a released version does, the test fails and the change has to ship as a new date instead:

    1. add the new date to the service's ``versions`` with a changelog line
    2. gate the change on ``ctx.version`` (or ``@tool(since=...)``) so older dates keep behaving
    3. ``myworld freeze`` to freeze the new version
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .core.instance import Instance
from .services import SERVICES, get_service

FROZEN_DIR = Path(__file__).parent / "frozen"


def fingerprint(service: str, version: str) -> dict[str, Any]:
    svc = get_service(service)
    inst = Instance(svc, rng_seed=0, version=version)
    tools = inst.list_tools()
    svc.probe(inst)
    trace = [{"tool": c["tool"], "args": c["args"], "ok": c["ok"], "result": c["result"],
              **({"notified": c["notified"]} if c.get("notified") else {})} for c in inst.calls]
    behavior = hashlib.sha256(json.dumps(trace, sort_keys=True, default=str).encode()).hexdigest()
    return {"service": service, "version": version, "changelog": svc.versions[version],
            "behavior_sha256": behavior, "probe_calls": len(trace), "tools": tools}


def frozen_path(service: str, version: str) -> Path:
    return FROZEN_DIR / f"{service}@{version}.json"


def check(service: str, version: str) -> list[str]:
    """Differences between a frozen version and what the code does now (empty = unchanged)."""
    path = frozen_path(service, version)
    if not path.exists():
        return [f"{service}@{version} has never been frozen (run `myworld freeze`)"]
    frozen, now = json.loads(path.read_text()), fingerprint(service, version)
    problems = []
    old_tools = {t["name"]: t for t in frozen["tools"]}
    new_tools = {t["name"]: t for t in now["tools"]}
    for name in sorted(set(old_tools) | set(new_tools)):
        if name not in new_tools:
            problems.append(f"{service}@{version}: tool {name} was removed")
        elif name not in old_tools:
            problems.append(f"{service}@{version}: tool {name} was added")
        elif old_tools[name] != new_tools[name]:
            problems.append(f"{service}@{version}: tool {name} definition changed")
    if frozen["behavior_sha256"] != now["behavior_sha256"]:
        problems.append(f"{service}@{version}: behavior on the probe changed")
    return problems


def check_all() -> list[str]:
    return [p for name, cls in SERVICES.items() for v in cls.versions for p in check(name, v)]


def freeze(services: list[str] | None = None) -> list[str]:
    """Freeze versions that aren't frozen yet. Never overwrites a released version."""
    FROZEN_DIR.mkdir(exist_ok=True)
    written = []
    for name in services or list(SERVICES):
        for v in SERVICES[name].versions:
            path = frozen_path(name, v)
            if path.exists():
                continue
            path.write_text(json.dumps(fingerprint(name, v), indent=1, sort_keys=True) + "\n")
            written.append(path.name)
    return written
