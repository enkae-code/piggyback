"""C3 per-page runner, without a browser or a model: the agent call and checklist are stubbed."""

import contextlib
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bench.c3 import checklist, prompts
from bench.c3 import run as runner

COMMIT = "b" * 40
MODEL = "claude-opus-5-5"
ARM_FILES = runner.ARM_FILES


def sha(data):
    return hashlib.sha256(data).hexdigest()


def make_run(tmp, run_id="r1"):
    """A locked run folder with real input files and prompts, and a manifest that matches them."""
    run = Path(tmp) / "store" / run_id
    inputs = run / "inputs"
    for sid in sorted({t["starter_id"] for t in prompts.load_tasks()}):
        (inputs / sid).mkdir(parents=True)
        (inputs / sid / "BRIEF.md").write_text(f"brief {sid}\n", encoding="utf-8")
        (inputs / sid / "DESIGN.md").write_text(f"design {sid}\n", encoding="utf-8")
        (inputs / sid / "screenshot.png").write_bytes(b"\x89PNG " + sid.encode())
    (inputs / "impeccable").mkdir(parents=True)
    (inputs / "impeccable" / "SKILL.md").write_text("skill\n", encoding="utf-8")
    for task in prompts.load_tasks():
        for arm in prompts.ARM_BLOCKS:
            path = run / "prompts" / task["id"] / f"{arm}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(prompts.expand(task, arm) + "\n", encoding="utf-8")
    inputs_sha = {
        p.relative_to(run).as_posix(): sha(p.read_bytes()) for p in sorted(run.rglob("*"))
        if p.is_file()
    }  # fmt: skip
    manifest = {
        "piggyback_commit": COMMIT,
        "harness_sha256": {"run.py": "h"},
        "starters_sha256": {"s": "x"},
        "inputs_sha256": inputs_sha,
    }
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return run.parent, run.name


@contextlib.contextmanager
def registered():
    """Real HEAD/tree checks replaced by fixed values that match make_run's manifest."""
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(runner, "clean_commit", return_value=COMMIT))
        stack.enter_context(
            mock.patch.object(runner, "harness_hashes", return_value={"run.py": "h"})
        )
        stack.enter_context(mock.patch.object(runner, "starter_hashes", return_value={"s": "x"}))
        yield


def agent_json(models=(MODEL,), session="sess-1", is_error=False):
    return json.dumps(
        {
            "session_id": session,
            "modelUsage": {m: {"inputTokens": 1} for m in models},
            "duration_ms": 5,
            "is_error": is_error,
        }
    )


def completed(stdout, returncode=0):
    return subprocess.CompletedProcess(["claude"], returncode, stdout=stdout, stderr="")


def report(outcome, failed=()):
    return {
        "outcome": outcome,
        "pass": outcome == "pass",
        "failed": list(failed),
        "correction_message": None,
    }


class FakeAgent:
    """Stands in for the one agent-call function: writes index.html each turn, returns JSON."""

    def __init__(self, stdout=None, models=(MODEL,)):
        self.calls = []
        self.stdout = stdout
        self.models = models

    def __call__(self, argv, cwd):
        self.calls.append((list(argv), Path(cwd)))
        (Path(cwd) / "index.html").write_text("<html></html>", encoding="utf-8")
        return completed(self.stdout or agent_json(self.models))


class ErrorOnCorrection(FakeAgent):
    """The first turn succeeds; every later turn returns output that is not JSON."""

    def __call__(self, argv, cwd):
        if self.calls:
            self.stdout = "not json"
        return super().__call__(argv, cwd)


def run_with(tmp, agent, checks, task="T01", arm="screenshot"):
    store, run_id = make_run(tmp)
    with registered(), mock.patch.object(runner, "agent_turn", side_effect=agent), \
            mock.patch.object(checklist, "run_bounded", side_effect=checks):  # fmt: skip
        result = runner.run_page(str(store), run_id, task, arm)
    return result, store / run_id / "pages" / task / arm


