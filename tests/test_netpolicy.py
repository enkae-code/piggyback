import socket
import unittest
from unittest import mock

from piggyback.netpolicy import host_allowed, local_name, origin, resolve_allowed, url_allowed

# Literal IPs and fixed names only: no DNS, so the table runs offline.
PUBLIC = ["93.184.216.34", "2606:4700:4700::1111"]
BLOCKED = [
    "127.0.0.1",
    "::1",
    "10.0.0.1",
    "192.168.1.1",
    "172.16.0.1",
    "169.254.1.1",
    "100.64.0.1",  # CGNAT: is_global blocks it
    "fc00::1",
    "fe80::1",
    "fec0::1",
    "0.0.0.0",
    "224.0.0.1",  # multicast: is_global is True, so the explicit check must catch it
    "ff02::1",
    "240.0.0.1",  # IPv4 reserved
    "4000::1",  # IPv6 reserved space that is_global calls global
    "::ffff:127.0.0.1",
    "::ffff:10.0.0.1",
    "2002:7f00:1::1",  # 6to4 wrapping loopback
    "2002:0808:0808::1",  # 6to4 wrapping a public address: blocked, not unwrapped
    "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo
    "localhost",
    "LOCALHOST.",
    "printer.local",
    "app.localhost",
    "",
]
BLOCKED_URLS = [
    "file:///etc/passwd",
    "data:text/html,<h1>x</h1>",
    "ftp://93.184.216.34/",
    "http://127.0.0.1:8000/",
    "http://10.0.0.1/",
    "http://[::1]/",
    "http://localhost/",
    "http:///nohost",
]
PIN = ("127.0.0.1", 8123)


class NetPolicyTests(unittest.TestCase):
    def test_public_hosts_allowed(self):
        for host in PUBLIC:
            with self.subTest(host=host):
                self.assertTrue(host_allowed(host))
        self.assertTrue(url_allowed("https://93.184.216.34/page"))

    def test_private_hosts_blocked(self):
        for host in BLOCKED:
            with self.subTest(host=host):
                self.assertFalse(host_allowed(host))

    def test_blocked_urls(self):
        for url in BLOCKED_URLS:
            with self.subTest(url=url):
                self.assertFalse(url_allowed(url))

    def test_loopback_allowed_only_for_the_pinned_origin(self):
        self.assertTrue(url_allowed("http://127.0.0.1:8123/", loopback=PIN))
        self.assertTrue(host_allowed("127.0.0.1", 8123, loopback=PIN))
        self.assertTrue(host_allowed("::ffff:127.0.0.1", 8123, loopback=PIN))

    def test_other_loopback_port_and_alias_refused_under_a_pin(self):
        for host, port in (("127.0.0.1", 9000), ("127.0.0.2", 8123), ("::1", 8123)):
            with self.subTest(host=host, port=port):
                self.assertFalse(host_allowed(host, port, loopback=PIN))
        self.assertFalse(url_allowed("http://127.0.0.1:9000/", loopback=PIN))
        self.assertFalse(url_allowed("http://127.0.0.2:8123/", loopback=PIN))

    def test_loopback_refused_without_a_pin(self):
        self.assertFalse(host_allowed("127.0.0.1", 8123))
        self.assertFalse(url_allowed("http://127.0.0.1:8123/"))

    def test_other_private_ranges_and_localhost_stay_blocked_under_a_pin(self):
        for host in ("10.0.0.1", "192.168.1.1", "169.254.1.1", "100.64.0.1", "fc00::1"):
            with self.subTest(host=host):
                self.assertFalse(host_allowed(host, 8123, loopback=PIN))
        self.assertFalse(host_allowed("localhost", 8123, loopback=PIN))

    def test_origin_of_a_url_is_its_literal_host_and_port(self):
        self.assertEqual(origin("http://127.0.0.1:8123/a?b"), ("127.0.0.1", 8123))
        self.assertEqual(origin("https://[::1]/"), ("::1", 443))
        self.assertEqual(origin("http://[::ffff:127.0.0.1]:81/"), ("127.0.0.1", 81))

    def test_a_name_pins_the_loopback_address_it_resolves_to(self):
        with mock.patch("socket.getaddrinfo", return_value=infos("127.0.0.1")):
            pin = origin("http://svc.example:8123/")
            self.assertEqual(pin, ("127.0.0.1", 8123))
            self.assertTrue(url_allowed("http://svc.example:8123/", loopback=pin))
            self.assertFalse(url_allowed("http://svc.example:9000/", loopback=pin))

    def test_every_resolved_address_must_pass(self):
        with mock.patch("socket.getaddrinfo", return_value=infos("93.184.216.34")):
            self.assertEqual(resolve_allowed("example.com"), "93.184.216.34")
        with mock.patch("socket.getaddrinfo", return_value=infos("93.184.216.34", "10.0.0.1")):
            self.assertIsNone(resolve_allowed("mixed.example"))
        with mock.patch("socket.getaddrinfo", return_value=infos("93.184.216.34", "fe80::1%eth0")):
            self.assertIsNone(resolve_allowed("scoped.example"))
        with mock.patch("socket.getaddrinfo", return_value=infos("127.0.0.1", "10.0.0.1")):
            self.assertIsNone(resolve_allowed("mixed.example", 8123, loopback=PIN))

    def test_lookup_failure_and_empty_answer_block(self):
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror("no such host")):
            self.assertIsNone(resolve_allowed("nx.example"))
        with mock.patch("socket.getaddrinfo", return_value=[]):
            self.assertIsNone(resolve_allowed("empty.example"))

    def test_hostname_resolving_to_the_pinned_address_needs_the_pin(self):
        with mock.patch("socket.getaddrinfo", return_value=infos("127.0.0.1")):
            self.assertIsNone(resolve_allowed("svc.example", 8123))
            self.assertEqual(resolve_allowed("svc.example", 8123, loopback=PIN), "127.0.0.1")
            self.assertIsNone(resolve_allowed("svc.example", 9000, loopback=PIN))

    def test_names_blocked_even_when_dns_says_public(self):
        # The name rules must not depend on what DNS answers.
        with mock.patch("socket.getaddrinfo", return_value=infos("93.184.216.34")):
            for name in ("printer.local", "app.localhost", "localhost", "LOCALHOST."):
                with self.subTest(name=name):
                    self.assertIsNone(resolve_allowed(name))

    def test_local_name_rewrites_only_the_localhost_name(self):
        self.assertEqual(local_name("http://LOCALHOST:8080/a?b"), "http://127.0.0.1:8080/a?b")
        self.assertEqual(
            local_name("https://example.com/localhost"), "https://example.com/localhost"
        )
        self.assertEqual(local_name("http://localhost:99999x/"), "http://localhost:99999x/")
        self.assertEqual(local_name("http://localhost:0/"), "http://127.0.0.1:0/")

    def test_subdomain_of_localhost_stays_refused_under_a_pin(self):
        self.assertFalse(url_allowed("http://app.localhost:3000", loopback=("127.0.0.1", 3000)))


def infos(*ips):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]


if __name__ == "__main__":
    unittest.main()
