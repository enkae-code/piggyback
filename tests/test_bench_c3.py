"""C3 checklist and prompt assembly, without a browser. Browser checks: test_bench_c3_browser.py."""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

from bench.c3 import checklist, prompts
from piggyback.proxy import FORBIDDEN

# Hermes ruling, not spec text: the default runtime line that fills the runtime placeholder.
RUNTIME_RULING = (
    "The page is checked offline: requests to any other host are blocked, so keep "
    "every script, font and image inside the project."
)

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "c3" / "spec_text.json"
AXE = ROOT / "bench" / "c3" / "vendor" / "axe-core" / "axe.min.js"
README = ROOT / "bench" / "c3" / "README.md"
ARM_IDS = ["piggyback-brief", "screenshot", "skill-alone", "skill-plus-piggyback"]


def spec_text():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class SpecFreeze(unittest.TestCase):
    def test_tasks_are_the_frozen_ten_rows(self):
        tasks = json.loads(checklist.TASKS_PATH.read_text(encoding="utf-8"))["tasks"]
        frozen = spec_text()["tasks"]
        self.assertEqual([t["id"] for t in tasks], [f"T{n:02d}" for n in range(1, 11)])
        for task in tasks:
            self.assertEqual(task["build_request"], frozen[task["id"]]["build_request"])
            self.assertEqual(task["reference"], frozen[task["id"]]["reference"])
            self.assertTrue(task["predicates"], task["id"])

    def test_one_predicate_per_content_item_in_each_row(self):
        # Content items named in each task row, in the order the row lists them.
        items = {
            "T01": 4, "T02": 3, "T03": 6, "T04": 3, "T05": 4,
            "T06": 4, "T07": 4, "T08": 3, "T09": 6, "T10": 4,
        }  # fmt: skip
        tasks = json.loads(checklist.TASKS_PATH.read_text(encoding="utf-8"))["tasks"]
        self.assertEqual({t["id"]: len(t["predicates"]) for t in tasks}, items)

    def test_prompt_strings_are_verbatim_spec(self):
        frozen = spec_text()
        self.assertEqual(prompts.SHARED_INSTRUCTION, frozen["shared_instruction"])
        self.assertEqual(prompts.ARM_BLOCKS, frozen["arms"])

    def test_prompt_is_the_ordered_assembly_of_its_parts(self):
        frozen = spec_text()
        for task in prompts.load_tasks():
            for arm in ARM_IDS:
                expected = "\n\n".join(
                    [
                        frozen["shared_instruction"],
                        task["build_request"],
                        f"Reference direction: {task['reference']}",
                        frozen["arms"][arm],
                        RUNTIME_RULING,
                    ]
                )
                with self.subTest(task=task["id"], arm=arm):
                    self.assertEqual(prompts.expand(task, arm), expected)

    def test_runtime_default_is_the_hermes_ruling(self):
        self.assertEqual(prompts.RUNTIME_DEFAULT, RUNTIME_RULING)

    def test_write_all_writes_forty_prompts_in_full(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = prompts.write_all(tmp)
            self.assertEqual(len(written), 40)
            self.assertEqual(len(list(Path(tmp).glob("*.txt"))), 40)
            task = prompts.load_tasks()[0]
            text = (Path(tmp) / "T01-screenshot.txt").read_text(encoding="utf-8")
            self.assertEqual(text, prompts.expand(task, "screenshot") + "\n")

    def test_readme_states_the_runtime_default_rule(self):
        self.assertIn("runtime_default", README.read_text(encoding="utf-8"))

    def test_axe_hash_matches_readme(self):
        digest = hashlib.sha256(AXE.read_bytes()).hexdigest()
        self.assertIn(digest, README.read_text(encoding="utf-8"))


class Predicates(unittest.TestCase):
    def snap(self, text="", counts=None, controls=()):
        return {"text": text, "counts": counts or {}, "controls": list(controls)}

    def test_text_is_case_insensitive_search(self):
        pred = {"item": "x", "kind": "text", "pattern": "status|summary", "rule": "r"}
        self.assertTrue(checklist.evaluate_predicate(pred, self.snap("Status Board"))["pass"])
        self.assertFalse(checklist.evaluate_predicate(pred, self.snap("hello"))["pass"])

    def test_count_needs_min(self):
        pred = {"item": "x", "kind": "count", "any": [{"tag": "tr"}], "min": 3, "rule": "r"}

        def rows(n, visible=True):
            return [
                {"tag": "tr", "attrs": {}, "visible": visible, "path": [], "text": ""}
                for _ in range(n)
            ]

        def snap_of(nodes):
            return {"text": "", "nodes": nodes, "controls": []}

        self.assertTrue(checklist.evaluate_predicate(pred, snap_of(rows(3)))["pass"])
        self.assertFalse(checklist.evaluate_predicate(pred, snap_of(rows(2)))["pass"])
        self.assertFalse(checklist.evaluate_predicate(pred, snap_of(rows(3, False)))["pass"])

    def test_control_name_matches_an_accessible_name(self):
        pred = {"item": "x", "kind": "control_name", "pattern": "acknowledg", "rule": "r"}
        ok = self.snap(controls=["Acknowledge alert"])
        self.assertTrue(checklist.evaluate_predicate(pred, ok)["pass"])
        self.assertFalse(checklist.evaluate_predicate(pred, self.snap(controls=["Close"]))["pass"])

    def test_unknown_kind_is_an_error(self):
        with self.assertRaises(ValueError):
            checklist.evaluate_predicate({"item": "x", "kind": "vibe", "rule": "r"}, self.snap())


class Checks(unittest.TestCase):
    def test_overflow_is_strictly_wider_than_viewport(self):
        self.assertTrue(checklist.has_horizontal_overflow(1441, 1440))
        self.assertFalse(checklist.has_horizontal_overflow(1440, 1440))

    def checks(self, **over):
        base = {
            "render": {"pass": True},
            "content": [],
            "overflow": [{"width": 1440, "pass": True}, {"width": 390, "pass": True}],
            "axe": {"error": None, "violations": [], "incomplete": [{"id": "x"}], "pass": True},
        }
        base.update(over)
        return base

    def test_passing_page_has_no_items_and_no_message(self):
        failed = checklist.failed_items(self.checks())
        self.assertEqual(failed, [])
        self.assertIsNone(checklist.correction_message(failed))

    def test_incomplete_results_never_fail(self):
        self.assertEqual(checklist.failed_items(self.checks()), [])

    def test_render_failure_names_the_first_page_error(self):
        checks = self.checks(
            render={"pass": False, "page_errors": ["Error: boom\nat x.js:1", "later"]}
        )
        self.assertEqual(
            checklist.failed_items(checks),
            [f"{checklist.RENDER_ITEM}; first page error: Error: boom"],
        )

    def test_page_error_alone_does_not_fail_a_rendered_page(self):
        checks = self.checks(
            render={"pass": True, "page_errors": ["late"], "console_errors": ["404"]}
        )
        self.assertEqual(checklist.failed_items(checks), [])

    def test_content_item_message_carries_the_frozen_rule(self):
        checks = self.checks(
            content=[
                {
                    "item": "standfirst",
                    "rule": "a paragraph directly after the main headline",
                    "pass": False,
                }
            ]
        )
        self.assertEqual(
            checklist.failed_items(checks),
            ["content item missing: standfirst (a paragraph directly after the main headline)"],
        )

    def test_geometry_offender_fails_overflow_even_when_scroll_width_fits(self):
        overflow = [
            {"width": 1440, "scroll_width": 1440, "client_width": 1440, "offender_count": 1},
            {"width": 390, "scroll_width": 390, "client_width": 390, "offender_count": 0},
        ]
        built = checklist.build_checks(
            {"text": "x", "nodes": [], "controls": []},
            [dict(o) for o in overflow],
            {"error": None, "violations": [], "incomplete": []},
            [],
            [],
            [],
        )
        self.assertEqual([o["pass"] for o in built["overflow"]], [False, True])

    def test_empty_body_fails_render_with_no_page_error(self):
        built = checklist.build_checks(
            {"text": "   ", "nodes": [], "controls": []},
            [],
            {"error": None, "violations": [], "incomplete": []},
            [],
            [],
            [],
        )
        self.assertFalse(built["render"]["pass"])
        self.assertEqual(checklist.failed_items(built), [checklist.RENDER_ITEM])

    def test_each_failed_check_adds_one_verbatim_item(self):
        checks = self.checks(
            render={"pass": False, "page_errors": []},
            content=[
                {
                    "item": "vessel/asset table",
                    "rule": "a visible table with at least 3 rows that hold data cells",
                    "pass": False,
                }
            ],
            overflow=[{"width": 1440, "pass": False}, {"width": 390, "pass": True}],
            axe={
                "error": None,
                "violations": [{"id": "image-alt", "help": "Images must have alternate text"}],
                "incomplete": [],
                "pass": False,
            },
        )
        self.assertEqual(
            checklist.failed_items(checks),
            [
                checklist.RENDER_ITEM,
                "content item missing: vessel/asset table "
                "(a visible table with at least 3 rows that hold data cells)",
                checklist.OVERFLOW_ITEM.format(width=1440),
                "axe-core violation image-alt: Images must have alternate text",
            ],
        )

    def test_axe_error_fails_closed(self):
        checks = self.checks(
            axe={"error": "no axe", "violations": [], "incomplete": [], "pass": False}
        )
        self.assertEqual(checklist.failed_items(checks), ["axe-core could not run: no axe"])

    def test_overflow_and_render_items_say_what_passes(self):
        self.assertEqual(
            checklist.OVERFLOW_ITEM.format(width=390),
            "horizontal overflow at 390 px: the page must not scroll sideways, and no element may "
            "end past the 390 px viewport edge unless an overflow hidden or clip ancestor clips it",
        )
        self.assertEqual(checklist.RENDER_ITEM, "page renders: the body must show rendered text")

    def test_renderer_crash_during_axe_is_not_a_result(self):
        from unittest import mock

        page = mock.MagicMock()
        page.evaluate.side_effect = RuntimeError("Target crashed")
        with self.assertRaises(RuntimeError):
            checklist.run_axe(page)

    def test_axe_script_error_with_a_live_page_still_fails_closed(self):
        from unittest import mock

        page = mock.MagicMock()
        page.evaluate.side_effect = [RuntimeError("axe bad"), 1]
        result = checklist.run_axe(page)
        self.assertEqual((result["error"], result["pass"]), ("axe bad", False))

    def kill_calls(self, windows):
        # killpg and SIGKILL are POSIX-only; on a real Windows run only the taskkill case is taken.
        from unittest import mock

        return (
            mock.patch.object(checklist, "WINDOWS", windows),
            mock.patch.object(checklist.os, "killpg", create=True),
            mock.patch.object(checklist.subprocess, "run"),
        )

    def test_worker_past_its_bound_is_killed_with_its_tree(self):
        from unittest import mock

        for windows in (False, True):
            if not windows and os.name == "nt":
                continue
            with self.subTest(windows=windows):
                worker = mock.MagicMock(pid=4242)
                worker.communicate.side_effect = [
                    subprocess.TimeoutExpired("worker", 1),
                    ("", ""),
                ]
                patches = self.kill_calls(windows)
                with (
                    mock.patch.object(checklist.subprocess, "Popen", return_value=worker),
                    patches[0],
                    patches[1] as killpg,
                    patches[2] as taskkill,
                ):
                    report = checklist.run_bounded("page.html", "T01", total_s=1)
                if windows:
                    taskkill.assert_called_once_with(
                        ["taskkill", "/T", "/F", "/PID", "4242"], capture_output=True
                    )
                    killpg.assert_not_called()
                else:
                    killpg.assert_called_once_with(4242, signal.SIGKILL)
                    taskkill.assert_not_called()
                self.assertEqual(report["outcome"], "no_runnable_output")
                self.assertEqual(report["detail"], "checklist timed out")
                self.assertIsNone(report["correction_message"])

    def test_interrupt_kills_the_worker_tree_and_reraises(self):
        from unittest import mock

        for windows in (False, True):
            if not windows and os.name == "nt":
                continue
            with self.subTest(windows=windows):
                worker = mock.MagicMock(pid=4343)
                worker.communicate.side_effect = KeyboardInterrupt()
                patches = self.kill_calls(windows)
                with (
                    mock.patch.object(checklist.subprocess, "Popen", return_value=worker),
                    patches[0],
                    patches[1] as killpg,
                    patches[2] as taskkill,
                    self.assertRaises(KeyboardInterrupt),
                ):
                    checklist.run_bounded("page.html", "T01", total_s=1)
                if windows:
                    taskkill.assert_called_once_with(
                        ["taskkill", "/T", "/F", "/PID", "4343"], capture_output=True
                    )
                    killpg.assert_not_called()
                else:
                    killpg.assert_called_once_with(4343, signal.SIGKILL)
                    taskkill.assert_not_called()

    def test_relative_file_target_is_resolved_in_the_callers_directory(self):

        if importlib.util.find_spec("playwright") is None:
            self.skipTest("playwright not installed; run with the measure extra")
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "page.html").write_text("<p>x</p>", encoding="utf-8")
            old = os.getcwd()
            os.chdir(tmp)
            try:
                report = checklist.run_bounded("page.html", "T01", total_s=60)
            finally:
                os.chdir(old)
        # The worker reports a file:// URL once a browser has loaded the page.
        self.assertNotEqual(report["outcome"], "checker_fault", report.get("detail"))
        self.assertIn(str(Path(tmp, "page.html").resolve()), report["target"])
        self.assertNotIn("refusing non-loopback target", report.get("detail") or "")

    def test_message_template_lists_items_verbatim(self):
        message = checklist.correction_message(["a item", "b item"])
        self.assertEqual(
            message,
            "The page failed these checklist items:\n- a item\n- b item\n"
            "Fix each listed item and return the complete page.",
        )


