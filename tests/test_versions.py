import pytest

from toolsim.core.instance import Instance, Service
from toolsim.core.tools import tool
from toolsim.env import Environment
from toolsim.services import SERVICES, get_service
from toolsim.versions import check_all, fingerprint


def test_every_released_version_still_matches_its_freeze():
    problems = check_all()
    assert not problems, "Released versions changed. Ship changes as a new dated version:\n" + "\n".join(problems)


def test_every_service_has_versions_and_a_probe():
    for name, cls in SERVICES.items():
        assert cls.versions, name
        assert all(len(v) == 10 and v[4] == v[7] == "-" for v in cls.versions), f"{name}: versions are YYYY-MM-DD"
        assert fingerprint(name, cls.latest_version())["probe_calls"] >= 5, f"{name}: probe too thin"


def test_fingerprints_are_deterministic():
    assert fingerprint("github", "2026-09-25") == fingerprint("github", "2026-09-25")


def test_versions_gate_tools_and_behavior():
    @tool("old_tool", read_only=True, until="2026-10-01")
    def old_tool(ctx):
        return "old"

    @tool("new_tool", read_only=True, since="2026-10-01")
    def new_tool(ctx):
        return "gated" if ctx.version >= "2026-10-01" else "never"

    class Demo(Service):
        name = "demo"
        versions = {"2026-09-01": "first", "2026-10-01": "renamed old_tool to new_tool"}
        tools = [old_tool, new_tool]

        def initial_state(self, seed, ctx):
            return {}

    old, new = Instance(Demo(), version="2026-09-01"), Instance(Demo())
    assert new.version == "2026-10-01"
    assert [t["name"] for t in old.list_tools()] == ["old_tool"]
    assert [t["name"] for t in new.list_tools()] == ["new_tool"]
    assert old.call("new_tool", {}).is_error and new.call("new_tool", {}).text == "gated"
    with pytest.raises(ValueError, match="no version"):
        Instance(Demo(), version="2025-01-01")


def test_environments_pin_versions():
    env = Environment.from_dict({"name": "e", "servers": {"gmail": {"version": "2026-09-25"}}})
    assert env.instantiate()["gmail"].version == "2026-09-25"
    with pytest.raises(ValueError, match="no version"):
        Environment.from_dict({"name": "e", "servers": {"gmail": {"version": "1999-01-01"}}})
    from toolsim.core import mcp
    info = mcp.handle(Instance(get_service("slack")), {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert info["result"]["serverInfo"]["version"] == SERVICES["slack"].latest_version()
