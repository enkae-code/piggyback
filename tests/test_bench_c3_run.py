"""C3 per-page runner, without a browser or a model: the agent call and checklist are stubbed."""

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from bench.c3 import checklist, prompts
from bench.c3 import manifest as mf
from bench.c3 import run as runner

COMMIT = "b" * 40
MODEL = "claude-opus-5-5"
CLI = "2.1.325 (Claude Code)"
CHROME = "156.0.1"
WINDOW = 1000000
ARM_FILES = runner.ARM_FILES


def sha(data):
    return hashlib.sha256(data).hexdigest()


def make_run(tmp, run_id="r1"):
    """A locked run folder with real input files and prompts, and a manifest that matches them."""
    exe = Path(tmp) / "claude-2.1.325"
    exe.write_text("", encoding="utf-8")
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
        "model": {
            "model": MODEL,
            "claude_cli_version": CLI,
            "context_window": WINDOW,
            "max_output_tokens": 128000,
        },
        "claude_executable": {"path": str(exe), "version": CLI},
        "checker_browser": {"chromium_version": CHROME},
        "inputs_sha256": inputs_sha,
    }
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return run.parent, run.name


@contextlib.contextmanager
def registered():
    """Real HEAD, tree, CLI and hash checks replaced by fixed values that match make_run."""
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(runner, "clean_commit", return_value=COMMIT))
        stack.enter_context(
            mock.patch.object(runner, "harness_hashes", return_value={"run.py": "h"})
        )
        stack.enter_context(mock.patch.object(runner, "starter_hashes", return_value={"s": "x"}))
        stack.enter_context(mock.patch.object(runner, "claude_version", return_value=CLI))
        stack.enter_context(
            mock.patch.object(runner, "checker_chromium", return_value=(CHROME, "/x/chrome"))
        )
        yield


def usage(model, window=WINDOW, max_out=128000):
    return {
        "inputTokens": 1,
        "contextWindow": window,
        "maxOutputTokens": max_out,
        "provider": "firstParty",
    }


def agent_json(
    models=(MODEL,), session="sess-1", is_error=False, denials=None, window=WINDOW, max_out=128000
):
    body = {
        "session_id": session,
        "modelUsage": {m: usage(m, window, max_out) for m in models},
        "duration_ms": 5,
        "is_error": is_error,
    }
    if denials is not None:
        body["permission_denials"] = denials
    return json.dumps(body)


def completed(stdout, returncode=0, stderr=""):
    return subprocess.CompletedProcess(["claude"], returncode, stdout=stdout, stderr=stderr)


def report(outcome, failed=()):
    return {
        "outcome": outcome,
        "pass": outcome == "pass",
        "failed": list(failed),
        "correction_message": None,
    }


class FakeAgent:
    """Stands in for the one agent-call function. Each turn writes index.html (unless write is
    False) and returns the JSON, or the stdout given. A transcript is written under a fake HOME."""

    def __init__(self, stdout=None, models=(MODEL,), write=True, returncode=0, home=None):
        self.calls = []
        self.stdout = stdout
        self.models = models
        self.write = write
        self.returncode = returncode
        self.home = home

    def __call__(self, argv, cwd):
        self.calls.append((list(argv), Path(cwd)))
        if self.write:
            (Path(cwd) / "index.html").write_text("<html></html>", encoding="utf-8")
        if self.home is not None:
            session = (
                argv[argv.index("--session-id") + 1]
                if "--session-id" in argv
                else argv[argv.index("--resume") + 1]
            )
            slug = re.sub(r"[^A-Za-z0-9]", "-", str(Path(cwd).resolve()))
            folder = Path(self.home) / ".claude" / "projects" / slug
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{session}.jsonl").write_text("{}\n", encoding="utf-8")
        body = self.stdout if self.stdout is not None else agent_json(self.models)
        return completed(body, self.returncode)


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
            first = json.dumps({"task": "T01", "arm": "screenshot", "model_conforms": True})
            (out / "run.json").write_text(first, encoding="utf-8")
            agent = FakeAgent()
            with registered(), mock.patch.object(runner, "agent_turn", side_effect=agent):
                with self.assertRaises(SystemExit) as caught:
                    runner.run_page(str(store), run_id, "T01", "screenshot")
            self.assertIn("the first attempt stands", str(caught.exception))
            self.assertEqual(agent.calls, [])
            self.assertEqual((out / "run.json").read_text(encoding="utf-8"), first)


class CorrectionLoop(unittest.TestCase):
    def test_stops_at_the_first_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = iter([report("fail", ["content item missing: x (text)"]), report("pass")])
            agent = FakeAgent()
            result, out = run_with(tmp, agent, lambda *a, **k: next(reports))
            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["turns"], 1)
            self.assertEqual(result["last_runnable_round"], 1)
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
            self.assertEqual(second[0], first[0])
            self.assertEqual(second[1], "-p")

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

    def test_sent_messages_are_kept_per_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            failed = ["x (text)"]
            reports = iter([report("fail", failed), report("pass")])
            _, out = run_with(tmp, FakeAgent(), lambda *a, **k: next(reports))
            prompt = (out.parent.parent.parent / "prompts" / "T01" / "screenshot.txt").read_text(
                encoding="utf-8"
            )
            rounds = out / "rounds"
            self.assertEqual((rounds / "sent-0.txt").read_text(encoding="utf-8"), prompt)
            self.assertEqual(
                (rounds / "sent-1.txt").read_text(encoding="utf-8"),
                checklist.correction_message(failed),
            )


