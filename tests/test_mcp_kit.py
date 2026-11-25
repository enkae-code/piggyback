"""`piggyback mcp --kit`: the read-only kit mode, with measure stubbed through the Session seam.

The startup cases run the real CLI in a subprocess. The tool cases build the session in-process
with a fake measure, so no browser is needed.
"""

import copy
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from piggyback import __version__, mcp
from piggyback import kit as kit_mod
from piggyback.measure import MeasureError
from tests.test_mcp import BUILD_URL, REF_URL, FakeMeasure, call, cli, exchange, load

FIXTURES = Path(__file__).parent / "fixtures" / "fingerprints"
LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
BUILD = "https://93.184.216.35/"  # public: measured without loopback


def kit_session(kit, build, measure_page):
    return mcp.Session([], build, measure_page, kit=kit)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rewrite(kit, name, text):
    """Change a kit file and its kit.json digest together: a kit an agent rewrote whole."""
    (kit / name).write_text(text, encoding="utf-8")
    data = json.loads((kit / "kit.json").read_text(encoding="utf-8"))
    data["artifacts"][name] = digest(kit / name)
    (kit / "kit.json").write_text(json.dumps(data), encoding="utf-8")


def with_fingerprint(kit, change):
    """The kit's frozen fingerprint with change(data) applied, its digests kept consistent."""
    data = load("ref.json")
    change(data)
    rewrite(kit, "fingerprint.json", json.dumps(data))


def write_kit(root):
    kit = Path(root) / "kit"
    kit.mkdir()
    shutil.copy(FIXTURES / "ref.json", kit / "fingerprint.json")
    texts = {
        "DESIGN.md": "---\nname: x\n---\n",
        "BRIEF.md": "# Brief\n\nHeading type: 40px\n",
        "MOTION.md": "# Motion\n\n| Name |\n|---|\n| M1 | a |\n| M2 | b |\n",
    }
    for name, text in texts.items():
        (kit / name).write_text(text, encoding="utf-8")
    artifacts = {name: digest(kit / name) for name in ("fingerprint.json", *texts)}
    kit_json = {"schema": 1, "piggyback": __version__, "artifacts": artifacts}
    (kit / "kit.json").write_text(json.dumps(kit_json), encoding="utf-8")
    return kit


