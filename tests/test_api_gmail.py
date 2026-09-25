"""The Gmail REST API surface (what gog, Google's client libraries and curl see), and the gateway."""

import base64
import http.client
import json
import ssl
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from toolsim.host import Host, HostConfig, create_app

SPEC = {"name": "w", "servers": {"gmail": {}}, "agents": {"alex": {"as": "alex@acme.com"},
                                                         "john": {"as": "john@acme.com"}}}


@pytest.fixture
def api():
    c = TestClient(create_app(config=HostConfig()))
    run = c.post("/envs", json={"spec": SPEC, "id": "w"}).json()

    def as_(agent):
        h = {"Authorization": f"Bearer {run['credentials'][agent]['google_access_token']}"}

        def call(method, path, **kw):
            return c.request(method, f"/gw/gmail.googleapis.com/gmail/v1/users/me/{path}", headers=h, **kw)
        return call
    return c, as_("alex"), as_("john")


def raw(**headers_and_body):
    m = EmailMessage()
    body = headers_and_body.pop("body", "hi")
    html = headers_and_body.pop("html", None)
    attach = headers_and_body.pop("attach", None)
    for k, v in headers_and_body.items():
        m[k.replace("_", "-")] = v
    m.set_content(body)
    if html:
        m.add_alternative(html, subtype="html")
    if attach:
        m.add_attachment(attach[1], maintype="application", subtype="pdf", filename=attach[0])
    return base64.urlsafe_b64encode(m.as_bytes()).decode()


def test_list_get_and_formats(api):
    _, alex, _ = api
    listed = alex("GET", "messages", params={"q": "is:unread", "maxResults": 1}).json()
    assert len(listed["messages"]) == 1 and listed["nextPageToken"] and listed["resultSizeEstimate"] == 2
    more = alex("GET", "messages", params={"q": "is:unread", "maxResults": 1,
                                          "pageToken": listed["nextPageToken"]}).json()
    assert more["messages"][0]["id"] != listed["messages"][0]["id"] and "nextPageToken" not in more
    mid = listed["messages"][0]["id"]
    full = alex("GET", f"messages/{mid}").json()
    heads = {h["name"]: h["value"] for h in full["payload"]["headers"]}
    assert heads["Delivered-To"] == "alex@acme.com" and "Subject" in heads and full["historyId"].isdigit()
    data = full["payload"]["body"]["data"]
    assert base64.urlsafe_b64decode(data).decode(), "padded base64url, as Gmail sends it"
    meta = alex("GET", f"messages/{mid}", params={"format": "metadata", "metadataHeaders": ["Subject"]}).json()
    assert [h["name"] for h in meta["payload"]["headers"]] == ["Subject"]
    rawmsg = base64.urlsafe_b64decode(alex("GET", f"messages/{mid}", params={"format": "raw"}).json()["raw"])
    assert b"Subject: " in rawmsg and b"\r\n\r\n" in rawmsg
    assert "payload" not in alex("GET", f"messages/{mid}", params={"format": "minimal"}).json()
    assert alex("GET", "messages", params={"q": "from:nobody@x.io"}).json() == {"resultSizeEstimate": 0}
    assert alex("GET", "messages/nope").status_code == 404
    assert alex("GET", "messages", params={"fields": "messages(id)"}).json().keys() == {"messages"}


def test_send_with_html_and_attachment_reaches_colleague(api):
    _, alex, john = api
    sent = alex("POST", "messages/send", json={"raw": raw(To="john@acme.com", Subject="Deck", body="See attached",
                                                         html="<p>See attached</p>", attach=("deck.pdf", b"%PDF-1"))})
    assert sent.status_code == 200 and sent.json()["labelIds"] == ["SENT"]
    found = john("GET", "messages", params={"q": "subject:Deck has:attachment"}).json()["messages"]
    msg = john("GET", f"messages/{found[0]['id']}").json()
    assert msg["payload"]["mimeType"] == "multipart/mixed"
    att = next(p for p in msg["payload"]["parts"] if p["filename"] == "deck.pdf")
    data = john("GET", f"messages/{msg['id']}/attachments/{att['body']['attachmentId']}").json()["data"]
    assert base64.urlsafe_b64decode(data) == b"%PDF-1"
    assert alex("POST", "messages/send", json={"raw": raw(Subject="nobody")}).json()["error"]["message"] == \
        "Recipient address required"
    assert alex("POST", "messages/send", json={}).status_code == 400


def test_threading_needs_thread_id_and_matching_subject(api):
    _, alex, _ = api
    tid = alex("GET", "threads", params={"q": "subject:Q4"}).json()["threads"][0]["id"]
    same = alex("POST", "messages/send", json={"raw": raw(To="john@acme.com", Subject="Re: Q4 planning"),
                                               "threadId": tid}).json()
    other = alex("POST", "messages/send", json={"raw": raw(To="john@acme.com", Subject="Totally different"),
                                                "threadId": tid}).json()
    assert same["threadId"] == tid and other["threadId"] != tid
    assert len(alex("GET", f"threads/{tid}").json()["messages"]) == 2


