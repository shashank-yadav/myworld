"""The HTTPS gateway: real clients, simulated services.

Point an agent's ``HTTPS_PROXY`` at the gateway and have it trust the gateway's CA, and every
request to the Google Workspace APIs or the GitHub API is answered by the toolsim host, from
the agent's environment, over real TLS. ``gog``, ``gh``, Google's Python client, Octokit and
``curl`` run unmodified. Everything else is tunneled to the internet (or refused with
``passthrough=False``, for sealed sandboxes).

    gw = Gateway(host, port=8443).start()
    env = gw.client_env(token)   # HTTPS_PROXY, SSL_CERT_FILE, REQUESTS_CA_BUNDLE, ...
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
from http import HTTPStatus
from pathlib import Path
from typing import Any

from ..api.http import handle
from .ca import CA

log = logging.getLogger("toolsim.gateway")


class _Quiet(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # asyncio's harmless start_tls chatter
        return "eof_received() has no effect" not in record.getMessage()


logging.getLogger("asyncio").addFilter(_Quiet())

INTERCEPT_SUFFIXES = (".googleapis.com",)
INTERCEPT_HOSTS = {"api.github.com", "uploads.github.com", "oauth2.googleapis.com"}
MAX_BODY = 50 * 1024 * 1024


def intercepts(host: str) -> bool:
    host = host.lower()
    return host in INTERCEPT_HOSTS or host.endswith(INTERCEPT_SUFFIXES)


class Gateway:
    def __init__(self, host: Any, *, bind: str = "127.0.0.1", port: int = 0, ca_dir: str | Path | None = None,
                 passthrough: bool = True, tls_port: int | None = None):
        self.host = host
        self.bind = bind
        self.port = port
        self.tls_port = tls_port  # direct TLS: for clients that ignore proxies (the API hosts resolve to us)
        self.passthrough = passthrough
        self.ca = CA(Path(ca_dir or os.environ.get("TOOLSIM_CA_DIR") or Path.home() / ".toolsim" / "ca"))
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.AbstractServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------------------------------

    def start(self) -> Gateway:
        ready = threading.Event()

        def run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._server = self._loop.run_until_complete(asyncio.start_server(self._client, self.bind, self.port,
                                                                              limit=MAX_BODY))
            self.port = self._server.sockets[0].getsockname()[1]
            if self.tls_port is not None:
                self._tls_server = self._loop.run_until_complete(asyncio.start_server(
                    self._direct, self.bind, self.tls_port, ssl=self._sni_context(), limit=MAX_BODY))
                self.tls_port = self._tls_server.sockets[0].getsockname()[1]
            ready.set()
            self._loop.run_forever()

        self._thread = threading.Thread(target=run, name="toolsim-gateway", daemon=True)
        self._thread.start()
        ready.wait(10)
        log.info("gateway listening on %s:%s (CA %s)", self.bind, self.port, self.ca.cert_path)
        return self

    def stop(self) -> None:
        """Stop listening and drop open connections (clients see a closed socket)."""
        loop, server = self._loop, self._server
        if loop and server:
            def shutdown() -> None:
                server.close()
                if getattr(self, "_tls_server", None):
                    self._tls_server.close()
                for task in asyncio.all_tasks(loop):
                    task.cancel()
                loop.call_later(0.05, loop.stop)
            loop.call_soon_threadsafe(shutdown)
            if self._thread:
                self._thread.join(5)

    @property
    def url(self) -> str:
        return f"http://{self.bind}:{self.port}"

    def hosts_file(self, ip: str = "127.0.0.1") -> str:
        """/etc/hosts lines for direct-TLS mode (serve on port 443, or forward 443 to ``tls_port``)."""
        names = ["gmail.googleapis.com", "www.googleapis.com", "oauth2.googleapis.com", "sheets.googleapis.com",
                 "docs.googleapis.com", "people.googleapis.com", "drive.googleapis.com", "calendar-json.googleapis.com",
                 "openidconnect.googleapis.com", "api.github.com", "uploads.github.com"]
        return "".join(f"{ip} {n}\n" for n in names)

    def client_env(self) -> dict[str, str]:
        """Environment variables that route an agent's clients through the gateway."""
        ca = str(self.ca.cert_path)
        return {"HTTPS_PROXY": self.url, "https_proxy": self.url, "HTTP_PROXY": self.url, "http_proxy": self.url,
                "NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1",
                "SSL_CERT_FILE": ca, "REQUESTS_CA_BUNDLE": ca, "CURL_CA_BUNDLE": ca, "NODE_EXTRA_CA_CERTS": ca,
                "HTTPLIB2_CA_CERTS": ca, "GIT_SSL_CAINFO": ca}

    # -- connections -------------------------------------------------------------------------------

    def _sni_context(self) -> Any:
        import ssl
        base = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        base.set_alpn_protocols(["http/1.1"])
        cert, key = self.ca._leaf("www.googleapis.com")
        base.load_cert_chain(cert, key)

        def pick(sslobj: Any, name: str | None, _ctx: Any) -> None:
            if name:  # present the certificate for the host the client asked for
                sslobj.context = self.ca.context_for(name)
        base.sni_callback = pick
        return base

    async def _direct(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Direct TLS: requests for API hosts that resolve to the gateway (/etc/hosts, DNS)."""
        try:
            while True:
                head = await _read_head(reader)
                if head is None:
                    return
                method, target, headers = head
                host = next((v for k, v in headers if k.lower() == "host"), "").split(":")[0]
                if not await self._dispatch(reader, writer, host, method, target, headers):
                    return
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        except Exception:
            log.exception("gateway connection failed")
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await _read_head(reader)
            if head is None:
                return
            method, target, headers = head
            if method == "CONNECT":
                host, _, port = target.rpartition(":")
                host = host.strip("[]")
                if intercepts(host):
                    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    await writer.drain()
                    await writer.start_tls(self.ca.context_for(host))
                    await self._serve(reader, writer, host)
                elif self.passthrough:
                    await self._tunnel(reader, writer, host, int(port or 443))
                else:
                    await _respond(writer, 403, b"toolsim gateway: this host is not reachable from the sandbox\n",
                                   {"content-type": "text/plain"}, close=True)
                return
            # plain-HTTP proxying (absolute-form URLs) or direct requests
            if target.startswith("http://"):
                rest = target[7:]
                host, _, path = rest.partition("/")
                await self._dispatch(reader, writer, host, method, "/" + path, headers)
                await self._serve(reader, writer, host)
            else:
                await _respond(writer, 400, b"toolsim gateway: use it as an HTTPS proxy (CONNECT)\n",
                               {"content-type": "text/plain"}, close=True)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        except Exception:  # never take the gateway down for one connection
            log.exception("gateway connection failed")
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, host: str) -> None:
        """HTTP/1.1 requests on one (decrypted) connection, kept alive until the client closes it."""
        while True:
            head = await _read_head(reader)
            if head is None:
                return
            method, target, headers = head
            if not await self._dispatch(reader, writer, host, method, target, headers):
                return

    async def _dispatch(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, host: str, method: str,
                        target: str, headers: list[tuple[str, str]]) -> bool:
        h = {k.lower(): v for k, v in headers}
        if h.get("expect", "").lower() == "100-continue":
            writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
            await writer.drain()
        body = await _read_body(reader, h)
        path, _, query = target.partition("?")
        if not intercepts(host.split(":")[0]):
            await _respond(writer, 403, b"toolsim gateway: host not simulated\n", {"content-type": "text/plain"})
            return True
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, handle, self.host, host, method, path, query, dict(headers), body)
        keep = h.get("connection", "").lower() != "close"
        await _respond(writer, result.status, b"" if method == "HEAD" else result.body, result.headers, close=not keep)
        return keep

    async def _tunnel(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, host: str, port: int) -> None:
        try:
            up_r, up_w = await asyncio.wait_for(asyncio.open_connection(host, port), 15)
        except (OSError, asyncio.TimeoutError):
            await _respond(writer, 502, b"", {}, close=True)
            return
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()

        async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            with contextlib.suppress(Exception):
                while data := await src.read(65536):
                    dst.write(data)
                    await dst.drain()
            with contextlib.suppress(Exception):
                dst.close()

        await asyncio.gather(pipe(reader, up_w), pipe(up_r, writer))


async def _read_head(reader: asyncio.StreamReader) -> tuple[str, str, list[tuple[str, str]]] | None:
    try:
        raw = await reader.readuntil(b"\r\n\r\n")
    except asyncio.IncompleteReadError:
        return None
    lines = raw.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) < 3:
        return None
    headers = []
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers.append((k.strip(), v.strip()))
    return parts[0].upper(), parts[1], headers


async def _read_body(reader: asyncio.StreamReader, h: dict[str, str]) -> bytes:
    if "chunked" in h.get("transfer-encoding", "").lower():
        out = bytearray()
        while True:
            size = int((await reader.readuntil(b"\r\n")).split(b";")[0].strip() or b"0", 16)
            if size == 0:
                while (await reader.readuntil(b"\r\n")) != b"\r\n":  # trailers
                    pass
                return bytes(out)
            out += await reader.readexactly(size)
            await reader.readexactly(2)
            if len(out) > MAX_BODY:
                raise ConnectionError("body too large")
    n = int(h.get("content-length") or 0)
    if n > MAX_BODY:
        raise ConnectionError("body too large")
    return await reader.readexactly(n) if n else b""


async def _respond(writer: asyncio.StreamWriter, status: int, body: bytes, headers: dict[str, str],
                   close: bool = False) -> None:
    try:
        reason = HTTPStatus(status).phrase
    except ValueError:
        reason = "Status"
    hs = {k.lower(): v for k, v in headers.items()}
    hs.setdefault("date", _http_date())
    hs["content-length"] = str(len(body))
    if close:
        hs["connection"] = "close"
    out = f"HTTP/1.1 {status} {reason}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hs.items()) + "\r\n"
    writer.write(out.encode("latin-1") + body)
    await writer.drain()


def _http_date() -> str:
    from email.utils import formatdate
    return formatdate(usegmt=True)