class StatusScoring(unittest.TestCase):
    def test_round_zero_error_with_a_passing_page_is_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = FakeAgent(stdout="not json")
            result, _ = run_with(tmp, agent, lambda *a, **k: report("pass"))
            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["turns"], 0)
            self.assertIsNotNone(result["rounds"][0]["agent"]["error"])
            self.assertEqual(len(agent.calls), 1)

    def test_round_zero_error_with_a_failing_runnable_page_is_agent_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = FakeAgent(stdout="not json")
            result, _ = run_with(tmp, agent, lambda *a, **k: report("fail", ["x (text)"]))
            self.assertEqual(result["status"], "agent_error")
            self.assertEqual(result["last_runnable_round"], 0)
            self.assertEqual(len(agent.calls), 1)

    def test_round_zero_error_with_no_page_is_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = FakeAgent(stdout="not json", write=False)

            def never_checked(*a, **k):
                raise AssertionError("the checklist must not run without a page")

            result, _ = run_with(tmp, agent, never_checked)
            self.assertEqual(result["status"], "no_output")
            self.assertIsNone(result["last_runnable_round"])

    def test_nonzero_exit_is_an_agent_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = FakeAgent(returncode=1, stdout=agent_json())
            result, _ = run_with(tmp, agent, lambda *a, **k: report("fail", ["x (text)"]))
            self.assertEqual(result["status"], "agent_error")
            self.assertIn("exit 1", result["rounds"][0]["agent"]["error"])

    def test_is_error_result_is_an_agent_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = FakeAgent(stdout=agent_json(is_error=True))
            result, _ = run_with(tmp, agent, lambda *a, **k: report("fail", ["x (text)"]))
            self.assertEqual(result["status"], "agent_error")
            self.assertIsNotNone(result["rounds"][0]["agent"]["error"])

    def test_agent_error_on_correction_after_a_page_is_agent_error(self):
        # Negative check for the page rule: an error after a runnable round is not no_output.
        with tempfile.TemporaryDirectory() as tmp:
            agent = ErrorOnCorrection()
            reports = iter([report("fail", ["x (text)"]), report("fail", ["x (text)"])])
            result, out = run_with(tmp, agent, lambda *a, **k: next(reports))
            self.assertEqual(result["status"], "agent_error")
            self.assertEqual(result["last_runnable_round"], 1)
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

    def test_no_runnable_checklist_is_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = FakeAgent()
            result, _ = run_with(tmp, agent, lambda *a, **k: report("no_runnable_output"))
            self.assertEqual(result["status"], "no_output")
            self.assertEqual(len(agent.calls), 1)

    def test_checker_fault_is_its_own_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = run_with(tmp, FakeAgent(), lambda *a, **k: report("checker_fault"))
            self.assertEqual(result["status"], "checker_fault")


class AgentOutput(unittest.TestCase):
    def test_timeout_keeps_the_partial_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:

            def slow(argv, cwd):
                raise subprocess.TimeoutExpired(argv, 1800, output=b"partial text")

            store, run_id = make_run(tmp)
            with registered(), mock.patch.object(runner, "agent_turn", side_effect=slow):
                result = runner.run_page(str(store), run_id, "T01", "screenshot")
            out = store / run_id / "pages" / "T01" / "screenshot"
            self.assertIn("timed out", result["rounds"][0]["agent"]["error"])
            self.assertEqual((out / "rounds" / "agent-0.json").read_text(), "partial text")
            self.assertEqual(result["status"], "no_output")

    def test_non_object_model_usage_is_an_agent_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = FakeAgent(stdout=json.dumps({"modelUsage": ["x"], "is_error": False}))
            result, _ = run_with(tmp, agent, lambda *a, **k: report("pass"))
            self.assertIn("modelUsage", result["rounds"][0]["agent"]["error"])
            self.assertEqual(result["model_evidence"], "missing")


