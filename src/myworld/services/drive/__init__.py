"""Google Drive.

Tool names and parameters follow the Google Workspace MCP server (taylorwilsdon/google_workspace_mcp,
Drive tools). The model follows the Drive API: files and folders with parents, per-file
permissions (owner, writer, commenter, reader), link sharing, trash, and Drive's query syntax.
Files shared with you as a reader can't be edited, and a workspace policy can block sharing
outside the company, both common agent failures.

Seed format::

    user: {email: alex@acme.com}
    domain: acme.com
    policy: {external_sharing: false}            # optional admin restriction
    folders: [{key: finance, name: Finance}, {key: q4, name: Q4, parent: finance}]
    files:
      - {name: Q4 budget, type: sheet, parent: q4, content: "team,amount\\neng,120000"}
      - {name: Vendor contract.pdf, mimeType: application/pdf, size: 482113,
         owner: legal@acme.com, shared_role: reader}  # someone else's file, shared with you
      - {name: Headcount, type: sheet, sheets: {Plan: "team,hc\neng,12", Summary: "total,=SUM(Plan!B2:B9)"}}

From 2026-09-25.1 there are Sheets and Docs tools too. Sheets hold real cells: A1 ranges with
optional sheet names ('Q4 plan'!A1:C9), USER_ENTERED values parse numbers and formulas (SUM,
AVERAGE, MIN, MAX, COUNT, arithmetic, cross-sheet refs) while RAW keeps text, writes can't spill
out of an explicit range or past the grid, and reads return formatted values with trailing
blanks trimmed, like the API. Docs use the Docs API's 1-based indices.
"""

from __future__ import annotations

from .actions import act_edit_content, act_revoke_access, act_share, act_trash
from .docs import create_doc, find_and_replace_doc, get_doc_content, modify_doc_text, search_docs
from .files import (
               copy_drive_file,
               create_drive_file,
               create_drive_folder,
               get_drive_file_content,
               get_drive_file_download_url,
               get_drive_shareable_link,
               import_to_google_doc,
               list_drive_items,
               manage_drive_access,
               search_drive_files,
               set_drive_file_permissions,
               update_drive_file,
)
from .service import Drive
from .sheets import (
               create_sheet,
               create_spreadsheet,
               get_spreadsheet_info,
               list_spreadsheets,
               modify_sheet_values,
               read_sheet_values,
)

Drive.tools = [search_drive_files, get_drive_file_content, get_drive_file_download_url, create_drive_file,
               create_drive_folder, import_to_google_doc, list_drive_items, copy_drive_file, update_drive_file,
               get_drive_shareable_link, manage_drive_access, set_drive_file_permissions,
               list_spreadsheets, get_spreadsheet_info, read_sheet_values, modify_sheet_values, create_spreadsheet,
               create_sheet, search_docs, get_doc_content, create_doc, modify_doc_text, find_and_replace_doc]
Drive.actions = [act_revoke_access, act_share, act_edit_content, act_trash]

__all__ = ["Drive"]
