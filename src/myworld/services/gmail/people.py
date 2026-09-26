"""gmail: Google Contacts (the People API, v1) for each mailbox.

Three kinds of people, as in Google Workspace:
  - **My Contacts** (``people/me/connections``): saved contacts. Seed them with ``contacts:`` in a
    mailbox; by default they are the colleagues you've corresponded with.
  - **Other contacts** (``otherContacts``): everyone else you've exchanged mail with.
  - **The directory** (``people:listDirectoryPeople``): everyone at the company.

``searchContacts`` serves from a cache that Google says to warm with an empty query: contacts
created since the last warm-up don't show up in searches until the next one.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from ...api import Request, Response, operation
from ...api.google import error, page
from ...core.instance import Instance
from .model import _addr

HOSTS = ("people.googleapis.com",)
FIELDS = ("names", "emailAddresses", "phoneNumbers", "organizations", "biographies", "addresses", "birthdays",
          "urls", "photos", "memberships", "metadata", "nicknames", "relations", "userDefined", "occupations",
          "locales", "externalIds", "clientData", "calendarUrls", "coverPhotos", "events", "genders", "imClients",
          "interests", "locations", "miscKeywords", "sipAddresses", "skills", "ageRanges")


def _hex(s: str, n: int = 16) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:n]


def _box(ctx: Instance) -> dict[str, Any]:
    return ctx.state["mailboxes"][ctx.actor]


def _name_of(ctx: Instance, email: str) -> str:
    box = ctx.state["mailboxes"].get(email)
    if box:
        return box["user"].get("name", email)
    return ctx.state.get("_names", {}).get(email) or email.split("@")[0].replace(".", " ").title()


def _phone(email: str) -> str:
    n = int(_hex(email, 8), 16) % 10000
    return f"+1 415-555-{n:04d}"


def _correspondents(ctx: Instance, box: dict[str, Any]) -> list[str]:
    me = box["user"]["email"].lower()
    seen: dict[str, int] = {}
    for m in box["messages"].values():
        for a in [m["from"], *m["to"], *m["cc"]]:
            e = _addr(a)
            if e and e != me and "@" in e and not re.search(r"(no-?reply|mailer-daemon|notifications?)@", e):
                seen[e] = seen.get(e, 0) + 1
    return sorted(seen, key=lambda e: -seen[e])


def _contacts(ctx: Instance) -> dict[str, dict[str, Any]]:
    """This mailbox's saved contacts, made on first use from the seed or its colleagues."""
    box = _box(ctx)
    if "contacts" not in box:
        domain = box["user"]["email"].split("@")[1]
        seeded = box.get("_seed_contacts")
        people = seeded if seeded is not None else [
            {"name": _name_of(ctx, e), "email": e, "phone": _phone(e), "organization": domain.split(".")[0].title()}
            for e in _correspondents(ctx, box) if e.endswith("@" + domain)]
        box["contacts"] = {}
        for p in people:
            _save(box, p)
        box["_contacts_cache"] = sorted(box["contacts"])
    return box["contacts"]


def _save(box: dict[str, Any], p: dict[str, Any], rid: str | None = None) -> dict[str, Any]:
    email = (p.get("email") or "").lower()
    rid = rid or f"people/c{int(_hex((email or p.get('name', '')) + str(len(box.get('contacts', {}))), 15), 16)}"
    rec = {"resourceName": rid, "name": p.get("name", ""), "emails": [email] if email else list(p.get("emails", [])),
           "phones": [p["phone"]] if p.get("phone") else list(p.get("phones", [])),
           "organization": p.get("organization"), "title": p.get("title"), "version": 1}
    box.setdefault("contacts", {})[rid] = rec
    return rec


def _source(kind: str, key: str) -> dict[str, Any]:
    return {"primary": True, "source": {"type": kind, "id": _hex(key)}}


