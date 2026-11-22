"""C3 run manifest, without a browser or a model: Chromium, the claude CLI and git are stubbed."""

import contextlib
import json
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bench.c1 import manifest as c1_manifest
from bench.c3 import manifest, prompts

COMMIT = "a" * 40
CLI = "2.1.325 (Claude Code)"
CLAUDE = "/x/claude-2.1.325"
STARTERS = manifest.starter_ids()
ARM_IDS = list(prompts.ARM_BLOCKS)


def png_header(width, height):
    return b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + struct.pack(">II", width, height) + b"\x08\x06"


def fake_measure(starter, folder):
    """Stands in for the Chromium measure: the same files, no browser."""
    (folder / "screenshot.png").write_bytes(png_header(*manifest.VIEWPORT))
    (folder / "BRIEF.md").write_text(f"brief for {starter}\n", encoding="utf-8")


def write_designs(folder, skip=None, blank=None):
    folder.mkdir(parents=True, exist_ok=True)
    for sid in STARTERS:
        if sid == skip:
            continue
        (folder / f"{sid}.md").write_text("" if sid == blank else f"# {sid}\n", encoding="utf-8")
    return folder


def stubbed(tmp):
    """Patches for every external call a build makes. Returns the patchers, not yet entered."""
    skill = Path(tmp) / "SKILL.md"
    skill.write_text("impeccable skill text\n", encoding="utf-8")
    return [
        mock.patch.object(manifest, "clean_commit", return_value=COMMIT),
        mock.patch.object(manifest, "bundled_chromium", return_value=("156.0.1", "bundled")),
        mock.patch.object(manifest, "measure_starter", side_effect=fake_measure),
        mock.patch.object(manifest, "checker_chromium", return_value=("156.0.1", "/x/chrome")),
        mock.patch.object(manifest, "claude_executable", return_value=(CLAUDE, CLI)),
        mock.patch.object(manifest, "impeccable_clean", return_value=None),
        mock.patch.object(manifest, "impeccable_commit", return_value="035c8f3"),
        mock.patch.object(manifest, "impeccable_version", return_value="1.2.3"),
        mock.patch.object(manifest, "SKILL_PATH", skill),
    ]


@contextlib.contextmanager
def locked_run(tmp, run_id="r1", seed=7):
    """A run locked by the real build (real hashes of the repo), with stubbed browser and CLI."""
    store = Path(tmp) / "store"
    designs = write_designs(Path(tmp) / "designs")
    with contextlib.ExitStack() as stack:
        for patcher in stubbed(tmp):
            stack.enter_context(patcher)
        path, data = manifest.build(str(store), run_id, str(designs), seed)
    yield store / run_id, data


class RunFolderNames(unittest.TestCase):
    def test_bad_run_ids_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ("../escape", "a/b", ""):
                with self.subTest(run_id=bad), self.assertRaises(SystemExit):
                    manifest.build(tmp, bad, tmp, 1)


