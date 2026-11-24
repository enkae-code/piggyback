import contextlib
import io
import os
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
        "allow_loopback": False,
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


class LinkGuardTests(unittest.TestCase):
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


class DebugPortTests(unittest.TestCase):
    def test_every_launch_gets_the_debugging_port_switch(self):
        seen = {}
        chromium = types.SimpleNamespace(launch=lambda **kw: seen.update(kw))
        shim = measure_mod._DebugPort(types.SimpleNamespace(chromium=chromium), 9222)
        shim.chromium.launch(channel=None, chromium_sandbox=True, args=["--a"])
        self.assertEqual(seen["args"], ["--a", "--remote-debugging-port=9222"])


class ReaStageTests(unittest.TestCase):
    def test_a_rea_failure_is_a_failed_stage_and_nothing_is_observed(self):
        req = {"rea": "/bin/rea", "scroll_dir": "/tmp/unused/scroll"}
        with (
            mock.patch("piggyback.tools.rea_version", return_value="6.7.0"),
            mock.patch("piggyback.tools.rea_target", side_effect=tools.ToolError("no target")),
            mock.patch("piggyback.tools.rea_observe") as observe,
        ):
            result = measure_mod._capture_rea(FakePage(lambda *_: None), req, 9222, [0, 300])
        self.assertEqual(result, {"state": "failed", "version": "6.7.0", "reason": "no target"})
        observe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
