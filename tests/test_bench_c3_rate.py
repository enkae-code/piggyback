"""C3 rating site on a fixture store: no inputs copied, comments stripped, key out of the site."""

import json
import tempfile
import unittest
from pathlib import Path

from bench.c3 import rate

RUN = "fixture-run"
ARMS = rate.ARMS
STARTERS = [
    "northline-ops",
    "mina-okafor",
    "field-journal",
    "ink-and-rule",
    "little-plot",
    "good-day-assembly",
]


def make_store(store):
    run = store / RUN
    run.mkdir(parents=True)
    (run / "manifest.json").write_text(json.dumps({"rater_seed": 7}), encoding="utf-8")
    for tid in [f"T{n:02d}" for n in range(1, 11)]:
        for arm in ARMS:
            project = run / "pages" / tid / arm / "project"
            project.mkdir(parents=True)
            (project / "index.html").write_text(
                f"<!-- secret {tid} {arm} --><h1>{tid} {arm}</h1>", encoding="utf-8"
            )
            for name in ("BRIEF.md", "SKILL.md", "screenshot.png"):
                (project / name).write_text("input", encoding="utf-8")
    for starter in STARTERS:
        folder = run / "inputs" / starter
        folder.mkdir(parents=True)
        (folder / "screenshot.png").write_bytes(b"png")
        (folder / "BRIEF.md").write_text("brief", encoding="utf-8")
    return run


class RateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "store"
        self.site = Path(self.tmp.name) / "site"
        self.run_dir = make_store(self.store)
        rate.build(self.store, RUN, self.site)
        self.key = json.loads((self.run_dir / "rating-key.json").read_text(encoding="utf-8"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_index_html_copied_and_comments_stripped(self):
        pages = sorted((self.site / "p").glob("*/index.html"))
        self.assertEqual(len(pages), 40)
        for page in self.site.glob("p/*/*"):
            self.assertEqual(page.name, "index.html")
        for page in pages:
            self.assertNotIn("<!--", page.read_text(encoding="utf-8"))
            self.assertNotIn("secret", page.read_text(encoding="utf-8"))
        for name in ("BRIEF.md", "SKILL.md"):
            self.assertEqual(list(self.site.rglob(name)), [])
        self.assertEqual(
            sorted(p.name for p in self.site.glob("ref/*.png")),
            sorted(f"{s}.png" for s in STARTERS),
        )
        self.assertTrue(self.key["html_comments_stripped"])

    def test_key_in_store_not_site(self):
        self.assertEqual(list(self.site.rglob("rating-key*")), [])
        self.assertEqual(len(self.key["tokens"]), 40)
        for token, cell in self.key["tokens"].items():
            text = (self.site / "p" / token / "index.html").read_text(encoding="utf-8")
            self.assertIn(f"{cell['task']} {cell['arm']}", text)

    def test_each_task_in_three_packets_and_pairs_cover_the_arms(self):
        counts = {f"T{n:02d}": 0 for n in range(1, 11)}
        for packet, entries in self.key["packets"].items():
            tasks = {e["task"] for e in entries}
            self.assertEqual(len(entries), 10, packet)
            for tid in tasks:
                counts[tid] += 1
        self.assertEqual(set(counts.values()), {3})
        for entries in self.key["packets"].values():
            for e in entries:
                self.assertEqual({e["A"], e["B"]}, set(rate.PAIRS[e["pair"]]))

    def test_second_run_refused(self):
        with self.assertRaises(SystemExit):
            rate.build(self.store, RUN, Path(self.tmp.name) / "other-site")


if __name__ == "__main__":
    unittest.main()
