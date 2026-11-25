import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import piggyback.measure as measure_mod
from piggyback import tools
from piggyback.cli import main
from piggyback.measure import MeasureError, check_url, measure, receipt_refusal

URL = "https://93.184.216.34/"


class FakePage:
    def __init__(self, on_shot):
        self.url = URL
        self.on_shot = on_shot

    def goto(self, url, **_):
        return types.SimpleNamespace(status=200)

    def evaluate(self, script):
        if "playState" in script:
            return 0
        if "fonts" in script:
            return None
        return {"motion": []}

    def wait_for_load_state(self, *_, **__):
        pass

    def screenshot(self, path, type):
        self.on_shot(self, path)

    def emulate_media(self, **_):
        pass


class FakeBrowser:
    version = "1.0"

    def __init__(self, page):
        self.page = page

    def new_context(self, **_):
        return types.SimpleNamespace(
            route=lambda *_: None,
            add_init_script=lambda *_: None,
            new_page=lambda: self.page,
        )

    def close(self):
        pass


class FakeProxy:
    port = 1
    blocked = []

    def __init__(self, _allow_loopback):
        pass

    def record(self, url):
        pass

    def close(self):
        pass


class FakeSyncPlaywright:
    def __enter__(self):
        return None

    def __exit__(self, *_):
        return False


def run_capture(shot, on_shot):
    """Run the real _capture with Playwright, the proxy and the browser faked."""
    api = types.SimpleNamespace(sync_playwright=FakeSyncPlaywright)
    fake_modules = {"playwright": types.ModuleType("playwright"), "playwright.sync_api": api}
    req = {
        "url": URL,
        "viewport": [1440, 900],
        "loopback": None,
        "screenshot": str(shot),
    }
    browser = FakeBrowser(FakePage(on_shot))
    with (
        mock.patch.dict(sys.modules, fake_modules),
        mock.patch("piggyback.measure.EgressProxy", FakeProxy),
        mock.patch("piggyback.measure.launch", return_value=(browser, "fake")),
    ):
        return measure_mod._capture(req)


class MeasureRefusalTests(unittest.TestCase):
    def test_motion_capture_orders_fingerprint_screenshot_then_reduced_motion(self):
        calls = []

        class FakePage:
            def evaluate(self, script):
                calls.append("count" if "playState" in script else "fingerprint")
                return 2 if "playState" in script else {"motion": []}

            def screenshot(self, **kwargs):
                calls.append("screenshot")

            def emulate_media(self, **kwargs):
                calls.append("emulate")

        result = measure_mod._capture_motion(FakePage(), "capture.png")
        self.assertEqual(calls, ["fingerprint", "screenshot", "emulate", "count"])
        self.assertEqual(result["reduced_motion"], {"still_running": 2})

    def test_hint_only_when_loopback_would_be_allowed(self):
        with self.assertRaises(MeasureError) as ctx:
            measure("http://10.0.0.1/")
        self.assertNotIn("--allow-loopback", str(ctx.exception))

    def test_cli_refuses_before_creating_out_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "new"
            with contextlib.redirect_stderr(io.StringIO()):
                code = main(["measure", "http://10.0.0.1/", "--out", str(out)])
            self.assertEqual(code, 1)
            self.assertFalse(out.exists())

    # These refuse before any browser starts, so they run without Playwright.
    def test_loopback_refused_without_flag(self):
        with self.assertRaisesRegex(MeasureError, "--allow-loopback"):
            measure("http://127.0.0.1:8000/")

    def test_non_http_refused(self):
        for url in ("file:///etc/passwd", "data:text/html,<h1>x</h1>", "http://10.0.0.1/"):
            with self.subTest(url=url), self.assertRaises(MeasureError):
                measure(url)


class ReceiptAndLoopbackTests(unittest.TestCase):
    def test_non_2xx_answer_is_refused_with_status_and_host(self):
        for status in (301, 404, 500):
            with self.subTest(status=status):
                refusal = receipt_refusal(status, "https://ref.example/", "https://ref.example/")
                self.assertEqual(
                    refusal,
                    f"page answered HTTP {status} from ref.example: measure a page that loads "
                    "with 2xx",
                )

    def test_no_http_response_is_refused(self):
        self.assertEqual(
            receipt_refusal(None, "https://ref.example/", "about:blank"),
            "page gave no HTTP response",
        )

    def test_2xx_on_same_host_passes(self):
        self.assertIsNone(receipt_refusal(200, "https://ref.example/a", "https://ref.example/b"))

    def test_redirect_to_another_host_is_refused_by_name(self):
        refusal = receipt_refusal(200, "https://ref.example/", "https://other.example/")
        self.assertIn("https://other.example/", refusal)

    def test_a_redirect_while_the_page_settles_is_refused(self):
        class Moved:
            url = "https://other.example/"

        with self.assertRaisesRegex(MeasureError, "redirected to https://other.example/"):
            measure_mod._refuse_if_moved(200, {"url": "https://ref.example/"}, Moved())

    def test_subdomain_of_localhost_is_refused_even_with_loopback_allowed(self):
        with self.assertRaises(MeasureError):
            check_url("http://app.localhost:3000", allow_loopback=True)

    def test_localhost_is_fetched_as_127_0_0_1(self):
        self.assertEqual(
            check_url("http://localhost:3000/a?b=1", allow_loopback=True),
            "http://127.0.0.1:3000/a?b=1",
        )

    def test_localhost_without_flag_still_hints_loopback(self):
        with self.assertRaisesRegex(MeasureError, "--allow-loopback"):
            check_url("http://localhost:3000/")

    def test_a_name_on_two_local_addresses_gets_the_specific_hint(self):
        two = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 0, 0, 0)),
        ]
        with mock.patch("socket.getaddrinfo", return_value=two):
            with self.assertRaises(MeasureError) as caught:
                check_url("http://svc.example:8123/", allow_loopback=True)
        self.assertEqual(
            str(caught.exception),
            "this name resolves to more than one local address: use 127.0.0.1 or [::1] in the URL",
        )

    def test_one_local_address_on_several_socket_types_gets_the_plain_refusal(self):
        one = [
            (socket.AF_INET, kind, 0, "", ("127.0.0.1", 0))
            for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM, socket.SOCK_RAW)
        ]
        with mock.patch("socket.getaddrinfo", return_value=one):
            with self.assertRaises(MeasureError) as caught:
                check_url("http://svc.local:8123/", allow_loopback=True)
        self.assertNotIn("more than one local address", str(caught.exception))


