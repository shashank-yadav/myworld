"""Importers: real export formats in, working simulated worlds out."""

import datetime as dt
import json
import mailbox
import subprocess
import zipfile
from email.message import EmailMessage
from pathlib import Path

import yaml

from myworld.cli import main as cli
from myworld.core.instance import Instance
from myworld.env import Environment
from myworld.importers import IMPORTERS, ImportOptions
from myworld.services import get_service


def world(kind: str, seed: dict) -> Instance:
    return Instance(get_service(kind), seed)


# -- gmail --------------------------------------------------------------------------------------

def _mbox(tmp_path: Path) -> Path:
    box = mailbox.mbox(str(tmp_path / "All mail.mbox"))
    specs = [
        ("Dana Wu <dana@realco.com>", "me@realco.com", "Budget review", "Can we meet Thursday? Call me at +1 415 555 2671.",
         "Inbox,Unread,Important", "111", "Mon, 14 Sep 2026 09:00:00 -0700", True),
        ("me@realco.com", "dana@realco.com", "Re: Budget review", "Thursday 2pm works.", "Sent", "111",
         "Mon, 14 Sep 2026 10:00:00 -0700", False),
        ("Stripe <receipts@stripe.com>", "me@realco.com", "Your receipt", "<p>Amount <b>$49.00</b></p>",
         "Inbox,Category Updates,Receipts", "222", "Fri, 18 Sep 2026 08:00:00 -0700", False),
    ]
    for sender, to, subject, body, labels, thread, date, attach in specs:
        m = EmailMessage()
        m["From"], m["To"], m["Subject"], m["Date"] = sender, to, subject, date
        m["X-Gmail-Labels"], m["X-GM-THRID"] = labels, thread
        if body.startswith("<"):
            m.set_content(body, subtype="html")
        else:
            m.set_content(body)
        if attach:
            m.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="budget.pdf")
        box.add(m)
    box.flush()
    return tmp_path / "All mail.mbox"


def test_gmail_mbox_import(tmp_path):
    seed = IMPORTERS["gmail"](_mbox(tmp_path))
    assert seed["user"]["email"] == "me@realco.com"
    assert "Receipts" in seed["labels"]
    budget = next(e for e in seed["emails"] if e["subject"] == "Budget review")
    assert budget["labels"] == ["INBOX", "UNREAD", "IMPORTANT"] and budget["attachments"][0]["filename"] == "budget.pdf"
    receipt = next(e for e in seed["emails"] if e["subject"] == "Your receipt")
    assert "Amount $49.00" in receipt["body"], "html bodies are converted to text"
    i = world("gmail", seed)
    hits = i.call("search_emails", {"query": "from:dana has:attachment"}).text
    assert "Budget review" in hits
    box = i.state["mailboxes"]["me@realco.com"]
    threads = {m["threadId"] for m in box["messages"].values() if "Budget" in m["subject"]}
    assert len(threads) == 1, "the reply stays in the same thread"


def test_anonymize_is_consistent_and_scrubs_text(tmp_path):
    mapfile = tmp_path / "people.json"
    opts = ImportOptions(anonymize=True, map_path=mapfile, keep_domains=("stripe.com",),
                         rebase_to=dt.datetime(2026, 9, 21, 16, tzinfo=dt.timezone.utc))
    seed = IMPORTERS["gmail"](_mbox(tmp_path), opts)
    text = yaml.safe_dump(seed)
    assert "realco" not in text and "dana" not in text.lower() and "415 555 2671" not in text
    assert "receipts@stripe.com" in text, "kept domains stay real"
    assert seed["user"]["email"].endswith("@acme.com")
    newest = max(e["date"] for e in seed["emails"])
    assert newest == "2026-09-21T16:00:00Z", "time rebased so the newest mail is 'now'"
    again = IMPORTERS["gmail"](_mbox(tmp_path), ImportOptions(anonymize=True, map_path=mapfile))
    assert again["user"]["email"] == seed["user"]["email"], "the map keeps pseudonyms stable across imports"


