"""Drive (v3), Sheets (v4), Docs (v1) and People (v1) REST surfaces."""

import json

import pytest
from fastapi.testclient import TestClient

from myworld.host import HostConfig, create_app

SPEC = {"name": "w", "servers": {"gmail": {}, "drive": {}},
        "agents": {"alex": {"as": "alex@acme.com"}, "john": {"as": "john@acme.com"}}}


@pytest.fixture
def api():
    c = TestClient(create_app(config=HostConfig()))
    run = c.post("/envs", json={"spec": SPEC, "id": "w"}).json()

    def as_(agent):
        h = {"Authorization": f"Bearer {run['credentials'][agent]['google_access_token']}"}
        return lambda method, host, path, **kw: c.request(method, f"/gw/{host}{path}", headers={**h, **kw.pop("headers", {})}, **kw)
    return c, as_("alex"), as_("john")


D = "www.googleapis.com"


def find(call, name):
    files = call("GET", D, "/drive/v3/files", params={"q": f"name = '{name}'", "fields": "files(id,name,mimeType)"}).json()
    return files["files"][0]["id"]


def test_drive_defaults_and_queries(api):
    _, alex, _ = api
    r = alex("GET", D, "/drive/v3/files").json()
    assert r["kind"] == "drive#fileList" and set(r["files"][0]) == {"kind", "id", "name", "mimeType"}
    assert alex("GET", D, "/drive/v3/files", params={"q": "budget"}).status_code == 400, "no bare words in q"
    fid = find(alex, "Q4 budget")
    assert set(alex("GET", D, f"/drive/v3/files/{fid}").json()) == {"kind", "id", "name", "mimeType"}
    full = alex("GET", D, f"/drive/v3/files/{fid}", params={"fields": "*"}).json()
    assert full["owners"][0]["me"] and full["capabilities"]["canEdit"] and "exportLinks" in full
    assert alex("GET", D, f"/drive/v3/files/{fid}", params={"alt": "media"}).status_code == 403
    csv = alex("GET", D, f"/drive/v3/files/{fid}/export", params={"mimeType": "text/csv"})
    assert csv.status_code == 200 and csv.text.startswith("team,")
    mine = alex("GET", D, "/drive/v3/files", params={"q": "'me' in owners and trashed = false"}).json()["files"]
    assert mine and alex("GET", D, "/drive/v3/about").status_code == 400, "about needs fields"