class LinkGuardTests(unittest.TestCase):
    @unittest.skipUnless(
        os.name == "posix", "POSIX only: fake #! scripts, sleep, signals, symlinks"
    )
    def test_write_measure_refuses_a_linked_fingerprint_before_the_browser_runs(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("piggyback.measure.measure") as m:
            target = Path(tmp) / "external.json"
            target.write_text("EXTERNAL", encoding="utf-8")
            out = Path(tmp) / "out"
            out.mkdir()
            os.symlink(target, out / "fingerprint.json")
            with self.assertRaisesRegex(MeasureError, "symbolic link"):
                measure_mod.write_measure("https://93.184.216.34/", out)
            m.assert_not_called()
            self.assertEqual(target.read_text(encoding="utf-8"), "EXTERNAL")


class ScreenshotPublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)
        self.shot = self.out / "screenshot.png"

    @unittest.skipUnless(
        os.name == "posix", "POSIX only: fake #! scripts, sleep, signals, symlinks"
    )
    def test_a_link_planted_after_the_early_check_is_never_written_through(self):
        external = self.out / "external.png"
        external.write_bytes(b"EXTERNAL")

        def plant_then_write(page, path):
            os.symlink(external, self.shot)
            Path(path).write_bytes(b"PNG")

        run_capture(self.shot, plant_then_write)
        self.assertEqual(external.read_bytes(), b"EXTERNAL")
        self.assertFalse(self.shot.is_symlink())
        self.assertEqual(self.shot.read_bytes(), b"PNG")

    def test_a_hardlinked_screenshot_keeps_its_old_bytes_on_the_other_name(self):
        other = self.out / "other.png"
        other.write_bytes(b"OLD")
        os.link(other, self.shot)

        def write(page, path):
            Path(path).write_bytes(b"PNG")

        run_capture(self.shot, write)
        self.assertEqual(other.read_bytes(), b"OLD")
        self.assertEqual(self.shot.read_bytes(), b"PNG")

    def test_success_leaves_only_the_screenshot(self):
        def write(page, path):
            Path(path).write_bytes(b"PNG")

        run_capture(self.shot, write)
        self.assertEqual([p.name for p in self.out.iterdir()], ["screenshot.png"])

    def test_a_late_refusal_leaves_no_screenshot_and_no_temp(self):
        def move_then_write(page, path):
            Path(path).write_bytes(b"PNG")
            page.url = "https://other.example/"

        with self.assertRaisesRegex(MeasureError, "redirected"):
            run_capture(self.shot, move_then_write)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_a_failed_capture_leaves_no_temp_and_no_screenshot(self):
        def partial_then_fail(page, path):
            Path(path).write_bytes(b"part")
            raise OSError("disk full")

        with self.assertRaises(OSError):
            run_capture(self.shot, partial_then_fail)
        self.assertEqual(list(self.out.iterdir()), [])


@unittest.skipUnless(
    os.name == "posix", "Windows keeps only a read-only bit, so POSIX modes can't be checked"
)
class ModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "fingerprint.json"

    def use_umask(self, mask):
        self.addCleanup(os.umask, os.umask(mask))

    def mode(self):
        return os.stat(self.path).st_mode & 0o777

    def test_a_new_file_under_umask_077_is_0600(self):
        self.use_umask(0o077)
        measure_mod.publish_json(self.path, {})
        self.assertEqual(self.mode(), 0o600)

    def test_an_existing_0600_file_is_replaced_and_stays_0600(self):
        self.use_umask(0)  # a wide umask would make a new file 0666: the old mode must narrow it
        self.path.write_text("{}", encoding="utf-8")
        os.chmod(self.path, 0o600)
        measure_mod.publish_json(self.path, {"new": True})
        self.assertEqual(self.mode(), 0o600)

    def test_a_new_file_under_umask_022_is_0644(self):
        self.use_umask(0o022)
        measure_mod.publish_json(self.path, {})
        self.assertEqual(self.mode(), 0o644)


class AtomicCallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)

    def fail_replace_of(self, name):
        real = os.replace

        def replace(src, dst):
            if Path(dst).name == name:
                raise OSError("disk full")
            real(src, dst)

        return mock.patch("piggyback.measure.os.replace", side_effect=replace)

    def test_write_measure_keeps_the_old_fingerprint_when_the_publish_fails(self):
        (self.out / "fingerprint.json").write_bytes(b'{"old": true}')
        fake = {"url": URL}
        with mock.patch("piggyback.measure.measure", return_value=fake):
            with self.fail_replace_of("fingerprint.json"), self.assertRaises(OSError):
                measure_mod.write_measure(URL, self.out)
        self.assertEqual((self.out / "fingerprint.json").read_bytes(), b'{"old": true}')
        self.assertEqual([p.name for p in self.out.iterdir()], ["fingerprint.json"])


class SingleMeasureErrorTests(unittest.TestCase):
    def test_the_error_text_is_cleaned_before_it_reaches_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            hostile = MeasureError("page says \x1b[2J *bold* <script>")
            with (
                mock.patch("piggyback.measure.measure", side_effect=hostile),
                contextlib.redirect_stderr(err),
            ):
                code = main(["measure", URL, "--out", tmp])
        self.assertEqual(code, 1)
        self.assertNotIn("\x1b", err.getvalue())
        self.assertIn(r"\*bold\*", err.getvalue())


class PublishTests(unittest.TestCase):
    def test_a_failed_publish_keeps_the_old_file_and_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fingerprint.json"
            path.write_text('{"old": true}', encoding="utf-8")
            with mock.patch("piggyback.measure.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    measure_mod.publish_json(path, {"new": True})
            self.assertEqual(path.read_text(encoding="utf-8"), '{"old": true}')
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["fingerprint.json"])


class DenseArrivalTests(unittest.TestCase):
    def test_the_arrival_frame_is_taken_right_after_the_zero_sample(self):
        events = []

        class Page:
            def evaluate(self, script, *_):
                events.append("sample")
                return {"scroll_y": 0}

            def wait_for_timeout(self, ms):
                time.sleep(ms / 1000)

            def screenshot(self, path, type):
                events.append("shot")

        measure_mod._dense_samples(Page(), time.monotonic(), "arrival.tmp")
        self.assertEqual(events, ["sample", "shot", "sample", "sample", "sample"])


REA_REQ = {
    "rea": "/bin/rea",
    "url": "http://127.0.0.1:8123/kit-motion.html",
    "viewport": (100, 100),
    "scroll_dir": "/tmp/unused/scroll",
    "loopback": ["127.0.0.1", 8123],
}


def rea_tools(target="T1", observed=b"{}"):
    """Patches that stand in for the tools and the browser checks of one REA pass."""
    return [
        mock.patch.object(
            tools, "devtools_endpoint", return_value=("http://127.0.0.1:9", "/devtools/browser/x")
        ),
        mock.patch.object(tools, "verify_browser"),
        mock.patch.object(tools, "rea_version", return_value="6.7.0"),
        mock.patch.object(
            tools,
            "rea_target",
            return_value=target,
            side_effect=None if target else tools.ToolError("no target"),
        ),
        mock.patch.object(tools, "rea_observe", return_value=observed),
        mock.patch.object(measure_mod, "_refuse_if_moved"),
        mock.patch.object(measure_mod, "_document_origin", return_value="origin"),
        mock.patch.object(measure_mod, "_scroll_to"),
        mock.patch.object(measure_mod, "_refuse_if_replaced"),
    ]


def rea_context(page):
    ctx = mock.Mock()
    ctx.new_page.return_value = page
    return ctx


def rea_page():
    page = mock.Mock(url=REA_REQ["url"])
    page.goto.return_value = types.SimpleNamespace(status=200)
    page.evaluate.return_value = 900
    return page


class ReaPassTests(unittest.TestCase):
    def run_pass(self, patches, ctx):
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            stack.enter_context(mock.patch.object(measure_mod, "_launch_rea", return_value=ctx))
            return measure_mod._rea_pass(mock.Mock(), REA_REQ, mock.Mock(port=1, blocked=[]))

    def test_a_missing_target_is_a_failed_stage_and_nothing_is_observed(self):
        patches = rea_tools(target=None)
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            stack.enter_context(
                mock.patch.object(measure_mod, "_launch_rea", return_value=rea_context(rea_page()))
            )
            observe = tools.rea_observe
            result = measure_mod._rea_pass(mock.Mock(), REA_REQ, mock.Mock(port=1, blocked=[]))
            observe.assert_not_called()
        self.assertEqual(
            result, {"state": "failed", "version": "6.7.0", "reason": "no target", "blocked": 0}
        )

    def test_a_replaced_rea_page_refuses_the_kit_and_publishes_nothing(self):
        patches = rea_tools()
        checks = [None, measure_mod.MeasureError("the scroll page navigated")]
        patches.append(mock.patch.object(measure_mod, "_refuse_if_replaced", side_effect=checks))
        publish = mock.patch.object(tools, "publish_rea")
        with publish as published:
            with self.assertRaises(measure_mod.MeasureError):
                self.run_pass([*patches], rea_context(rea_page()))
        published.assert_not_called()

    def test_a_captured_pass_publishes_the_observation_under_the_rea_stage(self):
        patches = rea_tools(observed=b'{"normalized_result": {}}')
        patches.append(mock.patch.object(measure_mod, "_refuse_if_replaced"))
        with mock.patch.object(tools, "publish_rea") as published:
            result = self.run_pass([*patches], rea_context(rea_page()))
        self.assertEqual(result, {"state": "captured", "version": "6.7.0", "blocked": 0})
        published.assert_called_once()