def _person(rec: dict[str, Any], mask: set[str], kind: str = "CONTACT") -> dict[str, Any]:
    key = rec["resourceName"]
    out: dict[str, Any] = {"resourceName": key, "etag": "%Eg" + _hex(f"{key}:{rec.get('version', 1)}", 20)}
    first, _, last = rec["name"].partition(" ")
    if "names" in mask and rec["name"]:
        out["names"] = [{"metadata": _source(kind, key), "displayName": rec["name"], "familyName": last or None,
                         "givenName": first, "displayNameLastFirst": f"{last}, {first}" if last else first,
                         "unstructuredName": rec["name"]}]
        out["names"][0] = {k: v for k, v in out["names"][0].items() if v is not None}
    if "emailAddresses" in mask and rec["emails"]:
        out["emailAddresses"] = [{"metadata": {**_source(kind, key), "primary": i == 0}, "value": e,
                                  **({"type": "work", "formattedType": "Work"} if kind == "CONTACT" else {})}
                                 for i, e in enumerate(rec["emails"])]
    if "phoneNumbers" in mask and rec["phones"]:
        out["phoneNumbers"] = [{"metadata": {**_source(kind, key), "primary": i == 0}, "value": ph,
                                "canonicalForm": "+" + re.sub(r"\D", "", ph), "type": "mobile",
                                "formattedType": "Mobile"} for i, ph in enumerate(rec["phones"])]
    if "organizations" in mask and rec.get("organization"):
        out["organizations"] = [{"metadata": _source(kind, key), "name": rec["organization"],
                                 **({"title": rec["title"]} if rec.get("title") else {})}]
    if "memberships" in mask and kind == "CONTACT":
        out["memberships"] = [{"metadata": {"source": {"type": "CONTACT", "id": _hex(key)}},
                               "contactGroupMembership": {"contactGroupId": "myContacts",
                                                          "contactGroupResourceName": "contactGroups/myContacts"}}]
    return out


def _mask(req: Request, param: str) -> set[str]:
    raw = req.arg(param) or req.arg("personFields") or req.arg("readMask") or ""
    fields = {f.strip().removeprefix("person.") for f in raw.split(",") if f.strip()}
    if not fields:
        raise error(400, f"{'personFields' if param == 'personFields' else 'readMask'} mask is required. Please specify "
                         "one or more valid paths. Valid paths are documented at "
                         "https://developers.google.com/people/api/rest/v1/people/get.")
    bad = fields - set(FIELDS)
    if bad:
        raise error(400, f"Invalid {param} mask path: \"{sorted(bad)[0]}\". Valid paths are documented at "
                         "https://developers.google.com/people/api/rest/v1/people/get.")
    return fields


def _matches(rec: dict[str, Any], q: str) -> bool:
    """Prefix matching on words of names and on email addresses, like Google's search."""
    q = q.lower().strip()
    words = rec["name"].lower().split() + [e.lower() for e in rec["emails"]] + \
        [e.split("@")[0].lower() for e in rec["emails"]] + [p.replace(" ", "") for p in rec["phones"]]
    return any(w.startswith(q) for w in words) or rec["name"].lower().startswith(q)


# -- my contacts ----------------------------------------------------------------------------------

@operation("people.people.connections.list", "GET", "/v1/people/{resource}/connections", hosts=HOSTS, read_only=True)
def connections_list(ctx: Instance, req: Request) -> Any:
    if req.params["resource"] != "me":
        raise error(400, "Only 'people/me' is valid.")
    mask = _mask(req, "personFields")
    recs = list(_contacts(ctx).values())
    order = req.arg("sortOrder") or "LAST_MODIFIED_ASCENDING"
    if order in ("FIRST_NAME_ASCENDING", "LAST_NAME_ASCENDING"):
        idx = 0 if order == "FIRST_NAME_ASCENDING" else -1
        recs.sort(key=lambda r: (r["name"].split() or [""])[idx].lower())
    chunk, token = page(recs, req, size="pageSize", default=100, maximum=1000)
    out: dict[str, Any] = {}
    if chunk:
        out["connections"] = [_person(r, mask) for r in chunk]
    if token:
        out["nextPageToken"] = token
    out.update(totalPeople=len(recs), totalItems=len(recs))
    return out


@operation("people.people.searchContacts", "GET", "/v1/people:searchContacts", hosts=HOSTS, read_only=True)
def search_contacts(ctx: Instance, req: Request) -> Any:
    mask = _mask(req, "readMask")
    box = _box(ctx)
    contacts = _contacts(ctx)
    q = req.arg("query")
    if q is None:
        raise error(400, "query is required")
    if not q.strip():  # the warm-up request: refreshes the search cache
        box["_contacts_cache"] = sorted(contacts)
        return {}
    size = min(req.int_arg("pageSize", 10), 30)
    hits = [contacts[r] for r in box.get("_contacts_cache", []) if r in contacts and _matches(contacts[r], q)][:size]
    return {"results": [{"person": _person(r, mask)} for r in hits]} if hits else {}