class ArmIsolation(unittest.TestCase):
    def test_each_arm_project_holds_exactly_its_allowed_files(self):
        for arm, names in ARM_FILES.items():
            with self.subTest(arm=arm), tempfile.TemporaryDirectory() as tmp:
                agent = FakeAgent()
                # The agent writes index.html itself; the inputs must be the only other files.
                _, out = run_with(tmp, agent, lambda *a, **k: report("pass"), arm=arm)
                project = sorted(p.name for p in (out / "project").iterdir())
                self.assertEqual(project, sorted([*names, "index.html"]))

    def test_no_starter_source_is_copied(self):
        with tempfile.TemporaryDirectory() as tmp:
            # The agent stub writes only index.html, so any other file would be a copied source.
            _, out = run_with(
                tmp, FakeAgent(), lambda *a, **k: report("pass"), arm="piggyback-brief"
            )
            self.assertEqual(
                sorted(p.name for p in (out / "project").iterdir()), ["BRIEF.md", "index.html"]
            )


class OutputClaim(unittest.TestCase):
    def test_existing_output_dir_is_refused_and_untouched(self):
        # Negative check for the first-attempt rule: without it a second run would overwrite.
        with tempfile.TemporaryDirectory() as tmp:
            store, run_id = make_run(tmp)
            out = store / run_id / "pages" / "T01" / "screenshot"
            out.mkdir(parents=True)
            (out / "run.json").write_text("first", encoding="utf-8")
            agent = FakeAgent()
            with registered(), mock.patch.object(runner, "agent_turn", side_effect=agent):
                with self.assertRaises(SystemExit):
                    runner.run_page(str(store), run_id, "T01", "screenshot")
            self.assertEqual(agent.calls, [])
            self.assertEqual((out / "run.json").read_text(encoding="utf-8"), "first")


