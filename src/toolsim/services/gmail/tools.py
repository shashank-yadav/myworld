"""gmail: agent-facing tools."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from ...core.instance import Instance
from ...core.tools import ToolError, tool
from .delivery import _after_send, _check_quota, _deliver
from .model import V1, _addr, _check_labels, _get, _invalid, _label_by_name, _mb, _new_label, _not_found, _store
from .search import _matches


@tool("send_email", destructive=False)
def send_email(ctx: Instance,
               to: Annotated[list[str], "List of recipient email addresses"],
               subject: Annotated[str, "Email subject"],
               body: Annotated[str, "Email body content (used for text/plain or when htmlBody not provided)"],
               cc: Annotated[list[str] | None, "List of CC recipients"] = None,
               bcc: Annotated[list[str] | None, "List of BCC recipients"] = None,
               mimeType: Annotated[Literal["text/plain", "text/html", "multipart/alternative"] | None, "Email content type"] = None,
               htmlBody: Annotated[str | None, "HTML version of the email body"] = None,
               attachments: Annotated[list[str] | None, "List of file paths to attach"] = None,
               threadId: Annotated[str | None, "Thread ID to reply to"] = None,
               inReplyTo: Annotated[str | None, "Message ID being replied to"] = None) -> str:
    """Sends a new email"""
    s = _mb(ctx)
    if not to:
        raise _invalid("Recipient address required")
    if ctx.at_least(V1):
        _check_quota(ctx, s, len(to) + len(cc or []) + len(bcc or []))
    for a in [*to, *(cc or []), *(bcc or [])]:
        if "@" not in _addr(a):
            raise _invalid(f"Invalid To header: {a}")
    if threadId and not any(m["threadId"] == threadId for m in s["messages"].values()):
        raise _not_found()
    user = s["user"]
    msg = _store(ctx, s, sender=f"{user['name']} <{user['email']}>", to=to, cc=cc or [], bcc=bcc or [],
                 subject=subject, body=body, html_body=htmlBody, labels=["SENT"], date=ctx.now(), thread_id=threadId,
                 attachments=[{"filename": p.rsplit("/", 1)[-1]} for p in attachments or []])
    # mail to yourself lands in your inbox too
    if user["email"].lower() in {_addr(a) for a in [*to, *(cc or [])]}:
        msg["labelIds"] += ["INBOX", "UNREAD"]
    _deliver(ctx, msg, s)  # colleagues in this world actually receive it
    if ctx.at_least(V1):
        _after_send(ctx, msg)
    return f"Email sent successfully with ID: {msg['id']}"


@tool("draft_email")
def draft_email(ctx: Instance,
                to: Annotated[list[str], "List of recipient email addresses"],
                subject: Annotated[str, "Email subject"],
                body: Annotated[str, "Email body content"],
                cc: Annotated[list[str] | None, "List of CC recipients"] = None,
                bcc: Annotated[list[str] | None, "List of BCC recipients"] = None,
                mimeType: Annotated[Literal["text/plain", "text/html", "multipart/alternative"] | None, "Email content type"] = None,
                htmlBody: Annotated[str | None, "HTML version of the email body"] = None,
                attachments: Annotated[list[str] | None, "List of file paths to attach"] = None,
                threadId: Annotated[str | None, "Thread ID to reply to"] = None,
                inReplyTo: Annotated[str | None, "Message ID being replied to"] = None) -> str:
    """Draft a new email"""
    s, user = _mb(ctx), _mb(ctx)["user"]
    msg = _store(ctx, s, sender=f"{user['name']} <{user['email']}>", to=to, cc=cc or [], bcc=bcc or [],
                 subject=subject, body=body, html_body=htmlBody, labels=["DRAFT"], date=ctx.now(), thread_id=threadId)
    did = "r" + "".join(ctx.rng.choice("0123456789") for _ in range(19))
    s["drafts"][did] = {"id": did, "messageId": msg["id"]}
    return f"Email draft created successfully with ID: {did}"


@tool("read_email", read_only=True)
def read_email(ctx: Instance, messageId: Annotated[str, "ID of the email message to retrieve"]) -> str:
    """Retrieves the content of a specific email"""
    m = _get(_mb(ctx), messageId)
    out = (f"Thread ID: {m['threadId']}\nSubject: {m['subject']}\nFrom: {m['from']}\nTo: {', '.join(m['to'])}\n"
           + (f"Cc: {', '.join(m['cc'])}\n" if m["cc"] else "") + f"Date: {m['date']}\n\n{m['body']}")
    if m["attachments"]:
        out += f"\n\nAttachments ({len(m['attachments'])}):\n" + "\n".join(
            f"- {a['filename']} ({a['mimeType']}, {a['size'] // 1024} KB, ID: {a['attachmentId']})" for a in m["attachments"])
    return out


@tool("download_attachment")
def download_attachment(ctx: Instance,
                        messageId: Annotated[str, "ID of the email message containing the attachment"],
                        attachmentId: Annotated[str, "ID of the attachment to download"],
                        filename: Annotated[str | None, "Filename to save the attachment as"] = None,
                        savePath: Annotated[str | None, "Directory path to save the attachment"] = None) -> str:
    """Downloads an email attachment to a specified location"""
    m = _get(_mb(ctx), messageId)
    att = next((a for a in m["attachments"] if a["attachmentId"] == attachmentId), None)
    if att is None:
        raise _not_found()
    path = f"{(savePath or '.').rstrip('/')}/{filename or att['filename']}"
    _mb(ctx)["downloads"].append({"messageId": messageId, "attachmentId": attachmentId, "path": path})
    return (f"Attachment downloaded successfully:\nFile: {filename or att['filename']}\nSize: {att['size']} bytes\n"
            f"Saved to: {path}")


@tool("search_emails", read_only=True)
def search_emails(ctx: Instance,
                  query: Annotated[str, "Gmail search query (e.g., 'from:example@gmail.com')"],
                  maxResults: Annotated[int | None, "Maximum number of results to return"] = None) -> str:
    """Searches for emails using Gmail search syntax"""
    s = _mb(ctx)
    lag = s.get("_index_lag", 0)
    cutoff = int((ctx.now().timestamp() - lag) * 1000) if lag else None
    since = s.get("_index_lag_since", 0)
    hits = [m for m in s["messages"].values() if _matches(s, m, query, ctx.now())
            and (cutoff is None or int(m["internalDate"]) <= since or int(m["internalDate"]) <= cutoff)]
    hits.sort(key=lambda m: int(m["internalDate"]), reverse=True)
    hits = hits[: maxResults or 10]
    return "\n".join(f"ID: {m['id']}\nSubject: {m['subject']}\nFrom: {m['from']}\nDate: {m['date']}\n" for m in hits)


@tool("modify_email", idempotent=True)
def modify_email(ctx: Instance,
                 messageId: Annotated[str, "ID of the email message to modify"],
                 labelIds: Annotated[list[str] | None, "List of label IDs to apply"] = None,
                 addLabelIds: Annotated[list[str] | None, "List of label IDs to add to the message"] = None,
                 removeLabelIds: Annotated[list[str] | None, "List of label IDs to remove from the message"] = None) -> str:
    """Modifies email labels (move to different folders)"""
    s = _mb(ctx)
    m = _get(s, messageId)
    add = _check_labels(s, (addLabelIds or []) + (labelIds or []))
    remove = _check_labels(s, removeLabelIds)
    m["labelIds"] = [l for l in dict.fromkeys(m["labelIds"] + add) if l not in remove]
    return f"Email {messageId} labels updated successfully"


@tool("delete_email", destructive=True, idempotent=True)
def delete_email(ctx: Instance, messageId: Annotated[str, "ID of the email message to delete"]) -> str:
    """Permanently deletes an email"""
    _get(_mb(ctx), messageId)
    del _mb(ctx)["messages"][messageId]
    return f"Email {messageId} deleted successfully"


@tool("list_email_labels", read_only=True)
def list_email_labels(ctx: Instance) -> str:
    """Retrieves all available Gmail labels"""
    labels = list(_mb(ctx)["labels"].values())
    system = [l for l in labels if l["type"] == "system"]
    user = [l for l in labels if l["type"] == "user"]
    fmt = lambda l: f"ID: {l['id']}\nName: {l['name']}\n"  # noqa: E731
    return (f"Found {len(labels)} labels ({len(system)} system, {len(user)} user):\n\n"
            "System Labels:\n" + "\n".join(map(fmt, system)) + "\nUser Labels:\n" + "\n".join(map(fmt, user)))


@tool("batch_modify_emails", idempotent=True)
def batch_modify_emails(ctx: Instance,
                        messageIds: Annotated[list[str], "List of message IDs to modify"],
                        addLabelIds: Annotated[list[str] | None, "List of label IDs to add to all messages"] = None,
                        removeLabelIds: Annotated[list[str] | None, "List of label IDs to remove from all messages"] = None,
                        batchSize: Annotated[int | None, "Number of messages to process in each batch (default: 50)"] = 50) -> str:
    """Modifies labels for multiple emails in batches"""
    s = _mb(ctx)
    add, remove = _check_labels(s, addLabelIds), _check_labels(s, removeLabelIds)
    ok, failed = [], []
    for mid in messageIds:
        m = s["messages"].get(mid)
        if m is None:
            failed.append(mid)
            continue
        m["labelIds"] = [l for l in dict.fromkeys(m["labelIds"] + add) if l not in remove]
        ok.append(mid)
    out = f"Batch label modification complete.\nSuccessfully processed: {len(ok)} messages\n"
    if failed:
        out += f"Failed to process: {len(failed)} messages\n\nFailed message IDs:\n" + "\n".join(
            f"- {m[:16]}... (Requested entity was not found.)" for m in failed)
    return out


@tool("batch_delete_emails", destructive=True)
def batch_delete_emails(ctx: Instance,
                        messageIds: Annotated[list[str], "List of message IDs to delete"],
                        batchSize: Annotated[int | None, "Number of messages to process in each batch (default: 50)"] = 50) -> str:
    """Permanently deletes multiple emails in batches"""
    s = _mb(ctx)
    ok = [m for m in messageIds if s["messages"].pop(m, None) is not None]
    failed = [m for m in messageIds if m not in ok]
    out = f"Batch delete operation complete.\nSuccessfully deleted: {len(ok)} messages\n"
    if failed:
        out += f"Failed to delete: {len(failed)} messages\n\nFailed message IDs:\n" + "\n".join(
            f"- {m[:16]}... (Requested entity was not found.)" for m in failed)
    return out


def _label_text(prefix: str, l: dict[str, Any]) -> str:
    return f"{prefix}\nID: {l['id']}\nName: {l['name']}\nType: {l['type']}"


@tool("create_label")
def create_label(ctx: Instance,
                 name: Annotated[str, "Name for the new label"],
                 messageListVisibility: Annotated[Literal["show", "hide"] | None, "Whether to show or hide the label in the message list"] = None,
                 labelListVisibility: Annotated[Literal["labelShow", "labelShowIfUnread", "labelHide"] | None, "Visibility of the label in the label list"] = None) -> str:
    """Creates a new Gmail label"""
    s = _mb(ctx)
    if _label_by_name(s, name):
        raise ToolError({"error": {"code": 409, "message": "Label name exists or conflicts", "status": "ALREADY_EXISTS"}},
                        status=409)
    return _label_text("Label created successfully:", _new_label(ctx, s, name, messageListVisibility or "show",
                                                                 labelListVisibility or "labelShow"))


@tool("update_label", idempotent=True)
def update_label(ctx: Instance,
                 id: Annotated[str, "ID of the label to update"],
                 name: Annotated[str | None, "New name for the label"] = None,
                 messageListVisibility: Annotated[Literal["show", "hide"] | None, "Whether to show or hide the label in the message list"] = None,
                 labelListVisibility: Annotated[Literal["labelShow", "labelShowIfUnread", "labelHide"] | None, "Visibility of the label in the label list"] = None) -> str:
    """Updates an existing Gmail label"""
    l = _mb(ctx)["labels"].get(id)
    if l is None:
        raise _not_found()
    if l["type"] == "system":
        raise _invalid("Invalid update request: system labels can't be modified")
    for k, v in (("name", name), ("messageListVisibility", messageListVisibility),
                 ("labelListVisibility", labelListVisibility)):
        if v is not None:
            l[k] = v
    return _label_text("Label updated successfully:", l)


@tool("delete_label", destructive=True, idempotent=True)
def delete_label(ctx: Instance, id: Annotated[str, "ID of the label to delete"]) -> str:
    """Deletes a Gmail label"""
    s = _mb(ctx)
    l = s["labels"].get(id)
    if l is None:
        raise _not_found()
    if l["type"] == "system":
        raise _invalid("Invalid delete request: system labels can't be deleted")
    del s["labels"][id]
    for m in s["messages"].values():
        m["labelIds"] = [x for x in m["labelIds"] if x != id]
    return f'Label "{l["name"]}" (ID: {id}) deleted successfully.'


@tool("get_or_create_label", idempotent=True)
def get_or_create_label(ctx: Instance,
                        name: Annotated[str, "Name of the label to get or create"],
                        messageListVisibility: Annotated[Literal["show", "hide"] | None, "Whether to show or hide the label in the message list"] = None,
                        labelListVisibility: Annotated[Literal["labelShow", "labelShowIfUnread", "labelHide"] | None, "Visibility of the label in the label list"] = None) -> str:
    """Gets an existing label by name or creates it if it doesn't exist"""
    s = _mb(ctx)
    existing = _label_by_name(s, name)
    if existing:
        return _label_text("Successfully found existing label:", s["labels"][existing])
    return _label_text("Successfully created new label:", _new_label(ctx, s, name, messageListVisibility or "show",
                                                                     labelListVisibility or "labelShow"))


