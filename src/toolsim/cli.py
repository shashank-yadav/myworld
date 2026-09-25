"""toolsim command line.

    toolsim services                          what can be simulated
    toolsim serve [--env ENV.yaml ...]        run the host; with --env, create that environment's servers
    toolsim stdio gmail [--seed s.yaml]       one instance over stdio, for agents configured with a command
    toolsim grade ENV.yaml --url URL          grade a finished run against the environment's checks
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
        print(f"{name:10} {s.title:16} {len(s.tools):2} tools: {', '.join(t.name for t in s.tools)}")


def cmd_stdio(args: argparse.Namespace) -> None:
    from .core.instance import Instance
    from .core.mcp import serve_stdio
    from .services import get_service
    inst = Instance(get_service(args.service), _load(args.seed), rng_seed=args.rng_seed, faults=_load(args.faults))
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
                               faults=env.faults_for(server), instance_id=f"{env.name}-{server}")
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
    s.set_defaults(fn=cmd_stdio)

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
