import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from piggyback import kit
from piggyback.cli import main

URL = "https://93.184.216.34/"
FAKE_FP = {
    "url": URL,
    "browser": "fake",
    "capture": {
        "status": 200,
        "final_url": URL,
        "browser": "fake",
        "browser_version": "1.0",
        "os": "test",
        "viewport": {"width": 1440, "height": 900},
        "captured_at": "2026-11-23T00:00:00+00:00",
    },
}


def fake_write_measure(url, out_dir, *, viewport=(1440, 900), allow_loopback=False, scroll=False):
    out = Path(out_dir)
    (out / "screenshot.png").write_bytes(b"\x89PNG shot")
    (out / "fingerprint.json").write_text(json.dumps(FAKE_FP), encoding="utf-8")
    (out / "scroll").mkdir()
    for i in range(6):
        (out / "scroll" / f"frame-{i}.png").write_bytes(b"\x89PNG frame %d" % i)
    (out / "scroll-samples.json").write_text(
        json.dumps({"coverage": {"scroll": "done"}, "steps": []}), encoding="utf-8"
    )
    return out / "fingerprint.json", out / "screenshot.png"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class KitCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def run_cli(self, *args):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch("piggyback.kit.write_measure") as m:
            code = main(["kit", *args])
        return code, m, err.getvalue()


class CliRefusalTests(KitCase):
    def test_non_empty_folder_is_refused_and_nothing_is_written(self):
        out = self.root / "kit"
        out.mkdir()
        (out / "keep.txt").write_text("mine", encoding="utf-8")
        code, m, err = self.run_cli(URL, "--out", str(out), "--request", "a page")
        self.assertEqual(code, 1)
        self.assertIn("empty folder", err)
        m.assert_not_called()
        self.assertEqual([p.name for p in out.iterdir()], ["keep.txt"])

    def test_partial_kit_folder_says_it_is_incomplete_and_must_be_removed(self):
        out = self.root / "kit"
        out.mkdir()
        (out / "screenshot.png").write_bytes(b"\x89PNG partial")
        code, m, err = self.run_cli(URL, "--out", str(out), "--request", "a page")
        self.assertEqual(code, 1)
        self.assertIn("incomplete kit", err)
        self.assertIn("remove it before re-running", err)
        m.assert_not_called()

    def test_a_file_at_out_is_refused(self):
        out = self.root / "kit"
        out.write_text("x", encoding="utf-8")
        code, m, _ = self.run_cli(URL, "--out", str(out), "--request", "a page")
        self.assertEqual(code, 1)
        m.assert_not_called()

    def test_a_symlinked_out_folder_is_refused(self):
        target = self.root / "elsewhere"
        target.mkdir()
        out = self.root / "kit"
        os.symlink(target, out)
        code, m, err = self.run_cli(URL, "--out", str(out), "--request", "a page")
        self.assertEqual(code, 1)
        self.assertIn("symbolic link", err)
        m.assert_not_called()
        self.assertEqual(list(target.iterdir()), [])

    def test_loopback_without_the_flag_is_refused_before_any_write(self):
        out = self.root / "kit"
        code, m, err = self.run_cli("http://10.0.0.1/", "--out", str(out), "--request", "a page")
        self.assertEqual(code, 1)
        self.assertIn("public http(s) address", err)
        m.assert_not_called()
        self.assertFalse(out.exists())

    def test_request_is_required(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            main(["kit", URL, "--out", str(self.root / "kit")])
        self.assertEqual(ctx.exception.code, 2)


class RequestBoundsTests(unittest.TestCase):
    def test_request_is_one_to_500_characters(self):
        self.assertEqual(kit.check_request("x" * 500), "x" * 500)
        self.assertEqual(kit.check_request("  a page  "), "a page")
        for bad in ("", "   ", "x" * 501):
            with self.subTest(length=len(bad)), self.assertRaisesRegex(ValueError, "1 to 500"):
                kit.check_request(bad)

    def test_a_bad_request_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("piggyback.kit.write_measure") as m:
            out = Path(tmp) / "kit"
            with self.assertRaises(ValueError):
                kit.make(URL, out, "x" * 501)
            m.assert_not_called()
            self.assertFalse(out.exists())


class PromptTests(unittest.TestCase):
    def test_prompt_carries_the_request_and_the_required_rules(self):
        prompt = kit.build_prompt("a landing page for Tidewater, a boat studio")
        self.assertIn("a landing page for Tidewater, a boat studio", prompt)
        for phrase in (
            "screenshot.png",
            "scroll/frame-0.png",
            "BRIEF.md",
            "MOTION.md",
            "If MOTION.md is present",
            "do not copy anything from the reference",
            "offline",
            "semantic HTML",
            "prefers-reduced-motion",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, prompt)

    def test_request_text_is_not_parsed_as_a_template(self):
        self.assertIn("{request}", kit.build_prompt("{request}"))


class KitFilesTests(KitCase):
    def make(self, out=None):
        out = out or self.root / "kit"
        with (
            mock.patch("piggyback.kit.write_measure", side_effect=fake_write_measure),
            mock.patch("piggyback.brief.brief", return_value="# Design brief\n"),
            mock.patch("piggyback.brief.design_md", return_value="---\nname: x\n---\n"),
        ):
            return kit.make(URL, out, "a landing page for Tidewater"), out

    def test_kit_json_digests_match_every_artifact(self):
        path, out = self.make()
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["schema"], 1)
        self.assertEqual(data["url"], URL)
        self.assertEqual(data["request"], "a landing page for Tidewater")
        self.assertEqual(data["stages"]["motion"], "not run")
        self.assertEqual(data["stages"]["scroll"], "done")
        self.assertEqual(data["browser"]["final_url"], URL)
        for name, digest in data["artifacts"].items():
            with self.subTest(artifact=name):
                self.assertEqual(sha256(out / name), digest)
        on_disk = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()} - {
            "kit.json"
        }
        self.assertEqual(set(data["artifacts"]), on_disk)

    def test_kit_json_is_written_last_so_a_failed_stage_leaves_no_kit(self):
        out = self.root / "kit"
        with (
            mock.patch("piggyback.kit.write_measure", side_effect=fake_write_measure),
            mock.patch("piggyback.brief.brief", side_effect=RuntimeError("brief broke")),
            self.assertRaisesRegex(RuntimeError, "brief broke"),
        ):
            kit.make(URL, out, "a landing page")
        self.assertFalse((out / "kit.json").exists())
        self.assertFalse((out / "BUILD-PROMPT.md").exists())

    def test_build_prompt_file_holds_the_request(self):
        _, out = self.make()
        self.assertIn(
            "a landing page for Tidewater", (out / "BUILD-PROMPT.md").read_text(encoding="utf-8")
        )


if __name__ == "__main__":
    unittest.main()