class TargetPolicy(unittest.TestCase):
    def test_loopback_and_file_targets_are_allowed(self):
        self.assertEqual(checklist.target_url("http://127.0.0.1:8123/a"), "http://127.0.0.1:8123/a")
        self.assertEqual(checklist.target_url("http://localhost:8123/"), "http://127.0.0.1:8123/")
        with tempfile.NamedTemporaryFile(suffix=".html") as page:
            self.assertTrue(checklist.target_url(page.name).startswith("file://"))

    def test_public_or_private_targets_are_refused(self):
        for target in [
            "https://example.com/",
            "http://10.0.0.1/",
            "http://printer.local/",
            "http://app.localhost:3000/",
            "missing-file.html",
            "ftp://127.0.0.1/",
        ]:
            with self.subTest(target=target), self.assertRaises(ValueError):
                checklist.target_url(target)

    def test_page_requests_are_limited_to_the_target_origin(self):
        origin = ("127.0.0.1", 8123)
        self.assertTrue(checklist.allowed_request("file:///x.html", origin))
        self.assertTrue(checklist.allowed_request("data:text/plain,a", None))
        self.assertTrue(checklist.allowed_request("http://127.0.0.1:8123/x.js", origin))
        self.assertFalse(checklist.allowed_request("http://127.0.0.1:9/x.js", origin))
        self.assertFalse(checklist.allowed_request("https://cdn.example.com/x.js", origin))

    def test_file_target_allows_no_http_request(self):
        self.assertFalse(checklist.allowed_request("http://127.0.0.1:8123/", None))

    def test_unknown_task_is_an_error(self):
        with self.assertRaises(ValueError):
            checklist.predicates_for("T99")


