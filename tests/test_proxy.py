import contextlib
import http.server
import socket
import threading
import unittest
from unittest import mock

from piggyback import proxy as proxy_module
from piggyback.proxy import EgressProxy

try:
    from .test_netpolicy import infos
except ImportError:  # plain `discover -s tests` without -t . imports test modules top-level
    from test_netpolicy import infos


class Hello(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "5")
        self.end_headers()
        self.wfile.write(b"hello")

    def log_message(self, *args):
        pass


class KeepAlive(http.server.BaseHTTPRequestHandler):
    # HTTP/1.1 keeps the connection open unless the request asks for close: the case to guard.
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.server.seen.append((self.path, self.headers.as_string()))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def send(port, raw):
    # A plain socket, not socket.create_connection: tests patch that name.
    with socket.socket() as s:
        s.settimeout(10)
        s.connect(("127.0.0.1", port))
        s.sendall(raw)
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    return data


def keepalive_origin():
    origin = http.server.ThreadingHTTPServer(("127.0.0.1", 0), KeepAlive)
    origin.seen = []
    threading.Thread(target=origin.serve_forever, daemon=True).start()
    return origin


class EgressProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.origin = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hello)
        threading.Thread(target=cls.origin.serve_forever, daemon=True).start()
        cls.origin_url = f"http://127.0.0.1:{cls.origin.server_address[1]}/"

    @classmethod
    def tearDownClass(cls):
        cls.origin.shutdown()
        cls.origin.server_close()

    def setUp(self):
        self.proxy = EgressProxy(loopback=None)

    def tearDown(self):
        self.proxy.close()

    def pin(self, origin):
        """Replace the proxy with one that allows only this origin's loopback address and port."""
        self.proxy.close()
        self.proxy = EgressProxy(loopback=("127.0.0.1", origin.server_address[1]))

    def test_plain_loopback_refused_and_recorded(self):
        reply = send(self.proxy.port, f"GET {self.origin_url} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        self.assertTrue(reply.startswith(b"HTTP/1.1 403"), reply)
        self.assertEqual(self.proxy.blocked, [self.origin_url])

    def test_query_string_not_recorded(self):
        reply = send(
            self.proxy.port, b"GET http://10.0.0.1/p?token=secret HTTP/1.1\r\nHost: x\r\n\r\n"
        )
        self.assertTrue(reply.startswith(b"HTTP/1.1 403"), reply)
        self.assertEqual(self.proxy.blocked, ["http://10.0.0.1/p"])

    def test_connect_to_private_refused_and_recorded(self):
        reply = send(
            self.proxy.port, b"CONNECT 10.0.0.1:443 HTTP/1.1\r\nHost: 10.0.0.1:443\r\n\r\n"
        )
        self.assertTrue(reply.startswith(b"HTTP/1.1 403"), reply)
        self.assertEqual(self.proxy.blocked, ["10.0.0.1:443"])

    def test_websocket_upgrade_to_private_refused_and_recorded(self):
        raw = (
            b"GET ws://10.0.0.1:1/ HTTP/1.1\r\nHost: 10.0.0.1:1\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n\r\n"
        )
        self.assertTrue(send(self.proxy.port, raw).startswith(b"HTTP/1.1 403"))
        self.assertEqual(self.proxy.blocked, ["ws://10.0.0.1:1/"])

    def test_non_http_scheme_refused(self):
        raw = b"GET file:///etc/passwd HTTP/1.1\r\nHost: x\r\n\r\n"
        self.assertTrue(send(self.proxy.port, raw).startswith(b"HTTP/1.1 403"))
        self.assertEqual(self.proxy.blocked, ["file:///etc/passwd"])

    def test_non_http_scheme_refused_without_dialing(self):
        # ftp://<public IP> passes the address check, so only the scheme check stops the dial.
        with mock.patch.object(proxy_module.socket, "create_connection") as dial:
            reply = send(self.proxy.port, b"GET ftp://8.8.8.8/ HTTP/1.1\r\nHost: x\r\n\r\n")
        dial.assert_not_called()
        self.assertTrue(reply.startswith(b"HTTP/1.1 403"), reply)
        self.assertEqual(self.proxy.blocked, ["ftp://8.8.8.8/"])

    def test_oversized_head_dropped(self):
        raw = b"GET http://x/ HTTP/1.1\r\nX: " + b"a" * (proxy_module.MAX_HEAD + 1000)
        try:
            reply = send(self.proxy.port, raw)
        except ConnectionError:  # the proxy hangs up mid-send
            reply = b""
        self.assertEqual(reply, b"")

    def test_connections_over_the_cap_are_closed(self):
        self.proxy.close()
        with mock.patch.object(proxy_module, "MAX_CONNECTIONS", 1):
            self.proxy = EgressProxy(loopback=None)
        # The accept loop is serial, so the first connection holds the only slot before the
        # second is accepted.
        holder = socket.create_connection(("127.0.0.1", self.proxy.port), timeout=10)
        self.addCleanup(holder.close)
        try:
            reply = send(self.proxy.port, b"GET http://10.0.0.1/ HTTP/1.1\r\nHost: x\r\n\r\n")
        except ConnectionError:  # closed before the request was read
            reply = b""
        self.assertEqual(reply, b"")
        self.assertEqual(self.proxy.blocked, [])

    def test_approved_origin_forwarded(self):
        self.pin(self.origin)
        reply = send(self.proxy.port, f"GET {self.origin_url} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        self.assertIn(b" 200 OK", reply)
        self.assertTrue(reply.endswith(b"hello"))
        self.assertEqual(self.proxy.blocked, [])

    def test_other_loopback_port_refused_without_dialing(self):
        self.pin(self.origin)
        other = self.origin.server_address[1] + 1
        with mock.patch.object(proxy_module.socket, "create_connection") as dial:
            reply = send(
                self.proxy.port, f"GET http://127.0.0.1:{other}/ HTTP/1.1\r\n\r\n".encode()
            )
        dial.assert_not_called()
        self.assertTrue(reply.startswith(b"HTTP/1.1 403"), reply)
        self.assertEqual(self.proxy.blocked, [f"http://127.0.0.1:{other}/"])

    def test_loopback_alias_refused_without_dialing(self):
        self.pin(self.origin)
        port = self.origin.server_address[1]
        with mock.patch.object(proxy_module.socket, "create_connection") as dial:
            reply = send(self.proxy.port, f"GET http://127.0.0.2:{port}/ HTTP/1.1\r\n\r\n".encode())
        dial.assert_not_called()
        self.assertTrue(reply.startswith(b"HTTP/1.1 403"), reply)

    def test_connect_to_other_loopback_port_refused_without_dialing(self):
        self.pin(self.origin)
        other = self.origin.server_address[1] + 1
        with mock.patch.object(proxy_module.socket, "create_connection") as dial:
            reply = send(self.proxy.port, f"CONNECT 127.0.0.1:{other} HTTP/1.1\r\n\r\n".encode())
        dial.assert_not_called()
        self.assertTrue(reply.startswith(b"HTTP/1.1 403"), reply)

    def test_upstream_gets_close_and_no_proxy_headers(self):
        origin = keepalive_origin()
        self.addCleanup(origin.server_close)
        self.addCleanup(origin.shutdown)
        self.pin(origin)
        raw = (
            f"GET http://127.0.0.1:{origin.server_address[1]}/ HTTP/1.1\r\nHost: x\r\n"
            "Connection: keep-alive\r\nProxy-Authorization: ***\r\n"
            "Proxy-Connection: keep-alive\r\n\r\n"
        ).encode()
        self.assertIn(b" 200 OK", send(self.proxy.port, raw))
        path, headers = origin.seen[0]
        self.assertIn("Connection: close", headers)
        self.assertNotIn("keep-alive", headers.lower())
        self.assertNotIn("proxy-", headers.lower())

    def test_unframeable_bodies_refused(self):
        self.pin(self.origin)
        bad = {
            "chunked": "Transfer-Encoding: chunked\r\n",
            "duplicate": "Content-Length: 1\r\nContent-Length: 1\r\n",
            "signed": "Content-Length: +1\r\n",
            "underscore": "Content-Length: 1_0\r\n",
        }
        for name, header in bad.items():
            with self.subTest(name):
                raw = f"POST {self.origin_url} HTTP/1.1\r\nHost: x\r\n{header}\r\nx".encode()
                self.assertIn(b" 400 ", send(self.proxy.port, raw))

    def test_second_request_on_same_connection_never_reaches_upstream(self):
        origin = keepalive_origin()
        self.addCleanup(origin.server_close)
        self.addCleanup(origin.shutdown)
        self.pin(origin)
        base = f"http://127.0.0.1:{origin.server_address[1]}"
        raw = (
            f"GET {base}/ HTTP/1.1\r\nHost: x\r\n\r\n"
            "GET http://10.0.0.1/secret HTTP/1.1\r\nHost: x\r\n\r\n"
        ).encode()
        reply = send(self.proxy.port, raw)
        self.assertIn(b" 200 OK", reply)
        self.assertEqual([path for path, _ in origin.seen], ["/"])

    def test_origin_ignoring_close_still_gets_only_one_request(self):
        # An origin that keeps serving after Connection: close: only the proxy stops the reuse.
        origin = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(origin.close)
        self.proxy.close()
        self.proxy = EgressProxy(loopback=("127.0.0.1", origin.getsockname()[1]))
        seen = []

        def serve():
            conn, _ = origin.accept()
            with conn:
                conn.settimeout(1)
                with contextlib.suppress(OSError):
                    while data := conn.recv(4096):
                        seen.extend(
                            line for line in data.split(b"\r\n") if line.startswith(b"GET ")
                        )
                        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")

        threading.Thread(target=serve, daemon=True).start()
        raw = (
            f"GET http://127.0.0.1:{origin.getsockname()[1]}/ HTTP/1.1\r\nHost: x\r\n\r\n"
            "GET http://10.0.0.1/secret HTTP/1.1\r\nHost: x\r\n\r\n"
        ).encode()
        send(self.proxy.port, raw)
        self.assertEqual(seen, [b"GET / HTTP/1.1"])

    def test_name_resolved_once_and_connect_uses_checked_address(self):
        # The second answer would rebind to loopback; the proxy must never ask for it.
        answers = [infos("93.184.216.34"), infos("127.0.0.1")]
        with (
            mock.patch("socket.getaddrinfo", side_effect=answers) as lookup,
            mock.patch.object(
                proxy_module.socket, "create_connection", side_effect=OSError
            ) as dial,
        ):
            reply = send(self.proxy.port, b"GET http://rebind.example/ HTTP/1.1\r\nHost: r\r\n\r\n")
        self.assertEqual(lookup.call_count, 1)
        dial.assert_called_once_with(("93.184.216.34", 80), timeout=proxy_module.TIMEOUT_S)
        self.assertTrue(reply.startswith(b"HTTP/1.1 502"), reply)

    def test_mixed_answer_refused_without_connecting(self):
        answers = infos("93.184.216.34", "10.0.0.1")
        with (
            mock.patch("socket.getaddrinfo", return_value=answers),
            mock.patch.object(proxy_module.socket, "create_connection") as dial,
        ):
            reply = send(self.proxy.port, b"CONNECT mixed.example:443 HTTP/1.1\r\n\r\n")
        dial.assert_not_called()
        self.assertTrue(reply.startswith(b"HTTP/1.1 403"), reply)
        self.assertEqual(self.proxy.blocked, ["mixed.example:443"])


if __name__ == "__main__":
    unittest.main()
