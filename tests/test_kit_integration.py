import contextlib
import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

if importlib.util.find_spec("playwright") is None:
    raise unittest.SkipTest("playwright not installed; run with the measure extra")

from piggyback import kit, tools  # noqa: E402
from piggyback.measure import MeasureError, write_measure  # noqa: E402
from tests.test_measure_integration import serve  # noqa: E402


def browser_processes(root):
    """Command lines of Chromium browser processes (no renderers or helpers) under pid root."""
    procs = {}
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            with contextlib.suppress(OSError):
                ppid = int((entry / "stat").read_text().rsplit(")", 1)[1].split()[1])
                procs[int(entry.name)] = (ppid, (entry / "cmdline").read_bytes())
    seen, frontier = set(), {root}
    while frontier:
        frontier = {pid for pid, (ppid, _) in procs.items() if ppid in frontier} - seen
        seen |= frontier
    return [
        procs[pid][1]
        for pid in seen
        if pid in procs and b"chrom" in procs[pid][1].lower() and b"--type=" not in procs[pid][1]
    ]


MARKER = "kit-marker-7f3a9c"  # in both fixtures' visible text and an HTML comment


class KitScrollIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = serve()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def capture(self, name):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "kit"
        write_measure(f"{self.base}/{name}", out, allow_loopback=True, scroll=True)
        samples = json.loads((out / "scroll-samples.json").read_text(encoding="utf-8"))
        fingerprint = json.loads((out / "fingerprint.json").read_text(encoding="utf-8"))
        self.assertEqual(fingerprint["screenshot"], "screenshot.png")  # no local path in the kit
        return out, samples

    def test_motion_page_gives_six_frames_and_the_known_animation(self):
        out, s = self.capture("kit-motion.html")
        self.assertEqual(s["coverage"], {"scroll": "done"})
        self.assertEqual([step["step"] for step in s["steps"]], list(range(6)))
        for i in range(6):
            self.assertTrue((out / "scroll" / f"frame-{i}.png").read_bytes().startswith(b"\x89PNG"))
        # The scroll page uses normal motion: a leaked reduced-motion emulation would drop it.
        animations = [a for a in s["steps"][0]["animations"] if a["kind"] == "animation"]
        self.assertTrue(any(a["duration"] == 1600 and a["delay"] == 200 for a in animations))

    def test_sticky_element_top_is_constant_across_steps(self):
        _, s = self.capture("kit-motion.html")
        tops = [[e["box"][1] for e in step["elements"] if e["tag"] == "nav"] for step in s["steps"]]
        seen = [t[0] for t in tops if t]
        self.assertGreaterEqual(len(seen), 2)
        self.assertEqual(len(set(seen)), 1)

    def test_scroll_triggered_element_fades_in_by_900_ms(self):
        _, s = self.capture("kit-motion.html")
        steps = s["steps"]
        entry = next(i for i, step in enumerate(steps) if step["scroll_y"] > 300)

        def aside(sample):
            return next(e for e in sample["elements"] if e["tag"] == "aside")

        dense = steps[entry]["dense"]
        self.assertLess(aside(dense[0])["opacity"], 1)
        self.assertGreaterEqual(aside(dense[-1])["opacity"], 0.99)

    def test_dense_samples_record_monotonic_actual_times(self):
        _, s = self.capture("kit-motion.html")
        for step in s["steps"]:
            actual = [d["actual_ms"] for d in step["dense"]]
            self.assertEqual(actual, sorted(actual))
            for sample in step["dense"]:
                self.assertGreaterEqual(sample["actual_ms"], sample["due_ms"])

    def test_static_page_has_no_animation_and_no_transform_or_opacity_change(self):
        _, s = self.capture("kit-static.html")
        self.assertEqual(s["coverage"], {"scroll": "done"})
        for step in s["steps"]:
            self.assertEqual(step["animations"], [])
        values = {}
        for step in s["steps"]:
            for e in step["elements"]:
                values.setdefault(e["index"], set()).add((json.dumps(e["transform"]), e["opacity"]))
        self.assertTrue(values)
        self.assertTrue(all(len(v) == 1 for v in values.values()))

    def test_a_host_change_during_scroll_refuses_the_whole_kit(self):
        # 127.0.0.2 is another loopback host on the same port: the page moves there on scroll.
        try:
            other = serve(host="127.0.0.2", port=self.server.server_address[1])
        except OSError as exc:
            self.skipTest(f"cannot bind 127.0.0.2 on this host: {exc}")
        self.addCleanup(other.server_close)
        self.addCleanup(other.shutdown)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "kit"
        with self.assertRaisesRegex(MeasureError, "navigated during its capture"):
            write_measure(
                f"{self.base}/kit-redirect-on-scroll.html", out, allow_loopback=True, scroll=True
            )
        self.assertFalse((out / "scroll-samples.json").exists())
        self.assertEqual(list((out / "scroll").glob("frame-*.png")), [])

    def test_same_document_url_changes_during_scroll_do_not_refuse(self):
        out, s = self.capture("kit-replacestate-on-scroll.html")
        self.assertEqual([step["step"] for step in s["steps"]], list(range(6)))
        self.assertEqual(len(list(out.glob("scroll/frame-?.png"))), 6)

    def test_a_slow_same_host_replacement_during_scroll_refuses_and_publishes_nothing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "kit"
        with self.assertRaisesRegex(MeasureError, "navigated during its capture"):
            write_measure(
                f"{self.base}/kit-slow-replace-on-scroll.html",
                out,
                allow_loopback=True,
                scroll=True,
            )
        self.assertFalse((out / "scroll-samples.json").exists())
        self.assertEqual(list((out / "scroll").glob("frame-*.png")), [])

    def test_a_slow_same_host_replacement_during_settle_refuses_and_publishes_nothing(self):
        # The fixture moves only on its second load (the scroll page), 500 ms in: during settle.
        # Chromium holds the capture until that document loads, so its DOMContentLoaded also refuses
        # it; this pins the outcome, and the early timeOrigin read guards engines that do not wait.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "kit"
        with self.assertRaisesRegex(MeasureError, "navigated during its capture"):
            write_measure(
                f"{self.base}/kit-slow-replace-during-settle.html",
                out,
                allow_loopback=True,
                scroll=True,
            )
        self.assertFalse((out / "scroll-samples.json").exists())
        self.assertEqual(list((out / "scroll").glob("frame-*.png")), [])

    def test_a_denied_request_during_scroll_is_recorded_as_blocked(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "kit"
        write_measure(
            f"{self.base}/kit-denied-on-scroll.html", out, allow_loopback=True, scroll=True
        )
        fingerprint = json.loads((out / "fingerprint.json").read_text(encoding="utf-8"))
        self.assertIn("http://10.0.0.1/kit-probe.png", fingerprint["blocked_requests"])

    def test_craft_marks_the_stop_where_the_aside_animates_in(self):
        if tools.find("PIGGYBACK_CRAFT", "craft-anything-cli") is None:
            self.skipTest("craft-anything-cli is not installed")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "kit"
        kit.make(f"{self.base}/kit-motion.html", out, "a landing page", allow_loopback=True)
        data = json.loads((out / "kit.json").read_text(encoding="utf-8"))
        self.assertEqual(data["stages"]["craft"]["state"], "done", data["stages"]["craft"])
        samples = json.loads((out / "scroll-samples.json").read_text(encoding="utf-8"))
        entry = next(step["step"] for step in samples["steps"] if step["scroll_y"] > 300)
        self.assertIn(entry, data["stages"]["craft"]["changed_steps"])

    def test_rea_counts_the_scroll_callback_in_the_script_that_carries_the_marker(self):
        if tools.find("PIGGYBACK_REA", "rea") is None:
            self.skipTest("rea is not installed")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "kit"
        kit.make(f"{self.base}/kit-motion.html", out, "a landing page", allow_loopback=True)
        data = json.loads((out / "kit.json").read_text(encoding="utf-8"))
        self.assertEqual(data["stages"]["rea"]["state"], "captured", data["stages"]["rea"])
        observed = json.loads((out / "rea-observation.json").read_text(encoding="utf-8"))
        self.assertFalse(observed["truncated"])
        holders = [
            s
            for s in observed["scripts"]
            if MARKER in (out / s["source"]["file"]).read_text(encoding="utf-8")
        ]
        self.assertEqual(len(holders), 1)
        text = (out / holders[0]["source"]["file"]).read_text(encoding="utf-8")
        callback = text.index("scrollY > 300")  # the fixture's scroll listener body
        self.assertTrue(
            any(
                f["count"] > 0 and any(r["start"] <= callback < r["end"] for r in f["ranges"])
                for f in holders[0]["functions"]
            ),
            "no executed function covers the scroll callback",
        )

    def test_the_main_browser_has_no_debugging_port_during_the_dense_pass(self):
        if tools.find("PIGGYBACK_REA", "rea") is None:
            self.skipTest("rea is not installed")
        # The capture runs in a worker process, so watch the real browser processes from outside.
        seen, done = [], threading.Event()

        def watch():
            while not done.is_set():
                seen.extend(browser_processes(os.getpid()))
                time.sleep(0.05)

        watcher = threading.Thread(target=watch)
        watcher.start()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "kit"
        try:
            kit.make(f"{self.base}/kit-motion.html", out, "a landing page", allow_loopback=True)
        finally:
            done.set()
            watcher.join()
        debug = [cmd for cmd in seen if b"--remote-debugging-port=" in cmd]
        self.assertTrue([cmd for cmd in seen if cmd not in debug], "the capture browser was seen")
        self.assertTrue(debug, "the REA browser was seen")
        # Only the REA browser, with its own private profile, ever has a debugging port.
        self.assertEqual([cmd for cmd in debug if b"piggyback-rea-" not in cmd], [])

    def test_samples_carry_no_page_text_html_or_urls(self):
        for name in ("kit-motion.html", "kit-static.html"):
            with self.subTest(page=name):
                out, _ = self.capture(name)
                raw = (out / "scroll-samples.json").read_text(encoding="utf-8")
                self.assertNotIn(MARKER, raw)
                self.assertNotIn("<", raw)
                self.assertNotIn("http", raw)


if __name__ == "__main__":
    unittest.main()