class EgressPolicy(unittest.TestCase):
    class FakeClient:
        def __init__(self):
            self.sent = []

        def sendall(self, data):
            self.sent.append(data)

    def proxy(self, origin):
        return checklist.TargetProxy(origin)

    def test_only_the_target_origin_is_connected_to(self):
        with contextlib.closing(self.proxy(("127.0.0.1", 8123))) as egress:
            for host, port in [("203.0.113.7", 9), ("127.0.0.1", 9), ("localhost", 8123)]:
                client = self.FakeClient()
                with self.subTest(host=host, port=port):
                    self.assertIsNone(egress._open(client, host, port, f"{host}:{port}"))
                    self.assertEqual(client.sent, [FORBIDDEN])
            self.assertEqual(len(egress.blocked), 3)

    def test_file_target_has_no_origin_and_connects_nowhere(self):
        with contextlib.closing(self.proxy(None)) as egress:
            client = self.FakeClient()
            self.assertIsNone(egress._open(client, "127.0.0.1", 8123, "127.0.0.1:8123"))
            self.assertEqual(client.sent, [FORBIDDEN])

    def test_origin_of_reads_scheme_host_and_default_port(self):
        self.assertEqual(checklist.origin_of("http://127.0.0.1:8123/a?b"), ("127.0.0.1", 8123))
        self.assertEqual(checklist.origin_of("http://127.0.0.1/"), ("127.0.0.1", 80))
        self.assertIsNone(checklist.origin_of("file:///x.html"))