class ModelFlag(unittest.TestCase):
    def test_a_non_opus_model_is_recorded_and_flagged(self):
        # Negative check for the model flag: without it a Sonnet run would count as Opus.
        with tempfile.TemporaryDirectory() as tmp:
            agent = FakeAgent(models=(MODEL, "claude-haiku-5-5"))
            result, out = run_with(tmp, agent, lambda *a, **k: report("pass"))
            self.assertEqual(result["model_ids"], sorted([MODEL, "claude-haiku-5-5"]))
            self.assertFalse(result["model_conforms"])
            self.assertEqual(result["model_flag"], ["claude-haiku-5-5"])
            self.assertEqual(result["model_evidence"], "confirmed")
            saved = json.loads((out / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["model_flag"], ["claude-haiku-5-5"])

    def test_opus_only_run_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = run_with(tmp, FakeAgent(), lambda *a, **k: report("pass"))
            self.assertEqual(result["model_flag"], [])
            self.assertEqual(result["model_evidence"], "confirmed")

    def test_missing_model_usage_is_recorded_as_missing_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = FakeAgent(stdout=json.dumps({"session_id": "s", "is_error": False}))
            result, _ = run_with(tmp, agent, lambda *a, **k: report("pass"))
            self.assertEqual(result["model_evidence"], "missing")
            self.assertEqual(result["model_ids"], [])
            self.assertEqual(result["model_flag"], [])


class CliVersionPin(unittest.TestCase):
    def test_every_turn_runs_the_recorded_executable_path(self):
        # Negative check for the pin: without argv[0] from the lock, a PATH swap reaches the agent.
        with tempfile.TemporaryDirectory() as tmp:
            store, run_id = make_run(tmp)
            pinned = json.loads((store / run_id / "manifest.json").read_text())
            pinned = pinned["claude_executable"]["path"]
            reports = iter([report("fail", ["x (text)"]), report("pass")])
            agent = FakeAgent()
            checks = lambda *a, **k: next(reports)  # noqa: E731
            with registered(), mock.patch.object(runner, "agent_turn", side_effect=agent), \
                    mock.patch.object(checklist, "run_bounded", side_effect=checks):  # fmt: skip
                runner.run_page(str(store), run_id, "T01", "screenshot")
            self.assertEqual([argv[0] for argv, _ in agent.calls], [pinned, pinned])

    def test_missing_pinned_executable_is_refused_before_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, run_id = make_run(tmp)
            (Path(tmp) / "claude-2.1.325").unlink()
            agent = FakeAgent()
            with registered(), mock.patch.object(runner, "agent_turn", side_effect=agent):
                with self.assertRaises(SystemExit) as caught:
                    runner.run_page(str(store), run_id, "T01", "screenshot")
            self.assertIn("missing", str(caught.exception))
            self.assertEqual(agent.calls, [])
            self.assertFalse((store / run_id / "pages").exists())

    def test_a_different_cli_is_refused_before_any_agent_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, run_id = make_run(tmp)
            agent = FakeAgent()
            with registered(), mock.patch.object(runner, "claude_version", return_value="9.9.9"), \
                    mock.patch.object(runner, "agent_turn", side_effect=agent):  # fmt: skip
                with self.assertRaises(SystemExit) as caught:
                    runner.run_page(str(store), run_id, "T01", "screenshot")
            self.assertIn("9.9.9", str(caught.exception))
            self.assertIn(CLI, str(caught.exception))
            self.assertEqual(agent.calls, [])
            self.assertFalse((store / run_id / "pages").exists())


class CheckerRuntime(unittest.TestCase):
    def test_a_different_checker_chromium_is_refused_before_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, run_id = make_run(tmp)
            agent = FakeAgent()
            other = mock.patch.object(runner, "checker_chromium", return_value=("999", "x"))
            with registered(), other, mock.patch.object(runner, "agent_turn", side_effect=agent):
                with self.assertRaises(SystemExit) as caught:
                    runner.run_page(str(store), run_id, "T01", "screenshot")
            self.assertIn("999", str(caught.exception))
            self.assertEqual(agent.calls, [])
            self.assertFalse((store / run_id / "pages").exists())


class AgentEnvironment(unittest.TestCase):
    def test_anthropic_key_in_the_host_env_does_not_reach_the_agent(self):
        host = {
            "PATH": "/usr/bin",
            "HOME": "/home/x",
            "ANTHROPIC_API_KEY": "sk-secret",
            "ANTHROPIC_BASE_URL": "http://elsewhere",
            "MAX_THINKING_TOKENS": "1",
            "CLAUDE_CODE_OAUTH_TOKEN": "tok",
        }
        with mock.patch.dict(os.environ, host, clear=True), \
                mock.patch.object(runner.subprocess, "run") as run:  # fmt: skip
            run.return_value = completed("{}")
            runner.agent_turn(["claude"], Path("/tmp"))
        env = run.call_args.kwargs["env"]
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertNotIn("MAX_THINKING_TOKENS", env)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["HOME"], "/home/x")
        self.assertEqual(env["DISABLE_AUTOUPDATER"], "1")
        self.assertEqual(set(env) - set(mf.ENV_ALLOWLIST), set(mf.AGENT_ENV))


