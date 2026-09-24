import pytest

from aops.classify import classify, classify_command, is_ambiguous_error


@pytest.mark.parametrize("kind,name,attrs,effect,system,obj", [
    ("mcp", "mcp_gmail_send_email", {"mcp_server": "gmail"}, "send", "email", "email"),
    ("mcp", "mcp_gmail_search_emails", {}, "read", "email", "email"),
    ("tool", "gmail_read_email", {}, "read", "email", "email"),
    ("mcp", "mcp_gcal_create_event", {"mcp_server": "gcal"}, "write", "calendar", "event"),
    ("mcp", "mcp__github__create_issue", {}, "write", "github", "issue"),
    ("mcp", "mcp_github_add_labels", {}, "write", "github", "label"),
    ("mcp", "mcp_stripe_create_refund", {}, "write", "payments", "refund"),
    ("mcp", "mcp_drive_delete_file", {}, "delete", "drive", "file"),
    ("tool", "slack_post_message", {}, "send", "slack", "message"),
    ("tool", "write_file", {}, "write", "filesystem", "file"),
    ("tool", "read_file", {}, "read", "filesystem", "file"),
    ("tool", "apply_patch", {}, "write", "filesystem", "file"),
    ("browser", "browser_click", {}, "execute", "browser", "page"),
    ("browser", "browser", {"args": {"action": "navigate", "url": "x"}}, "read", "browser", "page"),
    ("memory", "memory", {"args": {"action": "add"}}, "write", "memory", "memory"),
    ("tool", "message", {"args": {"action": "send", "channel": "telegram"}}, "send", "telegram", "message"),
    ("tool", "frobnicate_widget", {}, "unknown", "tool", None),
    ("tool", "whatever", {"effect": "send", "system": "fax", "object": "fax"}, "send", "fax", "fax"),
])
def test_classify(kind, name, attrs, effect, system, obj):
    e = classify(kind, name, attrs)
    assert (e.effect, e.system, e.object) == (effect, system, obj)


def test_memory_is_not_an_external_change():
    assert not classify("memory", "memory", {"args": {"action": "add"}}).changes_world
    assert classify("mcp", "mcp_gmail_send_email", {}).changes_world


@pytest.mark.parametrize("cmd,effect,system", [
    ("ls -la && cat README.md | grep foo", "read", "shell"),
    ("uv run pytest -q", "execute", "shell"),
    ("rm -rf build/", "delete", "filesystem"),
    ("echo hi > out.txt", "write", "filesystem"),
    ("make test 2>&1 >/dev/null", "execute", "shell"),
    ("git status && git diff", "read", "shell"),
    ("git commit -am x && git push origin main", "write", "git"),
    ("gh issue create --title x", "write", "github"),
    ("gh pr view 12", "read", "shell"),
    ("curl -X POST https://api.x.com/v1/things -d '{}'", "send", "web"),
    ("curl https://example.com", "read", "web"),
    ("sudo kubectl delete pod web-1", "delete", "infrastructure"),
    ("psql -c 'DELETE FROM users'", "delete", "database"),
    ("sed -i 's/a/b/' file.txt", "write", "filesystem"),
])
def test_classify_command(cmd, effect, system):
    e = classify_command(cmd)
    assert (e.effect, e.system) == (effect, system)


@pytest.mark.parametrize("text,ambiguous", [
    ("ReadTimeout: did not respond within 30s", True),
    ("ECONNRESET", True),
    ("502 Bad Gateway", True),
    ("connection reset by peer", True),
    ("404 File not found", False),
    ("403 Forbidden", False),
    ("invalid argument: to", False),
])
def test_ambiguous_errors(text, ambiguous):
    assert is_ambiguous_error(text) is ambiguous


def test_hermes_tool_search_bridge_is_unwrapped():
    e = classify("tool", "tool_call", {"args": {"calls": [{"name": "write_file", "arguments": {"path": "x"}}]}})
    assert (e.effect, e.system) == ("write", "filesystem")
    e = classify("tool", "tool_call", {"args": {"calls": [{"name": "terminal", "arguments": {"command": "rm -rf x"}}]}})
    assert e.effect == "delete"
    assert classify("tool", "tool_search", {"args": {"query": "file"}}).effect == "internal"


def test_hermes_memory_batch_and_git_init():
    args = {"target": "user", "operations": [{"action": "add", "content": "x"}]}
    assert classify("memory", "memory", {"args": args}).effect == "write"
    assert classify_command("git init").effect == "write"