class MainDocument(unittest.TestCase):
    URL = "http://127.0.0.1:9/"

    def response(self, status, url=URL):
        return types.SimpleNamespace(status=status, url=url)

    def test_no_response_is_not_runnable(self):
        with self.assertRaises(checklist.NotRunnable) as caught:
            checklist.main_document(None, self.URL)
        self.assertEqual(caught.exception.main, {"status": None, "final_url": None})

    def test_proxy_refusal_or_http_error_is_not_runnable(self):
        for status in (403, 404, 502):
            with self.subTest(status=status), self.assertRaises(checklist.NotRunnable) as caught:
                checklist.main_document(self.response(status), self.URL)
            self.assertEqual(caught.exception.main["status"], status)
            self.assertIn(f"HTTP {status}", str(caught.exception))

    def test_success_reports_status_and_final_url_after_redirects(self):
        final = "http://127.0.0.1:9/final.html"
        self.assertEqual(
            checklist.main_document(self.response(200, final), self.URL),
            {"status": 200, "final_url": final},
        )

    def test_file_target_without_a_response_is_not_a_failure(self):
        url = Path("/x/page.html").resolve().as_uri()
        self.assertEqual(checklist.main_document(None, url), {"status": None, "final_url": url})


class CheckerFaults(unittest.TestCase):
    """The page-side and checker-side failure paths, with Playwright replaced by a mock."""

    def fake_playwright(self, goto_error=None, launch_error=None, goto_response=None):
        from unittest import mock

        module = mock.MagicMock()
        p = module.sync_playwright.return_value.__enter__.return_value
        p.chromium.executable_path = "/bundled/chromium"
        if launch_error is not None:
            p.chromium.launch.side_effect = launch_error
        browser = p.chromium.launch.return_value
        browser.version = "1.0.0"
        page = browser.new_context.return_value.new_page.return_value
        if goto_error is not None:
            page.goto.side_effect = goto_error
        if goto_response is not None:
            page.goto.return_value = goto_response
        return mock.patch.dict(sys.modules, {"playwright.sync_api": module}), browser

    def run_main(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = checklist.main([*argv, "--worker"])  # in-process, so the mocks apply
        return code, json.loads(out.getvalue())

    def test_navigation_failure_is_no_runnable_output_with_json_and_no_correction(self):
        patcher, browser = self.fake_playwright(goto_error=RuntimeError("net::ERR_TIMED_OUT\nat x"))
        with patcher:
            code, report = self.run_main(["http://127.0.0.1:9/", "--task", "T01"])
        self.assertEqual(code, 1)
        self.assertEqual(report["outcome"], "no_runnable_output")
        self.assertEqual(report["detail"], "net::ERR_TIMED_OUT")
        self.assertIsNone(report["correction_message"])
        self.assertEqual(report["failed"], [])
        browser.close.assert_called_once()

    def test_http_error_main_document_is_no_runnable_output_with_status(self):
        error_page = types.SimpleNamespace(status=502, url="http://127.0.0.1:9/")
        patcher, browser = self.fake_playwright(goto_response=error_page)
        with patcher:
            code, report = self.run_main(["http://127.0.0.1:9/", "--task", "T01"])
        self.assertEqual(code, 1)
        self.assertEqual(report["outcome"], "no_runnable_output")
        self.assertEqual(report["main_document"], {"status": 502, "final_url": error_page.url})
        self.assertIsNone(report["correction_message"])
        self.assertEqual(report["failed"], [])
        browser.close.assert_called_once()

    def test_browser_launch_failure_is_a_checker_fault_with_json(self):
        patcher, _ = self.fake_playwright(launch_error=RuntimeError("Executable doesn't exist"))
        with patcher:
            code, report = self.run_main(["http://127.0.0.1:9/", "--task", "T01"])
        self.assertEqual(code, 2)
        self.assertEqual(report["outcome"], "checker_fault")
        self.assertEqual(report["detail"], "Executable doesn't exist")
        self.assertIsNone(report["correction_message"])

    def test_unknown_task_is_a_checker_fault_with_json(self):
        code, report = self.run_main(["http://127.0.0.1:9/", "--task", "T99"])
        self.assertEqual(code, 2)
        self.assertEqual(report["outcome"], "checker_fault")

    def test_refused_target_is_a_checker_fault_with_json(self):
        code, report = self.run_main(["https://example.com/", "--task", "T01"])
        self.assertEqual(code, 2)
        self.assertIn("refusing non-loopback target", report["detail"])

    def test_checklist_failure_exits_one_with_exactly_one_correction(self):
        failed = {
            "target": "file:///x.html",
            "task": "T01",
            "outcome": "fail",
            "pass": False,
            "failed": [
                "content item missing: acknowledge CTA (a button or link named acknowledge)"
            ],
        }
        from unittest import mock

        report = checklist.report_for(
            failed["target"], "T01", checklist.FAIL, failed=failed["failed"]
        )
        self.assertEqual(report["correction_message"].count("- content item missing"), 1)
        with mock.patch.object(checklist, "run_checklist", return_value=report):
            code, printed = self.run_main(["x.html", "--task", "T01"])
        self.assertEqual(code, 1)
        self.assertEqual(printed["correction_message"], report["correction_message"])


if __name__ == "__main__":
    unittest.main()