class BuildRefusals(unittest.TestCase):
    def test_missing_design_md_is_refused_before_any_write(self):
        # Negative check for the design guard: removing the check would let this build finish.
        with tempfile.TemporaryDirectory() as tmp:
            designs = write_designs(Path(tmp) / "d", skip=STARTERS[0])
            with contextlib.ExitStack() as stack:
                for patcher in stubbed(tmp):
                    stack.enter_context(patcher)
                with self.assertRaises(SystemExit) as caught:
                    manifest.build(str(Path(tmp) / "s"), "r1", str(designs), 1)
            self.assertIn(STARTERS[0], str(caught.exception))
            self.assertFalse((Path(tmp) / "s" / "r1").exists())

    def test_blank_design_md_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            designs = write_designs(Path(tmp) / "d", blank=STARTERS[-1])
            with contextlib.ExitStack() as stack:
                for patcher in stubbed(tmp):
                    stack.enter_context(patcher)
                with self.assertRaises(SystemExit):
                    manifest.build(str(Path(tmp) / "s"), "r1", str(designs), 1)

    def test_existing_manifest_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as tmp, locked_run(tmp) as (run, data):
            before = (run / "manifest.json").read_bytes()
            designs = write_designs(Path(tmp) / "designs")
            with contextlib.ExitStack() as stack:
                for patcher in stubbed(tmp):
                    stack.enter_context(patcher)
                with self.assertRaises(SystemExit) as caught:
                    manifest.build(str(run.parent), run.name, str(designs), 99)
            self.assertIn("already locked", str(caught.exception))
            self.assertEqual((run / "manifest.json").read_bytes(), before)

    def test_non_empty_run_folder_is_refused(self):
        # Negative check for the write-once guard on partial runs.
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "store" / "r1"
            run.mkdir(parents=True)
            (run / "stale.txt").write_text("x", encoding="utf-8")
            designs = write_designs(Path(tmp) / "designs")
            with contextlib.ExitStack() as stack:
                for patcher in stubbed(tmp):
                    stack.enter_context(patcher)
                with self.assertRaises(SystemExit):
                    manifest.build(str(run.parent), "r1", str(designs), 1)

    def test_dirty_worktree_is_refused(self):
        # Negative check for the clean-tree guard: the real clean_commit runs, git reports a change.
        with tempfile.TemporaryDirectory() as tmp:
            designs = write_designs(Path(tmp) / "designs")
            with mock.patch.object(c1_manifest, "_git", return_value=" M bench/c3/run.py"):
                with self.assertRaises(SystemExit) as caught:
                    manifest.build(str(Path(tmp) / "s"), "r1", str(designs), 1)
            self.assertIn("dirty", str(caught.exception))
            self.assertFalse((Path(tmp) / "s" / "r1").exists())

    def test_dirty_impeccable_checkout_is_refused(self):
        # Negative check for the Impeccable guard: the skill must come from a clean checkout.
        dirty = subprocess.CompletedProcess(["git"], 0, stdout=" M SKILL.md\n", stderr="")
        with mock.patch.object(manifest.subprocess, "run", return_value=dirty) as run:
            with self.assertRaises(SystemExit) as caught:
                manifest.impeccable_clean()
        self.assertIn("dirty", str(caught.exception))
        self.assertEqual(run.call_args.args[0][-2:], ["status", "--porcelain"])

    def test_screenshot_not_1440_by_900_is_refused(self):
        # Negative check for the viewport guard in measure_starter, with the measure stubbed.
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "inputs"
            folder.mkdir()

            def wrong_size(url, out, **kwargs):
                shot = Path(out) / "screenshot.png"
                shot.write_bytes(png_header(1440, 901))
                fingerprint = Path(out) / "fingerprint.json"
                fingerprint.write_text("{}", encoding="utf-8")
                return fingerprint, shot

            with mock.patch.object(manifest, "write_measure", side_effect=wrong_size):
                with self.assertRaises(SystemExit) as caught:
                    manifest.measure_starter(STARTERS[0], folder)
            self.assertIn("1440x900", str(caught.exception))
            self.assertFalse((folder / "screenshot.png").exists())


