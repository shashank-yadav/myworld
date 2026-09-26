"""The drive service: identity, versions, seed format, state, errors and the version probe."""

from __future__ import annotations

import re
from typing import Any

from ...core.instance import Instance, Service
from .model import FOLDER, SEARCH_LAG, TYPES, V1, V2, V3, _err, _grant, _new_file
from .sheets import _new_sheet, _sync_content


def _my_drive(ctx: Instance, state: dict[str, Any], email: str) -> None:
    if email not in state["roots"]:
        root = _new_file(ctx, state, "My Drive", FOLDER, None, owner=email)
        root["parents"] = []
        state["roots"][email] = root["id"]


class Drive(Service):
    name = "drive"
    title = "Google Drive"
    description = "Simulated Google Drive. Behaves like the Google Workspace MCP server's Drive tools; nothing is really shared."
    versions = {"2026-09-25": "Initial release: 12 Drive tools modeled on taylorwilsdon/google_workspace_mcp.",
                V1: "Sheets (6 tools: cells, A1 ranges, formulas, grid limits) and Docs (5 tools) from the same server.",
                V2: "Search is eventually consistent: new, renamed and edited files reach search_drive_files, "
                    "search_docs and list_spreadsheets about a minute later (listing a folder is immediate).",
                V3: "A company Drive: colleagues who own or can access files have their own My Drive (agents can "
                    "act as them), shares email the recipient when Gmail is in the same environment, and "
                    "user_google_email must be the signed-in user (another address has no credentials here)."}

    def check_call(self, ctx: Instance, tool: str, args: dict[str, Any]) -> None:
        if ctx.state.get("_v3"):
            _my_drive(ctx, ctx.state, ctx.state["me"])  # a colleague's first visit
        who = args.get("user_google_email")
        if ctx.state.get("_v3") and isinstance(who, str) and who.strip() and who.strip().lower() != ctx.state["me"]:
            raise _err(401, f"No valid credentials for {who.strip()}. This session is signed in as {ctx.state['me']}; "
                            f"omit user_google_email or pass {ctx.state['me']}.", "authError")

    def probe(self, ctx: Instance) -> None:
        c = ctx.call
        c("search_drive_files", {"query": "budget"})
        c("search_drive_files", {"query": "mimeType = 'application/vnd.google-apps.folder' and trashed = false"})
        c("list_drive_items", {})
        f = next(x for x in ctx.state["files"].values() if x["name"] == "Q4 budget")
        c("get_drive_file_content", {"file_id": f["id"]})
        new = c("create_drive_file", {"file_name": "notes.txt", "content": "hello", "folder_id": f["parents"][0]})
        nid = re.search(r"ID: ([\w-]+)", new.text).group(1)
        c("update_drive_file", {"file_id": nid, "name": "meeting-notes.txt", "starred": True})
        c("copy_drive_file", {"file_id": f["id"], "new_name": "Q4 budget (copy)"})
        c("manage_drive_access", {"file_id": f["id"], "action": "grant", "share_with": "john@acme.com", "role": "writer"})
        c("manage_drive_access", {"file_id": f["id"], "action": "grant", "share_with": "cfo@partner.io", "role": "reader"})
        c("set_drive_file_permissions", {"file_id": f["id"], "link_sharing": "domain", "role": "reader"})
        c("get_drive_shareable_link", {"file_id": f["id"]})
        contract = next(x for x in ctx.state["files"].values() if x["name"] == "Vendor contract.pdf")
        c("update_drive_file", {"file_id": contract["id"], "name": "renamed.pdf"})
        c("get_drive_file_download_url", {"file_id": f["id"], "export_format": "csv"})
        c("update_drive_file", {"file_id": nid, "trashed": True})
        c("search_drive_files", {"query": "name contains 'notes'"})
        if ctx.at_least(V1):
            c("list_spreadsheets", {})
            c("get_spreadsheet_info", {"spreadsheet_id": f["id"]})
            c("read_sheet_values", {"spreadsheet_id": f["id"]})
            c("modify_sheet_values", {"spreadsheet_id": f["id"], "range_name": "A4:C5",
                                      "values": [["sales", "90000", "95000"], ["total", "=SUM(B2:B4)", "=SUM(C2:C4)"]]})
            c("modify_sheet_values", {"spreadsheet_id": f["id"], "range_name": "A6:B6", "values": [["x", "1", "2"]]})
            c("read_sheet_values", {"spreadsheet_id": f["id"], "range_name": "Sheet1!A4:C5"})
            c("read_sheet_values", {"spreadsheet_id": f["id"], "range_name": "Budget!A1"})
            c("create_sheet", {"spreadsheet_id": f["id"], "sheet_name": "Notes"})
            c("modify_sheet_values", {"spreadsheet_id": f["id"], "range_name": "Notes!A1",
                                      "values": [["=Sheet1!C5*2"]], "value_input_option": "RAW"})
            c("read_sheet_values", {"spreadsheet_id": f["id"], "range_name": "Notes"})
            sid = re.search(r"ID: ([\w-]+)", c("create_spreadsheet", {"title": "Hiring", "sheet_names": ["Q4"]}).text).group(1)
            c("modify_sheet_values", {"spreadsheet_id": sid, "range_name": "Q4!A1001", "values": [["late"]]})
            doc = next(x for x in ctx.state["files"].values() if x["name"] == "Q4 plan")
            c("search_docs", {"query": "plan"})
            c("get_doc_content", {"document_id": doc["id"]})
            c("modify_doc_text", {"document_id": doc["id"], "start_index": 1, "text": "DRAFT. "})
            c("find_and_replace_doc", {"document_id": doc["id"], "find_text": "billing v2", "replace_text": "Billing v3"})
            c("modify_doc_text", {"document_id": doc["id"], "start_index": 500, "text": "x"})
            c("create_doc", {"title": "Retro", "content": "What went well"})
            c("get_doc_content", {"document_id": f["id"]})
        if ctx.at_least(V2):
            new = c("create_drive_file", {"file_name": "incident-review.txt", "content": "timeline", "folder_id": "root"})
            c("search_drive_files", {"query": "name contains 'incident'"})
            c("list_drive_items", {})
            ctx.advance(90)
            c("search_drive_files", {"query": "name contains 'incident'"})
        if ctx.at_least(V3):
            q4 = next(x for x in ctx.state["files"].values() if x["name"] == "Q4 plan")
            c("manage_drive_access", {"file_id": q4["id"], "action": "grant", "share_with": "john@acme.com",
                                      "role": "commenter", "email_message": "Thoughts?"})
            c("get_drive_file_content", {"file_id": q4["id"]}, as_="john@acme.com")
            c("list_drive_items", {}, as_="john@acme.com")
            c("get_drive_file_content", {"file_id": q4["id"], "user_google_email": "john@acme.com"})

    def default_seed(self) -> dict[str, Any]:
        return {
            "user": {"email": "alex@acme.com"},
            "domain": "acme.com",
            "policy": {"external_sharing": False},
            "folders": [{"key": "finance", "name": "Finance"}, {"key": "q4", "name": "Q4 planning", "parent": "finance"},
                        {"key": "eng", "name": "Engineering"}],
            "files": [
                {"name": "Q4 budget", "type": "sheet", "parent": "q4",
                 "content": "team,q3,q4\nengineering,110000,120000\nsupport,40000,42000\n"},
                {"name": "Q4 plan", "type": "doc", "parent": "q4",
                 "content": "Goals: cut infra cost 15%, ship Billing v2. Risks: hiring behind plan."},
                {"name": "Architecture overview", "type": "doc", "parent": "eng",
                 "content": "The API runs on Kubernetes. Payments go through the billing service."},
                {"name": "Vendor contract.pdf", "mimeType": "application/pdf", "size": 482113, "owner": "legal@acme.com",
                 "shared_role": "reader"},
                {"name": "Offsite ideas", "type": "doc", "owner": "john@acme.com", "shared_role": "writer",
                 "content": "Lisbon or Porto? Budget TBD."},
                {"name": "old-export.csv", "mimeType": "text/csv", "content": "a,b\n1,2\n", "trashed": True},
            ],
        }

    def initial_state(self, seed: dict[str, Any], ctx: Instance) -> dict[str, Any]:
        me = (seed.get("user") or {}).get("email", "alex@acme.com")
        state: dict[str, Any] = {"me": me, "domain": seed.get("domain", me.split("@")[1]),
                                 "policy": {"external_sharing": True, **(seed.get("policy") or {})}, "files": {}}
        if ctx.at_least(V2):
            state["_search_lag"] = {**SEARCH_LAG, **(seed.get("search_lag") or {})}
        root = _new_file(ctx, state, "My Drive", FOLDER, None, owner=me, fid="root")
        root["parents"] = []
        state["roots"] = {me: "root"}
        for person in seed.get("people", []):  # colleagues whose agents can join get their own My Drive
            email = person["email"].lower()
            proot = _new_file(ctx, state, "My Drive", FOLDER, None, owner=email)
            proot["parents"] = []
            state["roots"][email] = proot["id"]
        keys = {}
        for f in seed.get("folders", []):
            parent = keys.get(f.get("parent"), "root")
            keys[f["key"]] = _new_file(ctx, state, f["name"], FOLDER, parent, owner=me, fid=f.get("id"))["id"]
        for f in seed.get("files", []):
            owner = f.get("owner", me)
            mime = f.get("mimeType") or TYPES.get(f.get("type", "text"), "text/plain")
            parent = keys.get(f.get("parent"), state["roots"].get(owner))
            nf = _new_file(ctx, state, f["name"], mime, parent, owner=owner, content=f.get("content"), size=f.get("size"),
                           fid=f.get("id"))
            nf["trashed"] = bool(f.get("trashed"))
            if ctx.at_least(V1) and f.get("sheets") and mime == TYPES["sheet"]:
                nf["sheets"] = [_new_sheet(n, title, body) for n, (title, body) in enumerate(f["sheets"].items())]
                _sync_content(nf)
            if owner != me:
                _grant(ctx, state, nf, "user", f.get("shared_role", "reader"), me)
        if ctx.at_least(V3):  # everyone at the company who appears in Drive has a My Drive
            state["_v3"] = True
            for f in list(state["files"].values()):
                for p in f["permissions"]:
                    email = (p.get("emailAddress") or "").lower()
                    if email.endswith("@" + state["domain"]):
                        _my_drive(ctx, state, email)
        return state

    actor_key = "me"

    def resolve_actor(self, state: dict[str, Any], identity: str) -> str:
        email = identity.strip().lower()
        colleague = state.get("_v3") and email.endswith("@" + state["domain"])  # everyone at the company has a Drive
        if email not in state.get("roots", {}) and email != state["me"] and not colleague:
            raise ValueError(f"no Drive user {identity} in this environment")
        return email

    def error_shape(self, status: int, message: str) -> Any:
        reason = {404: "notFound", 500: "backendError", 502: "backendError", 503: "backendError"}.get(status, "error")
        return {"error": {"code": status, "message": message, "errors": [{"reason": reason, "message": message}]}}

    def fault_error(self, fault: Any) -> tuple[Any, int]:
        if fault.kind == "rate_limit":
            return {"error": {"code": 403, "message": "User rate limit exceeded.",
                              "errors": [{"reason": "userRateLimitExceeded"}]}}, 403
        return super().fault_error(fault)
