"""Serve REST requests for real API hosts from an environment's worlds.

A request arrives as (target host, method, path, query, headers, body): from the HTTPS gateway,
or directly at ``/gw/<host>/<path>`` on the myworld host. The bearer token says which run and
agent it belongs to (``tokens``); the host and path pick the operation; the agent's server for
that service runs it like any other call.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

from ..core.faults import TransportFault
from ..core.instance import Instance
from . import Response, resolve
from . import google as g

GOOGLE_SUFFIX = ".googleapis.com"
GITHUB_HOSTS = {"api.github.com", "uploads.github.com"}
SERVICES_BY_HOST = {  # which simulated services can answer on a host
    "gmail.googleapis.com": {"gmail"}, "calendar-json.googleapis.com": {"calendar"},
    "sheets.googleapis.com": {"drive"}, "docs.googleapis.com": {"drive"}, "drive.googleapis.com": {"drive"},
    "people.googleapis.com": {"gmail"},
    "www.googleapis.com": {"gmail", "calendar", "drive"},
    "api.github.com": {"github"}, "uploads.github.com": {"github"},
}
SCOPES = ("https://mail.google.com/ https://www.googleapis.com/auth/calendar https://www.googleapis.com/auth/drive "
          "https://www.googleapis.com/auth/spreadsheets https://www.googleapis.com/auth/documents "
          "https://www.googleapis.com/auth/contacts openid https://www.googleapis.com/auth/userinfo.email")


# -- tokens ---------------------------------------------------------------------------------------

@dataclass
class Grant:
    run: str | None       # environment run id
    instance: str | None  # or a standalone instance id
    agent: str | None


def mint(secret: str, *, run: str | None = None, instance: str | None = None, agent: str | None = None,
         kind: str = "google") -> str:
    """A bearer token for one agent in one run (Google-looking or GitHub-looking)."""
    claims = base64.urlsafe_b64encode(json.dumps({"r": run, "i": instance, "a": agent},
                                                 separators=(",", ":")).encode()).decode().rstrip("=")
    sig = base64.urlsafe_b64encode(hmac.new(secret.encode(), claims.encode(), hashlib.sha256).digest()[:16]
                                   ).decode().rstrip("=")
    return ("gho_" if kind == "github" else "ya29.a0") + claims + "." + sig


def verify(secret: str, token: str) -> Grant | None:
    token = token.strip()
    for prefix in ("ya29.a0", "gho_", "ghp_", "github_pat_"):
        if token.startswith(prefix):
            token = token[len(prefix):]
            break
    else:
        return None
    claims, _, sig = token.rpartition(".")
    want = base64.urlsafe_b64encode(hmac.new(secret.encode(), claims.encode(), hashlib.sha256).digest()[:16]
                                    ).decode().rstrip("=")
    if not claims or not hmac.compare_digest(sig, want):
        return None
    try:
        c = json.loads(base64.urlsafe_b64decode(claims + "=" * (-len(claims) % 4)))
    except ValueError:
        return None
    return Grant(c.get("r"), c.get("i"), c.get("a"))


# -- the request -------------------------------------------------------------------------------

@dataclass
class HttpResult:
    status: int
    body: bytes
    headers: dict[str, str]


def _json(status: int, value: Any, extra: dict[str, str] | None = None, pretty: bool = True) -> HttpResult:
    text = json.dumps(value, indent=2) + "\n" if pretty else json.dumps(value, separators=(",", ":"))
    return HttpResult(status, text.encode(),
                      {"content-type": "application/json; charset=UTF-8", **(extra or {})})


def _is_google(host: str) -> bool:
    return host.endswith(GOOGLE_SUFFIX)


def _error(host: str, status: int, message: str) -> HttpResult:
    if _is_google(host):
        return _json(status, g.error(status, message).payload)
    return _json(status, {"message": message, "documentation_url": "https://docs.github.com/rest",
                          "status": str(status)})


def handle(host_obj: Any, target: str, method: str, path: str, query: str, headers: dict[str, str],
           body: bytes) -> HttpResult:
    target = target.lower().split(":")[0]
    headers = {k.lower(): v for k, v in headers.items()}
    q = urllib.parse.parse_qs(query, keep_blank_values=True)
    secret = host_obj.gateway_secret
    if target == "oauth2.googleapis.com" or path.startswith(("/oauth2/", "/v1/userinfo")) and _is_google(target):
        return _oauth(host_obj, secret, target, method, path, q, headers, body)
    auth = headers.get("authorization", "")
    token = auth.split(None, 1)[1] if " " in auth else q.get("access_token", [""])[-1]
    grant = verify(secret, token) if token else None
    if grant is None:
        if target in GITHUB_HOSTS:
            return _json(401, {"message": "Bad credentials", "documentation_url": "https://docs.github.com/rest",
                               "status": "401"})
        return _json(401, {"error": {"code": 401, "message": "Request had invalid authentication credentials. "
                                     "Expected OAuth 2 access token, login cookie or other valid authentication "
                                     "credential. See https://developers.google.com/identity/sign-in/web/devconsole-project.",
                                     "errors": [{"message": "Invalid Credentials", "domain": "global",
                                                 "reason": "authError", "location": "Authorization",
                                                 "locationType": "header"}], "status": "UNAUTHENTICATED"}},
                     {"www-authenticate": 'Bearer realm="https://accounts.google.com/"'})
    servers = _servers(host_obj, grant)
    if servers is None:
        return _error(target, 401, "Bad credentials" if target in GITHUB_HOSTS else "Invalid Credentials")
    services = SERVICES_BY_HOST.get(target, set()) & {i.service.name for i in servers.values()}
    found = resolve(target, method, path, services) if services else None
    if found is None:
        return _error(target, 404, "Not Found" if target in GITHUB_HOSTS else
                      f"The requested URL {path} was not found on this server.")
    op, params = found
    if params.pop("__method_not_allowed__", None):
        return _error(target, 405, "Method Not Allowed")
    server, inst = next((s, i) for s, i in servers.items() if i.service.name == op.service)
    parsed: Any = None
    if body:
        ctype = headers.get("content-type", "")
        if "json" in ctype or (not ctype and body[:1] in (b"{", b"[")):
            try:
                parsed = json.loads(body)
            except ValueError:
                return _error(target, 400, "Invalid JSON payload received.")
        elif ctype.startswith("multipart/related"):
            parsed = _multipart_related(ctype, body)
        else:
            parsed = {"__bytes__": base64.b64encode(body).decode()}
    args = {"method": method.upper(), "path": path, "params": params, "query": q, "body": parsed,
            "headers": {k: v for k, v in headers.items() if k in ("content-type", "if-match", "accept", "content-range",
                                                                   "x-upload-content-type", "x-upload-content-length")},
            "host": target}
    run = host_obj.envs.get(grant.run) if grant.run else None
    as_ = run.env.identity_for(grant.agent, server) if run is not None and grant.agent else None
    try:
        result = inst.call(op.id, args, agent=grant.agent, as_=as_, tool=op.tool)
    except TransportFault as e:
        return HttpResult(e.status, e.body.encode() if isinstance(e.body, str) else e.body,
                          {"content-type": "text/html; charset=UTF-8"})
    if result.is_error:
        if not _is_google(target) and result.status == 200:  # GraphQL: errors travel in a 200 (writes rolled back)
            return _json(200, result.data, _github_headers(inst))
        status = result.status if result.status >= 400 else 400
        if _is_google(target):
            return _json(status, g.body(result.data if result.data is not None else result.text, status, target))
        data = dict(result.data) if isinstance(result.data, dict) else {"message": result.text}
        data.setdefault("documentation_url", "https://docs.github.com/rest")
        data.setdefault("status", str(status))
        return _json(status, data, _github_headers(inst))
    value = result.data
    pretty = q.get("prettyPrint", ["true"])[-1].lower() != "false"
    extra = _github_headers(inst) if target in GITHUB_HOSTS else {}
    if isinstance(value, Response):
        headers = {**extra, **value.headers}
        if value.body is None:
            return HttpResult(value.status, b"", headers)
        if isinstance(value.body, (bytes, bytearray)):
            return HttpResult(value.status, bytes(value.body), headers)
        return _json(value.status, value.body, headers, pretty)
    return _json(200, value, extra, pretty=pretty)


def _github_headers(inst: Instance) -> dict[str, str]:
    used = len(inst.calls)
    reset = int(inst.clock.timestamp()) + 3600
    return {"x-github-api-version-selected": "2022-11-28", "x-ratelimit-limit": "5000",
            "x-ratelimit-remaining": str(max(0, 5000 - used)), "x-ratelimit-used": str(used),
            "x-ratelimit-reset": str(reset), "x-ratelimit-resource": "core",
            "x-oauth-scopes": "gist, read:org, repo, workflow", "x-accepted-oauth-scopes": "repo",
            "x-github-media-type": "github.v3; format=json",
            "x-github-request-id": hashlib.sha1(f"{used}".encode()).hexdigest()[:8].upper() + ":1A2B:3C4D"}


def _multipart_related(ctype: str, body: bytes) -> Any:
    """``uploadType=multipart``: JSON metadata then the media, as Google's clients send uploads."""
    boundary = ctype.split("boundary=", 1)[-1].strip('"')
    parts = [p for p in body.split(b"--" + boundary.encode()) if p.strip() not in (b"", b"--")]
    meta: dict[str, Any] = {}
    media = b""
    for i, p in enumerate(parts):
        head, _, content = p.lstrip(b"\r\n").partition(b"\r\n\r\n")
        content = content.rstrip(b"\r\n")
        if i == 0 and b"json" in head.lower():
            try:
                meta = json.loads(content or b"{}")
            except ValueError:
                meta = {}
        else:
            media = content
            if b"content-transfer-encoding: base64" in head.lower():
                media = base64.b64decode(media)
    media_type = "application/octet-stream"
    for i, p in enumerate(parts):
        head = p.lstrip(b"\r\n").partition(b"\r\n\r\n")[0].decode(errors="replace").lower()
        if i > 0 and "content-type:" in head:
            media_type = head.split("content-type:", 1)[1].split("\r\n")[0].strip()
    return {**meta, "__media__": base64.b64encode(media).decode(), "__media_type__": media_type}