class BuildLayout(unittest.TestCase):
    def test_build_writes_exactly_the_frozen_inputs(self):
        with tempfile.TemporaryDirectory() as tmp, locked_run(tmp) as (run, data):
            self.assertEqual(
                sorted(p.name for p in (run / "inputs").iterdir()),
                sorted([*STARTERS, "impeccable"]),
            )
            for sid in STARTERS:
                names = sorted(p.name for p in (run / "inputs" / sid).iterdir())
                self.assertEqual(names, ["BRIEF.md", "DESIGN.md", "screenshot.png"])
                self.assertEqual(
                    (run / "inputs" / sid / "DESIGN.md").read_text(encoding="utf-8"),
                    f"# {sid}\n",
                )
            self.assertEqual(
                [p.name for p in (run / "inputs" / "impeccable").iterdir()], ["SKILL.md"]
            )

    def test_prompts_are_the_expanded_fifty(self):
        with tempfile.TemporaryDirectory() as tmp, locked_run(tmp) as (run, _):
            count = 0
            for task in prompts.load_tasks():
                for arm in ARM_IDS:
                    path = run / "prompts" / task["id"] / f"{arm}.txt"
                    text = path.read_text(encoding="utf-8")
                    self.assertEqual(text, prompts.expand(task, arm) + "\n")
                    count += 1
            self.assertEqual(count, 50)

    def test_manifest_records_locked_facts(self):
        with tempfile.TemporaryDirectory() as tmp, locked_run(tmp, seed=7) as (run, data):
            saved = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(saved, data)
            self.assertEqual(saved["piggyback_commit"], COMMIT)
            self.assertEqual(saved["rater_seed"], 7)
            self.assertEqual(saved["model"]["model"], "claude-opus-5-5")
            self.assertTrue(saved["model"]["settings"].startswith("CLI defaults"))
            self.assertEqual(saved["model"]["claude_cli_version"], CLI)
            self.assertEqual(saved["model"]["context_window"], 1000000)
            self.assertEqual(saved["model"]["max_output_tokens"], 128000)
            self.assertEqual(saved["claude_executable"], {"path": CLAUDE, "version": CLI})
            self.assertEqual(saved["impeccable"]["version"], "1.2.3")
            self.assertEqual(saved["prompts"]["T01"]["screenshot"], prompts.expand(
                prompts.load_tasks()[0], "screenshot") + "\n")  # fmt: skip
            self.assertEqual(saved["checker_browser"]["chromium_version"], "156.0.1")
            self.assertEqual(saved["capture_browser"]["browser"], "bundled")
            self.assertEqual(saved["impeccable"]["commit"], "035c8f3")
            self.assertEqual(saved["axe_core"]["version"], "4.14.1")
            self.assertEqual(saved["agent"]["env_adds"], sorted(manifest.AGENT_ENV))
            self.assertEqual(saved["agent"]["env_allowlist"], list(manifest.ENV_ALLOWLIST))
            self.assertEqual(saved["status_scoring"], manifest.STATUS_SCORING)
            self.assertEqual(len(saved["prompts_sha256"]), 50)
            self.assertTrue(all(len(v) == 64 for v in saved["prompts_sha256"].values()))
            self.assertEqual(saved["agent"]["max_correction_turns"], 5)
            self.assertIn("--strict-mcp-config", saved["agent"]["argv_first_turn"])
            self.assertIn("inputs/impeccable/SKILL.md", saved["inputs_sha256"])
            self.assertIn("prompts/T01/screenshot.txt", saved["inputs_sha256"])
            self.assertIn("northline-ops/index.html", saved["starters_sha256"])
            self.assertTrue(all(len(v) == 64 for v in saved["starters_sha256"].values()))

    def test_manifest_embeds_all_fifty_prompt_bodies(self):
        with tempfile.TemporaryDirectory() as tmp, locked_run(tmp) as (run, _):
            saved = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                sorted(saved["prompts"]), sorted(t["id"] for t in prompts.load_tasks())
            )
            count = sum(len(arms) for arms in saved["prompts"].values())
            self.assertEqual(count, 50)
            for task in prompts.load_tasks():
                for arm in ARM_IDS:
                    text = saved["prompts"][task["id"]][arm]
                    self.assertEqual(text, prompts.expand(task, arm) + "\n")

    def test_manifest_file_is_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp, locked_run(tmp) as (run, _):
            self.assertEqual((run / "manifest.json").stat().st_mode & 0o777, 0o600)


class AgentRecord(unittest.TestCase):
    def test_agent_env_disables_the_auto_updater(self):
        self.assertEqual(manifest.AGENT_ENV["DISABLE_AUTOUPDATER"], "1")

    def test_a_symlinked_store_is_refused_before_any_write(self):
        # Negative check for the store guard: a link to the store must not reach the write path.
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real"
            real.mkdir()
            link = Path(tmp) / "link"
            link.symlink_to(real, target_is_directory=True)
            designs = write_designs(Path(tmp) / "designs")
            with contextlib.ExitStack() as stack:
                for patcher in stubbed(tmp):
                    stack.enter_context(patcher)
                with self.assertRaises(SystemExit) as caught:
                    manifest.build(str(link), "r1", str(designs), 1)
            self.assertIn("symbolic link", str(caught.exception))
            self.assertEqual(list(real.iterdir()), [])

    def test_invocation_is_the_frozen_argv_and_env(self):
        self.assertEqual(
            manifest.AGENT_FLAGS,
            [
                "--model",
                "claude-opus-5-5",
                "--setting-sources",
                "project",
                "--strict-mcp-config",
                "--tools",
                "Read,Write,Edit,Glob,Grep",
                "--permission-mode",
                "acceptEdits",
                "--output-format",
                "json",
            ],  # fmt: skip
        )
        self.assertEqual(
            manifest.AGENT_ENV,
            {
                "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
                "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS": "1",
                "DISABLE_AUTOUPDATER": "1",
            },
        )  # fmt: skip


if __name__ == "__main__":
    unittest.main()