class ReaBrowserTests(unittest.TestCase):
    def test_the_rea_browser_has_a_loopback_debugging_port_and_the_capture_sandbox(self):
        ctx = mock.Mock()
        p = mock.Mock()
        p.chromium.launch_persistent_context.return_value = ctx
        measure_mod._launch_rea(
            p, Path("/tmp/profile"), mock.Mock(port=1, blocked=[]), REA_REQ, time.monotonic() + 60
        )
        kwargs = p.chromium.launch_persistent_context.call_args.kwargs
        self.assertTrue(kwargs["chromium_sandbox"])
        self.assertIn("--remote-debugging-port=0", kwargs["args"])
        self.assertIn("--remote-debugging-address=127.0.0.1", kwargs["args"])
        self.assertNotIn("--no-sandbox", kwargs["args"])
        ctx.add_init_script.assert_called_once()

    def test_a_rea_browser_that_cannot_start_is_a_stage_error(self):
        p = mock.Mock()
        p.chromium.launch_persistent_context.side_effect = RuntimeError("no browser")
        with self.assertRaises(tools.ToolError) as ctx:
            measure_mod._launch_rea(
                p,
                Path("/tmp/profile"),
                mock.Mock(port=1, blocked=[]),
                REA_REQ,
                time.monotonic() + 60,
            )
        self.assertEqual(ctx.exception.reason, "browser unavailable")


class ReaWindowTests(unittest.TestCase):
    def test_the_observation_window_covers_the_slowest_wheel_pass(self):
        worst = measure_mod.REA_ARM_MS + measure_mod.SCROLL_STEPS * (
            measure_mod.SCROLL_ARRIVE_MS + measure_mod.REA_STEP_MS
        )
        self.assertGreaterEqual(measure_mod.REA_WINDOW_MS, worst)
        self.assertLess(measure_mod.REA_STAGE_S * 1000, measure_mod.SCROLL_TOTAL_S * 1000)


