"""Craft and REA plumbing: child processes, the Craft MCP session and the browser endpoint."""

import http.server
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from piggyback import tools

FAKE = Path(__file__).parent / "fixtures" / "fake_craft_mcp.py"
# Stop 1 is the changed one: its hypothesis follows its own compare, before stop 2.
ORDER = [
    "core_brief_write",
    "web_pixels_compare",
    "web_pixels_compare",
    "core_hypothesis_record",
    "web_pixels_compare",
    "core_handoff_write",
]
ENV_OK = {"PATH", "HOME", "LC_CTYPE"}  # LC_CTYPE is set by the Python interpreter itself


def fake(root, **settings):
    head = "".join(f"{name} = {value!r}\n" for name, value in settings.items())
    path = Path(root) / "craft-anything-cli"
    path.write_text(
        f"#!{sys.executable}\n" + head + FAKE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    path.chmod(0o755)
    return str(path)


def write_script(root, name, body):
    path = Path(root) / name
    path.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class McpSessionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.pid_file = self.root / "pid"

    def steps(self, binary):
        self.folder = Path(tempfile.mkdtemp(dir=self.root))  # each call gets its own kit folder
        stops = [
            (
                k,
                300 * k,
                self.folder / "scroll" / f"frame-{k}-arrival.png",
                self.folder / "scroll" / f"frame-{k}.png",
            )
            for k in range(3)
        ]
        return tools.craft_steps(binary, self.folder / "craft", "happyrobot.ai", stops)

    def test_one_session_runs_every_step_and_reports_the_changed_stop(self):
        log = self.root / "log.txt"
        self.assertEqual(self.steps(fake(self.root, LOG=str(log))), [1])
        rows = [line.split(" ", 2) for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len({pid for pid, _, _ in rows}), 1)
        self.assertEqual([name for _, name, _ in rows], ORDER)
        self.assertTrue((self.folder / "craft" / "HANDOFF.md").exists())

    def test_the_journal_is_removed_before_publish_because_it_records_absolute_paths(self):
        self.assertEqual(self.steps(fake(self.root, JOURNAL=True)), [1])
        self.assertFalse((self.folder / "craft" / "recon.cany" / "journal").exists())

    def test_text_content_is_parsed_when_structured_content_is_missing(self):
        self.assertEqual(self.steps(fake(self.root, MODE="text")), [1])

    def test_a_reply_for_another_id_is_skipped(self):
        self.assertEqual(self.steps(fake(self.root, MODE="wrongid")), [1])

    def test_a_hung_craft_is_killed_at_the_deadline(self):
        with (
            mock.patch.object(tools, "MCP_DEADLINE_S", 1),
            self.assertRaises(tools.ToolError) as ctx,
        ):
            self.steps(fake(self.root, MODE="hang", PIDFILE=str(self.pid_file)))
        self.assertEqual(ctx.exception.reason, "timeout")
        self.assertFalse(alive(int(self.pid_file.read_text())))

    def test_a_child_that_hangs_before_initialize_is_failed_and_reaped(self):
        with (
            mock.patch.object(tools, "MCP_DEADLINE_S", 1),
            self.assertRaises(tools.ToolError) as ctx,
        ):
            self.steps(fake(self.root, MODE="hanginit", PIDFILE=str(self.pid_file)))
        self.assertEqual(ctx.exception.reason, "timeout")
        self.assertFalse(alive(int(self.pid_file.read_text())))

    def test_an_unterminated_200_mib_line_fails_fast_and_the_child_is_reaped(self):
        started = time.monotonic()
        with self.assertRaises(tools.ToolError) as ctx:
            self.steps(fake(self.root, MODE="bigbytes", PIDFILE=str(self.pid_file)))
        self.assertEqual(ctx.exception.reason, "output too large")
        self.assertLess(time.monotonic() - started, 30)
        self.assertFalse(alive(int(self.pid_file.read_text())))

    def test_a_child_that_stops_reading_stdin_hits_the_deadline_on_write(self):
        with (
            mock.patch.object(tools, "MCP_DEADLINE_S", 1),
            tools._McpSession(
                fake(self.root, MODE="stall", PIDFILE=str(self.pid_file)), self.root / "p"
            ) as mcp,
            self.assertRaises(tools.ToolError) as ctx,
        ):
            mcp.tool("core_brief_write", {"text": "x" * 4_000_000})
        self.assertEqual(ctx.exception.reason, "timeout")
        self.assertFalse(alive(int(self.pid_file.read_text())))

    def test_a_boolean_id_never_matches_a_request(self):
        with (
            mock.patch.object(tools, "MCP_DEADLINE_S", 1),
            self.assertRaises(tools.ToolError) as ctx,
        ):
            self.steps(fake(self.root, MODE="boolid"))
        self.assertEqual(ctx.exception.reason, "timeout")

    def test_an_error_result_fails_the_stage(self):
        with self.assertRaises(tools.ToolError) as ctx:
            self.steps(fake(self.root, MODE="isError"))
        self.assertEqual(ctx.exception.reason, "craft error")

    def test_a_json_rpc_error_fails_the_stage(self):
        with self.assertRaises(tools.ToolError) as ctx:
            self.steps(fake(self.root, MODE="rpcError"))
        self.assertEqual(ctx.exception.reason, "craft error")

    def test_an_oversized_line_fails_the_stage(self):
        with (
            mock.patch.object(tools, "MCP_LINE_MAX", 1000),
            self.assertRaises(tools.ToolError) as ctx,
        ):
            self.steps(fake(self.root, MODE="bigline"))
        self.assertEqual(ctx.exception.reason, "output too large")

    def test_total_output_is_charged_as_it_arrives(self):
        with (
            mock.patch.object(tools, "MCP_TOTAL_MAX", 100),
            self.assertRaises(tools.ToolError) as ctx,
        ):
            self.steps(fake(self.root))
        self.assertEqual(ctx.exception.reason, "output too large")

    def test_a_deeply_nested_reply_is_bad_output(self):
        with self.assertRaises(tools.ToolError) as ctx:
            self.steps(fake(self.root, MODE="deep"))
        self.assertEqual(ctx.exception.reason, "bad output")

    def test_text_that_is_not_a_string_is_bad_output(self):
        for mode in ("text5", "textdict"):
            with self.subTest(mode=mode), self.assertRaises(tools.ToolError) as ctx:
                self.steps(fake(self.root, MODE=mode))
            self.assertEqual(ctx.exception.reason, "bad output")

    def test_the_child_gets_path_and_home_only(self):
        env_log = self.root / "env.json"
        with mock.patch.dict(os.environ, {"PIGGYBACK_SECRET_TEST": "hunter2"}):
            self.steps(fake(self.root, ENVLOG=str(env_log)))
        keys = set(json.loads(env_log.read_text(encoding="utf-8")))
        self.assertNotIn("PIGGYBACK_SECRET_TEST", keys)
        self.assertLessEqual(keys, ENV_OK)

    def test_the_child_stdout_is_closed_after_the_session(self):
        with tools._McpSession(fake(self.root), self.root / "p") as mcp:
            mcp.tool("core_brief_write", {"text": "x"})
        self.assertTrue(mcp.proc.stdout.closed)

    def test_only_the_allowlisted_tools_can_be_called(self):
        with (
            tools._McpSession(fake(self.root), self.root / "p") as mcp,
            self.assertRaises(ValueError),
        ):
            mcp.tool("core_project_new", {})


class RunTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_a_hang_is_killed_and_reaped(self):
        pid_file = self.root / "pid"
        body = (
            f"import os, time\nopen({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
            "time.sleep(30)\n"
        )
        path = write_script(self.root, "slow", body)
        with self.assertRaises(tools.ToolError) as ctx:
            tools.run([path], timeout=1, cap=100)
        self.assertEqual(ctx.exception.reason, "timeout")
        self.assertFalse(alive(int(pid_file.read_text())))

    def test_output_over_the_cap_is_refused_while_the_child_writes(self):
        pid_file = self.root / "pid"
        body = (
            f"import os, sys, time\nopen({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
            "sys.stdout.write('x' * 800)\nsys.stdout.flush()\ntime.sleep(30)\n"
        )
        path = write_script(self.root, "flood", body)
        started = time.monotonic()
        with self.assertRaises(tools.ToolError) as ctx:
            tools.run([path], timeout=20, cap=100)
        self.assertEqual(ctx.exception.reason, "output too large")
        self.assertLess(time.monotonic() - started, 10)
        self.assertFalse(alive(int(pid_file.read_text())))

    def test_a_missing_binary_cannot_start(self):
        with self.assertRaises(tools.ToolError) as ctx:
            tools.run([str(self.root / "no-such-tool")], timeout=5, cap=10)
        self.assertEqual(ctx.exception.reason, "cannot start")


class DevToolsEndpointTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.profile = Path(tmp.name)

    def write(self, text):
        (self.profile / "DevToolsActivePort").write_text(text, encoding="utf-8")

    def test_the_port_and_browser_path_are_read_from_devtools_active_port(self):
        self.write("9222\n/devtools/browser/abc-DEF_1\n")
        self.assertEqual(
            tools.devtools_endpoint(self.profile),
            ("http://127.0.0.1:9222", "/devtools/browser/abc-DEF_1"),
        )

    def test_a_malformed_file_is_bad_output(self):
        for text in (
            "not-a-port\n/devtools/browser/x\n",
            "70000\n/devtools/browser/x\n",
            "9222\n/etc/passwd\n",
        ):
            with self.subTest(text=text):
                self.write(text)
                with self.assertRaises(tools.ToolError) as ctx:
                    tools.devtools_endpoint(self.profile)
                self.assertEqual(ctx.exception.reason, "bad output")

    def test_a_missing_file_is_no_debugging_port(self):
        with (
            mock.patch.object(tools, "DEVTOOLS_WAIT_S", 0.2),
            self.assertRaises(tools.ToolError) as ctx,
        ):
            tools.devtools_endpoint(self.profile)
        self.assertEqual(ctx.exception.reason, "no debugging port")


class _Version(http.server.BaseHTTPRequestHandler):
    body = b"{}"

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *_):
        pass