@operation("people.people.get", "GET", "/v1/people/{resource}", hosts=HOSTS, read_only=True)
def people_get(ctx: Instance, req: Request) -> Any:
    mask = _mask(req, "personFields")
    rid = req.params["resource"]
    if rid == "me":
        box = _box(ctx)
        me = {"resourceName": "people/" + str(int(_hex(box["user"]["email"], 15), 16)), "name": box["user"].get("name", ""),
              "emails": [box["user"]["email"]], "phones": [], "organization": None, "title": None}
        return _person(me, mask, "PROFILE")
    rec = _contacts(ctx).get("people/" + rid)
    if rec is None:
        raise error(404, "Requested entity was not found.")
    return _person(rec, mask)


def _body_person(b: dict[str, Any]) -> dict[str, Any]:
    names = b.get("names") or [{}]
    n = names[0]
    name = n.get("unstructuredName") or " ".join(x for x in (n.get("givenName"), n.get("familyName")) if x)
    orgs = b.get("organizations") or [{}]
    return {"name": name, "emails": [e["value"].lower() for e in b.get("emailAddresses") or [] if e.get("value")],
            "phones": [p["value"] for p in b.get("phoneNumbers") or [] if p.get("value")],
            "organization": orgs[0].get("name"), "title": orgs[0].get("title")}


@operation("people.people.createContact", "POST", "/v1/people:createContact", hosts=HOSTS)
def create_contact(ctx: Instance, req: Request) -> Any:
    box = _box(ctx)
    _contacts(ctx)
    p = _body_person(req.json())
    if not p["name"] and not p["emails"] and not p["phones"]:
        raise error(400, "Request person must not be empty.")
    rec = _save(box, {"name": p["name"], "emails": p["emails"], "phones": p["phones"],
                      "organization": p["organization"], "title": p["title"]})
    return _person(rec, _mask(req, "personFields") if req.arg("personFields") else {"names", "emailAddresses",
                                                                                     "phoneNumbers", "organizations"})


@operation("people.people.updateContact", "PATCH", "/v1/people/{resource}:updateContact", hosts=HOSTS)
def update_contact(ctx: Instance, req: Request) -> Any:
    contacts = _contacts(ctx)
    rec = contacts.get("people/" + req.params["resource"])
    if rec is None:
        raise error(404, "Requested entity was not found.")
    b = req.json()
    etag = b.get("etag") or next((s.get("etag") for s in (b.get("metadata") or {}).get("sources") or []), None)
    if not etag:
        raise error(400, "Request must set person.etag or person.metadata.sources.etag for the source that is being "
                         "updated.")
    if etag != _person(rec, set())["etag"]:
        raise error(400, "Request person.etag is different than the current person.etag. Clear local cache and get "
                         "the latest person.", status="FAILED_PRECONDITION")
    fields = {f.strip() for f in (req.arg("updatePersonFields") or "").split(",") if f.strip()}
    if not fields:
        raise error(400, "updatePersonFields mask is required. Please specify one or more valid paths.")
    p = _body_person(b)
    if "names" in fields:
        rec["name"] = p["name"]
    if "emailAddresses" in fields:
        rec["emails"] = p["emails"]
    if "phoneNumbers" in fields:
        rec["phones"] = p["phones"]
    if "organizations" in fields:
        rec["organization"], rec["title"] = p["organization"], p["title"]
    rec["version"] = rec.get("version", 1) + 1
    return _person(rec, fields | {"names", "emailAddresses"})


@operation("people.people.deleteContact", "DELETE", "/v1/people/{resource}:deleteContact", hosts=HOSTS,
           destructive=True)
def delete_contact(ctx: Instance, req: Request) -> Any:
    contacts = _contacts(ctx)
    if contacts.pop("people/" + req.params["resource"], None) is None:
        raise error(404, "Requested entity was not found.")
    return Response(200, {})


