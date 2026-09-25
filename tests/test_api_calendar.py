"""The Google Calendar REST API surface."""

import pytest
from fastapi.testclient import TestClient

from toolsim.host import HostConfig, create_app

SPEC = {"name": "w", "servers": {"gmail": {}, "calendar": {"extend": {"auto_respond": {"priya@acme.com": "accept"}}}},
        "agents": {"alex": {"as": "alex@acme.com"}, "john": {"as": "john@acme.com"}}}


@pytest.fixture
def api():
    c = TestClient(create_app(config=HostConfig()))
    run = c.post("/envs", json={"spec": SPEC, "id": "w"}).json()

    def as_(agent):
        h = {"Authorization": f"Bearer {run['credentials'][agent]['google_access_token']}"}
        return lambda method, path, **kw: c.request(method, f"/gw/www.googleapis.com/calendar/v3/{path}", headers=h, **kw)
    return c, as_("alex"), as_("john")


EVENT = {"summary": "Q4 sync", "start": {"dateTime": "2026-09-22T10:00:00-07:00"},
         "end": {"dateTime": "2026-09-22T10:30:00-07:00"}, "attendees": [{"email": "john@acme.com"}]}


def test_list_and_ordering_rules(api):
    _, alex, _ = api
    r = alex("GET", "calendars/primary/events", params={"timeMin": "2026-09-21T00:00:00Z",
                                                        "timeMax": "2026-09-26T00:00:00Z",
                                                        "singleEvents": "true", "orderBy": "startTime"}).json()
    assert r["kind"] == "calendar#events" and r["accessRole"] == "owner" and "nextSyncToken" in r
    starts = [e["start"]["dateTime"] for e in r["items"]]
    assert starts == sorted(starts) and all(e["kind"] == "calendar#event" and e["etag"] for e in r["items"])
    bad = alex("GET", "calendars/primary/events", params={"orderBy": "startTime"})
    assert bad.status_code == 400
    assert alex("GET", "calendars/primary/events", params={"timeMin": "2026-09-21T00:00:00"}).status_code == 400
    lst = alex("GET", "users/me/calendarList").json()
    assert lst["items"][0]["primary"] and lst["items"][0]["id"] == "alex@acme.com"
    assert alex("GET", "users/me/settings/timezone").json()["value"] == "America/Los_Angeles"


def test_recurring_events_are_series_unless_single_events(api):
    _, alex, _ = api
    series = {**EVENT, "summary": "Weekly", "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=4"], "attendees": []}
    ev = alex("POST", "calendars/primary/events", json=series).json()
    window = {"timeMin": "2026-09-21T00:00:00Z", "timeMax": "2026-10-31T00:00:00Z", "q": "Weekly"}
    masters = alex("GET", "calendars/primary/events", params=window).json()["items"]
    singles = alex("GET", "calendars/primary/events", params={**window, "singleEvents": "true"}).json()["items"]
    assert [e["id"] for e in masters] == [ev["id"]] and masters[0]["recurrence"]
    assert len(singles) == 4 and all(e["recurringEventId"] == ev["id"] for e in singles)
    inst = alex("GET", f"calendars/primary/events/{ev['id']}/instances").json()["items"]
    moved = alex("PATCH", f"calendars/primary/events/{inst[1]['id']}", json={"summary": "Weekly (moved)"}).json()
    assert moved["summary"] == "Weekly (moved)" and moved["recurringEventId"] == ev["id"]


def test_invites_only_email_with_send_updates_and_attendees_can_answer(api):
    c, alex, john = api
    quiet = alex("POST", "calendars/primary/events", json=EVENT).json()
    loud = alex("POST", "calendars/primary/events", params={"sendUpdates": "all"},
                json={**EVENT, "summary": "Loud"}).json()
    mails = [x.get("event") for x in c.get("/envs/w/timeline").json()["timeline"]]
    assert mails.count("deliver_email") == 1, "the REST default sends no invitations"
    theirs = john("GET", f"calendars/primary/events/{loud['id']}").json()
    assert theirs["organizer"]["email"] == "alex@acme.com" and not theirs["organizer"].get("self")
    assert john("PATCH", f"calendars/primary/events/{loud['id']}", json={"summary": "mine now"}).status_code == 403
    ok = john("PATCH", f"calendars/primary/events/{loud['id']}",
              json={"attendees": [{"email": "john@acme.com", "responseStatus": "accepted"}]})
    assert ok.status_code == 200
    assert {a["email"]: a["responseStatus"] for a in alex("GET", f"calendars/primary/events/{loud['id']}")
            .json()["attendees"]}["john@acme.com"] == "accepted"
    assert alex("DELETE", f"calendars/primary/events/{quiet['id']}").status_code == 204
    assert alex("DELETE", f"calendars/primary/events/{quiet['id']}").status_code == 410


def test_meet_links_all_day_events_and_freebusy(api):
    _, alex, _ = api
    meet = alex("POST", "calendars/primary/events", params={"conferenceDataVersion": "1"},
                json={**EVENT, "conferenceData": {"createRequest": {"requestId": "r1"}}}).json()
    assert meet["hangoutLink"].startswith("https://meet.google.com/")
    assert meet["conferenceData"]["createRequest"]["status"]["statusCode"] == "success"
    no_meet = alex("POST", "calendars/primary/events", json={**EVENT, "conferenceData": {"createRequest": {}}}).json()
    assert "hangoutLink" not in no_meet, "Meet needs conferenceDataVersion=1"
    day = alex("POST", "calendars/primary/events", json={"summary": "Offsite", "start": {"date": "2026-09-24"},
                                                         "end": {"date": "2026-09-25"}}).json()
    assert day["start"] == {"date": "2026-09-24"}
    fb = alex("POST", "freeBusy", json={"timeMin": "2026-09-22T00:00:00Z", "timeMax": "2026-09-23T00:00:00Z",
                                        "items": [{"id": "john@acme.com"}, {"id": "ghost@acme.com"}]}).json()
    assert fb["kind"] == "calendar#freeBusy" and fb["calendars"]["john@acme.com"]["busy"][0]["start"].endswith("Z")
    assert fb["calendars"]["ghost@acme.com"]["errors"][0]["reason"] == "notFound"
    assert alex("POST", "calendars/primary/events", json={"summary": "x"}).status_code == 400
