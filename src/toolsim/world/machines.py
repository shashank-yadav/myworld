"""Machines as components: whatever a shell command can snapshot, a world can branch.

``CommandComponent`` is driven by command templates, so any backend that has a CLI plugs in:
docker, podman, Firecracker tooling, a cloud VM service. Placeholders are filled with
shell-quoted values: ``{id}`` and other handle fields, ``{snapshot}``, and mutation parameters.

    CommandComponent({
        "snapshot": "docker commit {id}",                          # stdout: the snapshot (an image id)
        "restore":  "docker rm -f {id} >/dev/null && docker run -d --name {id} {snapshot} sleep infinity",
        "clone":    "docker run -d $(docker commit {id}) sleep infinity",   # stdout: the copy's handle
        "view":     "docker diff {id}",                                # stdout: JSON, or lines
        "mutate.exec": "docker exec {id} sh -c {cmd}",
    }, handle={"id": "agent-box"})

``DockerComponent`` is that, ready-made: filesystem snapshots are image layers (copy-on-write),
so checkpointing a container per step is cheap.

A machine is the one opaque part of a world, so it's treated as disk plus a reboot, by design:
files survive a snapshot, running processes restart. What matters to a task belongs in semantic
components (services, databases, directories), where it can be diffed, mutated and graded; a
memory image can't be. Where memory-level fidelity is really needed, a VM backend (Firecracker,
CRIU, a hosted VM service) plugs in through these same templates.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import uuid
from typing import Any

from .component import Component


class CommandComponent(Component):
    kind = "command"

    def __init__(self, commands: dict[str, str], handle: dict[str, Any] | None = None, timeout: float = 300):
        missing = {"snapshot", "restore", "view"} - set(commands)
        if missing:
            raise ValueError(f"a command component needs {', '.join(sorted(missing))} commands")
        self.commands = dict(commands)
        self.handle = dict(handle or {})
        self.timeout = timeout

    def _run(self, key: str, stdin: str | None = None, **values: Any) -> str:
        template = self.commands[key]
        fields = {**self.handle, **values}
        try:
            cmd = template.format(**{k: shlex.quote(v if isinstance(v, str) else json.dumps(v))
                                     for k, v in fields.items()})
        except KeyError as e:
            raise ValueError(f"{key}: the command needs {{{e.args[0]}}}") from None
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, input=stdin,  # noqa: S602  (configured commands)
                           timeout=self.timeout)
        if p.returncode != 0:
            raise RuntimeError(f"{key} failed ({p.returncode}): {(p.stderr or p.stdout).strip()[:500]}")
        return p.stdout.strip()

    @staticmethod
    def _value(text: str) -> Any:
        try:
            return json.loads(text)
        except ValueError:
            return text

    def snapshot(self) -> Any:
        return {"handle": dict(self.handle), "snapshot": self._value(self._run("snapshot"))}

    def restore(self, snap: Any) -> None:
        out = self._run("restore", snapshot=snap["snapshot"])
        new = self._value(out) if out else None
        if isinstance(new, dict):
            self.handle.update(new)  # a backend may hand back a new id

    def clone(self) -> CommandComponent:
        if "clone" not in self.commands:
            raise NotImplementedError("this machine has no clone command")
        out = self._value(self._run("clone"))
        return type(self)._from(self.commands, out if isinstance(out, dict) else {"id": out}, self.timeout)

    @classmethod
    def _from(cls, commands: dict[str, str], handle: dict[str, Any], timeout: float) -> CommandComponent:
        return CommandComponent(commands, handle, timeout)

    def view(self, snap: Any = None) -> Any:
        if snap is not None and "view_snapshot" not in self.commands:
            return {"snapshot": snap["snapshot"]}  # a machine's past state is its snapshot id
        text = self._run("view_snapshot", snapshot=snap["snapshot"]) if snap is not None else self._run("view")
        value = self._value(text)
        return value if isinstance(value, dict) else {"lines": [x for x in str(value).splitlines() if x]}

    def mutate(self, op: str, **params: Any) -> Any:
        key = f"mutate.{op}"
        if key not in self.commands:
            return super().mutate(op, **params)
        stdin = params.pop("stdin", None)
        return self._value(self._run(key, stdin=stdin, **params))

    def mutations(self) -> list[dict[str, Any]]:
        return [{"name": k.split(".", 1)[1], "doc": self.commands[k]} for k in self.commands if k.startswith("mutate.")]

    def close(self) -> None:
        if "close" in self.commands:
            try:
                self._run("close")
            except (RuntimeError, subprocess.TimeoutExpired):
                pass


def docker_commands(docker: str = "docker", workdir: str = "/") -> dict[str, str]:
    d = docker
    return {
        "snapshot": f"{d} commit {{id}}",
        "restore": f"{d} rm -f {{id}} >/dev/null && {d} run -d --name {{id}} {{snapshot}} sleep infinity >/dev/null",
        "clone": f"img=$({d} commit {{id}}) && n=toolsim-$(date +%s%N) && {d} run -d --name $n $img sleep infinity "
                 f">/dev/null && echo $n",
        "view": f"{d} diff {{id}}",
        "mutate.exec": f"{d} exec -w {shlex.quote(workdir)} {{id}} sh -c {{cmd}}",
        "mutate.write": f"{d} exec -i {{id}} sh -c \"mkdir -p \\$(dirname {{path}}) && cat > {{path}}\"",
        "close": f"{d} rm -f {{id}}",
    }


class DockerComponent(CommandComponent):
    """A container: snapshots are committed images (layers, copy-on-write), restore recreates the
    container from one, clone starts a new container from the current state."""

    kind = "docker"

    def __init__(self, image: str | None = None, name: str | None = None, *, docker: str = "docker",
                 workdir: str = "/", handle: dict[str, Any] | None = None, timeout: float = 300):
        super().__init__(docker_commands(docker, workdir), handle, timeout)
        self.docker = docker
        if handle is None:
            if not image:
                raise ValueError("a docker component needs an image")
            name = name or f"toolsim-{uuid.uuid4().hex[:10]}"
            subprocess.run([docker, "run", "-d", "--name", name, image, "sleep", "infinity"], check=True,
                           capture_output=True, timeout=timeout)
            self.handle = {"id": name}

    def mutate(self, op: str, **params: Any) -> Any:
        if op == "write":
            return super().mutate("write", path=params["path"], stdin=params.get("content", ""))
        return super().mutate(op, **params)

    @classmethod
    def _from(cls, commands: dict[str, str], handle: dict[str, Any], timeout: float) -> CommandComponent:
        docker = commands["snapshot"].split(" commit ")[0]
        return cls(docker=docker, handle=handle, timeout=timeout)
