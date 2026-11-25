"""Local forward proxy for the browser's HTTP, WebSocket and TLS traffic.

Chromium sends those requests here. Plain http and ws arrive as absolute URIs; https and wss
arrive as CONNECT. netpolicy resolves the host once and checks every address; the socket then
connects to that literal address, so a second DNS answer (rebinding) or a redirect cannot reach
a private address. Refused targets get 403 and are recorded in `blocked`.

Not covered: WebRTC UDP bypasses this proxy; browser.py blocks it
(launch switch + NO_WEBRTC init script).
WebTransport (HTTP/3 over QUIC) is never sent while a proxy is set (Chromium, probed
2026-11-16); a browser-integration test pins it.
"""

import contextlib
import socket
import threading
from urllib.parse import urlsplit

from . import netpolicy

TIMEOUT_S = 30  # idle sockets close after this; a quiet WebSocket is dropped the same way
MAX_CONNECTIONS = 64  # ponytail: thread per connection with a cap; asyncio if pages need more
MAX_BLOCKED = 50
MAX_HEAD = 16384
FORBIDDEN = b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
BAD_GATEWAY = b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
BAD_REQUEST = b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


class EgressProxy:
    def __init__(self, loopback=None):
        self.loopback = loopback  # the one (address, port) origin loopback may reach, or None
        self.blocked = []
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._server = socket.create_server(("127.0.0.1", 0))
        self.port = self._server.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def record(self, target):
        # The record is written into fingerprint.json; query strings can carry capability tokens.
        if len(self.blocked) < MAX_BLOCKED:
            self.blocked.append(target.split("?", 1)[0][:500])

    def close(self):
        # Open connections end on their own timeouts; the worker process exits right after.
        self._server.close()

    def _accept(self):
        while True:
            try:
                client, _ = self._server.accept()
            except OSError:
                return
            if not self._slots.acquire(blocking=False):
                client.close()
                continue
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client):
        try:
            with client:
                client.settimeout(TIMEOUT_S)
                self._handle(client)
        except (OSError, ValueError):
            pass  # one bad connection never takes the proxy down
        finally:
            self._slots.release()

    def _handle(self, client):
        head, rest = _read_head(client)
        if head is None:
            return
        request_line, *headers = head.split(b"\r\n")
        method, target, version = request_line.decode("latin-1").split(" ")
        if method == "CONNECT":
            self._tunnel(client, target, rest)
        else:
            self._forward(client, (method, target, version), headers, rest)

    def _tunnel(self, client, target, rest):
        parts = urlsplit(f"//{target}")
        upstream = self._open(client, parts.hostname or "", parts.port or 443, target)
        if upstream is None:
            return
        with upstream:
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            _relay(client, upstream, rest)

    def _forward(self, client, request, headers, rest):
        method, target, version = request
        parts = urlsplit(target)
        if parts.scheme not in ("http", "ws") or not parts.hostname:
            self.record(target)
            client.sendall(FORBIDDEN)
            return
        upgrade = any(h.lower().startswith(b"upgrade:") for h in headers)
        try:
            length = _body_length(headers, upgrade)
        except ValueError:
            client.sendall(BAD_REQUEST)
            return
        upstream = self._open(client, parts.hostname, parts.port or 80, target)
        if upstream is None:
            return
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        head = _origin_form(f"{method} {path} {version}", headers)
        with upstream:
            if upgrade:
                _relay(client, upstream, head + rest)
            else:
                _one_request(client, upstream, head, rest, length)

    def _open(self, client, host, port, shown):
        """Connect to a checked address, or answer 403/502 and return None."""
        address = netpolicy.resolve_allowed(host, port, self.loopback)
        if address is None:
            self.record(shown)
            client.sendall(FORBIDDEN)
            return None
        try:
            return socket.create_connection((address, port), timeout=TIMEOUT_S)
        except OSError:
            client.sendall(BAD_GATEWAY)
            return None


def _origin_form(request_line, headers):
    # Strip proxy-only headers. One request per upstream connection: unless it is an upgrade
    # (WebSocket), ask the server to close after its response.
    lines = [h for h in headers if not h.lower().startswith(b"proxy-")]
    if not any(h.lower().startswith(b"upgrade:") for h in headers):
        lines = [h for h in lines if not h.lower().startswith(b"connection:")]
        lines.append(b"Connection: close")
    return request_line.encode("latin-1") + b"\r\n" + b"\r\n".join(lines) + b"\r\n\r\n"


def _body_length(headers, upgrade):
    # Chunked bodies are not framed here, so they are refused (fail closed).
    if not upgrade and any(h.lower().startswith(b"transfer-encoding:") for h in headers):
        raise ValueError("chunked body")
    values = [
        h.split(b":", 1)[1].strip() for h in headers if h.lower().startswith(b"content-length:")
    ]
    if len(values) > 1:
        raise ValueError("duplicate Content-Length")
    if values and not values[0].isdigit():  # int() alone would accept "+5" and "1_0"
        raise ValueError("bad Content-Length")
    return int(values[0]) if values else 0


def _one_request(client, upstream, head, rest, length):
    # Forward exactly this request's body, then only the response. Bytes pipelined after the
    # body are never read, so they cannot reach another host through this upstream.
    upstream.sendall(head + rest[:length])
    left = length - len(rest)
    while left > 0:
        chunk = client.recv(min(left, 65536))
        if not chunk:
            return
        upstream.sendall(chunk)
        left -= len(chunk)
    _copy(upstream, client)


def _read_head(sock):
    data = b""
    while b"\r\n\r\n" not in data:
        if len(data) > MAX_HEAD:  # checked before blocking on another recv
            return None, b""
        chunk = sock.recv(4096)
        if not chunk:
            return None, b""
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    return head, rest


def _relay(client, upstream, first):
    upstream.sendall(first)
    back = threading.Thread(target=_copy, args=(upstream, client), daemon=True)
    back.start()
    _copy(client, upstream)
    back.join()


def _copy(src, dst):
    with contextlib.suppress(OSError):
        while data := src.recv(65536):
            dst.sendall(data)
    with contextlib.suppress(OSError):
        dst.shutdown(socket.SHUT_WR)
