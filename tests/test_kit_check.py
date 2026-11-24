import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from piggyback import kit
from piggyback.cli import main
from piggyback.measure import MeasureError

FIXTURES = Path(__file__).parent / "fixtures" / "fingerprints"
URL = "https://93.184.216.34/"
BUILD = "https://93.184.216.35/"

# A stand-in for `impeccable detect --json`: it reports DESIGN.md's presence and a finding with an
# absolute file path, the way the real tool does. Exit 2 means primary findings.
FAKE_DETECT = """\
import json, os, sys
target = sys.argv[-1]
seen = os.path.isfile(os.path.join(target, "DESIGN.md"))
found = [{
    "antipattern": "side-tab", "name": "Side-tab accent border", "description": "d",
    "severity": "warning", "category": "slop", "line": 0,
    "file": os.path.join(target, "index.html"),
    "snippet": "border-left | `x`" if seen else "none",
}] if seen else []
for root, _, names in os.walk(target):
    for name in names:
        if "s3cret" in open(os.path.join(root, name), errors="ignore").read():
            found.append({"name": "LEAK", "severity": "warning", "file": root, "snippet": ""})
print(json.dumps(found))
sys.exit(2 if found else 0)
"""


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def write_build(name):
    def write(url, out_dir, **_):
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "fingerprint.json").write_text(json.dumps(load(name)), encoding="utf-8")
        (out / "screenshot.png").write_bytes(b"\x89PNG")
        return out / "fingerprint.json", out / "screenshot.png"

    return write


fake_write_measure = write_build("build.json")


@unittest.skipUnless(os.name == "posix", "POSIX only: fake #! script and symlinks")
class KitCheckTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.kit = self.root / "kit"
        self.kit.mkdir()
        shutil.copy(FIXTURES / "ref.json", self.kit / "fingerprint.json")
        (self.kit / "DESIGN.md").write_text("---\nname: x\n---\n", encoding="utf-8")
        (self.kit / "kit.json").write_text("{}", encoding="utf-8")
        self.workspace = self.root / "site"
        self.workspace.mkdir()
        (self.workspace / "index.html").write_text("<h1>Hi</h1>", encoding="utf-8")
        self.fake = self.root / "impeccable"
        self.fake.write_text(f"#!{sys.executable}\n{FAKE_DETECT}", encoding="utf-8")
        self.fake.chmod(0o755)
        env = mock.patch.dict(os.environ, {"PIGGYBACK_IMPECCABLE": str(self.fake)})
        env.start()
        self.addCleanup(env.stop)

    def check(self, build=BUILD, workspace=None):
        with mock.patch("piggyback.kit.write_measure", side_effect=fake_write_measure):
            return kit.check(
                self.kit, build, workspace=workspace or self.workspace, allow_loopback=True
            )

    def test_check_writes_check_md_with_an_attempt_and_the_detect_findings(self):
        path = self.check()
        text = path.read_text(encoding="utf-8")
        self.assertIn("## Attempt 1", text)
        self.assertIn("Side-tab accent border", text)

    def test_design_md_is_visible_to_impeccable_during_the_check(self):
        text = self.check().read_text(encoding="utf-8")
        self.assertIn("Side-tab accent border", text)

    def test_detect_findings_are_escaped_and_carry_no_absolute_path(self):
        text = self.check().read_text(encoding="utf-8")
        self.assertIn("border-left \\| \\`x\\`", text)
        self.assertNotIn(str(self.root), text)

    def test_the_frozen_reference_is_never_refreshed(self):
        before = (self.kit / "fingerprint.json").read_bytes()
        self.check()
        self.assertEqual((self.kit / "fingerprint.json").read_bytes(), before)

    def test_a_second_check_of_the_same_build_appends_attempt_two(self):
        self.check()
        text = self.check().read_text(encoding="utf-8")
        self.assertIn("## Attempt 2", text)
        self.assertEqual(text.count("## Attempt 1"), 1)

    def test_a_difference_that_closes_is_listed_as_fixed_and_the_stop_line_says_so(self):
        self.check()
        with mock.patch("piggyback.kit.write_measure", side_effect=write_build("ref.json")):
            kit.check(self.kit, BUILD, workspace=self.workspace, allow_loopback=True)
        text = (self.kit / "CHECK.md").read_text(encoding="utf-8")
        self.assertIn("Still open: none", text)
        self.assertIn("Fixed: accent", text)
        self.assertIn("no actionable gaps remain", text)

    def test_a_new_build_url_starts_a_new_history(self):
        self.check()
        text = self.check(build="https://93.184.216.36/").read_text(encoding="utf-8")
        self.assertIn("## Attempt 1", text)
        self.assertNotIn("## Attempt 2", text)

    def test_the_third_attempt_stops_and_a_fourth_is_refused(self):
        for _ in range(3):
            self.check()
        text = (self.kit / "CHECK.md").read_text(encoding="utf-8")
        self.assertIn("correction cap", text)
        with self.assertRaises(ValueError):
            self.check()

    def test_a_workspace_that_is_a_symlink_is_refused(self):
        link = self.root / "link"
        link.symlink_to(self.workspace, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.check(workspace=link)

    def test_a_symlink_inside_the_workspace_is_skipped(self):
        (self.root / "secret.txt").write_text("s3cret", encoding="utf-8")
        (self.workspace / "leak.html").symlink_to(self.root / "secret.txt")
        text = self.check().read_text(encoding="utf-8")
        self.assertNotIn("LEAK", text)

    def test_without_a_workspace_the_detect_stage_is_not_run(self):
        with mock.patch("piggyback.kit.write_measure", side_effect=fake_write_measure):
            path = kit.check(self.kit, BUILD, allow_loopback=True)
        self.assertIn("not run", path.read_text(encoding="utf-8"))

    def test_a_kit_file_that_is_a_symlink_is_refused_and_nothing_is_measured(self):
        (self.kit / "DESIGN.md").unlink()
        (self.kit / "DESIGN.md").symlink_to(self.root / "secret.txt")
        (self.root / "secret.txt").write_text("x", encoding="utf-8")
        with mock.patch("piggyback.kit.write_measure") as measure:
            with self.assertRaises(MeasureError):
                kit.check(self.kit, BUILD, workspace=self.workspace)
        measure.assert_not_called()

    def test_a_build_url_that_is_not_public_is_refused_before_any_measure(self):
        with mock.patch("piggyback.kit.write_measure") as measure:
            with self.assertRaises(MeasureError):
                kit.check(self.kit, "http://127.0.0.1:9/", workspace=self.workspace)
        measure.assert_not_called()

    def test_the_cli_runs_kit_check(self):
        with mock.patch("piggyback.kit.write_measure", side_effect=fake_write_measure):
            code = main(
                [
                    "kit",
                    "check",
                    "--kit",
                    str(self.kit),
                    "--build",
                    BUILD,
                    "--workspace",
                    str(self.workspace),
                    "--allow-loopback",
                ]
            )
        self.assertEqual(code, 0)
        self.assertTrue((self.kit / "CHECK.md").is_file())

    def test_a_check_writes_no_home_path(self):
        self.check()
        for path in self.kit.rglob("*"):
            if path.is_file():
                self.assertNotIn(str(Path.home()), path.read_text(errors="ignore"))