def _servers(host_obj: Any, grant: Grant) -> dict[str, Instance] | None:
    if grant.run:
        run = host_obj.envs.get(grant.run)
        if run is None:
            return None
        names = run.env.agent_servers(grant.agent) if grant.agent else list(run.instances)
        return {s: run.instances[s] for s in names}
    if grant.instance:
        inst = host_obj.instances.get(grant.instance)
        return {grant.instance: inst} if inst is not None else None
    return None


# -- OAuth ---------------------------------------------------------------------------------------

def _identity(host_obj: Any, grant: Grant) -> tuple[str, str]:
    servers = _servers(host_obj, grant) or {}
    for s, inst in servers.items():
        if inst.service.name in ("gmail", "calendar", "drive"):
            run = host_obj.envs.get(grant.run) if grant.run else None
            who = run.env.identity_for(grant.agent, s) if run is not None and grant.agent else None
            email = inst.resolve_actor(who) if who else inst.service.default_actor(inst.state)
            if inst.service.name == "gmail":
                box = inst.state["mailboxes"].get(email, {})
                return email, box.get("user", {}).get("name", email)
            return email, email.split("@")[0].replace(".", " ").title()
    return "user@example.com", "User"


def _oauth(host_obj: Any, secret: str, target: str, method: str, path: str, q: dict[str, list[str]],
           headers: dict[str, str], body: bytes) -> HttpResult:
    form = urllib.parse.parse_qs(body.decode(errors="replace")) if body else {}
    if "json" in headers.get("content-type", "") and body:
        form = {k: [v] for k, v in json.loads(body).items()}
    val = lambda k: (form.get(k) or q.get(k) or [""])[-1]  # noqa: E731
    if path.rstrip("/") in ("/token", "/oauth2/v4/token"):
        grant_type = val("grant_type")
        token = val("refresh_token") if grant_type == "refresh_token" else val("code")
        if grant_type not in ("refresh_token", "authorization_code"):
            return _json(400, {"error": "unsupported_grant_type", "error_description": f"Invalid grant_type: {grant_type}"})
        grant = verify(secret, token)
        if grant is None or _servers(host_obj, grant) is None:
            return _json(400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
        out = {"access_token": token, "expires_in": 3599, "scope": SCOPES, "token_type": "Bearer"}
        if grant_type == "authorization_code":
            out["refresh_token"] = token
        return _json(200, out)
    if path.rstrip("/") == "/revoke":
        return _json(200, {})
    auth = headers.get("authorization", "")
    token = auth.split(None, 1)[1] if " " in auth else val("access_token") or val("id_token")
    grant = verify(secret, token) if token else None
    if grant is None:
        return _json(400 if "tokeninfo" in path else 401, {"error": "invalid_token",
                                                          "error_description": "Invalid Credentials"})
    email, name = _identity(host_obj, grant)
    if "tokeninfo" in path:
        exp = int(time.time()) + 3599
        return _json(200, {"azp": "myworld.apps.googleusercontent.com", "aud": "myworld.apps.googleusercontent.com",
                           "sub": str(int(hashlib.sha1(email.encode()).hexdigest(), 16))[:21], "scope": SCOPES,
                           "exp": str(exp), "expires_in": "3599", "email": email, "email_verified": "true",
                           "access_type": "offline"})
    first, _, last = name.partition(" ")
    return _json(200, {"sub": str(int(hashlib.sha1(email.encode()).hexdigest(), 16))[:21], "name": name,
                       "given_name": first, "family_name": last, "email": email, "email_verified": True,
                       "picture": "https://lh3.googleusercontent.com/a/default-user=s96-c",
                       "hd": email.split("@")[1] if not email.endswith("gmail.com") else None})