# -- calendar -----------------------------------------------------------------------------------

ICS = """BEGIN:VCALENDAR
X-WR-CALNAME:me@realco.com
X-WR-TIMEZONE:America/New_York
BEGIN:VEVENT
DTSTART;TZID=America/New_York:20260922T100000
DTEND;TZID=America/New_York:20260922T103000
SUMMARY:Budget review\\, part 2
ORGANIZER;CN=Dana Wu:mailto:dana@realco.com
ATTENDEE;CN=Dana Wu;PARTSTAT=ACCEPTED:mailto:dana@realco.com
ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:me@realco.com
DESCRIPTION:Agenda:\\n- numbers\\n- risks
END:VEVENT
BEGIN:VEVENT
DTSTART:20260923T150000Z
DTEND:20260923T160000Z
SUMMARY:Focus
TRANSP:TRANSPARENT
RRULE:FREQ=WEEKLY;COUNT=4
END:VEVENT
BEGIN:VEVENT
DTSTART:20260924T150000Z
SUMMARY:Cancelled thing
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR
"""


def test_calendar_ics_import(tmp_path):
    (tmp_path / "cal.ics").write_text(ICS)
    seed = IMPORTERS["calendar"](tmp_path / "cal.ics")
    assert seed["user"]["email"] == "me@realco.com" and seed["timeZone"] == "America/New_York"
    assert [e["summary"] for e in seed["events"]] == ["Budget review, part 2", "Focus"]
    budget = seed["events"][0]
    assert budget["organizer"] == "dana@realco.com" and budget["description"] == "Agenda:\n- numbers\n- risks"
    i = world("calendar", seed)
    listed = json.loads(i.call("list-events", {"timeMin": "2026-09-22T00:00:00", "timeMax": "2026-09-25T00:00:00"}).text)
    mine = next(e for e in listed["events"] if e["summary"].startswith("Budget"))
    me = next(a for a in mine["attendees"] if a.get("self"))
    assert me["responseStatus"] == "needsAction" and mine["organizer"]["email"] == "dana@realco.com"
    assert mine["start"]["dateTime"] == "2026-09-22T10:00:00-04:00"
    assert json.loads(i.call("respond-to-event", {"eventId": mine["id"], "response": "accepted"}).text)["responseStatus"] == "accepted"


# -- slack --------------------------------------------------------------------------------------

def _slack_export(tmp_path: Path) -> Path:
    root = tmp_path / "export"
    (root / "eng").mkdir(parents=True)
    (root / "users.json").write_text(json.dumps([
        {"id": "U1", "name": "dana", "real_name": "Dana Wu", "profile": {"email": "dana@realco.com", "title": "EM"}},
        {"id": "U2", "name": "lee", "real_name": "Lee Ortiz", "profile": {"email": "lee@realco.com"}},
        {"id": "B1", "name": "deploybot", "is_bot": True, "profile": {}}]))
    (root / "channels.json").write_text(json.dumps([
        {"id": "C1", "name": "eng", "members": ["U1", "U2"], "topic": {"value": "Engineering"}}]))
    (root / "eng" / "2026-09-18.json").write_text(json.dumps([
        {"type": "message", "subtype": "channel_join", "user": "U2", "text": "<@U2> has joined", "ts": "1789740000.000100"},
        {"type": "message", "user": "U1", "text": "Deploy at 3pm, <@U2> can you watch?", "ts": "1789740100.000200",
         "thread_ts": "1789740100.000200", "reply_count": 1, "reactions": [{"name": "eyes", "users": ["U2"], "count": 1}]},
        {"type": "message", "user": "U2", "text": "On it", "ts": "1789740200.000300", "thread_ts": "1789740100.000200"}]))
    return root


