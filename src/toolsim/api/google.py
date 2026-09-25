"""What every Google API shares: error bodies, page tokens, ``fields=`` partial responses, base64url."""

from __future__ import annotations

import base64
import datetime as dt
from typing import Any

from ..core.tools import ToolError
from . import Request

STATUS = {400: "INVALID_ARGUMENT", 401: "UNAUTHENTICATED", 403: "PERMISSION_DENIED", 404: "NOT_FOUND",
          409: "ALREADY_EXISTS", 412: "FAILED_PRECONDITION", 429: "RESOURCE_EXHAUSTED", 500: "INTERNAL",
          502: "UNAVAILABLE", 503: "UNAVAILABLE", 504: "DEADLINE_EXCEEDED"}
REASON = {400: "invalidArgument", 401: "authError", 403: "forbidden", 404: "notFound", 409: "conflict",
          412: "conditionNotMet", 429: "rateLimitExceeded", 500: "backendError", 502: "backendError",
          503: "backendError", 504: "backendError"}


def error(code: int, message: str, reason: str | None = None, *, status: str | None = None,
          domain: str = "global", location: str | None = None) -> ToolError:
    err: dict[str, Any] = {"message": message, "domain": domain, "reason": reason or REASON.get(code, "error")}
    if location:
        err.update(location=location, locationType="parameter")
    return ToolError({"error": {"code": code, "message": message, "errors": [err],
                                "status": status or STATUS.get(code, "UNKNOWN")}}, status=code)


def body(payload: Any, status: int) -> dict[str, Any]:
    """Any service error payload in Google's shape (``{"error": {code, message, errors, status}}``)."""
    e = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(e, dict) and "code" in e and "message" in e:
        out = {"code": e["code"], "message": e["message"]}
        out["errors"] = e.get("errors") or [{"message": e["message"], "domain": "global",
                                             "reason": REASON.get(int(e["code"]), "error")}]
        out["status"] = e.get("status") or STATUS.get(int(e["code"]), "UNKNOWN")
        return {"error": out}
    message = e if isinstance(e, str) else payload if isinstance(payload, str) else "Request failed."
    return error(status, str(message)).payload


# -- paging ------------------------------------------------------------------------------------

def page(items: list[Any], req: Request, *, size: str = "maxResults", default: int = 100, maximum: int = 500
         ) -> tuple[list[Any], str | None]:
    """A slice of ``items`` and the token for the next one (opaque to clients, like Google's)."""
    try:
        n = req.int_arg(size, default)
    except ValueError:
        raise error(400, f"Invalid value for {size}", location=size) from None
    if n <= 0:
        n = default
    n = min(n, maximum)
    start = _offset(req.arg("pageToken"))
    chunk = items[start:start + n]
    return chunk, (_token(start + n) if start + n < len(items) else None)


def _token(offset: int) -> str:
    return str(10**18 + offset * 7 + 3)


def _offset(token: str | None) -> int:
    if not token:
        return 0
    try:
        v = int(token) - 10**18 - 3
    except ValueError:
        raise error(400, "Invalid pageToken", location="pageToken") from None
    if v < 0 or v % 7:
        raise error(400, "Invalid pageToken", location="pageToken")
    return v // 7


# -- partial responses ---------------------------------------------------------------------------

def select(value: Any, fields: str | None) -> Any:
    """Apply a ``fields`` mask: ``files(id,name),nextPageToken``, ``items/summary``, ``*``."""
    if not fields or fields.strip() == "*":
        return value
    return _apply(value, _parse(fields))


def _parse(s: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    i = 0

    def parse_list(end: str | None) -> dict[str, Any]:
        nonlocal i
        res: dict[str, Any] = {}
        while i < len(s):
            if end and s[i] == end:
                i += 1
                return res
            if s[i] in ", ":
                i += 1
                continue
            j = i
            while i < len(s) and s[i] not in ",()":
                i += 1
            path = s[j:i].strip().split("/")
            sub: dict[str, Any] | None = None
            if i < len(s) and s[i] == "(":
                i += 1
                sub = parse_list(")")
            node = res
            for k, part in enumerate(path):
                last = k == len(path) - 1
                if last:
                    node[part] = _merge(node.get(part), sub)
                else:
                    node = node.setdefault(part, {}) if isinstance(node.get(part), dict) else node.setdefault(part, {})
        return res

    out = parse_list(None)
    return out


def _merge(a: Any, b: Any) -> Any:
    if a is None:
        return b
    if b is None:
        return None  # a bare name selects everything under it
    return {**a, **b} if isinstance(a, dict) and isinstance(b, dict) else None


def _apply(value: Any, mask: dict[str, Any] | None) -> Any:
    if mask is None:
        return value
    if isinstance(value, list):
        return [_apply(v, mask) for v in value]
    if not isinstance(value, dict):
        return value
    if "*" in mask:
        return {k: _apply(v, mask["*"]) for k, v in value.items()}
    return {k: _apply(value[k], sub) for k, sub in mask.items() if k in value}


# -- encoding ------------------------------------------------------------------------------------

def b64url(data: bytes | str, pad: bool = True) -> str:
    """URL-safe base64. Gmail keeps the ``=`` padding in ``body.data`` and ``raw`` (clients rely on it)."""
    if isinstance(data, str):
        data = data.encode()
    out = base64.urlsafe_b64encode(data).decode()
    return out if pad else out.rstrip("=")


def unb64url(s: str) -> bytes:
    s = s.strip().replace("\n", "")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def rfc3339(t: dt.datetime, millis: bool = True) -> str:
    t = t.astimezone(dt.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S") + (f".{t.microsecond // 1000:03d}" if millis else "") + "Z"