class KitStartup(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.kit = write_kit(self.root)

    def test_kit_and_ref_are_mutually_exclusive(self):
        done = cli("--kit", str(self.kit), "--ref", REF_URL)
        self.assertEqual(done.returncode, 2, done.stderr)

    def test_a_clean_kit_serves_and_lists_get_kit(self):
        done = cli("--kit", str(self.kit), "--build", BUILD_URL, stdin=json.dumps(LIST).encode())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(b'"name": "get_kit"', done.stdout)

    def test_a_changed_frozen_file_refuses_to_start(self):
        (self.kit / "DESIGN.md").write_text("tampered\n", encoding="utf-8")
        done = cli("--kit", str(self.kit))
        self.assertEqual(done.returncode, 1)
        self.assertIn(b"DESIGN.md changed since the kit was made", done.stderr)
        self.assertEqual(done.stdout, b"")

    def test_a_missing_brief_refuses_to_start_without_a_path_in_the_message(self):
        (self.kit / "BRIEF.md").unlink()
        done = cli("--kit", str(self.kit))
        self.assertEqual(done.returncode, 1)
        self.assertIn(b"BRIEF.md is missing or unreadable", done.stderr)
        self.assertNotIn(str(self.root).encode(), done.stderr)

    def test_a_kit_without_kit_json_refuses_to_start(self):
        (self.kit / "kit.json").unlink()
        done = cli("--kit", str(self.kit))
        self.assertEqual(done.returncode, 1)
        self.assertIn(b"kit.json is missing or unreadable", done.stderr)

    def test_a_build_that_is_not_allowed_refuses_to_start(self):
        done = cli("--kit", str(self.kit), "--build", "http://10.0.0.1/")
        self.assertEqual(done.returncode, 1)
        self.assertIn(b"mcp failed", done.stderr)

    @unittest.skipUnless(os.name == "posix", "POSIX only: symbolic links")
    def test_a_symlinked_kit_dir_refuses_to_start(self):
        link = self.root / "link"
        os.symlink(self.kit, link)
        done = cli("--kit", str(link))
        self.assertEqual(done.returncode, 1)
        self.assertIn(b"the kit must be a real folder, not a link", done.stderr)

    def test_an_out_of_range_viewport_refuses_to_start(self):
        with_fingerprint(self.kit, lambda d: d["viewport"].update(width=10**6))
        done = cli("--kit", str(self.kit))
        self.assertEqual(done.returncode, 1)
        self.assertIn(b"the reference viewport is out of range", done.stderr)
        self.assertEqual(done.stdout, b"")

    def test_a_reference_without_a_viewport_refuses_to_start(self):
        with_fingerprint(self.kit, lambda d: d.pop("viewport"))
        done = cli("--kit", str(self.kit))
        self.assertEqual(done.returncode, 1)
        self.assertIn(b"the reference has no viewport size", done.stderr)

    def test_a_brief_that_is_not_utf8_refuses_to_start(self):
        (self.kit / "BRIEF.md").write_bytes(b"# Brief\n\n\xff\xfe\n")
        data = json.loads((self.kit / "kit.json").read_text(encoding="utf-8"))
        data["artifacts"]["BRIEF.md"] = digest(self.kit / "BRIEF.md")
        (self.kit / "kit.json").write_text(json.dumps(data), encoding="utf-8")
        done = cli("--kit", str(self.kit))
        self.assertEqual(done.returncode, 1)
        self.assertIn(b"BRIEF.md is not UTF-8", done.stderr)

    def test_a_design_md_that_is_not_utf8_refuses_to_start(self):
        (self.kit / "DESIGN.md").write_bytes(b"---\nname: \xff\n---\n")
        data = json.loads((self.kit / "kit.json").read_text(encoding="utf-8"))
        data["artifacts"]["DESIGN.md"] = digest(self.kit / "DESIGN.md")
        (self.kit / "kit.json").write_text(json.dumps(data), encoding="utf-8")
        done = cli("--kit", str(self.kit))
        self.assertEqual(done.returncode, 1)
        self.assertIn(b"DESIGN.md is not UTF-8", done.stderr)
        self.assertEqual(done.stdout, b"")


def snapshot(kit):
    return {name: (kit / name).read_bytes() for name in (*kit_mod.FROZEN, "kit.json")}


class SingleRead(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.kit = write_kit(self.root)

    def test_startup_and_get_kit_use_only_the_first_verified_snapshot(self):
        later = self.root / "later"
        later.mkdir()
        other = write_kit(later)
        rewrite(other, "BRIEF.md", "# Brief\n\nHeading type: 99px\n")
        first, second = snapshot(self.kit), snapshot(other)
        reads = []

        def sequenced(_folder, name, _cap):  # the first verified pass gets the first snapshot
            reads.append(name)
            return (first if len(reads) <= len(first) else second)[name]

        with mock.patch("piggyback.kit._read", side_effect=sequenced):
            session = kit_session(self.kit, BUILD_URL, None)
            self.assertEqual(len(reads), len(first))  # one read per frozen file at startup
            reply = exchange(session, call("get_kit"))[0]
        self.assertTrue(reply["result"]["isError"])
        self.assertIn(
            "kit.json changed since the session started", reply["result"]["content"][0]["text"]
        )
        self.assertNotIn("99px", json.dumps(reply))


class KitMode(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.kit = write_kit(self.root)
        self.fake = FakeMeasure(load("ref.json"), load("build.json"))
        self.session = kit_session(self.kit, BUILD_URL, self.fake)

    def replies(self, *lines):
        return exchange(self.session, *lines)

    def text(self, reply):
        return reply["result"]["content"][0]["text"]

    def test_tools_list_adds_get_kit_only_in_kit_mode(self):
        names = {t["name"] for t in self.replies(LIST)[0]["result"]["tools"]}
        self.assertEqual(names, {"get_brief", "check", "get_design_md", "list_starters", "get_kit"})
        plain = exchange(mcp.Session([], None), LIST)[0]["result"]["tools"]
        self.assertNotIn("get_kit", {t["name"] for t in plain})

    def test_get_brief_is_the_frozen_brief_and_refresh_is_refused(self):
        reply, refused = self.replies(call("get_brief"), call("get_brief", {"refresh": True}, 2))
        self.assertTrue(self.text(reply).startswith(mcp.FRAMING))
        self.assertIn("Heading type: 40px", self.text(reply))
        self.assertTrue(refused["result"]["isError"])
        self.assertIn("frozen in kit mode", self.text(refused))

    def test_get_design_md_is_the_kit_design_md(self):
        reply = self.replies(call("get_design_md"))[0]
        self.assertIn("name: x", self.text(reply))

    def test_every_call_rechecks_the_frozen_digests_before_measuring(self):
        (self.kit / "BRIEF.md").write_text("changed\n", encoding="utf-8")
        brief, check = self.replies(call("get_brief"), call("check", rid=2))
        for reply in (brief, check):
            self.assertTrue(reply["result"]["isError"])
            self.assertIn("BRIEF.md changed since the kit was made", self.text(reply))
        self.assertEqual(self.fake.calls, [])

    def test_a_change_after_a_good_call_is_refused_by_the_next_call(self):
        self.assertFalse(self.replies(call("get_brief"))[0]["result"]["isError"])
        (self.kit / "BRIEF.md").write_text("changed\n", encoding="utf-8")
        reply = self.replies(call("get_brief", rid=2))[0]
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("BRIEF.md changed since the kit was made", self.text(reply))

    def test_a_manifest_rewritten_during_the_session_is_refused(self):
        self.assertFalse(self.replies(call("check"))[0]["result"]["isError"])
        with_fingerprint(self.kit, lambda d: d.update(note="rewritten"))
        brief, check, kit_info = self.replies(
            call("get_brief", rid=2), call("check", rid=3), call("get_kit", rid=4)
        )
        for reply in (brief, check, kit_info):
            self.assertTrue(reply["result"]["isError"])
            self.assertIn("kit.json changed since the session started", self.text(reply))
        self.assertEqual(len(self.fake.calls), 1)

    def test_check_without_a_build_is_refused(self):
        session = kit_session(self.kit, None, self.fake)
        reply = exchange(session, call("check"))[0]
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("no build configured", self.text(reply))

    def test_check_measures_the_build_at_the_kit_viewport_and_loopback_only_when_local(self):
        seen = []

        def measure(url, *, allow_loopback=False, viewport=None, **_):
            seen.append((url, allow_loopback, viewport))
            return copy.deepcopy(load("build.json"))

        exchange(kit_session(self.kit, BUILD, measure), call("check"))
        exchange(kit_session(self.kit, BUILD_URL, measure), call("check"))
        self.assertEqual(
            seen,
            [(BUILD, False, (1440, 900)), (BUILD_URL, True, (1440, 900))],
        )

    def test_check_refuses_an_out_of_range_viewport_without_measuring(self):
        with_fingerprint(self.kit, lambda d: d["viewport"].update(height=10**6))
        # Startup refuses this kit; check must refuse it again on its own, without measuring.
        with mock.patch("piggyback.kit.readiness"):
            session = kit_session(self.kit, BUILD_URL, self.fake)
        reply = exchange(session, call("check"))[0]
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("the reference viewport is out of range", self.text(reply))
        self.assertEqual(self.fake.calls, [])

    def test_the_fourth_check_is_refused_without_measuring(self):
        replies = self.replies(*(call("check", rid=n) for n in range(1, 5)))
        self.assertFalse(any(r["result"]["isError"] for r in replies[:3]))
        self.assertTrue(replies[3]["result"]["isError"])
        self.assertIn(
            "correction cap reached (3 attempts): restart with a new build", self.text(replies[3])
        )
        self.assertEqual(len(self.fake.calls), 3)

    def test_check_lists_open_fixed_and_uncomparable_and_writes_no_check_md(self):
        before = sorted(p.name for p in self.kit.iterdir())
        first = self.text(self.replies(call("check"))[0])
        self.assertIn("Attempt 1", first)
        self.assertIn("Fixed: none", first)
        self.assertIn("Next: fix the open differences", first)
        self.assertNotIn("Still open: none", first)
        self.assertEqual(sorted(p.name for p in self.kit.iterdir()), before)

    def test_check_marks_a_fix_on_the_next_check(self):
        self.replies(call("check"))
        self.fake.build = copy.deepcopy(self.fake.ref)
        second = self.text(self.replies(call("check", rid=2))[0])
        self.assertIn("Still open: none", second)
        self.assertNotIn("Fixed: none", second)

    def test_get_kit_reports_readiness_names_and_summaries_without_paths(self):
        (self.kit / "screenshot.png").write_bytes(b"\x89PNG")
        text = self.text(self.replies(call("get_kit"))[0])
        self.assertIn("Kit ready", text)
        self.assertIn("screenshot.png", text)
        self.assertIn("BRIEF.md", text)
        self.assertNotIn("scroll/frame-0.png", text)
        self.assertIn("MOTION.md: 2 rows", text)
        self.assertIn("BRIEF.md: 3 lines", text)
        self.assertNotIn("Heading type", text)
        self.assertNotIn(str(self.root), text)

    def test_get_kit_renders_structured_fields_only(self):
        rewrite(self.kit, "BRIEF.md", "# Brief\n\nSee /home/brodey/notes.md\n")
        data = json.loads((self.kit / "kit.json").read_text(encoding="utf-8"))
        data["piggyback"] = "/home/brodey/0.2"
        (self.kit / "kit.json").write_text(json.dumps(data), encoding="utf-8")
        brief = (self.kit / "BRIEF.md").read_bytes()
        session = kit_session(self.kit, BUILD_URL, self.fake)
        text = self.text(exchange(session, call("get_kit"))[0])
        self.assertTrue(text.startswith(mcp.FRAMING + "\n\n"))
        body = text[len(mcp.FRAMING) + 2 :]
        self.assertEqual(
            body.splitlines(),
            [
                "Kit ready: fingerprint.json, DESIGN.md, BRIEF.md and MOTION.md match kit.json.",
                "Piggyback unknown, kit schema 1.",
                "Other files present: none.",
                f"BRIEF.md: 3 lines, {len(brief)} bytes.",
                "MOTION.md: 2 rows.",
                "Latest CHECK.md: none yet.",
            ],
        )

    def test_get_kit_shows_a_schema_that_is_not_an_int_as_unknown(self):
        data = json.loads((self.kit / "kit.json").read_text(encoding="utf-8"))
        data["schema"] = "/home/brodey/x"
        (self.kit / "kit.json").write_text(json.dumps(data), encoding="utf-8")
        session = kit_session(self.kit, BUILD_URL, self.fake)
        text = self.text(exchange(session, call("get_kit"))[0])
        self.assertIn(f"Piggyback {__version__}, kit schema unknown.", text)

    def test_a_measure_failure_is_one_fixed_sentence_whatever_its_text(self):
        def boom(url, **_):
            raise MeasureError(f"browser failed in {self.root}/tmp/run")

        reply = exchange(kit_session(self.kit, BUILD_URL, boom), call("check"))[0]
        self.assertTrue(reply["result"]["isError"])
        self.assertEqual(self.text(reply), f"{mcp.FRAMING}\n\nthe build could not be measured")

    def test_each_measure_kind_has_its_own_fixed_sentence(self):
        sentences = {
            "timeout": "the build measurement timed out",
            "address": "the build address is not allowed",
            "data": "the build produced too much measurement data",
            "load": "the build page could not be loaded",
        }

        def failing(kind):
            def fail(url, **_):
                raise MeasureError(f"raw text in {self.root}", kind=kind)

            return fail

        for kind, sentence in sentences.items():
            with self.subTest(kind=kind):
                reply = exchange(kit_session(self.kit, BUILD_URL, failing(kind)), call("check"))[0]
                self.assertEqual(self.text(reply), f"{mcp.FRAMING}\n\n{sentence}")

    def test_get_kit_claims_only_the_four_frozen_files_and_names_the_rest_as_present(self):
        (self.kit / "screenshot.png").write_bytes(b"\x89PNG")
        text = self.text(self.replies(call("get_kit"))[0])
        self.assertIn(
            "Kit ready: fingerprint.json, DESIGN.md, BRIEF.md and MOTION.md match kit.json.", text
        )
        self.assertIn("Other files present: screenshot.png", text)

    def test_a_tampered_unfrozen_artifact_digest_is_not_claimed_as_frozen(self):
        (self.kit / "screenshot.png").write_bytes(b"\x89PNG")
        data = json.loads((self.kit / "kit.json").read_text(encoding="utf-8"))
        data["artifacts"]["screenshot.png"] = "0" * 64
        (self.kit / "kit.json").write_text(json.dumps(data), encoding="utf-8")
        # Startup sees the tampered manifest: the pin is taken from it, as a fresh session would.
        session = kit_session(self.kit, BUILD_URL, self.fake)
        text = self.text(exchange(session, call("get_kit"))[0])
        self.assertIn("Kit ready", text)
        self.assertNotIn("every frozen file", text)

    def test_get_kit_summaries_are_capped(self):
        rewrite(self.kit, "BRIEF.md", "\n".join("x" * 500 for _ in range(300)))
        session = kit_session(self.kit, BUILD_URL, self.fake)
        text = self.text(exchange(session, call("get_kit"))[0])
        self.assertLess(len(text), 2000)

    def test_a_long_valid_looking_version_is_shown_as_unknown(self):
        data = json.loads((self.kit / "kit.json").read_text(encoding="utf-8"))
        data["piggyback"] = "1.2.3-" + "a" * 8192
        (self.kit / "kit.json").write_text(json.dumps(data), encoding="utf-8")
        session = kit_session(self.kit, BUILD_URL, self.fake)
        text = self.text(exchange(session, call("get_kit"))[0])
        self.assertIn("Piggyback unknown, kit schema 1.", text)
        self.assertLess(len(text), 2000)

    def test_a_huge_integer_schema_is_shown_as_unknown(self):
        data = json.loads((self.kit / "kit.json").read_text(encoding="utf-8"))
        data["schema"] = int("9" * 3000)
        (self.kit / "kit.json").write_text(json.dumps(data), encoding="utf-8")
        session = kit_session(self.kit, BUILD_URL, self.fake)
        text = self.text(exchange(session, call("get_kit"))[0])
        self.assertIn("kit schema unknown.", text)
        self.assertLess(len(text), 2000)

    def test_a_long_attempt_number_is_not_echoed(self):
        (self.kit / "CHECK.md").write_text(
            f"## Attempt {'9' * 8192}\n\nFixed: none\nStill open: none\nUncomparable: none\n",
            encoding="utf-8",
        )
        session = kit_session(self.kit, BUILD_URL, self.fake)
        text = self.text(exchange(session, call("get_kit"))[0])
        self.assertNotIn("9" * 7, text)
        self.assertLess(len(text), 2000)

    def test_get_kit_shows_the_latest_check_md_attempt_as_counts(self):
        (self.kit / "CHECK.md").write_text(
            "# Check\n\n## Attempt 1\n\nFixed: none\nStill open: accent\nUncomparable: none\n\n"
            "## Attempt 2\n\nFixed: accent\nStill open: heading type\nUncomparable: none\n",
            encoding="utf-8",
        )
        text = self.text(self.replies(call("get_kit"))[0])
        self.assertIn("Attempt 2: open 1, fixed 1, uncomparable 0", text)
        self.assertNotIn("heading type", text)

    def test_an_unparseable_check_md_is_only_present(self):
        (self.kit / "CHECK.md").write_text("## Attempt 3\n\njunk\n", encoding="utf-8")
        text = self.text(self.replies(call("get_kit"))[0])
        self.assertIn("present (not summarised)", text)

    def test_kit_mode_errors_and_check_md_carry_no_absolute_path(self):
        (self.kit / "CHECK.md").write_text(
            f"# Check\n\n## Attempt 1\n\nFixed: none\nStill open: accent\nUncomparable: none\n\n"
            f"- found at {self.root}/site/x.html\n",
            encoding="utf-8",
        )

        def boom(url, **_):
            raise MeasureError(f"browser failed in {self.root}/tmp/run")

        session = kit_session(self.kit, BUILD_URL, boom)
        replies = exchange(session, call("check"), call("get_kit", rid=2))
        self.assertTrue(replies[0]["result"]["isError"])
        for reply in replies:
            self.assertNotIn(str(self.root), json.dumps(reply))

    def test_get_kit_rechecks_the_digests(self):
        (self.kit / "BRIEF.md").write_text("changed\n", encoding="utf-8")
        reply = self.replies(call("get_kit"))[0]
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("BRIEF.md changed since the kit was made", self.text(reply))

    def test_get_kit_refuses_arguments(self):
        reply = self.replies(call("get_kit", {"path": str(self.kit)}))[0]
        self.assertEqual(reply["error"]["code"], -32602)

    def test_get_kit_is_an_unknown_tool_outside_kit_mode(self):
        reply = exchange(mcp.Session([], None), call("get_kit"))[0]
        self.assertEqual(reply["error"]["code"], -32602)
        self.assertEqual(reply["error"]["message"], "unknown tool")

    @unittest.skipUnless(os.name == "posix", "POSIX only: symbolic links")
    def test_a_linked_scroll_folder_is_not_advertised(self):
        real = self.root / "elsewhere"
        real.mkdir()
        (real / "frame-0.png").write_bytes(b"\x89PNG")
        os.symlink(real, self.kit / "scroll")
        text = self.text(self.replies(call("get_kit"))[0])
        self.assertNotIn("frame-0.png", text)

    @unittest.skipUnless(os.name == "posix", "POSIX only: symbolic links")
    def test_a_real_scroll_folder_is_advertised(self):
        (self.kit / "scroll").mkdir()
        (self.kit / "scroll" / "frame-0.png").write_bytes(b"\x89PNG")
        text = self.text(self.replies(call("get_kit"))[0])
        self.assertIn("scroll/frame-0.png", text)

    @unittest.skipUnless(os.name == "posix", "POSIX only: symbolic links")
    def test_a_linked_impeccable_folder_is_not_advertised(self):
        real = self.root / "skills"
        real.mkdir()
        (real / "SKILL.md").write_text("skill\n", encoding="utf-8")
        os.symlink(real, self.kit / "impeccable")
        text = self.text(self.replies(call("get_kit"))[0])
        self.assertNotIn("impeccable/SKILL.md", text)

    @unittest.skipUnless(os.name == "posix", "POSIX only: symbolic links")
    def test_a_kit_root_replaced_by_a_link_mid_session_is_refused(self):
        self.assertFalse(self.replies(call("get_kit"))[0]["result"]["isError"])
        moved = self.root / "moved"
        os.rename(self.kit, moved)
        os.symlink(moved, self.kit)
        brief, info = self.replies(call("get_brief"), call("get_kit", rid=2))
        for reply in (brief, info):
            self.assertTrue(reply["result"]["isError"])
            self.assertIn("the kit must be a real folder, not a link", self.text(reply))
        self.assertEqual(self.fake.calls, [])


if __name__ == "__main__":
    unittest.main()