class FakeClock:
    """time.monotonic stand-in: advances only when a fake browser call spends time."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class ReaDeadlineTests(unittest.TestCase):
    """MAJOR 2: the REA stage has one deadline, started before the browser launch, and every
    browser or tool call gets the time left as its timeout."""

    def setUp(self):
        self.clock = FakeClock()
        patcher = mock.patch("piggyback.measure.time.monotonic", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def page(self, goto_cost=0.0):
        page = mock.Mock(url=REA_REQ["url"])

        def goto(url, *, wait_until, timeout):
            self.clock.now += min(goto_cost, timeout / 1000)  # spends what it takes, no more
            if goto_cost >= timeout / 1000:
                raise RuntimeError("Timeout")
            return types.SimpleNamespace(status=200)

        page.goto.side_effect = goto
        page.evaluate.return_value = 900
        return page

    def launcher(self, cost):
        def launch(profile, *, timeout, **_):
            self.clock.now += min(timeout / 1000, cost)
            if cost >= timeout / 1000:
                raise RuntimeError("Timeout")
            return rea_context(self.page())

        p = mock.Mock()
        p.chromium.launch_persistent_context.side_effect = launch
        return p

    def run_pass(self, p, observe=None, scroll=None):
        egress = mock.Mock(port=1, blocked=[])
        patches = [
            mock.patch.object(
                tools,
                "devtools_endpoint",
                return_value=("http://127.0.0.1:9", "/devtools/browser/x"),
            ),
            mock.patch.object(tools, "verify_browser"),
            mock.patch.object(tools, "rea_version", return_value="6.7.0"),
            mock.patch.object(tools, "rea_target", return_value="T1"),
            mock.patch.object(tools, "rea_observe", side_effect=observe or (lambda *a, **k: b"{}")),
            mock.patch.object(tools, "publish_rea"),
            mock.patch.object(measure_mod, "_refuse_if_moved"),
            mock.patch.object(measure_mod, "_document_origin", return_value="origin"),
            mock.patch.object(measure_mod, "_refuse_if_replaced"),
            mock.patch.object(measure_mod, "_scroll_to", side_effect=scroll),
        ]
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            return measure_mod._rea_pass(p, REA_REQ, egress)

    def test_the_launch_and_navigation_get_the_remaining_budget_as_timeouts(self):
        p = self.launcher(cost=1.0)
        result = self.run_pass(p)
        self.assertEqual(result["state"], "captured")
        launch_timeout = p.chromium.launch_persistent_context.call_args.kwargs["timeout"]
        self.assertLessEqual(launch_timeout, measure_mod.REA_STAGE_S * 1000)
        self.assertGreater(launch_timeout, 0)

    def test_a_slow_launch_fails_the_stage_as_a_timeout_within_its_budget(self):
        started = self.clock.now
        result = self.run_pass(self.launcher(cost=measure_mod.REA_STAGE_S + 30))
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["reason"], "timeout")
        self.assertLessEqual(self.clock.now - started, measure_mod.REA_STAGE_S + 1)

    def test_a_slow_navigation_fails_the_stage_as_a_timeout(self):
        started = self.clock.now
        p = mock.Mock()
        p.chromium.launch_persistent_context.side_effect = lambda *_, **__: rea_context(
            self.page(goto_cost=measure_mod.REA_STAGE_S + 30)
        )
        result = self.run_pass(p)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["reason"], "timeout")
        self.assertLessEqual(self.clock.now - started, measure_mod.REA_STAGE_S + 1)

    def test_the_wheel_pass_stops_when_the_deadline_has_passed(self):
        moves = []

        def slow_scroll(page, delta):
            moves.append(delta)
            self.clock.now += measure_mod.REA_STAGE_S / 2

        result = self.run_pass(self.launcher(cost=0.0), scroll=slow_scroll)
        self.assertEqual(
            result, {"state": "failed", "version": "6.7.0", "reason": "timeout", "blocked": 0}
        )
        self.assertLess(len(moves), measure_mod.SCROLL_STEPS)

    def test_a_launch_that_fails_after_the_deadline_is_a_timeout_not_another_channel(self):
        p = mock.Mock()

        def fail_late(*_, **kwargs):
            self.clock.now += measure_mod.REA_STAGE_S + 5
            raise RuntimeError("could not start")

        p.chromium.launch_persistent_context.side_effect = fail_late
        result = self.run_pass(p)
        self.assertEqual(result["reason"], "timeout")
        self.assertEqual(p.chromium.launch_persistent_context.call_count, 1)


class ReaRouteAndBlockedTests(unittest.TestCase):
    def test_the_rea_browser_gets_the_same_request_guard_as_the_capture(self):
        ctx = mock.Mock()
        p = mock.Mock()
        p.chromium.launch_persistent_context.return_value = ctx
        egress = mock.Mock(port=1, blocked=[])
        measure_mod._launch_rea(p, Path("/tmp/profile"), egress, REA_REQ, time.monotonic() + 60)
        ctx.route.assert_called_once()
        self.assertEqual(ctx.route.call_args.args[0], "**/*")

    def test_the_rea_result_counts_only_the_requests_blocked_during_the_pass(self):
        egress = mock.Mock(port=1, blocked=["http://earlier.example/"])

        def observe(*_, **__):
            egress.blocked.extend(["http://10.0.0.1/a", "http://10.0.0.1/b"])
            return b"{}"

        with (
            mock.patch.object(tools, "verify_browser"),
            mock.patch.object(
                tools,
                "devtools_endpoint",
                return_value=("http://127.0.0.1:9", "/devtools/browser/x"),
            ),
            mock.patch.object(tools, "rea_version", return_value="6.7.0"),
            mock.patch.object(tools, "rea_target", return_value="T1"),
            mock.patch.object(tools, "rea_observe", side_effect=observe),
            mock.patch.object(tools, "publish_rea"),
            mock.patch.object(measure_mod, "_refuse_if_moved"),
            mock.patch.object(measure_mod, "_document_origin", return_value="o"),
            mock.patch.object(measure_mod, "_refuse_if_replaced"),
            mock.patch.object(measure_mod, "_scroll_to"),
        ):
            p = mock.Mock()
            p.chromium.launch_persistent_context.return_value = rea_context(rea_page())
            result = measure_mod._rea_pass(p, REA_REQ, egress)
        self.assertEqual(result["blocked"], 2)


class ReaVerifyTests(unittest.TestCase):
    def test_the_observed_browser_is_verified_before_rea_is_called(self):
        with (
            mock.patch.object(
                tools,
                "devtools_endpoint",
                return_value=("http://127.0.0.1:9222", "/devtools/browser/x"),
            ),
            mock.patch.object(tools, "verify_browser") as verify,
            mock.patch.object(tools, "rea_version", return_value="6.7.0"),
            mock.patch.object(tools, "rea_target", side_effect=tools.ToolError("no target")),
            mock.patch.object(measure_mod, "_refuse_if_moved"),
            mock.patch.object(measure_mod, "_document_origin", return_value="o"),
            mock.patch.object(measure_mod, "_refuse_if_replaced"),
            mock.patch.object(measure_mod, "_scroll_to"),
        ):
            p = mock.Mock()
            p.chromium.launch_persistent_context.return_value = rea_context(rea_page())
            measure_mod._rea_pass(p, REA_REQ, mock.Mock(port=1, blocked=[]))
        verify.assert_called_once_with("http://127.0.0.1:9222", "/devtools/browser/x", mock.ANY)


class ReaBranchTests(unittest.TestCase):
    def test_the_rea_profile_is_removed_on_every_path(self):
        profile = Path(tempfile.mkdtemp(prefix="test-rea-profile-"))
        (profile / "DevToolsActivePort").write_text("9\n/devtools/browser/x\n", encoding="utf-8")
        p = mock.Mock()
        p.chromium.launch_persistent_context.side_effect = RuntimeError("no browser")
        with mock.patch.object(measure_mod.tempfile, "mkdtemp", return_value=str(profile)):
            result = measure_mod._rea_pass(p, REA_REQ, mock.Mock(port=1, blocked=[]))
        self.assertEqual(result["state"], "failed")
        self.assertFalse(profile.exists())

    def test_an_observer_that_outlives_the_budget_is_a_timeout(self):
        """Checked on _rea_observe itself: _rea_pass would map any later error to a timeout too."""

        def hang(*_, **__):
            time.sleep(10)
            return b"{}"

        with mock.patch.object(measure_mod, "REA_STAGE_S", 1):
            deadline = time.monotonic() + measure_mod.REA_STAGE_S
            started = time.monotonic()
            with (
                mock.patch.object(
                    tools,
                    "devtools_endpoint",
                    return_value=("http://127.0.0.1:9", "/devtools/browser/x"),
                ),
                mock.patch.object(tools, "verify_browser"),
                mock.patch.object(tools, "rea_version", return_value="6.7.0"),
                mock.patch.object(tools, "rea_target", return_value="T1"),
                mock.patch.object(tools, "rea_observe", side_effect=hang),
                mock.patch.object(measure_mod, "_refuse_if_moved"),
                mock.patch.object(measure_mod, "_document_origin", return_value="o"),
                mock.patch.object(measure_mod, "_refuse_if_replaced"),
                mock.patch.object(measure_mod, "_scroll_to"),
                self.assertRaises(tools.ToolError) as ctx,
            ):
                measure_mod._rea_observe(
                    rea_context(rea_page()), REA_REQ, Path("/tmp/unused"), {}, deadline
                )
            elapsed = time.monotonic() - started
        self.assertEqual(ctx.exception.reason, "timeout")
        self.assertLess(elapsed, 8)


SLACK_S = 0.05  # fake clock: the only overrun allowed is ceil to 1 ms per call; 50 ms is the margin


class ReaBudgetTests(unittest.TestCase):
    """One absolute deadline for the REA stage, started before the launch. Every fake call spends
    time like the real one: Playwright's timeout 0 means no limit, so a call sent 0 runs its full
    cost."""

    def setUp(self):
        self.clock = FakeClock()
        for patcher in (
            mock.patch("piggyback.measure.time.monotonic", self.clock),
            mock.patch("piggyback.tools.time.sleep", side_effect=self.advance),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def advance(self, seconds):
        self.clock.now += seconds

    def spend(self, cost, timeout_ms):
        if timeout_ms == 0:
            self.clock.now += cost
            return
        limit = timeout_ms / 1000
        self.clock.now += min(cost, limit)
        if cost >= limit:
            raise RuntimeError("Timeout")

    def wait(self, ms):
        self.clock.now += ms / 1000

    def page(self, goto_cost=0.0):
        page = rea_page()

        def goto(url, *, wait_until, timeout):
            self.spend(goto_cost, timeout)
            return types.SimpleNamespace(status=200)

        page.goto.side_effect = goto
        page.wait_for_timeout.side_effect = self.wait
        return page

    def rea(self, page, launch_cost=0.0, devtools=True, real_verify=False, close_cost=0.0):
        """One REA pass. Returns (result, publish_rea mock)."""

        def launch(profile, *, timeout, **_):
            if devtools:
                (Path(profile) / "DevToolsActivePort").write_text(
                    "9\n/devtools/browser/x\n", encoding="utf-8"
                )
            self.spend(launch_cost, timeout)
            ctx = rea_context(page)
            ctx.close.side_effect = lambda: self.spend(close_cost, 0)
            return ctx

        p = mock.Mock()
        p.chromium.launch_persistent_context.side_effect = launch
        patches = [
            mock.patch.object(tools, "rea_version", return_value="6.7.0"),
            mock.patch.object(tools, "rea_target", return_value="T1"),
            mock.patch.object(tools, "rea_observe", return_value=b"{}"),
            mock.patch.object(tools, "publish_rea"),
            mock.patch.object(measure_mod, "_refuse_if_moved"),
            mock.patch.object(measure_mod, "_document_origin", return_value="origin"),
            mock.patch.object(measure_mod, "_refuse_if_replaced"),
            mock.patch.object(measure_mod, "_scroll_to"),
        ]
        if not real_verify:
            patches.append(mock.patch.object(tools, "verify_browser"))
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            publish = tools.publish_rea
            result = measure_mod._rea_pass(p, REA_REQ, mock.Mock(port=1, blocked=[]))
        return result, publish

    def assert_budget(self, result, started, budget=None):
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["reason"], "timeout")
        self.assertLessEqual(
            self.clock.now - started, (budget or measure_mod.REA_STAGE_S) + SLACK_S
        )

    def test_a_sub_millisecond_navigation_budget_is_never_sent_as_no_timeout(self):
        started = self.clock.now
        page = self.page(goto_cost=1000)  # would run unbounded if sent timeout=0
        result, _ = self.rea(page, launch_cost=measure_mod.REA_STAGE_S - 0.0004)
        self.assertGreaterEqual(page.goto.call_args.kwargs["timeout"], 1)
        self.assert_budget(result, started)

    def test_devtools_discovery_shares_the_stage_deadline(self):
        started = self.clock.now
        result, _ = self.rea(self.page(), launch_cost=38.5, devtools=False)
        self.assert_budget(result, started)

    def test_verify_urlopen_is_capped_at_the_time_left(self):
        started = self.clock.now

        def hang(url, timeout):
            self.clock.now += timeout  # the listener never answers: the call lasts its timeout
            raise OSError("timed out")

        opener = mock.Mock()
        opener.open.side_effect = hang
        with mock.patch("piggyback.tools.urllib.request.build_opener", return_value=opener):
            result, _ = self.rea(self.page(), launch_cost=38.5, real_verify=True)
        self.assert_budget(result, started)

    def test_a_close_that_runs_past_the_deadline_publishes_nothing(self):
        # Playwright's close has no timeout of its own: only the worker's timeout bounds it.
        result, publish = self.rea(self.page(), close_cost=measure_mod.REA_STAGE_S + 0.5)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["reason"], "timeout")
        publish.assert_not_called()

    def test_the_last_wheel_step_cannot_publish_after_the_deadline(self):
        pre = (
            measure_mod.SCROLL_WAIT_MS
            + measure_mod.REA_ARM_MS
            + (measure_mod.SCROLL_STEPS - 1) * measure_mod.REA_STEP_MS
        ) / 1000
        budget = pre + measure_mod.REA_STEP_MS / 2000  # expires halfway through the last wait
        started = self.clock.now
        with mock.patch.object(measure_mod, "REA_STAGE_S", budget):
            result, publish = self.rea(self.page())
        self.assert_budget(result, started, budget)
        publish.assert_not_called()


class WorkerSignalTests(unittest.TestCase):
    def test_the_worker_installs_a_sigterm_handler_before_it_captures(self):
        with (
            mock.patch.object(sys, "stdin", io.StringIO("{}")),
            mock.patch.object(measure_mod, "_capture", return_value={}),
            mock.patch.object(measure_mod.signal, "signal") as install,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            measure_mod._worker()
        install.assert_called_once_with(measure_mod.signal.SIGTERM, mock.ANY)


CAPTURE = {"scroll": {"steps": []}}
CAPTURED = {"state": "captured", "version": "6.7.0", "blocked": 0}


class ReaWorkerTests(unittest.TestCase):
    """REA runs in its own worker after the capture's: a timeout is the REA stage, a refusal is
    the kit's."""

    def measure_with(self, *outcomes):
        calls = []

        def run_worker(request, timeout=None):
            calls.append(request)
            outcome = outcomes[len(calls) - 1]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with (
            mock.patch.object(measure_mod, "_run_worker", side_effect=run_worker),
            mock.patch.object(measure_mod, "clean_scroll", side_effect=lambda raw: raw),
        ):
            result = measure_mod.measure(
                "http://127.0.0.1:1/",
                allow_loopback=True,
                scroll_dir="scroll",
                rea_binary="rea",
            )
        return result, calls

    def test_a_worker_refusal_keeps_its_kind_on_both_paths(self):
        refusal = {"error": "/home/x/build returned 404", "refused": True, "kind": "load"}
        for outcomes in ((refusal,), (CAPTURE, refusal)):
            with self.assertRaises(MeasureError) as caught:
                self.measure_with(*outcomes)
            self.assertEqual(caught.exception.kind, "load")

    def test_the_worker_envelope_carries_the_refusal_kind(self):
        out = io.StringIO()
        with (
            mock.patch.object(sys, "stdin", io.StringIO("{}")),
            mock.patch.object(sys, "stdout", out),
            mock.patch.object(measure_mod.signal, "signal"),
            mock.patch.object(
                measure_mod, "_capture", side_effect=MeasureError("404", kind="load")
            ),
        ):
            self.assertEqual(measure_mod._worker(), 1)
        self.assertEqual(json.loads(out.getvalue())["kind"], "load")

    def test_the_rea_pass_runs_in_its_own_worker_after_the_capture(self):
        result, calls = self.measure_with(CAPTURE, {"rea": CAPTURED})
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["mode"], "rea")
        self.assertEqual(result["rea"], CAPTURED)

    def test_a_timed_out_rea_worker_fails_the_rea_stage_and_keeps_the_capture(self):
        result, _ = self.measure_with(
            CAPTURE, measure_mod.MeasureError("measure timed out after 45s")
        )
        self.assertEqual(result["rea"], {"state": "failed", "reason": "timeout"})
        self.assertEqual(result["scroll"], CAPTURE["scroll"])

    def test_a_refusal_from_the_rea_worker_refuses_the_kit(self):
        with self.assertRaises(measure_mod.MeasureError):
            self.measure_with(CAPTURE, {"error": "page moved", "refused": True})

    def test_a_setup_error_in_the_rea_worker_keeps_the_capture(self):
        result, _ = self.measure_with(CAPTURE, {"error": "proxy could not bind"})
        self.assertEqual(result["rea"], {"state": "failed", "reason": "page error"})
        self.assertEqual(result["scroll"], CAPTURE["scroll"])