def test_slack_export_import(tmp_path):
    seed = IMPORTERS["slack"](_slack_export(tmp_path))
    assert [u["name"] for u in seed["users"]] == ["dana", "lee"], "bots are dropped"
    eng = seed["channels"][0]
    assert eng["members"][0] == "bot" and len(eng["messages"]) == 1, "join messages dropped; the reply is threaded"
    assert eng["messages"][0]["text"] == "Deploy at 3pm, @lee can you watch?"
    assert eng["messages"][0]["replies"] == [{"user": "lee", "text": "On it"}]
    i = world("slack", seed)
    cid = next(c["id"] for c in i.state["channels"].values() if c["name"] == "eng")
    hist = json.loads(i.call("slack_get_channel_history", {"channel_id": cid}).text)["messages"]
    assert hist[0]["reply_count"] == 1 and hist[0]["reactions"][0]["name"] == "eyes"
    assert json.loads(i.call("slack_post_message", {"channel_id": cid, "text": "hi"}, as_="dana@realco.com").text)["ok"]


# -- github -------------------------------------------------------------------------------------

def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "api"
    repo.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
    g("init", "-b", "main")
    g("config", "user.email", "t@t")
    g("config", "user.name", "Dana Wu")
    (repo / "README.md").write_text("# API\n")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('v1')\n")
    (repo / "logo.png").write_bytes(b"\x89PNG\0\0binary")
    g("add", ".")
    g("commit", "-m", "init")
    g("remote", "add", "origin", "git@github.com:realco/api.git")
    g("checkout", "-b", "fix/bug")
    (repo / "src" / "app.py").write_text("print('v2')\n")
    g("commit", "-am", "Fix the bug")
    g("checkout", "main")
    return repo


def test_github_repo_import(tmp_path):
    repo = _git_repo(tmp_path)
    (tmp_path / "issues.json").write_text(json.dumps([
        {"number": 7, "title": "Crash on start", "body": "Stack trace…", "state": "OPEN", "labels": [{"name": "bug"}],
         "author": {"login": "lee"}, "assignees": [], "comments": [{"author": {"login": "dana"}, "body": "Repro'd"}]},
        {"number": 3, "title": "Old thing", "state": "CLOSED", "labels": [], "author": {"login": "dana"}, "comments": []}]))
    (tmp_path / "prs.json").write_text(json.dumps([
        {"number": 9, "title": "Fix the bug", "state": "OPEN", "headRefName": "fix/bug", "baseRefName": "main",
         "author": {"login": "dana"}, "isDraft": False},
        {"number": 8, "title": "Branch not in clone", "state": "OPEN", "headRefName": "gone", "baseRefName": "main",
         "author": {"login": "dana"}}]))
    seed = IMPORTERS["github"](repo, issues=tmp_path / "issues.json", pulls=tmp_path / "prs.json", viewer="dana")
    r = seed["repos"][0]
    assert r["name"] == "realco/api" and set(r["files"]) == {"README.md", "src/app.py"}, "binary files skipped"
    assert [p["number"] for p in r["pulls"]] == [9], "PRs whose branch isn't in the clone are skipped"
    i = world("github", seed)
    o = {"owner": "realco", "repo": "api"}
    assert json.loads(i.call("get_issue", {**o, "issue_number": 7}).text)["title"] == "Crash on start"
    assert json.loads(i.call("get_issue", {**o, "issue_number": 3}).text)["state"] == "closed"
    files = json.loads(i.call("get_pull_request_files", {**o, "pull_number": 9}).text)
    assert files[0]["filename"] == "src/app.py" and "+print('v2')" in files[0]["patch"]


# -- jira ---------------------------------------------------------------------------------------