class CorrectionLoop(unittest.TestCase):
    def test_stops_at_the_first_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = iter([report("fail", ["content item missing: x (text)"]), report("pass")])
            agent = FakeAgent()
            result, out = run_with(tmp, agent, lambda *a, **k: next(reports))
            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["turns"], 1)
            self.assertEqual(len(agent.calls), 2)
            self.assertEqual((out / "run.json").exists(), True)

    def test_stops_after_five_correction_turns(self):
        # Negative check for the bound: without it a seventh check fails fast on StopIteration.
        with tempfile.TemporaryDirectory() as tmp:
            agent = FakeAgent()
            reports = iter([report("fail", ["x (text)"])] * 6)
            result, _ = run_with(tmp, agent, lambda *a, **k: next(reports))
            self.assertEqual(result["status"], "failed_after_5")
            self.assertEqual(result["turns"], 5)
            self.assertEqual(len(agent.calls), 6)

    def test_correction_is_resumed_session_with_the_exact_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = FakeAgent(stdout=agent_json(session="sess-42"))
            failed = ["content item missing: x (text)"]
            reports = iter([report("fail", failed), report("pass")])
            run_with(tmp, agent, lambda *a, **k: next(reports))
            first, second = agent.calls[0][0], agent.calls[1][0]
            self.assertIn("--session-id", first)
            self.assertNotIn("--session-id", second)
            self.assertEqual(
                second[second.index("--resume") + 1], first[first.index("--session-id") + 1]
            )
            self.assertEqual(second[2], checklist.correction_message(failed))
            self.assertEqual(second[:2], ["claude", "-p"])  # fmt: skip

    def test_every_turn_runs_with_accept_edits(self):
        # Negative check for the write permission: without it claude -p cannot write index.html.
        with tempfile.TemporaryDirectory() as tmp:
            reports = iter([report("fail", ["x (text)"]), report("pass")])
            agent = FakeAgent()
            run_with(tmp, agent, lambda *a, **k: next(reports))
            self.assertEqual(len(agent.calls), 2)
            for argv, _ in agent.calls:
                pair = argv[argv.index("--permission-mode") : argv.index("--permission-mode") + 2]
                self.assertEqual(pair, ["--permission-mode", "acceptEdits"])

    def test_agent_error_on_correction_after_a_page_keeps_the_page(self):
        # Negative check for the page rule: an error after a runnable round is not no_output.
        with tempfile.TemporaryDirectory() as tmp:
            agent = ErrorOnCorrection()
            reports = iter([report("fail", ["x (text)"]), report("fail", ["x (text)"])])
            result, out = run_with(tmp, agent, lambda *a, **k: next(reports))
            self.assertEqual(result["status"], "agent_error_after_page")
            self.assertEqual(len(agent.calls), 2)
            self.assertIsNotNone(result["rounds"][1]["agent"]["error"])
            self.assertTrue((out / "project" / "index.html").is_file())

    def test_agent_error_on_correction_with_no_runnable_page_is_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = ErrorOnCorrection()
            reports = iter([report("fail", ["x (text)"]), report("no_runnable_output")])
            result, _ = run_with(tmp, agent, lambda *a, **k: next(reports))
            self.assertEqual(result["status"], "no_output")
            self.assertEqual(len(agent.calls), 2)

    def test_missing_page_is_no_output_with_no_correction(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, run_id = make_run(tmp)

            def no_file(url, *a, **k):
                raise AssertionError("checklist must not run without index.html")

            with registered(), mock.patch.object(runner, "agent_turn") as turn, \
                    mock.patch.object(checklist, "run_bounded", side_effect=no_file):  # fmt: skip
                turn.return_value = completed(agent_json())
                result = runner.run_page(str(store), run_id, "T01", "screenshot")
            self.assertEqual(result["status"], "no_output")
            self.assertEqual(turn.call_count, 1)

    def test_unparseable_agent_output_is_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = FakeAgent(stdout="not json")
            result, _ = run_with(tmp, agent, lambda *a, **k: report("pass"))
            self.assertEqual(result["status"], "no_output")
            self.assertEqual(len(agent.calls), 1)
            self.assertIsNotNone(result["rounds"][0]["agent"]["error"])

    def test_no_runnable_checklist_is_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = FakeAgent()
            result, _ = run_with(tmp, agent, lambda *a, **k: report("no_runnable_output"))
            self.assertEqual(result["status"], "no_output")
            self.assertEqual(len(agent.calls), 1)


class ModelFlag(unittest.TestCase):
    def test_a_non_opus_model_is_recorded_and_flagged(self):
        # Negative check for the model flag: without it a Sonnet run would count as Opus.
        with tempfile.TemporaryDirectory() as tmp:
            agent = FakeAgent(models=(MODEL, "claude-haiku-5-5"))
            result, out = run_with(tmp, agent, lambda *a, **k: report("pass"))
            self.assertEqual(result["model_ids"], sorted([MODEL, "claude-haiku-5-5"]))
            self.assertEqual(result["model_flag"], ["claude-haiku-5-5"])
            saved = json.loads((out / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["model_flag"], ["claude-haiku-5-5"])

    def test_opus_only_run_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = run_with(tmp, FakeAgent(), lambda *a, **k: report("pass"))
            self.assertEqual(result["model_flag"], [])


def no_agent(argv, cwd):
    raise AssertionError("the agent must not run when registration is refused")


class RegistrationGuards(unittest.TestCase):
    def setUp(self):
        # A removed guard must fail the test, not reach a real claude call.
        patcher = mock.patch.object(runner, "agent_turn", side_effect=no_agent)
        self.agent = patcher.start()
        self.addCleanup(patcher.stop)

    def test_head_change_since_lock_is_refused(self):
        # Negative check for the HEAD guard: only HEAD differs from the lock.
        with tempfile.TemporaryDirectory() as tmp:
            store, run_id = make_run(tmp)
            with registered(), mock.patch.object(runner, "clean_commit", return_value="c" * 40):
                with self.assertRaises(SystemExit) as caught:
                    runner.run_page(str(store), run_id, "T01", "screenshot")
            self.assertIn("HEAD", str(caught.exception))
            self.assertFalse((store / run_id / "pages").exists())

    def test_harness_change_since_lock_is_refused(self):
        # Negative check for the harness guard: only the harness hashes differ from the lock.
        with tempfile.TemporaryDirectory() as tmp:
            store, run_id = make_run(tmp)
            changed = {"run.py": "other"}
            with registered(), mock.patch.object(runner, "harness_hashes", return_value=changed):
                with self.assertRaises(SystemExit) as caught:
                    runner.run_page(str(store), run_id, "T01", "screenshot")
            self.assertEqual(
                str(caught.exception),
                "refusing: harness files changed since the manifest was locked",
            )

    def test_changed_frozen_input_is_refused(self):
        # Negative check for the frozen-input guard: a swapped BRIEF.md must not be used.
        with tempfile.TemporaryDirectory() as tmp:
            store, run_id = make_run(tmp)
            (store / run_id / "inputs" / "northline-ops" / "BRIEF.md").write_text(
                "x", encoding="utf-8"
            )
            with registered():
                with self.assertRaises(SystemExit):
                    runner.run_page(str(store), run_id, "T01", "piggyback-brief")

    def test_missing_manifest_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, run_id = make_run(tmp)
            (store / run_id / "manifest.json").unlink()
            with self.assertRaises(SystemExit):
                runner.run_page(str(store), run_id, "T01", "screenshot")


if __name__ == "__main__":
    unittest.main()
