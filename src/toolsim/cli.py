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


def cmd_stdio(args: argparse.Namespace) -> None:
    from .core.instance import Instance
    from .core.mcp import serve_stdio
    from .services import get_service
    inst = Instance(get_service(args.service), _load(args.seed), rng_seed=args.rng_seed, faults=_load(args.faults),
                    version=args.tool_version)
    serve_stdio(inst)


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from .env import Environment
    from .host import Host, create_app

    host = Host()
    base = f"http://{args.host}:{args.port}"
    for path in args.env or []:
        env = Environment.load(path)
        config: dict[str, Any] = {"mcpServers": {}}
        for server in env.servers:
            inst = host.create(env.service_for(server), env.seed_for(server), rng_seed=env.rng_seed,
                               faults=env.faults_for(server), instance_id=f"{env.name}-{server}",
                               version=env.version_for(server))
            config["mcpServers"][server] = {"url": f"{base}/instances/{inst.id}/mcp"}
        print(f"environment {env.name}: {', '.join(env.servers)}")
        print(f"task: {env.task.strip()}")
        print("MCP config for the agent:\n" + json.dumps(config, indent=2))
    print(f"toolsim host on {base}  (control API: {base}/docs)")
    uvicorn.run(create_app(host), host=args.host, port=args.port, log_level="warning")


def cmd_grade(args: argparse.Namespace) -> None:
    from .env import Environment
    env = Environment.load(args.env)
    worlds = {}
    for server in env.servers:
        iid = f"{args.prefix or env.name}-{server}"
        state = json.load(urllib.request.urlopen(f"{args.url}/instances/{iid}/state"))["state"]
        calls = json.load(urllib.request.urlopen(f"{args.url}/instances/{iid}/calls"))["calls"]
        worlds[server] = {"state": state, "calls": calls}
    report = env.grade(worlds)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for c in report["checks"]:
            print(f"{'✓' if c['passed'] else '✗'} {c['name']}  (matched {c['matched']}, expected {c['expected']})")
        print(f"{'PASSED' if report['passed'] else 'FAILED'}  score {report['score']:.2f}")
    sys.exit(0 if report["passed"] else 1)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="toolsim", description="Simulated tools for testing and training AI agents.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("services", help="list simulated services")
    s.set_defaults(fn=cmd_services)

    s = sub.add_parser("serve", help="run the multi-instance host")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--env", action="append", help="environment file to instantiate (repeatable)")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("stdio", help="serve one instance over stdio")
    s.add_argument("service")
    s.add_argument("--seed", help="seed file (YAML/JSON)")
    s.add_argument("--faults", help="faults file (YAML/JSON list)")
    s.add_argument("--rng-seed", type=int, default=0)
    s.add_argument("--version", dest="tool_version", help="service version date (default: latest)")
    s.set_defaults(fn=cmd_stdio)

    s = sub.add_parser("versions", help="list service versions and whether each still matches its frozen fingerprint")
    s.set_defaults(fn=cmd_versions)

    s = sub.add_parser("freeze", help="freeze new service versions (never overwrites released ones)")
    s.add_argument("service", nargs="*")
    s.set_defaults(fn=cmd_freeze)

    s = sub.add_parser("grade", help="grade a run against an environment's checks")
    s.add_argument("env")
    s.add_argument("--url", default="http://127.0.0.1:8765")
    s.add_argument("--prefix", help="instance id prefix (default: the environment name)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_grade)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
