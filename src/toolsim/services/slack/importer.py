"""Slack from a workspace export (the zip from Settings > Import/Export, or its unzipped folder).

Keeps users, public (and, when exported, private) channels, members, topics, messages, threads
and reactions. Join/leave and other system messages are dropped. The MCP server's bot joins
the channels you choose (default: every public channel).
"""

from __future__ import annotations

import json
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from ...importers.common import ImportOptions

SKIP_SUBTYPES = {"channel_join", "channel_leave", "channel_topic", "channel_purpose", "channel_name", "group_join",
                 "group_leave", "bot_add", "bot_remove", "pinned_item", "reminder_add"}


def _root(path: Path) -> Path:
    if path.is_file() and zipfile.is_zipfile(path):
        tmp = Path(tempfile.mkdtemp(prefix="toolsim-slack-"))
        with zipfile.ZipFile(path) as z:
            z.extractall(tmp)
        path = tmp
    if not (path / "users.json").exists():
        inner = [d for d in path.iterdir() if d.is_dir() and (d / "users.json").exists()]
        if not inner:
            raise ValueError(f"{path} doesn't look like a Slack export (no users.json)")
        path = inner[0]
    return path


def import_export(path: str | Path, opts: ImportOptions | None = None, *, bot_name: str = "assistant",
                  bot_channels: list[str] | None = None, home_domain: str | None = None) -> dict[str, Any]:
    opts = opts or ImportOptions()
    root = _root(Path(path))
    users_raw = json.loads((root / "users.json").read_text())
    emails = [u.get("profile", {}).get("email") for u in users_raw if u.get("profile", {}).get("email")]
    opts.home = opts.home or home_domain or (max(set(e.split("@")[1] for e in emails),
                                                  key=[e.split("@")[1] for e in emails].count) if emails else "example.com")
    names: dict[str, str] = {}
    users = []
    for u in users_raw:
        if u.get("deleted") or u.get("id") == "USLACKBOT" or u.get("is_bot"):
            continue
        prof = u.get("profile", {})
        real_email = prof.get("email") or f"{u['name']}@{opts.home}"
        email, real_name = opts.person(real_email, u.get("real_name") or prof.get("real_name"))
        handle = email.split("@")[0].replace(".", "") if opts.anonymize else u["name"]
        names[u["id"]] = handle
        users.append({"name": handle, "real_name": real_name if opts.anonymize else (u.get("real_name") or handle),
                      "email": email, "title": "" if opts.anonymize else prof.get("title", ""), "tz": u.get("tz", "UTC")})

    def mention(text: str) -> str:
        text = re.sub(r"<@(U\w+)(\|[^>]*)?>", lambda m: f"@{names.get(m.group(1), 'someone')}", text or "")
        text = re.sub(r"<#C\w+\|([^>]+)>", r"#\1", text)
        return opts.text(re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2 (\1)", text)) or ""

    chan_specs = [(c, False) for c in json.loads((root / "channels.json").read_text())]
    if (root / "groups.json").exists():
        chan_specs += [(c, True) for c in json.loads((root / "groups.json").read_text())]
    channels = []
    for c, private in chan_specs:
        folder = root / c["name"]
        msgs: list[dict[str, Any]] = []
        for day in sorted(folder.glob("*.json")) if folder.exists() else []:
            msgs += [m for m in json.loads(day.read_text()) if m.get("type") == "message"
                     and m.get("subtype") not in SKIP_SUBTYPES and m.get("user") in names]
        msgs.sort(key=lambda m: float(m["ts"]))
        by_ts = {}
        top = []
        for m in msgs:
            is_reply = m.get("thread_ts") and m["thread_ts"] != m["ts"]
            entry = {"user": names[m["user"]], "text": mention(m.get("text", ""))}
            reactions = {r["name"]: [names[x] for x in r.get("users", []) if x in names] for r in m.get("reactions", [])}
            if reactions := {k: v for k, v in reactions.items() if v}:
                entry["reactions"] = reactions
            if is_reply and m["thread_ts"] in by_ts:
                by_ts[m["thread_ts"]].setdefault("replies", []).append(entry)
            elif not is_reply:
                by_ts[m["ts"]] = entry
                top.append(entry)
        top = opts.newest(list(enumerate(top)), key=lambda it: it[0])
        members = [names[x] for x in c.get("members", []) if x in names]
        in_bot = (not private) if bot_channels is None else c["name"] in bot_channels
        channels.append({"name": c["name"], "private": private, "topic": mention(c.get("topic", {}).get("value", "")),
                         "purpose": mention(c.get("purpose", {}).get("value", "")),
                         "members": (["bot"] if in_bot else []) + members, "messages": [m for _, m in top],
                         **({"archived": True} if c.get("is_archived") else {})})
    opts.save_map()
    return {"team": {"name": "Imported workspace", "domain": opts.domain.split(".")[0] if opts.anonymize else "workspace"},
            "bot": {"name": bot_name}, "users": users, "channels": channels}
