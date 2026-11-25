"""Loopback is allowed for one exact origin: the approved address and port, and no other.

The unit cases cover the route guard. The browser case needs Playwright and Chromium: it skips
without them, and it is the case that proves a page cannot reach a second local listener.
"""

import http.server
import importlib.util
import threading
import unittest

from piggyback.measure import _guard, measure


class FakeRequest:
    def __init__(self, url):
        self.url = url


class FakeRoute:
    def __init__(self, url):
        self.request = FakeRequest(url)
        self.outcome = None

    def continue_(self):
        self.outcome = "continued"

    def abort(self, reason):
        self.outcome = f"aborted:{reason}"


class FakeEgress:
    def __init__(self):
        self.blocked = []

    def record(self, url):
        self.blocked.append(url)


class RouteGuardTests(unittest.TestCase):
    PIN = ("127.0.0.1", 8123)

    def route(self, url):
        egress = FakeEgress()
        route = FakeRoute(url)
        _guard(egress, self.PIN)(route)
        return route.outcome, egress.blocked

    def test_the_approved_origin_is_continued(self):
        self.assertEqual(self.route("http://127.0.0.1:8123/app.js"), ("continued", []))

    def test_another_loopback_port_is_aborted_and_recorded(self):
        url = "http://127.0.0.1:9000/probe?token=x"
        self.assertEqual(
            self.route(url), ("aborted:blockedbyclient", ["http://127.0.0.1:9000/probe?token=x"])
        )

    def test_a_loopback_alias_of_the_approved_port_is_aborted(self):
        self.assertEqual(self.route("http://127.0.0.2:8123/")[0], "aborted:blockedbyclient")

    def test_without_a_pin_loopback_is_aborted(self):
        egress = FakeEgress()
        route = FakeRoute("http://127.0.0.1:8123/")
        _guard(egress, None)(route)
        self.assertEqual(route.outcome, "aborted:blockedbyclient")


class ListenerHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.hits.append(self.path)
        body = self.server.body
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def listener(body):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ListenerHandler)
    server.hits, server.body = [], body
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def chromium_missing():
    from playwright.sync_api import sync_playwright

    from piggyback.browser import BrowserUnavailable, launch

    with sync_playwright() as p:
        try:
            browser, _ = launch(p)
        except BrowserUnavailable:
            return True
        browser.close()
    return False


@unittest.skipIf(importlib.util.find_spec("playwright") is None, "playwright not installed")
class BrowserOriginTests(unittest.TestCase):
    def test_a_page_cannot_reach_a_second_loopback_listener(self):
        second = listener(b"second")
        self.addCleanup(second.server_close)
        self.addCleanup(second.shutdown)
        port_b = second.server_address[1]
        page_html = (
            "<!doctype html><meta charset=utf-8><script>"
            f"fetch('http://127.0.0.1:{port_b}/probe').catch(() => {{}});"
            "</script><h1>origin A</h1>"
        ).encode()
        first = listener(page_html)
        self.addCleanup(first.server_close)
        self.addCleanup(first.shutdown)
        if chromium_missing():
            self.skipTest("Chromium is not available")
        url = f"http://127.0.0.1:{first.server_address[1]}/"
        result = measure(url, allow_loopback=True)
        self.assertEqual(second.hits, [])
        self.assertTrue(any(f":{port_b}/probe" in target for target in result["blocked_requests"]))


if __name__ == "__main__":
    unittest.main()