class WorkerRefusalTests(unittest.TestCase):
    """The worker says a refusal is a refusal; any other failure carries no such mark."""

    def worker_output(self, error):
        out = io.StringIO()
        with (
            mock.patch.object(sys, "stdin", io.StringIO('{"mode": "rea"}')),
            mock.patch.object(measure_mod, "_capture_rea", side_effect=error),
            mock.patch.object(measure_mod.signal, "signal"),
            contextlib.redirect_stdout(out),
        ):
            measure_mod._worker()
        return json.loads(out.getvalue())

    def test_a_page_refusal_is_marked_refused_and_keeps_its_status(self):
        reply = self.worker_output(
            measure_mod.MeasureError("page moved", status=301, final_url="http://x/")
        )
        self.assertIs(reply["refused"], True)
        self.assertEqual(reply["status"], 301)

    def test_a_setup_failure_is_not_marked_refused(self):
        reply = self.worker_output(RuntimeError("proxy could not bind"))
        self.assertNotIn("refused", reply)
        self.assertIn("error", reply)


class KillTreeTests(unittest.TestCase):
    def kill_on_windows(self, run_effect=None):
        proc = mock.Mock(pid=4242)
        with (
            mock.patch.object(measure_mod.os, "name", "nt"),
            mock.patch.object(measure_mod.subprocess, "run", side_effect=run_effect) as run,
        ):
            measure_mod.kill_tree(proc)
        return proc, run

    def test_a_failing_taskkill_still_kills_the_worker(self):
        proc, _ = self.kill_on_windows(FileNotFoundError("taskkill"))
        proc.kill.assert_called_once_with()

    def test_a_hung_taskkill_is_stopped_and_the_worker_is_still_killed(self):
        proc, _ = self.kill_on_windows(subprocess.TimeoutExpired("taskkill", 10))
        proc.kill.assert_called_once_with()

    def test_taskkill_has_a_timeout(self):
        _, run = self.kill_on_windows()
        self.assertLessEqual(run.call_args.kwargs["timeout"], 10)

    @unittest.skipUnless(os.name == "posix", "killpg is POSIX")
    def test_a_group_whose_only_member_is_a_zombie_leader_is_not_an_error(self):
        # macOS answers EPERM, not ESRCH, for a group left with only an unreaped leader.
        with mock.patch.object(
            measure_mod.os, "killpg", side_effect=PermissionError(1, "Operation not permitted")
        ):
            measure_mod.kill_tree(mock.Mock(pid=4242))


