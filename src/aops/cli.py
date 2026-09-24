"""aops command line."""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

from .store import EventStore, default_home

HERMES_MANIFEST = """\
name: aops
version: 0.1.0
description: Agent Operations. Records every LLM call, tool call, subagent and external change to the local aops collector.
provides_hooks:
{hooks}
"""


def cmd_serve(args: argparse.Namespace) -> None:
    try:
        import uvicorn
    except ImportError:
        sys.exit("The collector needs the server extra: pip install 'agent-operations[server]'")
    from .server import create_app, drain_spool

    store = EventStore(args.db)
    if args.demo:
        from .demo import seed
        print(f"seeded {seed(store)} demo events")
    drained = drain_spool(store)
    if drained:
        print(f"ingested {drained} spooled events")
    print(f"Agent Operations on http://{args.host}:{args.port}  (events: {store.path})")
    uvicorn.run(create_app(store), host=args.host, port=args.port, log_level="warning")


def cmd_demo(args: argparse.Namespace) -> None:
    from .demo import seed
    store = EventStore(args.db)
    print(f"seeded {seed(store)} demo events into {store.path}")


def cmd_install(args: argparse.Namespace) -> None:
    if args.runtime == "hermes":
        from .integrations import hermes
        dest = Path(args.dest or Path.home() / ".hermes" / "plugins" / "aops").expanduser()
        dest.mkdir(parents=True, exist_ok=True)
        pkg = Path(__file__).parent
        shutil.copy(pkg / "integrations" / "hermes.py", dest / "__init__.py")
        shutil.copy(pkg / "sdk.py", dest / "aops_sdk.py")
        (dest / "plugin.yaml").write_text(HERMES_MANIFEST.format(hooks="\n".join(f"  - {h}" for h in hermes.HOOKS)))
        print(f"installed Hermes plugin to {dest}\nnext: hermes plugins enable aops && aops serve")
    elif args.runtime == "openclaw":
        src = Path(__file__).resolve().parents[2] / "integrations" / "openclaw"
        if not src.exists():
            sys.exit("OpenClaw plugin sources not found; install from the repo: openclaw plugins install ./integrations/openclaw")
        print(f"run: openclaw plugins install {src}")


def cmd_tail(args: argparse.Namespace) -> None:
    """Print a one-line summary per run as they change. Handy without the dashboard."""
    from .trace import build_runs
    store = EventStore(args.db)
    seen: dict[str, tuple] = {}
    while True:
        ids = store.recent_run_ids(limit=args.limit)
        for run in reversed(build_runs(store.run_events(ids))):
            s = run.summary()
            key = (run.status, s["calls"], s["unknown"])
            if seen.get(run.run_id) == key:
                continue
            seen[run.run_id] = key
            flag = " ⚠ outcome unknown" if s["unknown"] else ""
            print(f"[{run.status:>9}] {run.name or run.run_id}: {s['calls']} calls · {s['changes']} external changes"
                  f" · {s['failures']} failures · ${s['cost']:.2f}{flag}", flush=True)
        if not args.follow:
            return
        time.sleep(1.0)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="aops", description="Agent Operations: see everything your agents do.")
    p.add_argument("--db", default=None, help=f"event log path (default {default_home() / 'events.db'})")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the local collector and dashboard")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=4319)
    s.add_argument("--demo", action="store_true", help="seed sample runs first")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("demo", help="seed sample runs into the event log")
    s.set_defaults(fn=cmd_demo)

    s = sub.add_parser("install", help="install an agent runtime integration")
    s.add_argument("runtime", choices=["hermes", "openclaw"])
    s.add_argument("--dest", help="plugin directory (Hermes default ~/.hermes/plugins/aops)")
    s.set_defaults(fn=cmd_install)

    s = sub.add_parser("tail", help="print run summaries")
    s.add_argument("-f", "--follow", action="store_true")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_tail)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
