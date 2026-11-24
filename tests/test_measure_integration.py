import contextlib
import functools
import http.server
import importlib.util
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

if importlib.util.find_spec("playwright") is None:
    raise unittest.SkipTest("playwright not installed; run with the measure extra")

from piggyback.browser import ARGS  # noqa: E402
from piggyback.cli import main  # noqa: E402
from piggyback.measure import BYPASS_ENV, MeasureError, measure  # noqa: E402
from piggyback.proxy import EgressProxy  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def serve(hits=None, host="127.0.0.1", port=0):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if hits is not None:
                hits.append(self.path)
            if self.path == "/bounce":
                self.send_response(302)
                self.send_header("Location", "http://10.0.0.1/")
                self.end_headers()
                return
            if self.path == "/slow-document":  # first bytes, then no end: the parser never finishes
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<!doctype html><title>x</title>")
                self.wfile.flush()
                time.sleep(20)
                return
            super().do_GET()

    handler = functools.partial(Handler, directory=str(FIXTURES))
    server = http.server.ThreadingHTTPServer((host, port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def lan_address():
    # A UDP connect sends nothing; it only asks the OS which address routes out.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            ip = s.getsockname()[0]
    except OSError:
        return None
    return None if ip.startswith("127.") else ip


RTC_PROBE = """async (target) => {
  const pc = new RTCPeerConnection({iceServers: [
    {urls: `stun:${target}`},
    {urls: `turn:${target}?transport=udp`, username: "u", credential: "p"},
  ]});
  pc.createDataChannel("probe");
  await pc.setLocalDescription(await pc.createOffer());
  await new Promise((done) => setTimeout(done, 3000));
  pc.close();
}"""


def page_udp(browser, target):
    page = browser.new_page()
    page.set_content("<!doctype html><title>rtc</title>")
    page.evaluate(RTC_PROBE, target)


def drain(sock):
    count = 0
    sock.setblocking(False)
    with contextlib.suppress(BlockingIOError):
        while True:
            sock.recv(2048)
            count += 1
    sock.setblocking(True)
    return count


class MeasureIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.other_hits = []
        cls.other = serve(cls.other_hits)
        cls.site_hits = []
        cls.site = serve(cls.site_hits)
        cls.base = f"http://127.0.0.1:{cls.site.server_address[1]}"
        cls.other_port = cls.other.server_address[1]

    @classmethod
    def tearDownClass(cls):
        for server in (cls.site, cls.other):
            server.shutdown()
            server.server_close()

    def test_text_anchor_boxes_hug_the_text(self):
        # The h1 block spans the viewport; its pin box must be the "Fixture heading" ink only.
        anchors = measure(f"{self.base}/index.html", allow_loopback=True)["anchors"]
        self.assertLess(anchors["heading"]["box"]["width"], 600)
        self.assertGreater(anchors["heading"]["box"]["width"], 100)
        # Text parked off to the side (animated headings) must not stretch the box past the element.
        box = measure(f"{self.base}/heading-offscreen.html", allow_loopback=True)["anchors"][
            "heading"
        ]["box"]
        self.assertLessEqual(box["x"] + box["width"], 1440)

    def test_index_fingerprint_and_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            shot = Path(tmp) / "shot.png"
            url = f"{self.base}/index.html?other={self.other_port}"
            result = measure(url, allow_loopback=True, screenshot=shot)
            self.assertEqual(result["anchors"]["heading"]["tag"], "h1")
            self.assertEqual(result["anchors"]["heading"]["font_size"], "40px")
            self.assertEqual(result["anchors"]["cta"]["tag"], "button")
            self.assertEqual(result["anchors"]["cta"]["radius"], "8px")
            self.assertIn("8px", [r["value"] for r in result["radius"]])
            self.assertEqual(result["anchors"]["card"]["count"], 3)
            self.assertTrue(any(m["duration"] == 1000 for m in result["motion"]))
            self.assertTrue(shot.read_bytes().startswith(b"\x89PNG"))
            # Loopback sibling reached (flag on); 10.0.0.1 aborted before it left the browser.
            self.assertTrue(any(p == "/" for p in self.other_hits))
            self.assertIn("http://10.0.0.1/", result["blocked_requests"])
            self.assertFalse(any(f":{self.other_port}" in u for u in result["blocked_requests"]))

    def test_page_canvas_background_leads_the_palette(self):
        # The light body paints the canvas; the dark footer and button are small boxes.
        result = measure(f"{self.base}/canvas-paper.html", allow_loopback=True)
        background = result["palette"]["background"]
        self.assertEqual(background[0]["value"], "rgb(245, 240, 228)")
        self.assertGreater(background[0]["share"], 0.5)
        self.assertIn("rgb(25, 27, 24)", [row["value"] for row in background])

    def test_dark_tinted_panels_are_not_the_accent(self):
        # rgb(21,32,35) panels pass the ratio test with no hue; only the mint button counts.
        palette = measure(f"{self.base}/accent-dark-tint.html", allow_loopback=True)["palette"]
        self.assertGreater(palette["accent_share"], 0)
        self.assertLess(palette["accent_share"], 0.02)

    def test_saturated_hero_is_the_accent(self):
        palette = measure(f"{self.base}/accent-hero.html", allow_loopback=True)["palette"]
        self.assertGreater(palette["accent_share"], 0.5)

    def test_saturated_page_canvas_is_the_accent(self):
        # The amber body paints the canvas; no box has a background, so only the canvas can count.
        palette = measure(f"{self.base}/accent-canvas-amber.html", allow_loopback=True)["palette"]
        self.assertGreater(palette["accent_share"], 0.8)

    def test_small_red_button_does_not_beat_the_amber_canvas(self):
        # The red button is the most saturated paint but the smaller area; the canvas is the accent.
        palette = measure(f"{self.base}/accent-canvas-red-button.html", allow_loopback=True)[
            "palette"
        ]
        self.assertGreater(palette["accent_share"], 0.8)

    def test_saturated_body_under_a_full_height_neutral_wrapper_is_not_the_accent(self):
        # The wrapper covers the viewport, so the amber body paints no visible canvas.
        palette = measure(f"{self.base}/accent-under-wrapper.html", allow_loopback=True)["palette"]
        self.assertLess(palette["accent_share"], 0.05)
        self.assertEqual(palette["background"][0]["value"], "rgb(245, 240, 228)")

    def test_saturated_hero_leads_the_background(self):
        # The 600px blue section out-areas the white canvas below it; the canvas must not win.
        background = measure(f"{self.base}/accent-hero.html", allow_loopback=True)["palette"][
            "background"
        ]
        self.assertEqual(background[0]["value"], "rgb(47, 107, 255)")

    def test_dark_html_canvas_and_light_body_box_both_appear(self):
        background = measure(f"{self.base}/html-dark-body-light.html", allow_loopback=True)[
            "palette"
        ]["background"]
        values = [row["value"] for row in background]
        self.assertIn("rgb(25, 27, 24)", values)
        self.assertIn("rgb(245, 240, 228)", values)

    def test_dark_tint_under_the_chroma_floor_is_not_the_accent(self):
        # Chroma 40 (rgb(30,60,70)) is a large panel; a chroma floor of 30 would make it the accent.
        palette = measure(f"{self.base}/accent-chroma-40.html", allow_loopback=True)["palette"]
        self.assertEqual(palette["accent_share"], 0)

    def test_dark_tint_over_the_chroma_floor_is_the_accent(self):
        # Chroma 60 (rgb(20,70,80)) is a large panel; a chroma floor of 66 would drop it.
        palette = measure(f"{self.base}/accent-chroma-60.html", allow_loopback=True)["palette"]
        self.assertGreater(palette["accent_share"], 0.1)

    def test_body_over_html_canvas_counts_only_its_exposed_area(self):
        # Blue covers 696px; the white body shows through the 204px below it (about 0.2 of the
        # viewport). A full-box body weight made white lead.
        background = measure(f"{self.base}/body-over-html-canvas.html", allow_loopback=True)[
            "palette"
        ]["background"]
        self.assertEqual(background[0]["value"], "rgb(47, 107, 255)")
        white = sum(r["share"] for r in background if r["value"] == "rgb(255, 255, 255)")
        self.assertLess(white, 0.3)
        self.assertNotIn("rgb(244, 178, 35)", [r["value"] for r in background])

    def test_body_over_html_canvas_accent_is_the_exposed_body(self):
        # White hero covers 396px; amber body shows through the other 504px (grid share 8/15).
        # The old full-box body weight reported an accent share of 1.
        palette = measure(f"{self.base}/body-amber-over-html-white.html", allow_loopback=True)[
            "palette"
        ]
        self.assertEqual(palette["background"][0]["value"], "rgb(244, 178, 35)")
        self.assertGreater(palette["accent_share"], 0.45)
        self.assertLess(palette["accent_share"], 0.65)

    def test_opacity_zero_overlay_does_not_hide_the_canvas(self):
        palette = measure(f"{self.base}/opaque-overlay-opacity-zero.html", allow_loopback=True)[
            "palette"
        ]
        self.assertEqual(palette["background"][0]["value"], "rgb(244, 178, 35)")
        self.assertGreater(palette["accent_share"], 0.8)

    def test_near_transparent_scrim_does_not_hide_the_canvas(self):
        # rgba(0,0,0,0.02) is not paint; the amber body still shows the whole viewport.
        palette = measure(f"{self.base}/scrim-over-amber.html", allow_loopback=True)["palette"]
        self.assertGreater(palette["accent_share"], 0.8)

    def test_transparent_full_viewport_media_does_not_hide_the_canvas(self):
        # An empty full-viewport svg paints nothing, so the amber body still leads.
        palette = measure(f"{self.base}/img-over-amber.html", allow_loopback=True)["palette"]
        self.assertEqual(palette["background"][0]["value"], "rgb(244, 178, 35)")
        self.assertGreater(palette["accent_share"], 0.8)

    def test_translucent_body_over_html_canvas_keeps_its_colour(self):
        palette = measure(
            f"{self.base}/body-translucent-over-html-white.html", allow_loopback=True
        )["palette"]
        self.assertEqual(palette["background"][0]["value"], "rgba(244, 178, 35, 0.4)")
        self.assertGreater(palette["accent_share"], 0.8)

    def test_http_404_is_refused_with_its_status(self):
        with self.assertRaisesRegex(MeasureError, "HTTP 404"):
            measure(f"{self.base}/no-such-page.html", allow_loopback=True)

    def test_a_2xx_answer_carries_its_receipt(self):
        result = measure(f"{self.base}/plain.html", allow_loopback=True)
        self.assertEqual(result["capture"]["status"], 200)
        self.assertEqual(result["capture"]["final_url"], f"{self.base}/plain.html")

    def test_effects_and_motion_props_for_defaults(self):
        result = measure(f"{self.base}/effects.html", allow_loopback=True)
        self.assertEqual(result["effects"]["gradient_text"], 1)
        self.assertEqual(result["effects"]["tinted_icon_squares"], 1)
        rise = [m for m in result["motion"] if {"opacity", "transform"} <= set(m["props"])]
        self.assertEqual(rise[0]["count"], 3)
        viewport = result["viewport"]
        for box in (
            result["effects"]["gradient_text_box"],
            result["effects"]["tinted_icon_square_box"],
            rise[0]["box"],
        ):
            self.assertIsNotNone(box)
            self.assertGreaterEqual(box["x"], 0)
            self.assertGreaterEqual(box["y"], 0)
            self.assertLessEqual(box["x"] + box["width"], viewport["width"])
            self.assertLessEqual(box["y"] + box["height"], viewport["height"])

    def test_container_is_the_centred_body_column(self):
        result = measure(f"{self.base}/container-body.html", allow_loopback=True)
        self.assertEqual(result["container_width"], 416)  # body max-width: 26em at 16px

    def test_full_width_section_with_gutters_does_not_stand_in_for_the_column(self):
        result = measure(f"{self.base}/gutter-section.html", allow_loopback=True)
        self.assertEqual(result["container_width"], 960)  # the 1392px band out-areas it

    def test_only_pastel_square_icon_tiles_count(self):
        result = measure(f"{self.base}/icon-tiles.html", allow_loopback=True)
        # The square and the 25%-radius tile count; not the circle or the button.
        self.assertEqual(result["effects"]["tinted_icon_squares"], 2)

    def test_tile_matching_the_body_colour_is_not_tinted(self):
        # White html, amber body, amber 48px tile: the tile matches the page it sits on, so it is
        # not tinted. Measured against the canvas (white) it was counted.
        result = measure(f"{self.base}/tinted-tile-on-amber-body.html", allow_loopback=True)
        self.assertEqual(result["effects"]["tinted_icon_squares"], 0)

    def test_container_is_the_inner_wrapper_and_hidden_text_is_dropped(self):
        result = measure(f"{self.base}/container-sections.html", allow_loopback=True)
        self.assertEqual(result["container_width"], 1100)
        self.assertEqual(result["anchors"]["heading"]["text"], "Ship faster")
        self.assertIsNone(result["anchors"]["card"])  # empty 120px boxes are not cards

    def test_narrow_text_block_does_not_stand_in_for_the_column(self):
        result = measure(f"{self.base}/container-column.html", allow_loopback=True)
        self.assertEqual(result["container_width"], 1120)  # 1200px box less 40px padding a side

    def test_carousel_slides_off_the_edge_are_not_cards(self):
        result = measure(f"{self.base}/carousel.html", allow_loopback=True)
        self.assertEqual(result["anchors"]["card"]["count"], 3)  # five slides, two off screen
        self.assertGreaterEqual(result["anchors"]["card"]["box"]["x"], 0)

    def test_body_copy_in_a_div_in_the_first_screen_is_the_body(self):
        # The div lead is on screen; the <p> below the fold is not the body.
        body = measure(f"{self.base}/body-div-lead.html", allow_loopback=True)["anchors"]["body"]
        self.assertEqual(body["tag"], "div")
        self.assertEqual(
            body["text"], "Our new tools help small teams ship without the usual overhead."
        )

    def test_heading_span_is_never_the_body(self):
        # The span is 40+ characters inside the h1 and comes first in DOM order.
        body = measure(f"{self.base}/body-span-in-heading.html", allow_loopback=True)["anchors"][
            "body"
        ]
        self.assertEqual(body["tag"], "div")
        self.assertTrue(body["text"].startswith("Teams keep the lead copy"))

    def test_body_p_in_the_first_screen_beats_an_earlier_div_with_text(self):
        body = measure(f"{self.base}/body-p-after-div-badge.html", allow_loopback=True)["anchors"][
            "body"
        ]
        self.assertEqual(body["tag"], "p")
        self.assertTrue(body["text"].startswith("Teams keep the paragraph"))

    def test_card_group_in_the_first_screen_beats_a_larger_group_below_the_fold(self):
        card = measure(f"{self.base}/card-grid-below-fold.html", allow_loopback=True)["anchors"][
            "card"
        ]
        self.assertEqual(card["count"], 3)  # the four-card row sits below the fold
        self.assertLessEqual(card["box"]["y"] + card["box"]["height"], 900)

    def test_button_row_is_not_a_card(self):
        result = measure(f"{self.base}/button-row.html", allow_loopback=True)
        self.assertIsNone(result["anchors"]["card"])
        self.assertEqual(result["anchors"]["cta"]["tag"], "button")

    def test_no_max_width_means_no_container(self):
        self.assertIsNone(
            measure(f"{self.base}/plain.html", allow_loopback=True)["container_width"]
        )

    def test_missing_anchor_is_null(self):
        result = measure(f"{self.base}/plain.html", allow_loopback=True)
        self.assertIsNone(result["anchors"]["cta"])
        self.assertIsNone(result["anchors"]["card"])
        self.assertEqual(result["anchors"]["heading"]["font_size"], "32px")

    def test_private_address_refused_without_flag(self):
        with self.assertRaises(MeasureError):
            measure(f"http://127.0.0.1:{self.site.server_address[1]}/")

    def test_redirect_into_private_range_is_aborted(self):
        result = measure(f"{self.base}/redirect.html", allow_loopback=True)
        self.assertIn("http://10.0.0.1/", result["blocked_requests"])

    def test_late_redirect_to_another_host_is_refused_and_leaves_no_screenshot(self):
        # 127.0.0.2 is another loopback host and serves the same port; localhost would be refused
        # by policy before the page could reach it, so it cannot show a host change.
        try:
            other = serve(host="127.0.0.2", port=self.site.server_address[1])
        except OSError as exc:
            self.skipTest(f"cannot bind 127.0.0.2 on this host: {exc}")
        self.addCleanup(other.server_close)
        self.addCleanup(other.shutdown)
        with tempfile.TemporaryDirectory() as tmp:
            shot = Path(tmp) / "shot.png"
            with self.assertRaises(MeasureError):
                measure(f"{self.base}/late-redirect.html", allow_loopback=True, screenshot=shot)
            self.assertFalse(shot.exists())

    def test_websocket_to_private_range_is_refused(self):
        result = measure(f"{self.base}/socket.html", allow_loopback=True)
        # Only the proxy records WebSocket traffic (the route guard never sees it), so this proves
        # the socket went through the proxy. Chromium sends it as an absolute URI or as CONNECT.
        self.assertTrue(any("10.0.0.1:8080" in target for target in result["blocked_requests"]))
        # The proxy answers 403 after recording, so the page's error handler runs after the record.
        self.assertIn("/ws-refused", self.site_hits)

    def test_measured_pages_have_no_webrtc(self):
        # Second WebRTC layer, for channels that ignore the launch switch; checked from a frame too.
        measure(f"{self.base}/rtc.html", allow_loopback=True)
        self.assertIn("/rtc-undefined-undefined", self.site_hits)

    def test_webrtc_sends_no_udp_to_lan(self):
        # The launch-switch layer, on the bundled browser that honours it (installed Chrome does
        # not; NO_WEBRTC covers that, see above). Control first: without the switch the same page
        # must reach the LAN listener, so a silent probe cannot pass the real check.
        lan = lan_address()
        if lan is None:
            self.skipTest("no LAN address on this host")
        from playwright.sync_api import sync_playwright

        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(listener.close)
        listener.bind(("0.0.0.0", 0))
        target = f"{lan}:{listener.getsockname()[1]}"
        with sync_playwright() as p:
            control = p.chromium.launch(chromium_sandbox=True)
            try:
                page_udp(control, target)
            finally:
                control.close()
            control_count = drain(listener)
            egress = EgressProxy(allow_loopback=True)
            self.addCleanup(egress.close)
            with mock.patch.dict(os.environ):
                os.environ.pop(
                    BYPASS_ENV, None
                )  # the worker strips it too: loopback must be proxied
                browser = p.chromium.launch(
                    chromium_sandbox=True,
                    args=ARGS,
                    proxy={"server": f"http://127.0.0.1:{egress.port}"},
                )
            try:
                page_udp(browser, target)
            finally:
                browser.close()
            flagged_count = drain(listener)
        self.assertGreater(control_count, 0, "control saw no UDP: the probe measures nothing")
        self.assertEqual(flagged_count, 0, "WebRTC sent UDP to a LAN host")

    def test_webtransport_never_leaves_while_the_proxy_is_set(self):
        # Control first: unproxied, the same page must reach a UDP listener on loopback, so a
        # silent listener under the proxy means something. The heading proves the script ran and
        # that the WebTransport constructor existed and accepted the call.
        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        url = f"{self.base}/webtransport.html?port={listener.getsockname()[1]}"
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            control = p.chromium.launch(chromium_sandbox=True)
            try:
                tab = control.new_page()
                tab.goto(url, wait_until="load")
                tab.wait_for_timeout(2000)
                self.assertEqual(tab.inner_text("h1"), "WebTransport function, constructed")
            finally:
                control.close()
            control_count = drain(listener)
        result = measure(url, allow_loopback=True)
        self.assertEqual(result["anchors"]["heading"]["text"], "WebTransport function, constructed")
        self.assertGreater(control_count, 0, "control saw no UDP: the probe measures nothing")
        self.assertEqual(drain(listener), 0, "WebTransport sent UDP while the proxy was set")

    def test_cli_measure_writes_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(
                    ["measure", f"{self.base}/plain.html", "--out", tmp, "--allow-loopback"]
                )
            self.assertEqual(code, 0)
            data = json.loads((Path(tmp) / "fingerprint.json").read_text(encoding="utf-8"))
            self.assertTrue(data["captured_with"].startswith("piggyback "))
            self.assertTrue((Path(tmp) / "screenshot.png").exists())

    def test_doctor_reports_sandboxed_browser(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["doctor"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("starts with sandbox", out.getvalue())


if __name__ == "__main__":
    unittest.main()