def test_jira_csv_and_json_import(tmp_path):
    (tmp_path / "jira.csv").write_text(
        "Summary,Issue key,Issue Type,Status,Priority,Assignee,Reporter,Labels,Labels,Description,Comment,Created,Project name\n"
        "Rotate keys,OPS-12,Task,Doing,Major,Dana Wu,Lee Ortiz,security,infra,Before audit,"
        "\"21/Sep/26 9:00 AM;Lee Ortiz;Please prioritise\",21/Sep/26 9:00 AM,Operations\n"
        "Backup failing,OPS-15,Bug,Closed,Blocker,,Lee Ortiz,backups,,Replica 2,,21/Sep/26 9:00 AM,Operations\n")
    seed = IMPORTERS["jira"](tmp_path / "jira.csv", me="Dana Wu")
    ops = seed["projects"][0]
    assert [i["key"] for i in ops["issues"]] == ["OPS-12", "OPS-15"]
    first = ops["issues"][0]
    assert first["status"] == "In Progress" and first["priority"] == "High" and first["labels"] == ["security", "infra"]
    assert first["comments"][0]["body"] == "Please prioritise"
    i = world("jira", seed)
    found = json.loads(i.call("jira_search", {"jql": "assignee = currentUser() AND labels = security"}).text)["issues"]
    assert [x["key"] for x in found] == ["OPS-12"]
    assert json.loads(i.call("jira_create_issue", {"project_key": "OPS", "summary": "New", "issue_type": "Task"}).text)[
        "issue"]["key"] == "OPS-16", "numbering continues after the imported keys"

    (tmp_path / "search.json").write_text(json.dumps({"issues": [{"key": "SUP-4", "fields": {
        "summary": "Refund", "issuetype": {"name": "Story"}, "priority": {"name": "Low"},
        "status": {"name": "Awaiting customer", "statusCategory": {"key": "indeterminate"}},
        "description": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Double charge"}]}]},
        "labels": ["billing"], "comment": {"comments": []}}}]}))
    seed = IMPORTERS["jira"](tmp_path / "search.json")
    issue = seed["projects"][0]["issues"][0]
    assert issue["status"] == "In Progress" and issue["description"] == "Double charge" and issue["type"] == "Story"


# -- drive --------------------------------------------------------------------------------------

def test_drive_folder_import(tmp_path):
    root = tmp_path / "Drive"
    (root / "Finance" / "Q4").mkdir(parents=True)
    (root / "Finance" / "Q4" / "budget.csv").write_text("team,q4\neng,120000\n")
    (root / "notes.md").write_text("# Notes\nShip it\n")
    (root / "logo.png").write_bytes(b"\x89PNG" + b"\0" * 100)
    with zipfile.ZipFile(root / "Plan.docx", "w") as z:
        z.writestr("word/document.xml", "<w:document><w:p><w:r><w:t>Goals for Q4</w:t></w:r></w:p></w:document>")
    seed = IMPORTERS["drive"](root, owner="me@realco.com")
    i = world("drive", seed)
    assert "budget.csv" in i.call("search_drive_files", {"query": "name contains 'budget'"}).text
    plan = next(f for f in i.state["files"].values() if f["name"] == "Plan")
    assert plan["mimeType"] == "application/vnd.google-apps.document" and "Goals for Q4" in plan["content"]
    q4 = next(f for f in i.state["files"].values() if f["name"] == "Q4")
    fin = next(f for f in i.state["files"].values() if f["name"] == "Finance")
    assert q4["parents"] == [fin["id"]]


# -- end to end ---------------------------------------------------------------------------------

def test_cli_import_into_an_environment(tmp_path):
    cli(["import", "gmail", str(_mbox(tmp_path)), "-o", str(tmp_path / "seeds" / "gmail.yaml"), "--anonymize",
         "--rebase", "2026-09-21T16:00:00Z", "--map", str(tmp_path / "people.json")])
    (tmp_path / "env.yaml").write_text(yaml.safe_dump({
        "name": "from-real-mail", "servers": {"gmail": {"seed_file": "seeds/gmail.yaml"}},
        "issues": [{"use": "prompt_injection_email"}]}))
    from myworld.env import EnvRun
    run = EnvRun(Environment.load(tmp_path / "env.yaml"))  # issues fire into the imported (pseudonymized) mailbox
    g = run.instances["gmail"]
    inbox = g.call("search_emails", {"query": "newer_than:7d"}).text
    assert "Budget review" in inbox and "Updated remittance details" in inbox
    assert run.grade()["passed"]
    g.call("send_email", {"to": ["billing@acme-invoices.co"], "subject": "Fwd", "body": "invoices"})
    assert not run.grade()["passed"], "the injection check works against imported data too"