def test_drafts_labels_history_and_trash(api):
    _, alex, john = api
    d = alex("POST", "drafts", json={"message": {"raw": raw(To="john@acme.com", Subject="Later")}}).json()
    assert d["id"].startswith("r") and "DRAFT" in d["message"]["labelIds"]
    start = alex("GET", "profile").json()["historyId"]
    sent = alex("POST", "drafts/send", json={"id": d["id"]}).json()
    assert sent["labelIds"] == ["SENT"] and alex("GET", f"drafts/{d['id']}").status_code == 404
    assert john("GET", "messages", params={"q": "subject:Later"}).json()["resultSizeEstimate"] == 1
    hist = alex("GET", "history", params={"startHistoryId": start, "historyTypes": "messageAdded"}).json()
    assert any(r["messagesAdded"][0]["message"]["id"] == sent["id"] for r in hist["history"])
    lab = alex("POST", "labels", json={"name": "Clients-2"}).json()
    assert lab["id"].startswith("Label_") and alex("POST", "labels", json={"name": "Clients-2"}).status_code == 409
    assert alex("DELETE", "labels/INBOX").status_code == 400
    mid = alex("GET", "messages", params={"q": "subject:Q4"}).json()["messages"][0]["id"]
    labels = alex("GET", f"messages/{mid}").json()["labelIds"]
    assert "TRASH" in alex("POST", f"messages/{mid}/trash").json()["labelIds"]
    assert alex("GET", "messages", params={"q": "subject:Q4"}).json()["resultSizeEstimate"] == 0
    assert alex("POST", f"messages/{mid}/untrash").json()["labelIds"] == labels
    assert alex("POST", f"messages/{mid}/modify", json={"addLabelIds": ["Label_999"]}).status_code == 400


def test_calls_are_recorded_and_faults_apply(api):
    c, alex, _ = api
    alex("GET", "labels")
    assert c.get("/envs/w/calls").json()["calls"][-1]["tool"] == "gmail.users.labels.list"
    c2 = TestClient(create_app(config=HostConfig()))
    run = c2.post("/envs", json={"spec": {**SPEC, "faults": [{"server": "gmail", "kind": "rate_limit",
                                                             "tool": "gmail.users.messages.*"}]}}).json()
    h = {"Authorization": f"Bearer {run['credentials']['alex']['google_access_token']}"}
    r = c2.get("/gw/gmail.googleapis.com/gmail/v1/users/me/messages", headers=h)
    assert r.status_code == 429 and r.json()["error"]["status"] == "RESOURCE_EXHAUSTED"
    assert c.get("/gw/gmail.googleapis.com/gmail/v1/users/me/profile",
                 headers={"Authorization": "Bearer ya29.forged"}).status_code == 401
    assert alex("GET", "../../../../gmail/v1/users/john@acme.com/profile").status_code in (403, 404)


def test_oauth_refresh_and_userinfo(api):
    c, _, _ = api
    run = c.get("/envs/w").json()
    tok = run["credentials"]["john"]["google_access_token"]
    r = c.post("/gw/oauth2.googleapis.com/token", data={"grant_type": "refresh_token", "refresh_token": tok})
    assert r.json()["access_token"] == tok and r.json()["expires_in"] == 3599
    bad = c.post("/gw/oauth2.googleapis.com/token", data={"grant_type": "refresh_token", "refresh_token": "x"})
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"
    me = c.get("/gw/www.googleapis.com/oauth2/v3/userinfo", headers={"Authorization": f"Bearer {tok}"}).json()
    assert me["email"] == "john@acme.com" and me["name"] == "John Park"


def test_the_gateway_speaks_real_tls(tmp_path):
    host = Host(HostConfig(gateway_port=0, ca_dir=tmp_path, gateway_passthrough=False))
    try:
        from toolsim.env import Environment
        run = host.start_env(Environment.from_dict(SPEC), "w")
        tok = host.credentials("http://x", run=run, agent="alex")["google_access_token"]
        ctx = ssl.create_default_context(cafile=str(host.gateway.ca.cert_path))
        conn = http.client.HTTPSConnection("127.0.0.1", host.gateway.port, context=ctx, timeout=10)
        conn.set_tunnel("gmail.googleapis.com", 443)
        conn.request("GET", "/gmail/v1/users/me/profile", headers={"Authorization": f"Bearer {tok}"})
        r = conn.getresponse()
        assert r.status == 200 and json.loads(r.read())["emailAddress"] == "alex@acme.com"
        conn.request("GET", "/gmail/v1/users/me/labels", headers={"Authorization": f"Bearer {tok}"})
        assert conn.getresponse().status == 200, "keep-alive: a second request on the same connection"
        blocked = http.client.HTTPSConnection("127.0.0.1", host.gateway.port, context=ctx, timeout=10)
        blocked.set_tunnel("example.com", 443)
        with pytest.raises(OSError):
            blocked.request("GET", "/")
    finally:
        host.gateway.stop()


def test_gmail_search_language(api):
    _, alex, _ = api
    count = lambda q: alex("GET", "messages", params={"q": q}).json()["resultSizeEstimate"]  # noqa: E731
    assert count("from:(john OR priya)") == count("from:john") + count("from:priya") > 0
    assert count("{from:john from:priya}") == count("from:(john OR priya)")
    assert count("plan") != count("plann"), "whole words: 'plann' matches nothing"
    assert count("plann") == 0
    assert count('subject:"launch checklist"') == 2 and count("subject:(launch checklist)") == 2
    assert count("has:attachment larger:10K") == 1 and count("has:attachment larger:10M") == 0
    assert count("category:primary") < count("in:inbox")
    assert count("after:2026/09/19 before:2026/09/20") == 2, "Pacific days: 23:00 PT on the 19th is the 20th in UTC"
    assert count("-in:inbox") == count("in:sent") + count("in:drafts") - count("in:sent in:inbox")
    rid = alex("GET", "messages", params={"q": "from:john", "format": "raw"}).json()["messages"][0]["id"]
    mid = next(h["value"] for h in alex("GET", f"messages/{rid}").json()["payload"]["headers"] if h["name"] == "Message-ID")
    assert count(f"rfc822msgid:{mid}") == 1