class VerifyBrowserTests(unittest.TestCase):
    def setUp(self):
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _Version)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.endpoint = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.port = self.server.server_address[1]

    def answer(self, path):
        _Version.body = json.dumps(
            {"webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}{path}"}
        ).encode("utf-8")

    def test_the_listener_that_answers_with_our_browser_path_is_accepted(self):
        self.answer("/devtools/browser/abc")
        tools.verify_browser(self.endpoint, "/devtools/browser/abc")

    def test_a_listener_with_another_browser_path_is_not_ours(self):
        self.answer("/devtools/browser/other")
        with self.assertRaises(tools.ToolError) as ctx:
            tools.verify_browser(self.endpoint, "/devtools/browser/abc")
        self.assertEqual(ctx.exception.reason, "not our browser")

    def test_a_port_with_nothing_listening_is_not_ours(self):
        with self.assertRaises(tools.ToolError) as ctx:
            tools.verify_browser("http://127.0.0.1:1", "/devtools/browser/abc")
        self.assertEqual(ctx.exception.reason, "not our browser")


REA_JSON = b'{"normalized_result": {"coverage": {"scripts": []}, "requests": []}}'


class PublishReaTests(unittest.TestCase):
    def test_rea_is_created_exclusively(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "rea").mkdir()
            with self.assertRaises(FileExistsError):
                tools.publish_rea(Path(tmp), REA_JSON, "6.7.0", 9000)


