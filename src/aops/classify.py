"""Decide what a step did to the world: read it, changed it, or can't tell.

Classification is heuristic (tool names, MCP server names, arguments, shell commands)
and always overridable: an integration or tool can set ``attrs["effect"]``,
``attrs["system"]`` and ``attrs["object"]`` explicitly.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import asdict, dataclass
from typing import Any

# effect values
READ, WRITE, DELETE, SEND, EXECUTE, INTERNAL, UNKNOWN = (
    "read", "write", "delete", "send", "execute", "internal", "unknown")
CHANGING = frozenset({WRITE, DELETE, SEND})


@dataclass(frozen=True)
class Effect:
    effect: str             # read | write | delete | send | execute | internal | unknown
    system: str             # email, calendar, github, filesystem, shell, browser, memory, ...
    object: str | None      # email, event, file, issue, ...
    verb: str | None        # past tense for change feeds: "sent", "created", ...

    @property
    def changes_world(self) -> bool:
        """True when the step may have changed something outside the agent."""
        return self.effect in CHANGING and self.system != "memory"

    def label(self) -> str:
        obj = self.object or "action"
        return f"{obj} {self.verb}" if self.verb else obj

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "changes_world": self.changes_world, "label": self.label()}


# token -> (effect, past tense)
_VERBS: dict[str, tuple[str, str]] = {}
for words, eff, past in [
    ("send reply forward invite notify", SEND, "sent"),
    ("post publish", SEND, "posted"),
    ("create insert new book schedule make register submit", WRITE, "created"),
    ("add attach", WRITE, "added"),
    ("update edit modify patch set rename move replace upsert put change label assign", WRITE, "updated"),
    ("write save append overwrite", WRITE, "modified"),
    ("comment", WRITE, "commented"),
    ("upload", WRITE, "uploaded"),
    ("merge", WRITE, "merged"),
    ("push commit", WRITE, "pushed"),
    ("deploy release", WRITE, "deployed"),
    ("approve reject", WRITE, "reviewed"),
    ("pay charge transfer refund purchase buy order checkout", WRITE, "made"),
    ("delete remove trash destroy drop purge erase unlink", DELETE, "deleted"),
    ("archive", DELETE, "archived"),
    ("close cancel revoke disable unsubscribe", DELETE, "closed"),
    ("get list search read fetch find query view lookup describe show check browse "
     "navigate snapshot screenshot extract scrape download count status diff log inspect "
     "retrieve recall peek scroll wait hover", READ, ""),
]:
    for w in words.split():
        _VERBS[w] = (eff, past)

_SYSTEMS = [
    ("email", "gmail email mail inbox smtp outlook imap"),
    ("calendar", "calendar gcal cal meeting"),
    ("github", "github gh"),
    ("gitlab", "gitlab"),
    ("slack", "slack"),
    ("discord", "discord"),
    ("telegram", "telegram"),
    ("notion", "notion"),
    ("linear", "linear"),
    ("jira", "jira"),
    ("drive", "drive gdrive gdocs docs sheets dropbox"),
    ("payments", "stripe payment payments paypal"),
    ("database", "db sql postgres mysql sqlite supabase mongo redis"),
    ("filesystem", "file files fs filesystem dir directory path"),
    ("browser", "browser playwright puppeteer page"),
    ("shell", "terminal shell bash exec sh zsh command"),
    ("memory", "memory remember"),
    ("web", "web http url websearch"),
    ("messaging", "sms whatsapp imessage twilio"),
]
_SYSTEM_BY_TOKEN = {tok: sys for sys, toks in _SYSTEMS for tok in toks.split()}

_OBJECTS = [
    ("email", "email emails mail"),
    ("draft", "draft drafts"),
    ("event", "event events meeting meetings invite"),
    ("file", "file files document doc page_file"),
    ("issue", "issue issues ticket tickets"),
    ("pull request", "pr pull pulls"),
    ("comment", "comment comments"),
    ("message", "message messages msg chat dm"),
    ("page", "page pages"),
    ("row", "row rows record records table"),
    ("refund", "refund refunds"),
    ("payment", "payment charge invoice order"),
    ("commit", "commit commits"),
    ("branch", "branch"),
    ("repository", "repo repository"),
    ("task", "task tasks todo"),
    ("contact", "contact contacts"),
    ("label", "label labels"),
]
_OBJECT_BY_TOKEN = {tok: obj for obj, toks in _OBJECTS for tok in toks.split()}


def tokens(name: str) -> list[str]:
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name or "")
    return [t for t in re.split(r"[^a-zA-Z0-9]+", name.lower()) if t]


def _first(toks: list[str], table: dict[str, Any]) -> Any:
    for t in toks:
        if t in table:
            return table[t]
    return None


# Exact tool names used by the runtimes we integrate with first (Hermes, OpenClaw).
_KNOWN_TOOLS: dict[str, Effect] = {
    # Hermes
    "read_file": Effect(READ, "filesystem", "file", None),
    "search_files": Effect(READ, "filesystem", "file", None),
    "write_file": Effect(WRITE, "filesystem", "file", "modified"),
    "patch": Effect(WRITE, "filesystem", "file", "modified"),
    "web_search": Effect(READ, "web", None, None),
    "web_extract": Effect(READ, "web", None, None),
    "execute_code": Effect(EXECUTE, "shell", "script", "ran"),
    "delegate_task": Effect(INTERNAL, "agent", "subagent", "started"),
    "todo": Effect(INTERNAL, "agent", "task", None),
    "clarify": Effect(INTERNAL, "agent", None, None),
    "vision_analyze": Effect(READ, "web", None, None),
    "session_search": Effect(READ, "memory", None, None),
    # OpenClaw
    "read": Effect(READ, "filesystem", "file", None),
    "write": Effect(WRITE, "filesystem", "file", "modified"),
    "edit": Effect(WRITE, "filesystem", "file", "modified"),
    "apply_patch": Effect(WRITE, "filesystem", "file", "modified"),
    "web_fetch": Effect(READ, "web", None, None),
    "image": Effect(READ, "web", None, None),
    "memory_search": Effect(READ, "memory", None, None),
    "memory_get": Effect(READ, "memory", None, None),
    "sessions_spawn": Effect(INTERNAL, "agent", "subagent", "started"),
    "sessions_list": Effect(READ, "agent", None, None),
    "sessions_history": Effect(READ, "agent", None, None),
}

_SHELL_TOOLS = {"terminal", "exec", "bash", "shell", "run_command", "process"}


def classify(kind: str, name: str, attrs: dict[str, Any] | None = None) -> Effect:
    attrs = attrs or {}
    args = attrs.get("args") if isinstance(attrs.get("args"), dict) else {}
    if kind == "llm":
        return Effect(INTERNAL, "model", None, None)
    if kind in ("approval", "agent", "message") and not attrs.get("effect"):
        eff = SEND if kind == "message" and attrs.get("direction") == "outbound" else INTERNAL
        return _override(Effect(eff, "agent" if kind != "message" else "messaging", "message" if kind == "message" else None,
                                "sent" if eff == SEND else None), attrs)
    if kind == "subagent":
        return _override(Effect(INTERNAL, "agent", "subagent", "started"), attrs)

    base = name.split("__")[-1] if "__" in name else name  # mcp__server__tool
    lname = base.lower()

    if kind == "terminal" or lname in _SHELL_TOOLS:
        cmd = args.get("command") or args.get("cmd") or attrs.get("command") or ""
        if isinstance(cmd, list):
            cmd = " ".join(map(str, cmd))
        return _override(classify_command(str(cmd)), attrs)
    if kind == "memory" or lname == "memory":
        ops = args.get("operations") if isinstance(args.get("operations"), list) else [args]
        actions = {str(o.get("action") or "").lower() for o in ops if isinstance(o, dict)}
        eff = (WRITE if actions & {"add", "replace", "update", "write", "save"}
               else DELETE if actions & {"remove", "delete"} else READ)
        return _override(Effect(eff, "memory", "memory", {WRITE: "written", DELETE: "removed"}.get(eff)), attrs)
    if kind == "browser" or lname.startswith("browser"):
        return _override(_classify_browser(lname, args), attrs)
    if lname in ("message", "send_message"):
        action = str(args.get("action") or "send").lower()
        eff, past = _VERBS.get(action, (SEND, "sent"))
        channel = str(args.get("channel") or args.get("platform") or args.get("provider") or "messaging").lower()
        return _override(Effect(eff, _SYSTEM_BY_TOKEN.get(channel, channel), "message", past or None), attrs)
    if lname in ("tool_search", "tool_describe"):  # Hermes tool-search catalog reads
        return _override(Effect(INTERNAL, "agent", None, None), attrs)
    if lname == "tool_call":  # Hermes bridge: classify the tool it was trying to reach
        target, inner = bridge_target(args)
        if target and target != "tool_call":
            return classify(kind, target, {**attrs, "args": inner})
        return _override(Effect(UNKNOWN, "tool", None, None), attrs)
    if lname in _KNOWN_TOOLS:
        return _override(_KNOWN_TOOLS[lname], attrs)

    toks = tokens(name)
    server = attrs.get("mcp_server")
    if server:
        toks = tokens(str(server)) + toks
    vi = next((i for i, t in enumerate(toks) if t in _VERBS), None)
    eff, past = _VERBS[toks[vi]] if vi is not None else (UNKNOWN, None)
    system = _first(toks, _SYSTEM_BY_TOKEN) or (str(server).lower() if server else "tool")
    # the object usually follows the verb ("create_refund"), even if it is itself verb-like
    after = toks[vi + 1:] if vi is not None else []
    obj = _first(after, _OBJECT_BY_TOKEN) or _first([t for t in toks if t not in _VERBS], _OBJECT_BY_TOKEN)
    if obj is None and system == "email":
        obj = "email"
    if system == "calendar" and obj is None:
        obj = "event"
    if obj == "email" and system == "tool":
        system = "email"
    return _override(Effect(eff, system, obj, past or None), attrs)


def bridge_target(args: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """The real tool behind a Hermes ``tool_call`` bridge call, when the model named one."""
    calls = args.get("calls")
    call = calls[0] if isinstance(calls, list) and calls and isinstance(calls[0], dict) else args
    name = call.get("name") or call.get("tool") or call.get("tool_name")
    inner = call.get("arguments") or call.get("args") or {}
    return (str(name) if name else None), (inner if isinstance(inner, dict) else {})


def _classify_browser(lname: str, args: dict[str, Any]) -> Effect:
    action = str(args.get("action") or lname.removeprefix("browser").strip("_") or "").lower()
    if action in ("click", "type", "press", "fill", "select", "submit", "act", "upload", "drag", "evaluate"):
        # A click can submit a form or buy something. We can't tell from here, so it's
        # "execute": visible in traces, not counted as a confirmed change.
        return Effect(EXECUTE, "browser", "page", "interacted")
    return Effect(READ, "browser", "page", None)


_READ_CMDS = {
    "ls", "cat", "head", "tail", "less", "more", "grep", "rg", "ag", "find", "fd", "pwd", "echo", "which",
    "whoami", "env", "printenv", "wc", "sort", "uniq", "diff", "file", "stat", "du", "df", "ps", "top",
    "date", "tree", "jq", "yq", "awk", "cut", "tr", "uname", "hostname", "id", "type", "man", "sleep",
    "true", "false", "test", "[", "cd", "realpath", "dirname", "basename", "md5sum", "sha256sum", "nl",
}
_DELETE_CMDS = {"rm", "rmdir", "unlink", "shred", "trash"}
_WRITE_CMDS = {"mv", "cp", "mkdir", "touch", "tee", "chmod", "chown", "ln", "dd", "truncate", "install", "rsync", "scp"}
_READONLY_SUB = {
    "git": {"status", "log", "diff", "show", "branch", "remote", "rev-parse", "ls-files", "blame", "fetch", "grep", "config"},
    "gh": {"view", "list", "status", "diff", "checks", "search", "browse"},
    "kubectl": {"get", "describe", "logs", "top", "explain", "version"},
    "docker": {"ps", "images", "logs", "inspect", "version"},
    "npm": {"ls", "list", "view", "outdated", "test", "run"},
    "terraform": {"plan", "show", "validate", "fmt", "output"},
}


def classify_command(command: str) -> Effect:
    """Classify a shell command line by its riskiest segment."""
    if not command.strip():
        return Effect(EXECUTE, "shell", "command", "ran")
    if re.search(r"(^|[^>&0-9])>>?\s*(?!&|/dev/null)\S", command):
        worst = Effect(WRITE, "filesystem", "file", "modified")
    else:
        worst = None
    for seg in re.split(r"&&|\|\||;|\|", command):
        eff = _classify_segment(seg.strip())
        if eff and (worst is None or _rank(eff) > _rank(worst)):
            worst = eff
    return worst or Effect(READ, "shell", "command", None)


def _rank(e: Effect) -> int:
    return {READ: 0, INTERNAL: 0, EXECUTE: 1, UNKNOWN: 1, WRITE: 2, SEND: 3, DELETE: 4}[e.effect] + (
        1 if e.system not in ("shell", "filesystem") else 0)


def _classify_segment(seg: str) -> Effect | None:
    if not seg:
        return None
    try:
        parts = shlex.split(seg, posix=True)
    except ValueError:
        parts = seg.split()
    while parts and ("=" in parts[0] and not parts[0].startswith("-") or parts[0] in ("sudo", "env", "time", "nohup", "exec")):
        parts = parts[1:]
    if not parts:
        return None
    prog = parts[0].rsplit("/", 1)[-1]
    rest = parts[1:]
    sub = next((p for p in rest if not p.startswith("-")), "")
    if prog in _READ_CMDS:
        return Effect(READ, "shell", "command", None)
    if prog in _DELETE_CMDS:
        return Effect(DELETE, "filesystem", "file", "deleted")
    if prog in _WRITE_CMDS or (prog == "sed" and any(p.startswith("-i") for p in rest)):
        return Effect(WRITE, "filesystem", "file", "modified")
    if prog == "gh" and len(rest) >= 2 and rest[1] in _READONLY_SUB["gh"]:
        return Effect(READ, "shell", "command", None)
    if prog in _READONLY_SUB and sub in _READONLY_SUB[prog]:
        return Effect(READ, "shell", "command", None)
    if prog == "git":
        if sub == "push":
            return Effect(WRITE, "github" if "github" in seg else "git", "commit", "pushed")
        if sub in ("init", "clone", "commit", "merge", "rebase", "reset", "checkout", "switch", "tag", "cherry-pick", "revert", "stash", "add", "rm", "mv", "clean", "restore"):
            return Effect(WRITE, "filesystem", "repository", "modified")
    if prog == "gh" and len(rest) >= 2:
        noun, act = rest[0], rest[1]
        eff, past = _VERBS.get(act, (WRITE, "updated"))
        obj = {"issue": "issue", "pr": "pull request", "release": "release", "repo": "repository"}.get(noun, noun)
        return Effect(eff, "github", obj, past or None)
    if prog == "curl" or prog == "wget" or prog == "http" or prog == "httpie":
        method = _curl_method(rest)
        if method in ("POST", "PUT", "PATCH"):
            return Effect(SEND, "web", "http request", "sent")
        if method == "DELETE":
            return Effect(DELETE, "web", "http resource", "deleted")
        return Effect(READ, "web", None, None)
    if prog in ("npm", "pnpm", "yarn", "bun") and sub == "publish":
        return Effect(WRITE, "registry", "package", "published")
    if prog in ("npm", "pnpm", "yarn", "bun", "pip", "pip3", "uv", "poetry", "brew", "apt", "apt-get", "cargo") and sub in (
            "install", "add", "remove", "uninstall", "i", "sync", "upgrade", "update"):
        return Effect(WRITE, "filesystem", "dependency", "installed")
    if prog in ("kubectl", "helm", "terraform", "pulumi", "docker", "flyctl", "vercel", "wrangler", "aws", "gcloud", "az"):
        if sub in ("delete", "destroy", "rm", "rmi", "uninstall"):
            return Effect(DELETE, "infrastructure", "resource", "deleted")
        return Effect(WRITE, "infrastructure", "resource", "changed")
    if prog in ("psql", "mysql", "sqlite3", "mongo", "mongosh", "redis-cli"):
        up = seg.upper()
        if re.search(r"\b(DELETE|DROP|TRUNCATE)\b", up):
            return Effect(DELETE, "database", "row", "deleted")
        if re.search(r"\b(INSERT|UPDATE|ALTER|CREATE)\b", up):
            return Effect(WRITE, "database", "row", "updated")
        return Effect(READ, "database", None, None)
    return Effect(EXECUTE, "shell", "command", "ran")


def _curl_method(args: list[str]) -> str:
    for i, a in enumerate(args):
        if a in ("-X", "--request") and i + 1 < len(args):
            return args[i + 1].upper()
        if a.startswith("-X") and len(a) > 2:
            return a[2:].upper()
    if any(a in ("-d", "--data", "--data-raw", "--data-binary", "-F", "--form", "--json") for a in args):
        return "POST"
    return "GET"


def _override(e: Effect, attrs: dict[str, Any]) -> Effect:
    if not any(k in attrs for k in ("effect", "system", "object", "verb")):
        return e
    return Effect(
        effect=str(attrs.get("effect") or e.effect),
        system=str(attrs.get("system") or e.system),
        object=attrs.get("object") or e.object,
        verb=attrs.get("verb") or e.verb,
    )


# --- outcomes -------------------------------------------------------------------------

_AMBIGUOUS = re.compile(
    r"time[d ]?\s*out|timeout|deadline|etimedout|econnreset|econnaborted|epipe|broken pipe|"
    r"connection (reset|aborted|closed|lost)|socket hang ?up|server disconnected|remote end closed|"
    r"unexpected eof|\b50[234]\b|bad gateway|gateway|service unavailable|interrupted|aborted|killed",
    re.I,
)


def is_ambiguous_error(*texts: Any) -> bool:
    """Did the call fail in a way where the remote side may still have acted?"""
    return any(t and _AMBIGUOUS.search(str(t)) for t in texts)


# Exit codes where the request may already have reached the server.
_AMBIGUOUS_EXIT = {
    "curl": {18, 28, 52, 55, 56, 92},  # partial transfer, timeout, empty reply, send/recv failure, HTTP/2 stream
    "wget": {4},                       # network failure (may be mid-transfer)
}
_KILLED_EXIT = {124, 137, 143}          # `timeout`, SIGKILL, SIGTERM: interrupted mid-flight


def is_ambiguous_exit(command: str, exit_code: Any) -> bool:
    try:
        code = int(exit_code)
    except (TypeError, ValueError):
        return False
    if code in _KILLED_EXIT:
        return True
    progs = {seg.strip().split()[0].rsplit("/", 1)[-1] for seg in re.split(r"&&|\|\||;|\|", command or "") if seg.strip()}
    return any(code in _AMBIGUOUS_EXIT.get(p, ()) for p in progs)
