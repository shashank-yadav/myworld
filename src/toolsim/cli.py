"""toolsim command line.

    toolsim services                          what can be simulated
    toolsim serve [--env ENV.yaml ...]        run the host; with --env, create that environment's servers
    toolsim stdio gmail [--seed s.yaml]       one instance over stdio, for agents configured with a command
    toolsim grade ENV.yaml --url URL          grade a finished run against the environment's checks
    toolsim versions                          service versions and whether each still matches its freeze
    toolsim freeze [SERVICE ...]              freeze newly added versions
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any

import yaml


def _load(path: str | None) -> Any:
    return yaml.safe_load(Path(path).read_text()) if path else None


def cmd_services(args: argparse.Namespace) -> None:
    from .services import SERVICES
    for name, cls in SERVICES.items():
        s = cls()
        tools = [t.name for t in s.tools if t.in_version(s.latest_version())]
        print(f"{name:10} {s.title:16} @{s.latest_version()}  {len(tools):2} tools: {', '.join(tools)}")


def cmd_versions(args: argparse.Namespace) -> None:
    from .services import SERVICES
    from .versions import check
    for name, cls in SERVICES.items():
        for v, note in cls.versions.items():
            problems = check(name, v)
            mark = "✓" if not problems else "✗"
            latest = "  (latest)" if v == cls.latest_version() else ""
            print(f"{mark} {name}@{v}{latest}  {note}")
            for pr in problems:
                print(f"    {pr}")


def cmd_freeze(args: argparse.Namespace) -> None:
    from .versions import check_all, freeze
    written = freeze(args.service or None)
    print("\n".join(f"froze {w}" for w in written) or "nothing new to freeze")
    problems = check_all()
    if problems:
        print("\nReleased versions no longer match the code. Ship the change as a new dated version:")
        print("\n".join(f"  {p}" for p in problems))
        sys.exit(1)


def cmd_issues(args: argparse.Namespace) -> None:
    import inspect
    import re as _re

    from .issues import ISSUES
    from .services import SERVICES
    for name, (fn, needs, summary) in sorted(ISSUES.items()):
        params = sorted(set(_re.findall(r'p\["(\w+)"\]|p\.get\("(\w+)"', inspect.getsource(fn)) and
                            [a or b for a, b in _re.findall(r'p\["(\w+)"\]|p\.get\("(\w+)"', inspect.getsource(fn))]))
        print(f"{name:22} {summary}")
        print(f"{'':22} needs: {', '.join(sorted(needs)) or 'any server'}; params: {', '.join(params) or '-'}")
    print("\nWorld actions per service (for custom `events:`):")
    for name, cls in SERVICES.items():
        print(f"  {name:9} {', '.join(a.name for a in cls.actions)}")


def cmd_tasks(args: argparse.Namespace) -> None:
    import json
    import sys

    from .rl import tasks
    if args.list:
        for name, (fn, servers) in sorted(tasks.FAMILIES.items()):
            print(f"{name:24} {', '.join(servers):14} {(fn.__doc__ or '').strip()}")
        return
    families = args.families.split(",") if args.families else None
    specs = tasks.generate(args.n, seed=args.seed, families=families, hard=args.hard)
    out = open(args.out, "w") if args.out else sys.stdout  # noqa: SIM115
    try:
        for spec in specs:
            out.write(json.dumps(spec) + "\n")
    finally:
        if args.out:
            out.close()
            counts: dict[str, int] = {}
            for spec in specs:
                counts[spec["family"]] = counts.get(spec["family"], 0) + 1
            print(f"wrote {len(specs)} validated tasks to {args.out}: "
                  + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())), file=sys.stderr)


def cmd_import(args: argparse.Namespace) -> None:
    import datetime as dt

    from .importers import IMPORTERS, ImportOptions
    opts = ImportOptions(anonymize=args.anonymize, domain=args.domain, keep_domains=tuple(args.keep_domain or ()),
                         limit=args.limit, map_path=Path(args.map) if args.map else None,
                         rebase_to=dt.datetime.fromisoformat(args.rebase.replace("Z", "+00:00")) if args.rebase else None)
    extra = {k: v for k, v in {"owner": args.owner, "issues": args.issues, "pulls": args.pulls, "name": args.repo,
                               "viewer": args.viewer, "me": args.me}.items() if v}
    import inspect
    accepted = inspect.signature(IMPORTERS[args.kind]).parameters
    seed = IMPORTERS[args.kind](args.path, opts, **{k: v for k, v in extra.items() if k in accepted})
    text = yaml.safe_dump(seed, sort_keys=False, allow_unicode=True, width=120)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text)
        counts = {k: len(v) for k, v in seed.items() if isinstance(v, list)}
        print(f"wrote {args.output}: {counts}")
        print(f"use it:  servers: {{{args.kind}: {{seed_file: {args.output}}}}}")
    else:
        print(text)


def cmd_stdio(args: argparse.Namespace) -> None:
    from .core.instance import Instance
    from .core.mcp import serve_stdio
    from .env import parse_time
    from .services import get_service
    inst = Instance(get_service(args.service), _load(args.seed), rng_seed=args.rng_seed, faults=_load(args.faults),
                    version=args.tool_version, speed=parse_time(args.time))
    serve_stdio(inst)


def cmd_serve(args: argparse.Namespace) -> None:
    import logging
    import os

    import uvicorn

    from .env import Environment, parse_time
    from .host import Host, HostConfig, create_app

    token = args.token or os.environ.get("TOOLSIM_TOKEN")
    if args.host not in ("127.0.0.1", "localhost", "::1") and not token and not args.no_auth:
        raise SystemExit(f"refusing to listen on {args.host} without a token: pass --token (or TOOLSIM_TOKEN), "
                         "or --no-auth if the network is trusted")
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = HostConfig(token=token, allowed_origins=args.allow_origin or [],
                        env_dirs=[Path(d) for d in (args.env_dir or [])], max_instances=args.max_instances,
                        max_hang_s=args.max_hang, speed=parse_time(args.time))
    host = Host(config)
    base = f"http://{args.host}:{args.port}"
    for path in args.env or []:
        env = Environment.load(path)
        run = host.start_env(env, env.name)
        print(f"environment {env.name}: servers {', '.join(env.servers)}; agents {', '.join(env.agents)}")
        for agent, cfg in run.agent_configs(base).items():
            print(f"\n[{agent}] task: {cfg['task']}")
            print(json.dumps({"mcpServers": cfg["mcpServers"]}, indent=2))
        print(f"\ngrade with: toolsim grade {path} --url {base}   (snapshot/fork: {base}/docs)")
    if token:
        print("auth: send 'Authorization: Bearer <token>' (or ?token=) on every request", flush=True)
    print(f"toolsim host on {base}  (control API: {base}/docs)", flush=True)
    uvicorn.run(create_app(host), host=args.host, port=args.port, log_level=args.log_level)


def cmd_grade(args: argparse.Namespace) -> None:
    from .env import Environment
    env = Environment.load(args.env)
    if not args.url.startswith(("http://", "https://")):
        raise SystemExit("--url must be an http(s) URL")
    import os
    req = urllib.request.Request(f"{args.url.rstrip('/')}/envs/{args.run or env.name}/grade")  # noqa: S310 (http(s) checked)
    token = args.token or os.environ.get("TOOLSIM_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    report = json.load(urllib.request.urlopen(req, timeout=30))  # noqa: S310 (scheme checked above)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for c in report["checks"]:
            print(f"{'✓' if c['passed'] else '✗'} {c['name']}  (matched {c['matched']}, expected {c['expected']})")
        print(f"{'PASSED' if report['passed'] else 'FAILED'}  score {report['score']:.2f}")
    sys.exit(0 if report["passed"] else 1)


def _time_arg(v: str) -> str | float:
    try:
        return float(v)
    except ValueError:
        return v


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="toolsim", description="Simulated tools for testing and training AI agents.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("services", help="list simulated services")
    s.set_defaults(fn=cmd_services)

    s = sub.add_parser("serve", help="run the multi-instance host")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--env", action="append", help="environment file to instantiate (repeatable)")
    s.add_argument("--env-dir", action="append", help="directory POST /envs may load environment files from")
    s.add_argument("--token", help="require this bearer token on every request (or set TOOLSIM_TOKEN)")
    s.add_argument("--no-auth", action="store_true", help="allow a non-localhost bind without a token")
    s.add_argument("--allow-origin", action="append", help="browser origin allowed besides localhost (repeatable)")
    s.add_argument("--max-instances", type=int, default=2000)
    s.add_argument("--max-hang", type=float, default=120.0, help="cap on real-time fault delays (seconds)")
    s.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    s.add_argument("--time", type=_time_arg, default=None,
                   help="clock for envs/instances that don't set one: virtual (default, fast), realtime, or a speed "
                        "like 60 (a simulated minute per real second)")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("stdio", help="serve one instance over stdio")
    s.add_argument("service")
    s.add_argument("--seed", help="seed file (YAML/JSON)")
    s.add_argument("--faults", help="faults file (YAML/JSON list)")
    s.add_argument("--rng-seed", type=int, default=0)
    s.add_argument("--version", dest="tool_version", help="service version date (default: latest)")
    s.add_argument("--time", type=_time_arg, default=None, help="virtual (default), realtime, or a speed like 60")
    s.set_defaults(fn=cmd_stdio)

    s = sub.add_parser("import", help="build a seed from an export of a real tool")
    s.add_argument("kind", choices=["gmail", "calendar", "slack", "github", "jira", "drive"])
    s.add_argument("path", help="the export: mbox, .ics, Slack zip/folder, git repo, Jira CSV/JSON, or a folder")
    s.add_argument("-o", "--output", help="seed file to write (default: print)")
    s.add_argument("--anonymize", action="store_true", help="replace people with consistent pseudonyms and scrub text")
    s.add_argument("--map", help="pseudonym map file, shared across imports so people stay consistent")
    s.add_argument("--domain", default="acme.com", help="company domain for pseudonymized colleagues")
    s.add_argument("--keep-domain", action="append", help="external domain to leave as-is (repeatable)")
    s.add_argument("--rebase", help="shift time so the newest item lands here (e.g. 2026-09-21T16:00:00Z)")
    s.add_argument("--limit", type=int, help="keep at most N newest items per collection")
    s.add_argument("--owner", help="gmail/calendar/drive: the account owner's email")
    s.add_argument("--me", help="jira: your display name")
    s.add_argument("--viewer", help="github: your login")
    s.add_argument("--repo", help="github: owner/name (default: from the origin remote)")
    s.add_argument("--issues", help="github: `gh issue list --json ...` output")
    s.add_argument("--pulls", help="github: `gh pr list --json ...` output")
    s.set_defaults(fn=cmd_import)

    s = sub.add_parser("issues", help="list the issue library and world actions")
    s.set_defaults(fn=cmd_issues)

    s = sub.add_parser("tasks", help="generate validated RL tasks (JSONL environment specs)")
    s.add_argument("-n", type=int, default=100, help="how many tasks (default 100)")
    s.add_argument("--seed", type=int, default=0, help="base seed; each task gets its own world seed")
    s.add_argument("--families", help="comma-separated task families (default: all; see --list)")
    s.add_argument("--hard", action="store_true", help="add flaky APIs to every task")
    s.add_argument("--out", help="output file (default: stdout)")
    s.add_argument("--list", action="store_true", help="list task families")
    s.set_defaults(fn=cmd_tasks)

    s = sub.add_parser("versions", help="list service versions and whether each still matches its frozen fingerprint")
    s.set_defaults(fn=cmd_versions)

    s = sub.add_parser("freeze", help="freeze new service versions (never overwrites released ones)")
    s.add_argument("service", nargs="*")
    s.set_defaults(fn=cmd_freeze)

    s = sub.add_parser("grade", help="grade a run against an environment's checks")
    s.add_argument("env")
    s.add_argument("--url", default="http://127.0.0.1:8765")
    s.add_argument("--run", help="environment run id (default: the environment name)")
    s.add_argument("--token", help="bearer token for the host (or TOOLSIM_TOKEN)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_grade)

    args = p.parse_args(argv)
    try:
        args.fn(args)
    except KeyboardInterrupt:
        sys.exit(130)
    except (ValueError, OSError) as e:  # user-facing problems: a clear message, not a traceback
        if os.environ.get("TOOLSIM_DEBUG"):
            raise
        print(f"toolsim: error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