class CraftTreeSafetyTests(unittest.TestCase):
    """MAJOR 1: Craft's tree is validated before anything it could redirect is written."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.outside = self.root / "outside.txt"
        self.outside.write_bytes(b"outside original\n")

    def steps_into(self, folder, binary):
        stops = [
            (
                k,
                300 * k,
                folder / "scroll" / f"frame-{k}-arrival.png",
                folder / "scroll" / f"frame-{k}.png",
            )
            for k in range(3)
        ]
        return tools.craft_steps(binary, folder / "craft", "happyrobot.ai", stops)

    def test_a_handoff_symlink_left_by_craft_never_redirects_the_write(self):
        folder = self.root / "kit"
        folder.mkdir()
        binary = fake(self.root, MODE="link_handoff", OUTSIDE=str(self.outside))
        with self.assertRaises(tools.ToolError):
            self.steps_into(folder, binary)
        self.assertEqual(self.outside.read_bytes(), b"outside original\n")

    def test_a_symlinked_project_parent_never_lets_the_journal_removal_escape(self):
        folder = self.root / "kit"
        folder.mkdir()
        binary = fake(self.root, MODE="link_project", OUTSIDE=str(self.outside))
        with self.assertRaises(tools.ToolError):
            self.steps_into(folder, binary)
        self.assertEqual(self.outside.read_bytes(), b"outside original\n")
        self.assertTrue(self.outside.exists())

    def test_a_hardlink_inside_craft_is_refused(self):
        folder = self.root / "kit"
        folder.mkdir()
        binary = fake(self.root, MODE="hardlink_out", OUTSIDE=str(self.outside))
        with self.assertRaises(tools.ToolError) as ctx:
            self.steps_into(folder, binary)
        self.assertEqual(ctx.exception.reason, "unsafe output")
        self.assertEqual(self.outside.read_bytes(), b"outside original\n")

    def test_a_clean_run_writes_handoff_exclusively_with_mode_0600(self):
        folder = self.root / "kit"
        folder.mkdir()
        self.steps_into(folder, fake(self.root))
        handoff = folder / "craft" / "HANDOFF.md"
        self.assertEqual(handoff.stat().st_mode & 0o777, 0o600)
        self.assertFalse(handoff.is_symlink())


class McpBoundedWriteTests(unittest.TestCase):
    """MINOR 10: a child that stops reading stdin fails within the deadline, by measured time."""

    def test_a_stalled_write_fails_within_a_short_deadline(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        started = time.monotonic()
        with (
            mock.patch.object(tools, "MCP_DEADLINE_S", 1),
            self.assertRaises(tools.ToolError) as ctx,
            tools._McpSession(fake(root, MODE="stall"), root / "p") as mcp,
        ):
            mcp.tool("core_brief_write", {"text": "x" * 4_000_000})
        self.assertEqual(ctx.exception.reason, "timeout")
        self.assertLess(time.monotonic() - started, 15)


class RemoveUnsafeRootTests(unittest.TestCase):
    """MINOR 3: an unsafe stage root is unlinked, never followed."""

    def test_a_symlink_where_a_folder_should_be_is_unlinked_not_followed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        target = root / "elsewhere"
        target.mkdir()
        (target / "keep.txt").write_text("keep", encoding="utf-8")
        stage = root / "rea"
        os.symlink(target, stage)
        tools.remove_tree(stage)
        self.assertFalse(os.path.lexists(stage))
        self.assertEqual((target / "keep.txt").read_text(encoding="utf-8"), "keep")

    def test_a_real_folder_tree_is_removed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        stage = Path(tmp.name) / "craft"
        (stage / "sub").mkdir(parents=True)
        (stage / "sub" / "f").write_text("x", encoding="utf-8")
        tools.remove_tree(stage)
        self.assertFalse(stage.exists())


if __name__ == "__main__":
    unittest.main()
