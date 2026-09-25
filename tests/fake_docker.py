#!/usr/bin/env python3
"""A stand-in for the docker CLI: containers and images are directories under $FAKE_DOCKER_HOME."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

home = Path(os.environ["FAKE_DOCKER_HOME"])
(home / "containers").mkdir(parents=True, exist_ok=True)
(home / "images").mkdir(parents=True, exist_ok=True)
log = home / "log.txt"
with log.open("a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\n")
args = sys.argv[1:]
cmd = args.pop(0)


def files(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha1(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


if cmd == "run":
    name = args[args.index("--name") + 1] if "--name" in args else "c" + os.urandom(4).hex()
    image = [a for a in args if a not in ("-d", "--name", name)][0]
    root = home / "containers" / name
    src = home / "images" / image.replace(":", "_")
    shutil.copytree(src / "fs", root) if src.exists() else root.mkdir()
    (home / "containers" / f"{name}.image").write_text(image)
    print(name)
elif cmd == "commit":
    root = home / "containers" / args[0]
    sha = "sha256:" + hashlib.sha256(json.dumps(files(root), sort_keys=True).encode()).hexdigest()[:24]
    dest = home / "images" / sha.replace(":", "_")
    if not dest.exists():
        shutil.copytree(root, dest / "fs")
    print(sha)
elif cmd == "rm":
    name = args[-1]
    shutil.rmtree(home / "containers" / name, ignore_errors=True)
elif cmd == "diff":
    root = home / "containers" / args[0]
    base_img = home / "images" / (home / "containers" / f"{args[0]}.image").read_text().replace(":", "_") / "fs"
    before = files(base_img) if base_img.exists() else {}
    now = files(root)
    for p in sorted(set(before) | set(now)):
        if p not in now:
            print(f"D /{p}")
        elif p not in before:
            print(f"A /{p}")
        elif before[p] != now[p]:
            print(f"C /{p}")
elif cmd == "exec":
    args = [a for a in args if a != "-i"]
    if args[0] == "-w":
        args = args[2:]
    root = home / "containers" / args[0]
    sys.exit(subprocess.run(args[1:], cwd=root, stdin=sys.stdin).returncode)
else:
    sys.exit(f"fake docker: unsupported {cmd}")