class CrashReceipt(unittest.TestCase):
    def test_missing_executable_leaves_a_runner_crash_receipt_and_reraises(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, run_id = make_run(tmp)
            gone = mock.patch.object(runner, "agent_turn", side_effect=FileNotFoundError("claude"))
            with registered(), gone:
                with self.assertRaises(FileNotFoundError):
                    runner.run_page(str(store), run_id, "T01", "screenshot")
            saved = json.loads(
                (store / run_id / "pages" / "T01" / "screenshot" / "run.json").read_text()
            )
            self.assertEqual(saved["status"], "runner_crash")
            self.assertEqual(saved["error"]["type"], "FileNotFoundError")
            self.assertEqual(saved["rounds"], [])

    def test_crash_after_a_round_keeps_the_rounds_made(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, run_id = make_run(tmp)
            calls = iter([completed(agent_json())])

            def once(argv, cwd):
                (Path(cwd) / "index.html").write_text("<html></html>", encoding="utf-8")
                try:
                    return next(calls)
                except StopIteration:
                    raise OSError("disk gone") from None

            fail = report("fail", ["x"])
            with registered(), mock.patch.object(runner, "agent_turn", side_effect=once), \
                    mock.patch.object(checklist, "run_bounded", return_value=fail):  # fmt: skip
                with self.assertRaises(OSError):
                    runner.run_page(str(store), run_id, "T01", "screenshot")
            saved = json.loads(
                (store / run_id / "pages" / "T01" / "screenshot" / "run.json").read_text()
            )
            self.assertEqual(saved["status"], "runner_crash")
            self.assertEqual(len(saved["rounds"]), 1)


class Transcript(unittest.TestCase):
    def test_session_file_is_copied_under_a_fake_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            agent = FakeAgent(home=home)
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                result, out = run_with(tmp, agent, lambda *a, **k: report("pass"))
            self.assertEqual(result["transcript"], "kept")
            self.assertEqual(
                (out / "rounds" / "transcript.jsonl").read_text(encoding="utf-8"), "{}\n"
            )

    def test_missing_session_file_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                result, out = run_with(tmp, FakeAgent(), lambda *a, **k: report("pass"))
            self.assertEqual(result["transcript"], "missing")
            self.assertFalse((out / "rounds" / "transcript.jsonl").exists())

    def test_slug_matches_the_claude_project_folder(self):
        project = Path("/home/brodey/.hermes/cache/scratch/c3probe2")
        slug = runner.transcript_path(project, "s").parent.name
        self.assertEqual(slug, "-home-brodey--hermes-cache-scratch-c3probe2")
        underscore = runner.transcript_path(Path("/tmp/a_b.c/x"), "s").parent.name
        self.assertEqual(underscore, "-tmp-a-b-c-x")

    def test_copy_failure_is_recorded_and_the_receipt_is_still_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            real_copy = shutil.copyfile

            def fail_transcript(src, dst, *a, **k):
                if str(dst).endswith("transcript.jsonl"):
                    raise PermissionError("denied")
                return real_copy(src, dst, *a, **k)

            copy = mock.patch.object(runner.shutil, "copyfile", side_effect=fail_transcript)
            with mock.patch.dict(os.environ, {"HOME": str(home)}), copy:
                result, out = run_with(tmp, FakeAgent(home=home), lambda *a, **k: report("pass"))
            self.assertEqual(result["transcript"], "lookup_failed: PermissionError")
            self.assertEqual(result["status"], "pass")
            self.assertTrue((out / "run.json").is_file())


class PathRefusals(unittest.TestCase):
    def test_symlinked_task_dir_is_refused_before_any_agent_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, run_id = make_run(tmp)
            elsewhere = Path(tmp) / "elsewhere"
            elsewhere.mkdir()
            (store / run_id / "pages").mkdir()
            (store / run_id / "pages" / "T01").symlink_to(elsewhere, target_is_directory=True)
            agent = FakeAgent()
            with registered(), mock.patch.object(runner, "agent_turn", side_effect=agent):
                with self.assertRaises(SystemExit) as caught:
                    runner.run_page(str(store), run_id, "T01", "screenshot")
            self.assertIn("symbolic link", str(caught.exception))
            self.assertEqual(agent.calls, [])
            self.assertEqual(list(elsewhere.iterdir()), [])


class ModelConformance(unittest.TestCase):
    def test_a_conforming_page_is_gate_eligible(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = run_with(tmp, FakeAgent(), lambda *a, **k: report("pass"))
            self.assertTrue(result["model_conforms"])
            self.assertTrue(result["gate_eligible"])

    def test_wrong_model_only_turn_is_not_conforming_and_halts_the_next_page(self):
        # Negative check for the model gate: a Haiku-only turn must not be gate eligible, and the
        # next page of the same run must be refused.
        with tempfile.TemporaryDirectory() as tmp:
            agent = FakeAgent(models=("claude-haiku-5-5",))
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result, out = run_with(tmp, agent, lambda *a, **k: report("pass"))
            self.assertIn("STOP: T01/screenshot", stderr.getvalue())
            self.assertEqual(result["status"], "model_mismatch")
            self.assertFalse(result["model_conforms"])
            self.assertFalse(result["gate_eligible"])
            saved = json.loads((out / "run.json").read_text(encoding="utf-8"))
            self.assertFalse(saved["model_conforms"])
            store = out.parents[3]
            with registered(), mock.patch.object(runner, "agent_turn", side_effect=FakeAgent()):
                with self.assertRaises(SystemExit) as caught:
                    runner.run_page(str(store), "r1", "T01", "hand-design-md")
            self.assertIn("T01/screenshot", str(caught.exception))
            self.assertFalse((store / "r1" / "pages" / "T01" / "hand-design-md").exists())

    def test_missing_model_usage_is_recorded_but_does_not_halt_the_gate(self):
        # Missing evidence is not a model change: recorded, and the next page may still run.
        with tempfile.TemporaryDirectory() as tmp:
            agent = FakeAgent(stdout=json.dumps({"session_id": "s", "is_error": False}))
            result, out = run_with(tmp, agent, lambda *a, **k: report("pass"))
            self.assertEqual(result["model_evidence"], "missing")
            self.assertTrue(result["model_conforms"])
            store = out.parents[3]
            passing = mock.patch.object(checklist, "run_bounded", return_value=report("pass"))
            with registered(), mock.patch.object(runner, "agent_turn", side_effect=FakeAgent()):
                with passing:
                    runner.run_page(str(store), "r1", "T01", "hand-design-md")

    def test_context_window_mismatch_is_not_conforming(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = FakeAgent(stdout=agent_json(window=200000))
            result, _ = run_with(tmp, agent, lambda *a, **k: report("pass"))
            self.assertFalse(result["model_conforms"])
            usage = result["rounds"][0]["agent"]["model_usage"][MODEL]
            self.assertEqual(usage["contextWindow"], 200000)

    def test_no_output_still_counts_for_the_gate_as_a_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = run_with(tmp, FakeAgent(), lambda *a, **k: report("no_runnable_output"))
            self.assertEqual(result["status"], "no_output")
            self.assertTrue(result["gate_eligible"])


class ModelMismatch(unittest.TestCase):
    def test_a_wrong_model_turn_with_a_failing_page_makes_no_further_agent_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = FakeAgent(models=("claude-haiku-5-5",))
            result, _ = run_with(tmp, agent, lambda *a, **k: report("fail", ["x"]))
            self.assertEqual(result["status"], "model_mismatch")
            self.assertFalse(result["model_conforms"])
            self.assertEqual(len(agent.calls), 1)

    def test_the_cli_exits_3_on_a_model_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, run_id = make_run(tmp)
            agent = FakeAgent(models=("claude-haiku-5-5",))
            turn = mock.patch.object(runner, "agent_turn", side_effect=agent)
            passing = mock.patch.object(checklist, "run_bounded", return_value=report("pass"))
            argv = ["--store", str(store), "--run", run_id, "T01", "screenshot"]
            with registered(), turn, passing, contextlib.redirect_stderr(io.StringIO()):
                code = runner.main(argv)
            self.assertEqual(code, 3)

    def test_an_output_cap_other_than_the_lock_is_a_mismatch_with_no_further_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = FakeAgent(stdout=agent_json(max_out=64000))
            result, _ = run_with(tmp, agent, lambda *a, **k: report("fail", ["x"]))
            self.assertEqual(result["status"], "model_mismatch")
            self.assertFalse(result["model_conforms"])
            self.assertEqual(len(agent.calls), 1)
            usage_seen = result["rounds"][0]["agent"]["model_usage"][MODEL]
            self.assertEqual(usage_seen["maxOutputTokens"], 64000)

    def test_checker_error_after_a_wrong_model_turn_keeps_the_round_and_halts_the_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, run_id = make_run(tmp)
            agent = FakeAgent(models=("claude-haiku-5-5",))

            def checker_down(*a, **k):
                raise OSError("checker down")

            turn = mock.patch.object(runner, "agent_turn", side_effect=agent)
            checks = mock.patch.object(checklist, "run_bounded", side_effect=checker_down)
            with registered(), turn, checks:
                with self.assertRaises(OSError):
                    runner.run_page(str(store), run_id, "T01", "screenshot")
            saved = json.loads(
                (store / run_id / "pages" / "T01" / "screenshot" / "run.json").read_text()
            )
            self.assertEqual(saved["status"], "model_mismatch")
            self.assertEqual(saved["error"]["type"], "OSError")
            self.assertEqual(len(saved["rounds"]), 1)
            self.assertFalse(saved["model_conforms"])
            self.assertEqual(len(agent.calls), 1)
            with registered(), mock.patch.object(runner, "agent_turn", side_effect=FakeAgent()):
                with self.assertRaises(SystemExit) as caught:
                    runner.run_page(str(store), run_id, "T01", "hand-design-md")
            self.assertIn("T01/screenshot", str(caught.exception))
            self.assertFalse((store / run_id / "pages" / "T01" / "hand-design-md").exists())


def interrupted_arm(store, run_id, task_id, arm, *turns):
    """A claimed page that stopped before publish: a project folder and one raw output per turn."""
    out = store / run_id / "pages" / task_id / arm
    (out / "rounds").mkdir(parents=True)
    (out / "project").mkdir()
    (out / "project" / "index.html").write_text("<html>page</html>", encoding="utf-8")
    for n, text in enumerate(turns):
        (out / "rounds" / f"agent-{n}.json").write_text(text, encoding="utf-8")
    return out


class Interrupted(unittest.TestCase):
    def test_an_interrupted_arm_refuses_the_next_page_with_no_agent_call(self):
        # Negative check for the interrupted guard: without it a wrong-model turn left by a killed
        # runner would not halt the gate, and the next page would run.
        with tempfile.TemporaryDirectory() as tmp:
            store, run_id = make_run(tmp)
            wrong = agent_json(models=("claude-haiku-5-5",))
            interrupted_arm(store, run_id, "T01", "screenshot", wrong)
            agent = FakeAgent()
            with registered(), mock.patch.object(runner, "agent_turn", side_effect=agent):
                with self.assertRaises(SystemExit) as caught:
                    runner.run_page(str(store), run_id, "T01", "hand-design-md")
            self.assertIn("T01/screenshot was interrupted", str(caught.exception))
            self.assertEqual(agent.calls, [])

    def test_reconcile_records_a_wrong_model_interrupt_and_keeps_the_gate_halted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, run_id = make_run(tmp)
            out = interrupted_arm(
                store, run_id, "T01", "screenshot", agent_json(models=("claude-haiku-5-5",))
            )
            no_turn = mock.patch.object(
                runner, "agent_turn", side_effect=AssertionError("no agent")
            )
            with registered(), no_turn:
                result = runner.reconcile(str(store), run_id, "T01", "screenshot")
            saved = json.loads((out / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "model_mismatch")
            self.assertFalse(saved["model_conforms"])
            self.assertTrue(saved["reconciled"])
            self.assertEqual(saved["error"]["type"], "Interrupted")
            self.assertEqual(saved["rounds"][0]["agent"]["error"], "interrupted before publish")
            self.assertIsNone(saved["rounds"][0]["checklist"])
            self.assertEqual(result, saved)
            self.assertEqual(sorted(p.name for p in (out / "project").iterdir()), ["index.html"])
            self.assertEqual((out / "project" / "index.html").read_text(), "<html>page</html>")
            with registered(), mock.patch.object(runner, "agent_turn", side_effect=FakeAgent()):
                with self.assertRaises(SystemExit) as caught:
                    runner.run_page(str(store), run_id, "T01", "hand-design-md")
            self.assertIn("T01/screenshot is not model-conforming", str(caught.exception))

    def test_reconcile_of_a_conforming_interrupt_lets_the_next_page_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, run_id = make_run(tmp)
            out = interrupted_arm(store, run_id, "T01", "screenshot", agent_json(session="sess-9"))
            with registered():
                saved = runner.reconcile(str(store), run_id, "T01", "screenshot")
            self.assertEqual(saved["status"], "runner_crash")
            self.assertTrue(saved["model_conforms"])
            self.assertEqual(saved["session_id"], "sess-9")
            self.assertTrue((out / "run.json").is_file())
            passing = mock.patch.object(checklist, "run_bounded", return_value=report("pass"))
            with (
                registered(),
                passing,
                mock.patch.object(runner, "agent_turn", side_effect=FakeAgent()),
            ):
                result = runner.run_page(str(store), run_id, "T01", "hand-design-md")
            self.assertEqual(result["status"], "pass")

    def test_reconcile_refuses_an_arm_that_already_has_run_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, run_id = make_run(tmp)
            out = interrupted_arm(store, run_id, "T01", "screenshot", agent_json())
            (out / "run.json").write_text("{}", encoding="utf-8")
            with registered(), self.assertRaises(SystemExit) as caught:
                runner.reconcile(str(store), run_id, "T01", "screenshot")
            self.assertIn("already has run.json", str(caught.exception))
            self.assertEqual((out / "run.json").read_text(encoding="utf-8"), "{}")

    def test_reconcile_refuses_a_missing_arm_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, run_id = make_run(tmp)
            with registered(), self.assertRaises(SystemExit) as caught:
                runner.reconcile(str(store), run_id, "T01", "screenshot")
            self.assertIn("does not exist", str(caught.exception))
            self.assertFalse((store / run_id / "pages").exists())

    def test_reconcile_cli_exits_3_on_a_model_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, run_id = make_run(tmp)
            interrupted_arm(
                store, run_id, "T01", "screenshot", agent_json(models=("claude-haiku-5-5",))
            )
            argv = ["--store", str(store), "--run", run_id, "--reconcile", "T01", "screenshot"]
            with registered(), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(runner.main(argv), 3)


class EvidenceBeforeParse(unittest.TestCase):
    def test_raw_output_is_on_disk_before_the_parser_is_entered(self):
        real_parse = runner.turn_from_output
        out = Path("unused")
        seen = []

        def check_then_parse(raw, *a, **k):
            seen.append((out / "rounds" / "agent-0.json").is_file())
            return real_parse(raw, *a, **k)

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "store" / "r1" / "pages" / "T01" / "screenshot"
            parse = mock.patch.object(runner, "turn_from_output", side_effect=check_then_parse)
            with parse:
                result, _ = run_with(tmp, FakeAgent(), lambda *a, **k: report("pass"))
            self.assertEqual(seen, [True])
            self.assertEqual(result["status"], "pass")

    def test_write_failure_after_wrong_model_turn_is_model_mismatch_and_halts_next_page(self):
        class BlockedRaw(FakeAgent):
            def __call__(self, argv, cwd):
                result = super().__call__(argv, cwd)
                (Path(cwd).parent / "rounds" / "agent-0.json").mkdir()
                return result

        with tempfile.TemporaryDirectory() as tmp:
            store, run_id = make_run(tmp)
            agent = BlockedRaw(models=("claude-haiku-5-5",))
            turn = mock.patch.object(runner, "agent_turn", side_effect=agent)
            with (
                registered(),
                turn,
                mock.patch.object(checklist, "run_bounded", return_value=report("fail", ["x"])),
            ):
                with self.assertRaises(OSError):
                    runner.run_page(str(store), run_id, "T01", "screenshot")
            saved = json.loads(
                (store / run_id / "pages" / "T01" / "screenshot" / "run.json").read_text()
            )
            self.assertEqual(saved["status"], "model_mismatch")
            self.assertEqual(len(saved["rounds"]), 1)
            self.assertFalse(saved["model_conforms"])
            with registered(), mock.patch.object(runner, "agent_turn", side_effect=FakeAgent()):
                with self.assertRaises(SystemExit) as caught:
                    runner.run_page(str(store), run_id, "T01", "hand-design-md")
            self.assertIn("T01/screenshot is not model-conforming", str(caught.exception))


class RunLock(unittest.TestCase):
    def test_held_lock_refuses_run_page_and_reconcile_with_no_agent_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, run_id = make_run(tmp)
            interrupted_arm(store, run_id, "T01", "screenshot", agent_json())
            agent = FakeAgent()
            turn = mock.patch.object(runner, "agent_turn", side_effect=agent)
            with registered(), turn, runner.run_lock(store / run_id):
                for call in (
                    lambda: runner.run_page(str(store), run_id, "T01", "hand-design-md"),
                    lambda: runner.reconcile(str(store), run_id, "T01", "screenshot"),
                ):
                    with self.assertRaises(SystemExit) as caught:
                        call()
                    self.assertIn("is active in another process", str(caught.exception))
                    self.assertNotIn("reconcile", str(caught.exception))
            self.assertEqual(agent.calls, [])

    def test_lock_is_released_after_a_page_so_the_next_page_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, run_id = make_run(tmp)
            passing = mock.patch.object(checklist, "run_bounded", return_value=report("pass"))
            turn = mock.patch.object(runner, "agent_turn", side_effect=FakeAgent())
            with registered(), turn, passing:
                first = runner.run_page(str(store), run_id, "T01", "screenshot")
                second = runner.run_page(str(store), run_id, "T01", "hand-design-md")
            self.assertEqual((first["status"], second["status"]), ("pass", "pass"))


class SessionPersistence(unittest.TestCase):
    def test_reconcile_of_an_arm_killed_before_agent0_uses_the_persisted_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            store, run_id = make_run(tmp)
            out = store / run_id / "pages" / "T01" / "screenshot"
            (out / "rounds").mkdir(parents=True)
            (out / "project").mkdir()
            (out / "rounds" / "session.txt").write_text("persisted-sess", encoding="utf-8")
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                transcript = runner.transcript_path(out / "project", "persisted-sess")
                transcript.parent.mkdir(parents=True)
                transcript.write_text("{}\n", encoding="utf-8")
                with registered():
                    saved = runner.reconcile(str(store), run_id, "T01", "screenshot")
            self.assertEqual(saved["status"], "runner_crash")
            self.assertEqual(saved["transcript"], "kept")
            self.assertEqual(saved["session_id"], "persisted-sess")


class ReconcileNeedsNoCli(unittest.TestCase):
    def test_reconcile_works_when_the_pinned_executable_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, run_id = make_run(tmp)
            interrupted_arm(store, run_id, "T01", "screenshot", agent_json())
            (Path(tmp) / "claude-2.1.325").unlink()
            no_cli = mock.patch.object(runner, "claude_version", side_effect=AssertionError("cli"))
            no_chrome = mock.patch.object(
                runner, "checker_chromium", side_effect=AssertionError("chromium")
            )
            with registered(), no_cli, no_chrome:
                saved = runner.reconcile(str(store), run_id, "T01", "screenshot")
            self.assertEqual(saved["status"], "runner_crash")
            self.assertTrue(saved["model_conforms"])


def read_url(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        with exc:
            return f"HTTP {exc.code}"


class LoopbackChecker(unittest.TestCase):
    def check(self, project, fetch=None):
        """Runs check_page with the checklist stubbed; returns the URL it was given."""
        seen = []

        def run_bounded(url, task_id, *a, **k):
            seen.append(url)
            if fetch is not None:
                fetch(url)
            return report("pass")

        with mock.patch.object(checklist, "run_bounded", side_effect=run_bounded):
            runner.check_page(project, "T01")
        return seen[0]

    def project(self, tmp):
        project = Path(tmp) / "project"
        project.mkdir()
        (project / "index.html").write_text("<html>page</html>", encoding="utf-8")
        return project

    def test_the_checklist_gets_a_loopback_url_not_a_file_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            url = self.check(self.project(tmp))
            self.assertRegex(url, r"^http://127\.0\.0\.1:\d+/index\.html$")

    def test_requests_cannot_reach_files_outside_the_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self.project(tmp)
            (Path(tmp) / "outside.txt").write_text("OUTSIDE SECRET", encoding="utf-8")
            got = {}

            def fetch(url):
                base = url[: -len("/index.html")]
                got["page"] = read_url(base + "/index.html")
                for path in ("/../outside.txt", "/%2e%2e/outside.txt"):
                    got[path] = read_url(base + path)

            self.check(project, fetch)
            self.assertEqual(got["page"], "<html>page</html>")
            self.assertNotIn("OUTSIDE SECRET", got["/../outside.txt"])
            self.assertNotIn("OUTSIDE SECRET", got["/%2e%2e/outside.txt"])

    def test_the_server_is_down_once_the_check_ends(self):
        with tempfile.TemporaryDirectory() as tmp:
            port = urlsplit(self.check(self.project(tmp))).port
            with self.assertRaises(OSError):
                socket.create_connection(("127.0.0.1", port), timeout=5)


class PathLength(unittest.TestCase):
    def test_a_transcript_folder_name_over_255_is_refused_before_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            deep = Path(tmp) / ("d" * 120) / ("e" * 120)
            deep.mkdir(parents=True)
            store, run_id = make_run(deep)
            agent = FakeAgent()
            with registered(), mock.patch.object(runner, "agent_turn", side_effect=agent):
                with self.assertRaises(SystemExit) as caught:
                    runner.run_page(str(store), run_id, "T01", "screenshot")
            self.assertRegex(str(caught.exception), r"is \d+ characters; the limit is 255")
            self.assertEqual(agent.calls, [])
            self.assertFalse((store / run_id / "pages").exists())


class ConformanceFailsClosed(unittest.TestCase):
    def prior(self, store, run_id, text):
        out = store / run_id / "pages" / "T02" / "screenshot"
        out.mkdir(parents=True)
        (out / "run.json").write_text(text, encoding="utf-8")

    def refused(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            store, run_id = make_run(tmp)
            self.prior(store, run_id, text)
            agent = FakeAgent()
            with registered(), mock.patch.object(runner, "agent_turn", side_effect=agent):
                with self.assertRaises(SystemExit) as caught:
                    runner.run_page(str(store), run_id, "T01", "hand-design-md")
            self.assertEqual(agent.calls, [])
            self.assertFalse((store / run_id / "pages" / "T01").exists())
            return str(caught.exception)

    def test_a_prior_run_json_with_no_verdict_refuses_before_claim(self):
        self.assertIn("T02/screenshot is not model-conforming", self.refused("{}"))

    def test_a_prior_run_json_that_is_not_json_refuses_before_claim(self):
        self.assertIn("is not readable JSON", self.refused("x"))


class PermissionAudit(unittest.TestCase):
    def test_denials_are_recorded_by_tool_and_path_and_flagged(self):
        # Negative check for the escape audit: without it a denied write leaves no trace.
        denial = {
            "tool_name": "Write",
            "tool_use_id": "t1",
            "tool_input": {"file_path": "/elsewhere/index.html", "content": "secret body"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            agent = FakeAgent(stdout=agent_json(denials=[denial]))
            result, _ = run_with(tmp, agent, lambda *a, **k: report("pass"))
            self.assertEqual(result["denials_total"], 1)
            self.assertEqual(result["audit_flag"], "permission_denials")
            kept = result["rounds"][0]["agent"]["permission_denials"]
            self.assertEqual(kept, [{"tool_name": "Write", "path": "/elsewhere/index.html"}])

    def test_no_denials_leaves_no_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = run_with(tmp, FakeAgent(), lambda *a, **k: report("pass"))
            self.assertEqual(result["denials_total"], 0)
            self.assertIsNone(result["audit_flag"])


class StorePath(unittest.TestCase):
    def test_a_symlinked_store_is_refused_before_any_agent_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, run_id = make_run(tmp)
            link = Path(tmp) / "link"
            link.symlink_to(store, target_is_directory=True)
            agent = FakeAgent()
            with registered(), mock.patch.object(runner, "agent_turn", side_effect=agent):
                with self.assertRaises(SystemExit) as caught:
                    runner.run_page(str(link), run_id, "T01", "screenshot")
            self.assertIn("symbolic link", str(caught.exception))
            self.assertEqual(agent.calls, [])

    def test_a_symlinked_parent_of_the_store_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, run_id = make_run(tmp)
            up = Path(tmp) / "up"
            up.symlink_to(Path(tmp), target_is_directory=True)
            with registered(), mock.patch.object(runner, "agent_turn", side_effect=FakeAgent()):
                with self.assertRaises(SystemExit) as caught:
                    runner.run_page(str(up / "store"), run_id, "T01", "screenshot")
            self.assertIn("symbolic link", str(caught.exception))


class Refusals(unittest.TestCase):
    def test_unknown_task_is_refused_before_any_agent_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, run_id = make_run(tmp)
            agent = FakeAgent()
            with registered(), mock.patch.object(runner, "agent_turn", side_effect=agent):
                with self.assertRaises(SystemExit) as caught:
                    runner.run_page(str(store), run_id, "T99", "screenshot")
            self.assertIn("unknown task", str(caught.exception))
            self.assertEqual(agent.calls, [])

    def test_unknown_arm_is_refused_before_any_agent_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, run_id = make_run(tmp)
            agent = FakeAgent()
            with registered(), mock.patch.object(runner, "agent_turn", side_effect=agent):
                with self.assertRaises(SystemExit) as caught:
                    runner.run_page(str(store), run_id, "T01", "bogus-arm")
            self.assertIn("unknown arm", str(caught.exception))
            self.assertEqual(agent.calls, [])


class RegistrationGuards(unittest.TestCase):
    def setUp(self):
        # A removed guard must fail the test, not reach a real claude call.
        patcher = mock.patch.object(
            runner, "agent_turn", side_effect=AssertionError("the agent must not run")
        )
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