def test_uploads_and_sharing(api):
    c, alex, john = api
    boundary = "b0undary"
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            + json.dumps({"name": "notes.md"}) + f"\r\n--{boundary}\r\nContent-Type: text/markdown\r\n\r\n# Hi\r\n"
            f"--{boundary}--").encode()
    up = alex("POST", D, "/upload/drive/v3/files", params={"uploadType": "multipart", "fields": "id,mimeType,size"},
              content=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"}).json()
    assert up["mimeType"] == "text/markdown" and up["size"] == "4"
    start = alex("POST", D, "/upload/drive/v3/files", params={"uploadType": "resumable", "fields": "id,name,webViewLink"},
                 json={"name": "big.bin"}, headers={"X-Upload-Content-Type": "application/octet-stream"})
    loc = start.headers["location"].split("googleapis.com", 1)[1]
    part = alex("PUT", D, loc, content=b"a" * 10, headers={"Content-Range": "bytes 0-9/20"})
    assert part.status_code == 308 and part.headers["range"] == "bytes=0-9"
    done = alex("PUT", D, loc, content=b"b" * 10, headers={"Content-Range": "bytes 10-19/20"}).json()
    assert done["name"] == "big.bin" and done["webViewLink"]
    assert alex("GET", D, f"/drive/v3/files/{done['id']}", params={"alt": "media"}).content == b"a" * 10 + b"b" * 10
    fid = up["id"]
    assert john("GET", D, f"/drive/v3/files/{fid}").status_code == 404
    perm = alex("POST", D, f"/drive/v3/files/{fid}/permissions",
                json={"type": "user", "role": "reader", "emailAddress": "john@acme.com"}).json()
    assert perm["role"] == "reader" and john("GET", D, f"/drive/v3/files/{fid}").status_code == 200
    assert any(x.get("event") == "deliver_email" for x in c.get("/envs/w/timeline").json()["timeline"]), \
        "users are notified by default (sendNotificationEmail)"
    own = alex("POST", D, f"/drive/v3/files/{fid}/permissions",
               json={"type": "user", "role": "owner", "emailAddress": "john@acme.com"})
    assert own.status_code == 403 and "transferOwnership" in own.json()["error"]["message"]
    anyone = alex("POST", D, f"/drive/v3/files/{fid}/permissions", json={"type": "anyone", "role": "reader"})
    assert anyone.status_code == 403, "this company doesn't allow public links"
    domain = alex("POST", D, f"/drive/v3/files/{fid}/permissions", json={"type": "domain", "role": "reader",
                                                                          "domain": "acme.com"}).json()
    assert domain["type"] == "domain" and any(p["type"] == "domain" for p in alex(
        "GET", D, f"/drive/v3/files/{fid}/permissions").json()["permissions"])
    assert john("DELETE", D, f"/drive/v3/files/{fid}").status_code == 403
    assert alex("DELETE", D, f"/drive/v3/files/{fid}").status_code == 204


S = "sheets.googleapis.com"


def test_sheets_values(api):
    _, alex, _ = api
    fid = find(alex, "Q4 budget")
    got = alex("GET", S, f"/v4/spreadsheets/{fid}/values/Sheet1!A1:C9").json()
    assert got["range"] == "Sheet1!A1:C9" and got["values"][0] == ["team", "q3", "q4"]
    assert alex("PUT", S, f"/v4/spreadsheets/{fid}/values/Sheet1!A5", json={"values": [["x"]]}).status_code == 400, \
        "valueInputOption is required"
    up = alex("PUT", S, f"/v4/spreadsheets/{fid}/values/Sheet1!D1:D2", params={"valueInputOption": "USER_ENTERED"},
              json={"values": [["total"], ["=B2+C2"]]}).json()
    assert up["updatedCells"] == 2
    assert alex("GET", S, f"/v4/spreadsheets/{fid}/values/Sheet1!D2").json()["values"] == [["230000"]]
    assert alex("GET", S, f"/v4/spreadsheets/{fid}/values/Sheet1!D2",
                params={"valueRenderOption": "FORMULA"}).json()["values"] == [["=B2+C2"]]
    app = alex("POST", S, f"/v4/spreadsheets/{fid}/values/Sheet1!A:C:append",
               params={"valueInputOption": "RAW"}, json={"values": [["ops", "1", "2"]]}).json()
    assert app["tableRange"].startswith("Sheet1!A1") and app["updates"]["updatedRange"] == "Sheet1!A4:C4"
    assert "values" not in alex("GET", S, f"/v4/spreadsheets/{fid}/values/Sheet1!H1:H3").json()
    bu = alex("POST", S, f"/v4/spreadsheets/{fid}:batchUpdate",
              json={"requests": [{"addSheet": {"properties": {"title": "Notes"}}}, {"repeatCell": {}}]}).json()
    assert bu["replies"][0]["addSheet"]["properties"]["title"] == "Notes"
    dup = alex("POST", S, f"/v4/spreadsheets/{fid}:batchUpdate",
               json={"requests": [{"addSheet": {"properties": {"title": "notes"}}}]})
    assert dup.status_code == 400 and "errors" not in dup.json()["error"], "Sheets errors have no errors[]"


def test_docs_indexes(api):
    _, alex, _ = api
    doc = alex("POST", "docs.googleapis.com", "/v1/documents", json={"title": "Notes"}).json()
    did = doc["documentId"]
    base = f"/v1/documents/{did}:batchUpdate"
    alex("POST", "docs.googleapis.com", base, json={"requests": [
        {"insertText": {"location": {"index": 1}, "text": "Hello world"}},
        {"insertText": {"endOfSegmentLocation": {}, "text": "!"}}]})
    got = alex("GET", "docs.googleapis.com", f"/v1/documents/{did}").json()
    runs = [e["textRun"]["content"] for p in got["body"]["content"][1:] for e in p["paragraph"]["elements"]]
    assert runs == ["Hello world!\n"] and got["body"]["content"][-1]["endIndex"] == 14
    bad = alex("POST", "docs.googleapis.com", base, json={"requests": [
        {"deleteContentRange": {"range": {"startIndex": 1, "endIndex": 14}}}]})
    assert bad.status_code == 400 and "newline" in bad.json()["error"]["message"]
    r = alex("POST", "docs.googleapis.com", base, json={"requests": [
        {"replaceAllText": {"containsText": {"text": "WORLD"}, "replaceText": "there"}}]}).json()
    assert r["replies"][0]["replaceAllText"]["occurrencesChanged"] == 1
    tabs = alex("GET", "docs.googleapis.com", f"/v1/documents/{did}", params={"includeTabsContent": "true"}).json()
    assert "body" not in tabs and tabs["tabs"][0]["documentTab"]["body"]


P = "people.googleapis.com"


def test_contacts_directory_and_the_search_cache(api):
    _, alex, _ = api
    assert alex("GET", P, "/v1/people/me/connections").status_code == 400, "personFields is required"
    conns = alex("GET", P, "/v1/people/me/connections", params={"personFields": "names,emailAddresses"}).json()
    assert {c["emailAddresses"][0]["value"] for c in conns["connections"]} == {"john@acme.com", "priya@acme.com"}
    made = alex("POST", P, "/v1/people:createContact", json={"names": [{"givenName": "Dana", "familyName": "Scully"}],
                                                            "emailAddresses": [{"value": "dana@fbi.gov"}]}).json()
    q = {"query": "dana", "readMask": "names"}
    assert alex("GET", P, "/v1/people:searchContacts", params=q).json() == {}, "not in the cache yet"
    alex("GET", P, "/v1/people:searchContacts", params={"query": "", "readMask": "names"})
    assert alex("GET", P, "/v1/people:searchContacts", params=q).json()["results"][0]["person"]["names"][0][
        "displayName"] == "Dana Scully"
    rid = made["resourceName"].split("/")[1]
    upd = {"names": [{"givenName": "Dana", "familyName": "Mulder"}]}
    assert alex("PATCH", P, f"/v1/people/{rid}:updateContact", params={"updatePersonFields": "names"},
                json=upd).status_code == 400, "etag is required"
    ok = alex("PATCH", P, f"/v1/people/{rid}:updateContact", params={"updatePersonFields": "names"},
              json={**upd, "etag": made["etag"]}).json()
    assert ok["names"][0]["displayName"] == "Dana Mulder"
    stale = alex("PATCH", P, f"/v1/people/{rid}:updateContact", params={"updatePersonFields": "names"},
                 json={**upd, "etag": made["etag"]})
    assert stale.status_code == 400
    others = alex("GET", P, "/v1/otherContacts", params={"readMask": "emailAddresses"}).json()["otherContacts"]
    assert any(o["emailAddresses"][0]["value"] == "receipts@stripe.com" for o in others)
    d = alex("GET", P, "/v1/people:listDirectoryPeople", params={"readMask": "names,emailAddresses",
                                                                 "sources": "DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE"}).json()
    assert {"alex@acme.com", "john@acme.com"} <= {p["emailAddresses"][0]["value"] for p in d["people"]}