@operation("people.contactGroups.list", "GET", "/v1/contactGroups", hosts=HOSTS, read_only=True)
def contact_groups(ctx: Instance, req: Request) -> Any:
    n = len(_contacts(ctx))
    groups = [{"resourceName": f"contactGroups/{g}", "etag": "%E" + _hex(g, 10), "groupType": "SYSTEM_CONTACT_GROUP",
               "name": g, "formattedName": label, **({"memberCount": n} if g == "myContacts" else {})}
              for g, label in (("myContacts", "My Contacts"), ("starred", "Starred"), ("friends", "Friends"),
                               ("family", "Family"), ("coworkers", "Coworkers"))]
    return {"contactGroups": groups, "totalItems": len(groups), "nextSyncToken": "EJ" + _hex(str(n), 12)}


# -- other contacts and the directory --------------------------------------------------------------

def _others(ctx: Instance) -> list[dict[str, Any]]:
    saved = {e for r in _contacts(ctx).values() for e in r["emails"]}
    return [{"resourceName": f"otherContacts/c{int(_hex(e, 15), 16)}", "name": _name_of(ctx, e) if
             ctx.state.get("_names", {}).get(e) or e in ctx.state["mailboxes"] else "", "emails": [e], "phones": []}
            for e in _correspondents(ctx, _box(ctx)) if e not in saved]


@operation("people.otherContacts.list", "GET", "/v1/otherContacts", hosts=HOSTS, read_only=True)
def other_contacts_list(ctx: Instance, req: Request) -> Any:
    mask = _mask(req, "readMask")
    if mask - {"names", "emailAddresses", "phoneNumbers", "metadata", "photos"}:
        raise error(400, "readMask includes fields not supported for other contacts.")
    recs = _others(ctx)
    chunk, token = page(recs, req, size="pageSize", default=100, maximum=1000)
    out: dict[str, Any] = {"otherContacts": [_person(r, mask, "OTHER_CONTACT") for r in chunk]} if chunk else {}
    if token:
        out["nextPageToken"] = token
    out["totalSize"] = len(recs)
    return out


@operation("people.otherContacts.search", "GET", "/v1/otherContacts:search", hosts=HOSTS, read_only=True)
def other_contacts_search(ctx: Instance, req: Request) -> Any:
    mask = _mask(req, "readMask")
    q = req.arg("query") or ""
    if not q.strip():
        return {}
    hits = [r for r in _others(ctx) if _matches(r, q)][: min(req.int_arg("pageSize", 10), 30)]
    return {"results": [{"person": _person(r, mask, "OTHER_CONTACT")} for r in hits]} if hits else {}


def _directory(ctx: Instance) -> list[dict[str, Any]]:
    domain = ctx.actor.split("@")[1]
    emails = sorted({e for e in [*ctx.state["mailboxes"], *ctx.state.get("_directory", [])] if e.endswith("@" + domain)})
    return [{"resourceName": f"people/{int(_hex(e, 15), 16)}", "name": _name_of(ctx, e), "emails": [e],
             "phones": [_phone(e)], "organization": domain.split(".")[0].title()} for e in emails]


@operation("people.people.listDirectoryPeople", "GET", "/v1/people:listDirectoryPeople", hosts=HOSTS, read_only=True)
def directory_list(ctx: Instance, req: Request) -> Any:
    mask = _mask(req, "readMask")
    if not req.args("sources"):
        raise error(400, "sources is required")
    recs = _directory(ctx)
    chunk, token = page(recs, req, size="pageSize", default=100, maximum=1000)
    out: dict[str, Any] = {"people": [_person(r, mask, "DOMAIN_PROFILE") for r in chunk]}
    if token:
        out["nextPageToken"] = token
    return out


@operation("people.people.searchDirectoryPeople", "GET", "/v1/people:searchDirectoryPeople", hosts=HOSTS,
           read_only=True)
def directory_search(ctx: Instance, req: Request) -> Any:
    mask = _mask(req, "readMask")
    if not req.args("sources"):
        raise error(400, "sources is required")
    q = req.arg("query") or ""
    hits = [r for r in _directory(ctx) if _matches(r, q)] if q.strip() else []
    return {"people": [_person(r, mask, "DOMAIN_PROFILE") for r in hits], "totalSize": len(hits)} if hits else {}
