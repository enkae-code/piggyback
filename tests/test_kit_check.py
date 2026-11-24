import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
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
count = int(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "count")).read() or 0)
found = [{
    "antipattern": "side-tab", "name": "Side-tab accent border /home/brodey/secret",
    "description": "d", "severity": "warning", "category": "slop", "line": 0,
    "file": os.path.join(target, "index.html"),
    "snippet": f"border-left | `x` {target}" if seen else "none",
} for _ in range(count)] if seen else []
for root, _, names in os.walk(target):
    for name in names:
        if "s3cret" in open(os.path.join(root, name), errors="ignore").read():
            found.append({"name": "LEAK", "severity": "warning", "file": root, "snippet": ""})
print(json.dumps(found))
sys.exit(2 if found else 0)
"""


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def write_data(data):
    def write(url, out_dir, **_):
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "fingerprint.json").write_text(json.dumps(data), encoding="utf-8")
        (out / "screenshot.png").write_bytes(b"\x89PNG")
        return out / "fingerprint.json", out / "screenshot.png"

    return write


def write_build(name):
    return write_data(load(name))


def kit_build_digest(url):
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


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
        digests = {
            name: hashlib.sha256((self.kit / name).read_bytes()).hexdigest()
            for name in ("fingerprint.json", "DESIGN.md")
        }
        (self.kit / "kit.json").write_text(json.dumps({"artifacts": digests}), encoding="utf-8")
        self.workspace = self.root / "site"
        self.workspace.mkdir()
        (self.workspace / "index.html").write_text("<h1>Hi</h1>", encoding="utf-8")
        self.fake = self.root / "impeccable"
        self.fake.write_text(f"#!{sys.executable}\n{FAKE_DETECT}", encoding="utf-8")
        self.fake.chmod(0o755)
        (self.root / "count").write_text("1", encoding="utf-8")
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
        self.assertEqual(text.count("## Attempt 2"), 1)
        self.assertIn("Attempt 1 (earlier):", text)

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
            with self.assertRaises(ValueError):
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

    def make_build(self, data, **kwargs):
        with mock.patch("piggyback.kit.write_measure", side_effect=write_data(data)) as measure:
            path = kit.check(self.kit, BUILD, allow_loopback=True, **kwargs)
        return path.read_text(encoding="utf-8"), measure

    def test_a_reference_that_changed_after_the_kit_is_refused_before_measuring(self):
        (self.kit / "fingerprint.json").write_text("{}", encoding="utf-8")
        with mock.patch("piggyback.kit.write_measure") as measure:
            with self.assertRaisesRegex(ValueError, "changed since the kit was made"):
                kit.check(self.kit, BUILD, workspace=self.workspace)
        measure.assert_not_called()

    def test_a_symlinked_kit_folder_is_refused(self):
        link = self.root / "kitlink"
        link.symlink_to(self.kit, target_is_directory=True)
        with self.assertRaises(ValueError):
            kit.check(link, BUILD, workspace=self.workspace)

    def test_a_fifo_in_the_kit_is_refused_without_hanging(self):
        (self.kit / "DESIGN.md").unlink()
        os.mkfifo(self.kit / "DESIGN.md")
        outcome = {}

        def run_check():
            try:
                kit.check(self.kit, BUILD, workspace=self.workspace)
            except ValueError as exc:
                outcome["error"] = exc

        reader = threading.Thread(target=run_check, daemon=True)
        reader.start()
        reader.join(5)
        self.assertFalse(reader.is_alive())
        self.assertIn("error", outcome)

    def test_a_hard_linked_workspace_file_is_skipped(self):
        (self.root / "secret.txt").write_text("s3cret", encoding="utf-8")
        os.link(self.root / "secret.txt", self.workspace / "leak.html")
        text = self.check().read_text(encoding="utf-8")
        self.assertNotIn("LEAK", text)

    def test_a_workspace_over_the_byte_bound_is_skipped_and_the_gap_still_runs(self):
        with mock.patch.object(kit, "WORKSPACE_MAX", 4):
            text, measure = self.make_build(load("build.json"), workspace=self.workspace)
        self.assertIn("Impeccable: skipped: workspace too large", text)
        self.assertIn("## Differences", text)
        measure.assert_called_once()

    def test_the_byte_bound_counts_every_file_together(self):
        (self.workspace / "index.html").unlink()
        for name in ("a.html", "b.html"):
            (self.workspace / name).write_text("abc", encoding="utf-8")
        with mock.patch.object(kit, "WORKSPACE_MAX", 4):
            text, _ = self.make_build(load("build.json"), workspace=self.workspace)
        self.assertIn("Impeccable: skipped: workspace too large", text)

    def test_a_workspace_with_too_many_entries_is_skipped(self):
        for i in range(3):
            (self.workspace / f"p{i}.html").write_text("x", encoding="utf-8")
        with mock.patch.object(kit, "WORKSPACE_ENTRIES", 2):
            text, _ = self.make_build(load("build.json"), workspace=self.workspace)
        self.assertIn("Impeccable: skipped: workspace too large", text)

    def test_a_workspace_nested_too_deep_is_skipped(self):
        deep = self.workspace / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "x.html").write_text("x", encoding="utf-8")
        with mock.patch.object(kit, "WORKSPACE_DEPTH", 2):
            text, _ = self.make_build(load("build.json"), workspace=self.workspace)
        self.assertIn("Impeccable: skipped: workspace too large", text)

    def test_detect_lists_at_most_detect_max_findings(self):
        (self.root / "count").write_text("5", encoding="utf-8")
        with mock.patch.object(kit, "DETECT_MAX", 2):
            text, _ = self.make_build(load("build.json"), workspace=self.workspace)
        self.assertEqual(text.count("- Side-tab accent border"), 2)

    def test_detect_text_never_shows_an_absolute_path(self):
        text = self.check().read_text(encoding="utf-8")
        self.assertNotIn("/home/brodey", text)
        self.assertNotIn(tempfile.gettempdir(), text)

    def test_a_finding_is_shown_by_its_path_relative_to_the_workspace(self):
        self.assertIn("at index.html", self.check().read_text(encoding="utf-8"))

    def test_a_malformed_history_starts_attempt_one_with_a_note(self):
        (self.kit / "CHECK.md").write_text(
            '# Check\n<!-- piggyback-check {"build": "%s", "attempts": "bad"} -->\n'
            % kit_build_digest(BUILD),
            encoding="utf-8",
        )
        text = self.check().read_text(encoding="utf-8")
        self.assertIn("## Attempt 1", text)
        self.assertIn("could not be read", text)

    def test_a_history_naming_an_unknown_concept_is_malformed(self):
        state = {
            "build": kit_build_digest(BUILD),
            "attempts": [
                {"n": 1, "open": ["nope"], "fixed": [], "uncomparable": [], "warnings": 0}
            ],
        }
        (self.kit / "CHECK.md").write_text(
            f"# Check\n<!-- piggyback-check {json.dumps(state)} -->\n", encoding="utf-8"
        )
        text = self.check().read_text(encoding="utf-8")
        self.assertIn("## Attempt 1", text)
        self.assertIn("could not be read", text)

    def test_uncomparable_items_are_named_and_block_the_all_clear(self):
        ref = load("ref.json")
        ref_without_nav = json.loads(json.dumps(ref))
        del ref_without_nav["anchors"]["nav"]
        text, _ = self.make_build(ref_without_nav)
        self.assertIn("Uncomparable: nav_type", text)
        self.assertNotIn("no actionable gaps remain", text)

    def test_an_open_difference_is_not_also_listed_as_uncomparable(self):
        build = load("ref.json")
        build["anchors"]["heading"].update(font_size="72px", line_height="normal")
        text, _ = self.make_build(build)
        self.assertRegex(text, r"Still open: [^\n]*heading_type")
        self.assertNotRegex(text, r"Uncomparable: [^\n]*heading_type")

    def test_the_all_clear_needs_no_open_and_no_uncomparable_items(self):
        text, _ = self.make_build(load("ref.json"))
        self.assertIn("Stop: no actionable gaps remain.", text)

    def test_kit_help_names_the_check_command(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            main(["kit", "--help"])
        self.assertIn("kit check", out.getvalue())

    def test_a_check_writes_no_home_path(self):
        self.check()
        for path in self.kit.rglob("*"):
            if path.is_file():
                self.assertNotIn(str(Path.home()), path.read_text(errors="ignore"))