def _filter_text(f: dict[str, Any]) -> str:
    crit = ", ".join(f"{k}: {v}" for k, v in f["criteria"].items())
    act = ", ".join(f"{k}: {v}" for k, v in f["action"].items())
    return f"ID: {f['id']}\nCriteria: {crit}\nActions: {act}\n"


@tool("create_filter")
def create_filter(ctx: Instance,
                  criteria: Annotated[dict, "Criteria for matching emails (from, to, subject, query, negatedQuery, hasAttachment, size, sizeComparison)"],
                  action: Annotated[dict, "Actions to perform on matching emails (addLabelIds, removeLabelIds, forward)"]) -> str:
    """Creates a new Gmail filter with custom criteria and actions"""
    s = _mb(ctx)
    if not criteria:
        raise _invalid("Filter criteria must not be empty")
    _check_labels(s, action.get("addLabelIds"))
    _check_labels(s, action.get("removeLabelIds"))
    fid = "ANe1Bm" + ctx.token(20, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
    s["filters"][fid] = {"id": fid, "criteria": dict(criteria), "action": dict(action)}
    return "Filter created successfully:\n" + _filter_text(s["filters"][fid])


@tool("list_filters", read_only=True)
def list_filters(ctx: Instance) -> str:
    """Retrieves all Gmail filters"""
    fs = list(_mb(ctx)["filters"].values())
    if not fs:
        return "No filters found."
    return f"Found {len(fs)} filters:\n\n" + "\n".join(_filter_text(f) for f in fs)


@tool("get_filter", read_only=True)
def get_filter(ctx: Instance, filterId: Annotated[str, "ID of the filter to retrieve"]) -> str:
    """Gets details of a specific Gmail filter"""
    f = _mb(ctx)["filters"].get(filterId)
    if f is None:
        raise _not_found()
    return "Filter details:\n" + _filter_text(f)


@tool("delete_filter", destructive=True, idempotent=True)
def delete_filter(ctx: Instance, filterId: Annotated[str, "ID of the filter to delete"]) -> str:
    """Deletes a Gmail filter"""
    if _mb(ctx)["filters"].pop(filterId, None) is None:
        raise _not_found()
    return f"Filter {filterId} deleted successfully."


@tool("create_filter_from_template")
def create_filter_from_template(ctx: Instance,
                                template: Annotated[Literal["fromSender", "withSubject", "withAttachments", "largeEmails", "containingText", "mailingList"], "Pre-defined filter template to use"],
                                parameters: Annotated[dict, "Template-specific parameters (senderEmail, subjectText, searchText, listIdentifier, sizeInBytes, labelIds, archive, markAsRead)"]) -> str:
    """Creates a filter using a pre-defined template for common scenarios"""
    p = parameters
    criteria = {
        "fromSender": lambda: {"from": p.get("senderEmail")},
        "withSubject": lambda: {"subject": p.get("subjectText")},
        "withAttachments": lambda: {"hasAttachment": True},
        "largeEmails": lambda: {"size": p.get("sizeInBytes"), "sizeComparison": "larger"},
        "containingText": lambda: {"query": p.get("searchText")},
        "mailingList": lambda: {"query": f"list:{p.get('listIdentifier')}"},
    }[template]()
    if any(v is None for v in criteria.values()):
        raise _invalid(f"Missing required parameter for template {template}")
    action: dict[str, Any] = {}
    if p.get("labelIds"):
        action["addLabelIds"] = p["labelIds"]
    remove = (["INBOX"] if p.get("archive") else []) + (["UNREAD"] if p.get("markAsRead") else [])
    if remove:
        action["removeLabelIds"] = remove
    return create_filter.fn(ctx, criteria=criteria, action=action)
