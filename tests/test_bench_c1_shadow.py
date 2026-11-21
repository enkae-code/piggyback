"""C1 ground truth over shadow DOM: guard tests in snapshot.py and targets.py; split fixture."""

import json
import unittest
from pathlib import Path

from bench.c1 import targets
from bench.c1.snapshot import Snapshot
from tests.test_bench_c1 import el, evidence, with_shadow

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "c1" / "shadow"


class ShadowGuards(unittest.TestCase):
    def test_elements_excludes_shadow_elements(self):  # snapshot.py elements()
        data = evidence(
            [
                el("div", box=(0, 0, 600, 200)),  # 0: light
                el("span", parent=0, box=(0, 0, 100, 20)),  # 1: shadow
            ]
        )
        with_shadow(data, [1])
        self.assertEqual([e.idx for e in Snapshot(data).elements()], [0])

    def test_in_svg_ignores_a_shadow_svg_above_a_slotted_div(self):  # targets.py in_svg
        data = evidence(
            [
                el("my-el", box=(0, 0, 600, 200)),  # 0: light host
                el("svg", parent=0, box=(0, 0, 600, 200)),  # 1: shadow svg
                el("slot", parent=1),  # 2: shadow slot
                el("div", parent=2, box=(0, 0, 200, 120)),  # 3: slotted light div
            ]
        )
        with_shadow(data, [1, 2])
        snap = Snapshot(data)
        self.assertFalse(targets.in_svg(snap, snap.els[3]))

    def test_outside_body_ignores_a_shadow_nav_wrapper(self):  # targets.py _outside_body
        data = evidence(
            [
                el("my-el", box=(0, 0, 600, 200)),  # 0: light host
                el("nav", parent=0, box=(0, 0, 600, 40)),  # 1: shadow nav
                el("slot", parent=1),  # 2: shadow slot
                el("p", parent=2, box=(0, 50, 600, 40), text="s" * 40),  # 3: slotted p, text 4
            ]
        )
        with_shadow(data, [1, 2])
        t = targets.lock_targets(data)["body"]
        self.assertEqual((t["tag"], t["dom_index"]), ("p", 3))

    def test_card_with_only_a_slotted_contentless_child_qualifies(self):  # targets.py has_content
        cards = []
        for x, base in ((0, 1), (220, 4), (440, 7)):
            cards += [
                el("div", parent=0, box=(x, 0, 200, 120), attrs={"class": "card"}),  # base: card
                el("slot", parent=base),  # base+1: shadow slot
                el("img", parent=base + 1, box=(x, 0, 200, 120)),  # base+2: light, no text
            ]
        data = evidence([el("div", box=(0, 0, 780, 200)), *cards])
        with_shadow(data, [2, 5, 8])
        t = targets.lock_targets(data)["card"]
        self.assertEqual((t.get("count"), t.get("dom_index")), (3, 1))


class SplitSlotCards(unittest.TestCase):
    def test_cards_split_across_named_slots_group_under_the_host(self):
        # Chromium capture (780x380): host X-G (7) with shadow <div><slot a></div> and
        # <section><slot b></section>. Light cards 10 and 12 are slotted to a, card 16 to b.
        # measure.js groups by parentElement (the host), so all three are one group of 3.
        data = json.loads((FIXTURE / "split.json").read_text(encoding="utf-8"))
        snap = Snapshot(data)
        self.assertEqual([snap.light_parent(i) for i in (10, 12, 16)], [7, 7, 7])
        t = targets.lock_targets(data)["card"]
        got = (t["count"], t["tag"], t["dom_index"], t["box"])
        self.assertEqual(got, (3, "div", 10, [0, 0, 200, 120]))
