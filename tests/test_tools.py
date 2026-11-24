"""Craft MCP session: one fixed-allowlist stdio session per kit run, against a fake server."""

import sys
import tempfile
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


def fake(root, **settings):
    head = "".join(f"{name} = {value!r}\n" for name, value in settings.items())
    path = Path(root) / "craft-anything-cli"
    path.write_text(
        f"#!{sys.executable}\n" + head + FAKE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    path.chmod(0o755)
    return str(path)


class McpSessionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def steps(self, binary, **kwargs):
        stops = [
            (k, 300 * k, self.root / f"frame-{k}-arrival.png", self.root / f"frame-{k}.png")
            for k in range(3)
        ]
        return tools.craft_steps(binary, self.root / "craft", "happyrobot.ai", stops)

    def test_one_session_runs_every_step_and_reports_the_changed_stop(self):
        log = self.root / "log.txt"
        changed = self.steps(fake(self.root, LOG=str(log)))
        self.assertEqual(changed, [1])
        rows = [line.split() for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len({pid for pid, _ in rows}), 1)
        self.assertEqual([name for _, name in rows], ORDER)
        self.assertTrue((self.root / "craft" / "HANDOFF.md").exists())

    def test_text_content_is_parsed_when_structured_content_is_missing(self):
        self.assertEqual(self.steps(fake(self.root, MODE="text")), [1])

    def test_a_reply_for_another_id_is_skipped(self):
        self.assertEqual(self.steps(fake(self.root, MODE="wrongid")), [1])

    def test_a_hung_craft_is_killed_at_the_deadline(self):
        with (
            mock.patch.object(tools, "MCP_DEADLINE_S", 1),
            self.assertRaises(tools.ToolError) as ctx,
        ):
            self.steps(fake(self.root, MODE="hang"))
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

    def test_only_the_allowlisted_tools_can_be_called(self):
        with (
            tools._McpSession(fake(self.root), self.root / "p") as mcp,
            self.assertRaises(ValueError),
        ):
            mcp.tool("core_project_new", {})


if __name__ == "__main__":
    unittest.main()