FAKE_WORKER = """import signal, subprocess, sys, time
sys.path.insert(0, {repo!r})
from piggyback import tools


def stop(_signum, _frame):
    raise SystemExit(1)


signal.signal(signal.SIGTERM, stop)
proc = tools._spawn(["sleep", "30"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
open({pid!r}, "w").write(str(proc.pid))
try:
    time.sleep(30)
finally:
    tools._reap(proc)
"""


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@unittest.skipUnless(
    os.name == "posix", "the fake worker is a #! script and the tool is a POSIX process"
)
class WorkerTimeoutTests(unittest.TestCase):
    def test_a_timed_out_worker_leaves_no_tool_running(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        pid_file = root / "tool.pid"
        script = root / "fake-worker"
        body = FAKE_WORKER.format(repo=str(Path(__file__).resolve().parents[1]), pid=str(pid_file))
        script.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
        script.chmod(0o755)
        self.addCleanup(lambda: pid_file.exists() and _kill_quietly(int(pid_file.read_text())))
        with mock.patch.object(sys, "executable", str(script)):
            started = time.monotonic()
            with self.assertRaises(measure_mod.MeasureError):
                measure_mod._run_worker({"scroll_dir": None}, timeout=2)
            elapsed = time.monotonic() - started
        self.assertFalse(alive(int(pid_file.read_text())))
        self.assertLess(elapsed, 2 + measure_mod.WORKER_GRACE_S + 3)

    def test_the_timeout_path_needs_no_os_waitid(self):
        # macOS Python before 3.13 has no os.waitid; take it away everywhere else too.
        patcher = mock.patch.dict(os.__dict__)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.__dict__.pop("waitid", None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        pid_file = Path(tmp.name) / "tool.pid"
        script = Path(tmp.name) / "fake-worker"
        body = FAKE_WORKER.format(repo=str(Path(__file__).resolve().parents[1]), pid=str(pid_file))
        script.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
        script.chmod(0o755)
        self.addCleanup(lambda: pid_file.exists() and _kill_quietly(int(pid_file.read_text())))
        with mock.patch.object(sys, "executable", str(script)):
            with self.assertRaises(measure_mod.MeasureError):
                measure_mod._run_worker({"scroll_dir": None}, timeout=2)
        self.assertTrue(_gone(int(pid_file.read_text())))

    def test_a_worker_that_exited_while_a_grandchild_holds_stderr_is_killed_with_it(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        pid_file = Path(tmp.name) / "grandchild.pid"
        script = Path(tmp.name) / "fake-worker"
        body = GRANDCHILD.format(pid=str(pid_file))
        script.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
        script.chmod(0o755)
        self.addCleanup(lambda: pid_file.exists() and _kill_quietly(int(pid_file.read_text())))
        with mock.patch.object(sys, "executable", str(script)):
            started = time.monotonic()
            with self.assertRaises(measure_mod.MeasureError):
                measure_mod._run_worker({"scroll_dir": None}, timeout=2)
            elapsed = time.monotonic() - started
        self.assertTrue(_gone(int(pid_file.read_text())))
        # The grandchild sleeps 30 s: only the kill keeps this under the bound.
        self.assertLess(elapsed, 2 + measure_mod.WORKER_GRACE_S + 3)


GRANDCHILD = """import subprocess, sys
child = subprocess.Popen(["sleep", "30"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
open({pid!r}, "w").write(str(child.pid))
sys.exit(0)
"""


def _gone(pid):
    """True once pid is no longer running: gone, or a zombie nobody will reap."""
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return not state.stdout.strip() or state.stdout.strip().startswith("Z")


def _kill_quietly(pid):
    with contextlib.suppress(OSError):
        os.kill(pid, 9)


if __name__ == "__main__":
    unittest.main()
