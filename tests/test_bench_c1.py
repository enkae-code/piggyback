"""Non-browser C1 harness tests: synthetic DOMSnapshot fixtures, tolerance, lock, capture."""

import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bench.c1 import capture, manifest, score, targets
from bench.c1.snapshot import Snapshot
from piggyback import fingerprint as fp
from piggyback.gap import ANCHORS, gap
from piggyback.measure import EVIDENCE_STYLES, MeasureError

REPO = Path(__file__).resolve().parents[1]
RECEIPT = "2026-11-17T12:00:00+00:00"


def _meta():
    """Per-side audit meta as capture.py records it, for score.score_pair."""
    return {
        side: {
            "requested_url": "https://x.test/",
            "final_url": "https://x.test/",
            "receipt": RECEIPT,
        }
        for side in ("reference", "build")
    }


BASE = {
    "display": "block",
    "visibility": "visible",
    "opacity": "1",
    "font-size": "16px",
    "line-height": "normal",
    "letter-spacing": "normal",
    "background-color": "rgba(0, 0, 0, 0)",
    "font-family": "serif",
    "color": "rgb(0, 0, 0)",
    "font-weight": "400",
    "border-top-left-radius": "0px",
    "box-shadow": "none",
    "padding-top": "0px",
    "padding-right": "0px",
    "padding-bottom": "0px",
    "padding-left": "0px",
}


def el(tag, parent=-1, box=None, text=None, attrs=None, **style):
    """One element spec. text: a text child with the same box (or none when box is None)."""
    return {
        "tag": tag,
        "parent": parent,
        "box": box,
        "text": text,
        "attrs": attrs or {},
        "style": {**BASE, **{k.replace("_", "-"): v for k, v in style.items()}},
    }


def evidence(elements, viewport=(1440, 900), scroll=(0, 0)):
    """A DOMSnapshot-shaped dict (what piggyback.measure saves) built from element specs."""
    strings = [""]

    def s(value):
        if value not in strings:
            strings.append(value)
        return strings.index(value)

    nodes = {
        "nodeType": [],
        "parentIndex": [],
        "nodeName": [],
        "nodeValue": [],
        "attributes": [],
        "backendNodeId": [],
    }
    layout = {"nodeIndex": [], "styles": [], "bounds": []}
    text_boxes = {"layoutIndex": [], "bounds": []}

    def add(kind, parent, name, value, attrs):
        nodes["nodeType"].append(kind)
        nodes["parentIndex"].append(parent)
        nodes["nodeName"].append(s(name))
        nodes["nodeValue"].append(s(value))
        nodes["attributes"].append(attrs)
        nodes["backendNodeId"].append(
            len(nodes["nodeType"])
        )  # stable id per node, as Chromium gives
        return len(nodes["nodeType"]) - 1

    for spec in elements:
        flat = []
        for key, value in spec["attrs"].items():
            flat += [s(key), s(value)]
        index = add(1, spec["parent"], spec["tag"].upper(), "", flat)
        if spec["box"] is not None:
            layout["nodeIndex"].append(index)
            layout["styles"].append([s(spec["style"][k]) for k in EVIDENCE_STYLES])
            layout["bounds"].append(list(spec["box"]))
        if spec["text"] is not None:
            text_index = add(3, index, "#text", spec["text"], [])
            if spec["box"] is not None:
                layout["nodeIndex"].append(text_index)
                layout["styles"].append([s(spec["style"][k]) for k in EVIDENCE_STYLES])
                layout["bounds"].append(list(spec["box"]))
                text_boxes["layoutIndex"].append(len(layout["nodeIndex"]) - 1)
                text_boxes["bounds"].append(list(spec["box"]))
    return {
        "styles": EVIDENCE_STYLES,
        "page": {
            "url": "https://x.test/",
            "title": "t",
            "scroll": list(scroll),
            "viewport": list(viewport),
            "text": "",
        },
        "strings": strings,
        "documents": [{"nodes": nodes, "layout": layout, "textBoxes": text_boxes}],
    }


def target_of(elements, slot, viewport=(1440, 900)):
    return targets.lock_targets(evidence(elements, viewport))[slot]


def with_shadow(data, ids):
    """Chromium's real shape: no nodeType-11 root; each shadow-tree node is in shadowRootType."""
    data["strings"].append("open")
    data["documents"][0]["nodes"]["shadowRootType"] = {
        "index": ids,
        "value": [len(data["strings"]) - 1] * len(ids),
    }


class TargetRules(unittest.TestCase):
    def test_heading_is_largest_font(self):
        t = target_of(
            [
                el("h2", box=(0, 0, 300, 40), text="small", font_size="20px"),
                el("h1", box=(0, 50, 300, 80), text="big", font_size="48px"),
            ],
            "heading",
        )
        self.assertEqual(t["tag"], "h1")

    def test_heading_tie_goes_to_larger_clipped_area_then_dom_order(self):
        t = target_of(
            [
                el("h1", box=(0, 0, 100, 40), text="a", font_size="32px"),
                el("h2", box=(0, 50, 300, 40), text="b", font_size="32px"),
            ],
            "heading",
        )
        self.assertEqual(t["tag"], "h2")
        t = target_of(
            [
                el("h1", box=(0, 0, 100, 40), text="a", font_size="32px"),
                el("h2", box=(0, 50, 100, 40), text="b", font_size="32px"),
            ],
            "heading",
        )
        self.assertEqual(t["tag"], "h1")

    def test_heading_must_be_rendered_and_on_screen(self):
        for bad in (
            dict(display="none"),
            dict(visibility="hidden"),
            dict(opacity="0"),
        ):
            t = target_of(
                [el("h1", box=(0, 0, 100, 40), text="x", font_size="60px", **bad)], "heading"
            )
            self.assertEqual(t, {"absent": True}, bad)
        off = target_of([el("h1", box=(0, 1000, 100, 40), text="x", font_size="60px")], "heading")
        self.assertEqual(off, {"absent": True})
        right = target_of([el("h1", box=(1500, 0, 100, 40), text="x", font_size="60px")], "heading")
        self.assertEqual(right, {"absent": True})

    def test_heading_empty_text_skipped(self):
        t = target_of(
            [
                el("h1", box=(0, 0, 100, 40), text="", font_size="60px"),
                el("h3", box=(0, 0, 100, 40), text="ok", font_size="20px"),
            ],
            "heading",
        )
        self.assertEqual(t["tag"], "h3")

    def test_heading_box_is_text_ink_clipped_to_element(self):
        # The element is 300 wide, but the text ink covers only 120 px of it.
        spec = el("h1", box=(0, 0, 300, 60), text="hi", font_size="40px")
        spec["text_box"] = (10, 10, 120, 30)
        data = evidence([spec])
        data["documents"][0]["textBoxes"] = {"layoutIndex": [1], "bounds": [[10, 10, 120, 30]]}
        t = targets.lock_targets(data)["heading"]
        self.assertEqual(t["box"], [10, 10, 120, 30])

    def test_body_first_p_with_forty_chars(self):
        short = "too short"
        long = "x" * 40
        t = target_of(
            [
                el("p", box=(0, 0, 300, 20), text=short),
                el("p", box=(0, 30, 300, 20), text=long),
                el("p", box=(0, 60, 300, 20), text="y" * 80),
            ],
            "body",
        )
        self.assertEqual(t["text"], "x" * 40)

    def test_body_p_in_the_first_screen_beats_an_earlier_div_with_text(self):
        badge = el("div", box=(0, 0, 600, 30), text="b" * 40)
        lead = el("p", box=(0, 40, 600, 30), text="p" * 40)
        self.assertEqual(target_of([badge, lead], "body")["text"], "p" * 40)

    def test_body_p_crossing_the_fold_is_not_the_body(self):
        crossing = el("p", box=(0, 880, 300, 40), text="p" * 40)
        lead = el("p", box=(0, 40, 300, 40), text="q" * 40)
        self.assertEqual(target_of([crossing, lead], "body")["text"], "q" * 40)

    def test_body_takes_div_text_in_the_first_screen_not_a_p_below_the_fold(self):
        below = el("p", box=(0, 1000, 300, 20), text="p" * 40)
        lead = el("div", box=(0, 0, 600, 60), text="d" * 40)
        self.assertEqual(target_of([lead, below], "body")["text"], "d" * 40)

    def test_body_skips_nav_and_cookie_banner_text(self):
        nav = el("nav", box=(0, 0, 600, 200))
        inner = el("div", parent=0, box=(0, 0, 600, 60), text="n" * 40)
        cookie = el("div", box=(0, 300, 600, 60), text="c" * 40, attrs={"class": "CookieBar"})
        footer = el("div", box=(0, 480, 600, 60), text="f" * 40, attrs={"role": "contentinfo"})
        cookie_id = el("div", box=(0, 560, 600, 60), text="i" * 40, attrs={"id": "cookie-x"})
        lead = el("div", box=(0, 400, 600, 60), text="d" * 40)
        self.assertEqual(
            target_of([nav, inner, cookie, footer, cookie_id, lead], "body")["text"], "d" * 40
        )

    def test_heading_text_is_never_the_body(self):
        h1 = el("h1", box=(0, 0, 600, 40))
        span = el("span", parent=0, box=(0, 0, 600, 40), text="s" * 40)
        lead = el("div", box=(0, 60, 600, 40), text="d" * 40)
        self.assertEqual(target_of([h1, span, lead], "body")["text"], "d" * 40)

    def test_fallback_uses_own_text_not_descendants(self):
        wrapper = el("div", box=(0, 0, 600, 200), text="w" * 10)
        child = el("div", parent=0, box=(0, 20, 600, 60), text="c" * 40)
        t = target_of([wrapper, child], "body")
        self.assertEqual((t["tag"], t["text"]), ("div", "c" * 40))

    def test_footer_paragraph_is_never_the_body(self):
        footer = el("footer", box=(0, 0, 600, 60))
        p = el("p", parent=0, box=(0, 0, 600, 40), text="f" * 40)
        lead = el("div", box=(0, 100, 600, 40), text="d" * 40)
        self.assertEqual(target_of([footer, p, lead], "body")["text"], "d" * 40)

    def test_li_fallback_is_the_body(self):
        t = target_of([el("li", box=(0, 0, 600, 40), text="l" * 40)], "body")
        self.assertEqual((t["tag"], t["text"]), ("li", "l" * 40))

    def test_body_length_counts_utf16_units(self):
        astral = "a" * 38 + "\U0001f680"  # 39 code points, 40 UTF-16 units
        t = target_of([el("div", box=(0, 0, 600, 60), text=astral)], "body")
        self.assertEqual(t.get("text"), astral)
        short = target_of([el("div", box=(0, 0, 600, 60), text="a" * 39)], "body")
        self.assertEqual(short, {"absent": True})

    def test_body_text_uses_ecmascript_whitespace(self):
        feff = target_of([el("div", box=(0, 0, 600, 60), text="\ufeff" + "a" * 39)], "body")
        self.assertEqual(feff, {"absent": True})
        sep = "a" * 39 + "\x1c"  # \x1c is not JS whitespace: it counts toward the 40
        t = target_of([el("div", box=(0, 0, 600, 60), text=sep)], "body")
        self.assertEqual(t["text"], sep)

    def test_recorded_inner_text_trims_ecmascript_whitespace(self):
        ev = evidence([el("p", box=(0, 0, 600, 60), text="a" * 39)])
        backend = ev["documents"][0]["nodes"]["backendNodeId"][0]  # the <p> is node 0
        ev["inner_text"] = {str(backend): "\ufeff" + "a" * 39}
        self.assertEqual(targets.lock_targets(ev)["body"], {"absent": True})

    def test_cookie_match_is_ascii_case_insensitive(self):
        # U+212A (Kelvin sign) lowers to ASCII "k" in Python; CSS [class*=cookie i] ignores it.
        t = target_of(
            [el("div", box=(0, 0, 600, 60), text="c" * 40, attrs={"class": "coo\u212aie-bar"})],
            "body",
        )
        self.assertEqual(t.get("text"), "c" * 40)

    def test_card_larger_group_wins_and_equal_groups_keep_first(self):
        def card(cls, x, text):
            return el("div", parent=0, box=(x, 0, 200, 100), text=text, attrs={"class": cls})

        wrapper = el("div", box=(0, 0, 900, 120))
        a = [card("a", x, f"a{n}") for n, x in enumerate((0, 220, 440))]
        b = [card("b", x, f"b{n}") for n, x in enumerate((0, 220, 440, 660))]
        t = target_of([wrapper, *a, *b], "card")
        self.assertEqual((t["text"], t["count"]), ("b0", 4))
        equal_b = [card("b", x, f"b{n}") for n, x in enumerate((0, 220, 440))]
        t = target_of([wrapper, *a, *equal_b], "card")
        self.assertEqual((t["text"], t["count"]), ("a0", 3))

    def test_cta_prefers_painted_background_over_larger_unpainted(self):
        t = target_of(
            [
                el("button", box=(0, 0, 400, 100), text="huge plain", font_size="16px"),
                el("button", box=(0, 200, 100, 40), text="go", background_color="rgb(10, 20, 30)"),
            ],
            "cta",
        )
        self.assertEqual(t["text"], "go")

    def test_cta_falls_back_to_area_when_nothing_painted(self):
        t = target_of(
            [
                el("button", box=(0, 0, 100, 40), text="small"),
                el("a", box=(0, 60, 200, 40), text="big", attrs={"class": "Main-BTN"}),
            ],
            "cta",
        )
        self.assertEqual(t["text"], "big")

    def test_cta_candidate_rules(self):
        plain_link = el("a", box=(0, 0, 200, 40), text="link")
        self.assertEqual(target_of([plain_link], "cta"), {"absent": True})
        role = el("a", box=(0, 0, 200, 40), text="role", attrs={"role": "button"})
        self.assertEqual(target_of([role], "cta")["text"], "role")

    def test_nav_first_in_dom_order_or_absent(self):
        self.assertEqual(target_of([el("div", box=(0, 0, 10, 10))], "nav"), {"absent": True})
        t = target_of(
            [
                el("div", box=(0, 0, 500, 60), attrs={"role": "navigation"}),
                el("nav", box=(0, 0, 500, 60)),
            ],
            "nav",
        )
        self.assertEqual(t["tag"], "div")

    def test_card_needs_three_qualifying_siblings(self):
        card = dict(box=(0, 0, 200, 120), attrs={"class": "c"}, parent=0)
        two = [
            el("div", parent=-1, box=(0, 0, 900, 900)),
            el("div", **{**card, "text": "a"}),
            el("div", **{**card, "text": "b"}),
        ]
        self.assertEqual(target_of(two, "card"), {"absent": True})
        three = [el("div", parent=-1, box=(0, 0, 900, 900))] + [
            el("div", **{**card, "box": (i * 210, 0, 200, 120), "text": f"c{i}"}) for i in range(3)
        ]
        t = target_of(three, "card")
        self.assertEqual((t["count"], t["text"]), (3, "c0"))

    def test_card_group_in_the_first_screen_beats_a_larger_group_below_the_fold(self):
        spec = [el("div", box=(0, 0, 1440, 160))]  # parent of the top row, node 0
        spec += [
            el("div", parent=0, box=(x, 20, 200, 120), text=f"t{x}", attrs={"class": "c"})
            for x in (0, 220, 440)
        ]
        spec += [el("div", box=(0, 1160, 1440, 600))]  # parent of the lower row, node 7
        spec += [
            el("div", parent=7, box=(x, 1180, 200, 120), text=f"b{x}", attrs={"class": "c"})
            for x in (0, 220, 440, 660)
        ]
        t = target_of(spec, "card")
        self.assertEqual((t["count"], t["box"][1]), (3, 20))

    def test_card_excludes_offscreen_small_and_svg(self):
        parent = el("div", box=(0, 0, 900, 900))
        cards = [el("div", parent=0, box=(0, 0, 100, 50), text="small", attrs={"class": "c"})]
        cards += [
            el("div", parent=0, box=(i * 210, 0, 200, 120), text=f"x{i}", attrs={"class": "c"})
            for i in range(2)
        ]
        cards += [el("div", parent=0, box=(1300, 0, 200, 120), text="edge", attrs={"class": "c"})]
        self.assertEqual(target_of([parent] + cards, "card"), {"absent": True})

    def test_card_svg_and_controls_excluded(self):
        parent = el("div", box=(0, 0, 900, 900))
        svg = el("svg", parent=0, box=(0, 0, 400, 400))
        groups = [
            el("g", parent=1, box=(i * 150, 0, 140, 100), text="g", attrs={"class": "x"})
            for i in range(3)
        ]
        controls = [
            el("button", parent=0, box=(i * 210, 0, 200, 120), text="b", attrs={"class": "x"})
            for i in range(3)
        ]
        self.assertEqual(target_of([parent, svg, *groups], "card"), {"absent": True})
        self.assertEqual(target_of([parent, *controls], "card"), {"absent": True})

    def test_html_cards_inside_foreign_object_are_cards(self):
        svg = el("svg", box=(0, 0, 900, 400))
        foreign = el("foreignObject", parent=0, box=(0, 0, 900, 400))
        cards = [
            el("article", parent=1, box=(i * 210, 0, 200, 120), text="c", attrs={"class": "tile"})
            for i in range(3)
        ]
        self.assertEqual(target_of([svg, foreign, *cards], "card")["count"], 3)

    def test_foreign_object_wrappers_are_svg_not_cards(self):
        svg = el("svg", box=(0, 0, 900, 400))
        wrappers = [
            el(
                "foreignObject",
                parent=0,
                box=(i * 210, 0, 200, 120),
                text="c",
                attrs={"class": "w"},
            )
            for i in range(3)
        ]
        self.assertEqual(target_of([svg, *wrappers], "card"), {"absent": True})

    def test_lock_is_deterministic_and_has_all_slots(self):
        data = evidence([el("h1", box=(0, 0, 100, 40), text="x", font_size="30px")])
        first = targets.lock_targets(data)
        self.assertEqual(list(first), list(targets.SLOTS))
        self.assertEqual(first, targets.lock_targets(data))


class ToleranceAndRounding(unittest.TestCase):
    def test_px_edge_is_inside_and_one_point_one_is_out(self):
        self.assertTrue(score.within("px", 56.0, 57.0))
        self.assertFalse(score.within("px", 56.0, 57.1))

    def test_zero_must_be_zero(self):
        self.assertFalse(score.within("px", 0.0, 0.4))
        self.assertTrue(score.within("px", 0.0, 0.0))

    def test_string_normalised(self):
        self.assertTrue(score.within("string", "Normal", "  normal "))

    def test_relative_two_percent(self):
        self.assertTrue(score.within("rel", 100.0, 101.9))
        self.assertFalse(score.within("rel", 100.0, 102.1))

    def test_threshold_rounds_up_to_whole_pin(self):
        self.assertEqual(score.required_correct(10), 9)
        self.assertEqual(score.required_correct(11), 10)  # 9.9 rounds up to 10
        self.assertEqual(score.required_correct(70), 63)  # 0.9*70 is not exact in floats
        self.assertEqual(score.required_correct(0), 0)

    def test_box_match_is_exact_after_math_round(self):
        pin = {"x": 10.4, "y": 20, "width": 100, "height": 40}
        self.assertTrue(score.box_matches(pin, [10, 20, 100, 40]))  # 10.4 rounds to 10
        self.assertFalse(score.box_matches(pin, [11, 20, 100, 40]))  # no 2px allowance
        self.assertFalse(score.box_matches(pin, [10, 20, 100, 41]))
        self.assertFalse(score.box_matches(pin, None))

    def test_placement_matches_app_rule(self):
        vp = {"width": 1440, "height": 900}
        self.assertEqual(score.placement({"x": 0, "y": 0, "width": 10, "height": 10}, vp), "in")
        self.assertEqual(
            score.placement({"x": 0, "y": 900, "width": 10, "height": 10}, vp), "below"
        )
        self.assertEqual(
            score.placement({"x": 1500, "y": 0, "width": 10, "height": 10}, vp), "outside"
        )
        self.assertEqual(
            score.placement({"x": 0, "y": 880, "width": 10, "height": 30}, vp), "partly"
        )
        self.assertEqual(score.placement(None, vp), "none")


class HandValues(unittest.TestCase):
    ALL = ["font_size", "line_height", "letter_spacing"]

    def test_tracking_converts_through_em(self):
        t = {"style": {"font-size": "20px", "line-height": "24px", "letter-spacing": "-0.4px"}}
        hand = score.hand_values(t, self.ALL)
        self.assertEqual(hand["font_size"], 20.0)
        self.assertEqual(hand["line_height"], 24.0)
        self.assertAlmostEqual(hand["letter_spacing"], -0.4)

    def test_normal_line_height_is_a_string(self):
        t = {"style": {"font-size": "20px", "line-height": "normal", "letter-spacing": "normal"}}
        self.assertEqual(score.hand_values(t, self.ALL)["line_height"], "normal")

    def test_only_requested_props_are_returned(self):
        t = {"style": {"font-size": "20px", "line-height": "24px"}}  # no tracking locked
        self.assertEqual(
            set(score.hand_values(t, ["font_size", "line_height"])), {"font_size", "line_height"}
        )

    def test_missing_locked_value_is_a_harness_defect_not_incorrect(self):
        t = {"style": {"font-size": "20px", "line-height": "24px"}}  # tracking never locked
        with self.assertRaises(score.HarnessDefect) as ctx:
            score.hand_values(t, ["letter_spacing"])
        self.assertIn("letter-spacing", str(ctx.exception))
        with self.assertRaises(score.HarnessDefect):
            score.hand_values({"style": {"font-size": "20px"}}, ["line_height"])

    def test_difference_scores_only_what_it_reports(self):
        self.assertEqual(
            score.reported_props("Body text at x anchor: 16px/24px → 18px/24px"),
            ["font_size", "line_height"],
        )
        detail = "Body text at x anchor: 16px/24px → 18px/24px, tracking 0.00em → 0.02em"
        self.assertEqual(
            score.reported_props(detail), ["font_size", "line_height", "letter_spacing"]
        )


class LockCoversReportedProps(unittest.TestCase):
    def test_every_slot_locks_every_type_key_gap_can_report(self):
        data = evidence(
            [
                el("h1", box=(0, 0, 300, 60), text="H", font_size="40px", line_height="44px"),
                el("p", box=(0, 70, 300, 20), text="x" * 40, letter_spacing="0.5px"),
                el("button", box=(0, 100, 100, 40), text="go", background_color="rgb(1, 2, 3)"),
                el("nav", box=(0, 0, 500, 60)),
                el("div", parent=-1, box=(0, 200, 900, 900)),
            ]
        )
        locked = targets.lock_targets(data)
        for slot in targets.SLOTS:
            if locked[slot].get("absent"):
                continue
            self.assertEqual(set(locked[slot]["style"]), set(targets.TYPE_KEYS), slot)
        self.assertEqual(locked["heading"]["style"]["font-size"], "40px")
        self.assertEqual(locked["body"]["style"]["letter-spacing"], "0.5px")
        self.assertEqual(locked["nav"]["style"]["line-height"], "normal")


def write_manifest(run, chromium="156.0.1"):
    """A registered manifest in run, as bench.c1.manifest writes it (fields score reads)."""
    data = {
        "piggyback_commit": "c0ffee",
        "chromium_version": chromium,
        "spot_check": {"seed": 1, "pool": ["C1-01"], "sample": ["C1-01"]},
    }
    (Path(run) / "manifest.json").write_text(json.dumps(data))


SMALL_PAIRS = {
    "primary": [{"id": "C1-01", "reference": "https://x.test/", "build": "https://x.test/"}],
    "reserves": [],
}


def decision(pid, value="keep"):
    return {
        "pair_id": pid,
        "decision": value,
        "reason": "test",
        "decided_by": "brodey",
        "decided_at_utc": RECEIPT,
    }


class LockAndOrdering(unittest.TestCase):
    def setUp(self):
        # Registration and the primary set are exercised in round6; here a one-pair run suffices.
        for name in ("targets.check_registered", "score.check_registered"):
            patch = mock.patch(f"bench.c1.{name}")
            patch.start()
            self.addCleanup(patch.stop)
        for name in ("targets.load_pairs", "score.load_pairs"):
            patch = mock.patch(f"bench.c1.{name}", return_value=SMALL_PAIRS)
            patch.start()
            self.addCleanup(patch.stop)

    def make_run(self, root, pair="C1-01", status="ok"):
        run = Path(root) / "run1"
        record = {"pair_id": pair, "status": status, "sides": {}}
        for side in ("reference", "build"):
            d = run / pair / side / "attempt-1"
            d.mkdir(parents=True)
            snapshot = evidence([el("h1", box=(0, 0, 100, 40), text="x", font_size="30px")])
            (d / "evidence.json").write_text(json.dumps(snapshot))
            record["sides"][side] = {
                "state": "ok",
                "requested_url": "https://x.test/",
                "attempt": 1,
            }
        (run / pair / "pair.json").write_text(json.dumps(record))
        write_manifest(run)
        return run

    def test_lock_writes_hash_and_refuses_relock(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as root:
            run = self.make_run(root)
            record = targets.lock(root, "run1")
            self.assertEqual(record["targets_sha256"], targets.sha256_file(run / "targets.json"))
            with self.assertRaises(SystemExit):
                targets.lock(root, "run1")

    def test_score_refuses_without_lock(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as root:
            self.make_run(root)
            with self.assertRaises(SystemExit) as ctx:
                score.run_score(root, "run1")
            self.assertIn("not locked", str(ctx.exception))

    def test_score_refuses_when_targets_changed_after_lock(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as root:
            run = self.make_run(root)
            targets.lock(root, "run1")
            (run / "targets.json").write_text("{}")
            with self.assertRaises(SystemExit) as ctx:
                score.run_score(root, "run1")
            self.assertIn("changed", str(ctx.exception))

    def test_unresolved_pairs_are_not_counted_as_retained(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as root:
            run = self.make_run(root, status="failed")  # failed capture: no snapshot to lock
            (run / "eligibility.json").write_text(json.dumps([decision("C1-01")]))
            targets.lock(root, "run1")
            report = score.run_score(root, "run1")
            self.assertEqual(report["retained_pairs"], 0)
            self.assertIsNone(report["avg_pinned_anchors"])
            self.assertIn("C1-01", report["unresolved"])
            self.assertEqual(report["verdict"], "incomplete")
            # Unresolved pairs keep their audit rows: a summary row and five anchor rows.
            summary = (run / "pair_summary.csv").read_text()
            self.assertIn("C1-01,0,0,0,failed", summary)
            anchors = (run / "anchors.csv").read_text()
            self.assertIn("C1-01,heading,not found,", anchors)
            self.assertEqual(
                sum(1 for line in anchors.splitlines() if line.startswith("C1-01,")), 5
            )

    def test_fingerprint_is_not_read_by_target_lock(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as root:
            run = self.make_run(root)
            (run / "C1-01" / "reference" / "fingerprint.json").write_text("not json")
            self.assertIn("C1-01", targets.lock(root, "run1")["pairs"])


class PrivateWrite(unittest.TestCase):
    """score._private_write (verdict.json, CSVs): exact bytes, owner-only, never via a link."""

    def test_writes_exact_bytes_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "verdict.json"
            score._private_write(path, "a\nb\n")
            self.assertEqual(path.read_bytes(), b"a\nb\n")
            if os.name != "nt":  # Windows keeps no POSIX mode bits
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_refuses_a_symbolic_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            victim = Path(tmp) / "victim"
            victim.write_text("keep")
            link = Path(tmp) / "verdict.json"
            try:
                link.symlink_to(victim)
            except OSError:
                self.skipTest("symlinks need privileges here")
            with self.assertRaises((OSError, SystemExit)):
                score._private_write(link, "overwritten")
            self.assertEqual(victim.read_text(), "keep")


class PairsFile(unittest.TestCase):
    def test_counts_ids_and_urls(self):
        data = json.loads((REPO / "bench/c1/pairs.json").read_text(encoding="utf-8"))
        self.assertEqual([p["id"] for p in data["primary"]], [f"C1-{n:02d}" for n in range(1, 21)])
        self.assertEqual([r["id"] for r in data["reserves"]], [f"R{n}" for n in range(1, 7)])
        self.assertEqual(data["primary"][0]["reference"], "https://buffer.com/")
        self.assertEqual(data["primary"][0]["build"], "https://www.todoist.com/")
        self.assertEqual(data["primary"][-1]["build"], "https://www.irs.gov/")
        self.assertEqual(data["reserves"][-1]["build"], "https://www.wingstop.com/")
        for pair in data["primary"] + data["reserves"]:
            self.assertTrue(pair["reference"].startswith("https://"))
            self.assertTrue(pair["reference"].endswith("/") or pair["reference"].endswith("/3/"))
            self.assertTrue(pair["build"].endswith("/") or pair["build"].endswith("/3/"))


class CaptureRecords(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_store_inside_repo_refused(self):
        with self.assertRaises(SystemExit):
            capture.check_store(REPO / "bench")

    def test_timeout_retried_once_and_both_attempts_kept(self):
        calls = []

        def fake(url, **_):
            calls.append(url)
            raise MeasureError("measure timed out after 90s")

        side = self.root / "ref"
        state, attempt, record = capture.capture_side("https://x.test/", side, measure_fn=fake)
        self.assertEqual((state, attempt, len(calls)), ("failed", 2, 2))
        self.assertEqual(record["requested_url"], "https://x.test/")
        self.assertEqual(record["class"], "timeout")
        for n in (1, 2):
            path = side / f"attempt-{n}" / "failure.json"
            if os.name != "nt":  # Windows keeps no POSIX mode bits
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_network_error_retried_once_and_both_attempts_kept(self):
        calls = []

        def fake(url, **_):
            calls.append(url)
            if len(calls) == 1:
                raise MeasureError("page.goto: net::ERR_SOCKET_NOT_CONNECTED at https://x.test/")
            return {"capture": {"status": 200, "final_url": url, "captured_at": RECEIPT}}

        state, attempt, _ = capture.capture_side(
            "https://x.test/", self.root / "net", measure_fn=fake
        )
        self.assertEqual((state, attempt, len(calls)), ("ok", 2, 2))
        failure = json.loads((self.root / "net" / "attempt-1" / "failure.json").read_text())
        self.assertEqual(failure["class"], "network")

    def test_network_error_twice_fails_as_network(self):
        calls = []

        def fake(url, **_):
            calls.append(url)
            raise MeasureError("page.goto: net::ERR_CONNECTION_RESET at https://x.test/")

        state, attempt, record = capture.capture_side(
            "https://x.test/", self.root / "nn", measure_fn=fake
        )
        self.assertEqual((state, attempt, len(calls)), ("failed", 2, 2))
        self.assertEqual(record["class"], "network")

    def test_playwright_timeout_message_is_retried(self):
        calls = []

        def fake(url, **_):
            calls.append(url)
            if len(calls) == 1:
                raise MeasureError("Timeout 30000ms exceeded.")
            return {"capture": {"status": 200, "final_url": url, "captured_at": RECEIPT}}

        state, attempt, _ = capture.capture_side(
            "https://x.test/", self.root / "p", measure_fn=fake
        )
        self.assertEqual((state, attempt, len(calls)), ("ok", 2, 2))
        self.assertTrue((self.root / "p" / "attempt-1" / "failure.json").exists())
        self.assertTrue((self.root / "p" / "attempt-2" / "fingerprint.json").exists())

    def test_non_timeout_error_not_retried(self):
        calls = []

        def fake(url, **_):
            calls.append(url)
            raise MeasureError("page answered HTTP 403 from x.test: measure a page that loads")

        state, attempt, record = capture.capture_side(
            "https://x.test/", self.root / "s", measure_fn=fake
        )
        self.assertEqual((state, attempt, len(calls)), ("failed", 1, 1))
        self.assertEqual(record["class"], "http_status")

    def test_failure_record_keeps_structured_status_and_final_url(self):
        def fake(url, **_):
            raise MeasureError(
                "page redirected to https://y.test/, not the host you gave",
                status=200,
                final_url="https://y.test/",
            )

        _, _, record = capture.capture_side("https://x.test/", self.root / "r", measure_fn=fake)
        self.assertEqual(record["status"], 200)
        self.assertEqual(record["final_url"], "https://y.test/")
        self.assertEqual(record["class"], "cross_host_redirect")

    def test_failure_screenshot_referenced_only_when_it_exists_and_is_0600(self):
        def fake(url, *, screenshot, **_):
            Path(screenshot).write_bytes(b"png")
            os.chmod(screenshot, 0o644)
            raise MeasureError("page answered HTTP 500", status=500, final_url=url)

        _, _, record = capture.capture_side("https://x.test/", self.root / "s5", measure_fn=fake)
        shot = Path(record["screenshot"])
        self.assertTrue(shot.exists())
        if os.name != "nt":  # Windows keeps no POSIX mode bits
            self.assertEqual(stat.S_IMODE(shot.stat().st_mode), 0o600)

    def test_new_attempt_never_reuses_or_overwrites_a_folder(self):
        side = self.root / "n"
        first, n1 = capture.new_attempt(side)
        (first / "failure.json").write_text("kept")
        second, n2 = capture.new_attempt(side)
        self.assertEqual((n1, n2), (1, 2))
        self.assertEqual((first / "failure.json").read_text(), "kept")

    def test_pair_with_attempts_is_never_captured_again(self):
        run = self.root / "run"
        (run / "C1-01" / "reference" / "attempt-1").mkdir(parents=True)
        with self.assertRaises(SystemExit):
            capture.capture_pair(capture.select_pairs(["C1-01"])[0], run)

    def test_bot_wall_is_kept_and_flagged(self):
        def fake(url, *, screenshot, evidence, **_):
            Path(evidence).write_text(
                json.dumps({"page": {"title": "Just a moment...", "text": "Checking your browser"}})
            )
            return {"capture": {"status": 200, "final_url": url, "captured_at": RECEIPT}}

        state, attempt, receipt = capture.capture_side(
            "https://x.test/", self.root / "b", measure_fn=fake
        )
        self.assertEqual((state, attempt), ("ok", 1))
        self.assertEqual(receipt["suspected_challenge"], "Just a moment")
        kept = json.loads((self.root / "b" / "attempt-1" / "fingerprint.json").read_text())
        self.assertEqual(kept["suspected_challenge"], "Just a moment")

    def test_unknown_pair_id_refused(self):
        with self.assertRaises(SystemExit):
            capture.select_pairs(["C1-99"])

    def test_selected_pairs_keep_given_order_and_never_substitute(self):
        picked = capture.select_pairs(["R2", "C1-01"])
        self.assertEqual([p["id"] for p in picked], ["R2", "C1-01"])


class MeasureHookOff(unittest.TestCase):
    def test_fingerprint_unchanged_and_no_cdp_when_off(self):
        import piggyback.measure as m

        class Page:
            def __init__(self):
                self.calls = []

            def evaluate(self, script, *_):
                self.calls.append(("evaluate", script[:20]))
                if "playState" in script:
                    return 0
                return {"anchors": {"heading": None}}

            def screenshot(self, **_):
                self.calls.append(("shot",))

            def emulate_media(self, **_):
                self.calls.append(("emulate",))

            def wait_for_load_state(self, *_, **__):
                pass

            @property
            def context(self):
                raise AssertionError("no CDP session when evidence is off")

        page = Page()
        out = m._capture_motion(page, None)
        self.assertEqual(out["anchors"], {"heading": None})
        self.assertEqual(out["reduced_motion"], {"still_running": 0})
        self.assertNotIn("context", [c[0] for c in page.calls])


class ManifestAndSpotCheck(unittest.TestCase):
    def test_spot_check_is_seeded_and_five_of_pool(self):
        pool = [f"C1-{n:02d}" for n in range(1, 21)]
        a = manifest.spot_check(pool, 7)
        self.assertEqual(a, manifest.spot_check(pool, 7))
        self.assertEqual(len(a), 5)
        self.assertTrue(set(a) <= set(pool))

    def test_dirty_worktree_refused(self):
        with mock.patch.object(manifest, "_git", return_value=" M x"):
            with self.assertRaises(SystemExit):
                manifest.clean_commit()


class SnapshotRead(unittest.TestCase):
    def test_inner_text_skips_hidden_nodes(self):
        data = evidence(
            [
                el("p", box=(0, 0, 300, 20), text="shown"),
                el("span", parent=0, box=None, text="hidden"),
            ]
        )
        snap = Snapshot(data)
        self.assertEqual(snap.inner_text(0), "shown")

    def test_body_ignores_shadow_root_text_before_a_light_dom_lead(self):
        # measure.js queries the light DOM only: the shadow <p>, first in DOM order, must not win.
        data = evidence(
            [
                el("my-el", box=(0, 0, 600, 200)),  # 0: host
                el("div", parent=0, box=(0, 0, 600, 40)),  # 1: shadow div
                el("p", parent=1, box=(0, 0, 600, 40), text="s" * 40),  # 2: shadow p, text 3
                el("p", box=(0, 300, 600, 40), text="p" * 40),  # 4: light p, text 5
            ]
        )
        with_shadow(data, [1, 2, 3])
        t = targets.lock_targets(data)["body"]
        self.assertEqual((t["tag"], t["dom_index"], t["text"]), ("p", 4, "p" * 40))

    def test_slotted_light_paragraph_is_body_eligible(self):
        # Slotted light content sits under the shadow <slot> but is light DOM: measure.js finds it.
        data = evidence(
            [
                el("my-el", box=(0, 0, 600, 200)),  # 0: host
                el("slot", parent=0),  # 1: shadow slot
                el("p", parent=1, box=(0, 100, 600, 40), text="s" * 40),  # 2: slotted p, text 3
            ]
        )
        with_shadow(data, [1])
        t = targets.lock_targets(data)["body"]
        self.assertEqual((t["tag"], t["dom_index"]), ("p", 2))

    def test_shadow_text_does_not_extend_host_ink_box(self):
        data = evidence(
            [
                el("my-el", box=(0, 0, 600, 200)),  # 0: host
                el("p", parent=0, box=(0, 150, 600, 40), text="s" * 40),  # 1: shadow p, text 2
            ]
        )
        with_shadow(data, [1, 2])
        snap = Snapshot(data)
        self.assertEqual(targets.ink_box(snap, snap.els[0]), (0, 0, 600, 200))

    def test_slotted_paragraph_under_light_nav_is_excluded_from_body(self):
        # Chromium: <nav><x-u>(shadow: <slot>)<p>slotted</p></x-u></nav>. The slotted p's flat
        # parent is the shadow slot; the walk must pass it and reach the light nav.
        data = evidence(
            [
                el("nav", box=(0, 0, 600, 40)),  # 0
                el("x-u", parent=0, box=(0, 0, 600, 40)),  # 1: light host
                el("slot", parent=1),  # 2: shadow slot
                el("p", parent=2, box=(0, 50, 600, 40), text="s" * 40),  # 3: slotted, text 4
                el("p", box=(0, 300, 600, 40), text="p" * 40),  # 5: plain, text 6
            ]
        )
        with_shadow(data, [2])
        t = targets.lock_targets(data)["body"]
        self.assertEqual((t["tag"], t["dom_index"]), ("p", 5))

    def test_slotted_light_heading_text_counts_and_shadow_text_does_not(self):
        # Chromium: <h1><x-s>(shadow: <b>SHADOWTXT</b><slot>)<span>Light heading</span></x-s></h1>
        data = evidence(
            [
                el("h1", box=(0, 0, 400, 40)),  # 0
                el("x-s", parent=0, box=(0, 0, 200, 40)),  # 1: light host
                el("b", parent=1, box=(0, 0, 80, 40), text="SHADOWTXT"),  # 2: shadow, text 3
                el("slot", parent=1),  # 4: shadow slot
                el("span", parent=4, box=(0, 0, 200, 40), text="Light heading"),  # 5, text 6
            ]
        )
        with_shadow(data, [2, 3, 4])
        t = targets.lock_targets(data)["heading"]
        self.assertEqual((t["tag"], t["text"], t["box"]), ("h1", "Light heading", [0, 0, 200, 40]))

    def test_inner_text_and_own_text_take_slotted_light_text_only(self):
        # Chromium: div host with shadow 'SHADOWONLY <slot>' and light text 'slot light'.
        data = evidence(
            [
                el("div", box=(0, 0, 600, 200)),  # 0: host
                el("span", parent=0, box=(0, 0, 100, 20), text="SHADOWONLY "),  # 1: shadow, text 2
                el("slot", parent=0, box=(0, 40, 200, 20), text="slot light"),  # 3, text 4
            ]
        )
        with_shadow(data, [1, 2, 3])
        snap = Snapshot(data)
        self.assertEqual(snap.inner_text(0), "slot light")
        self.assertEqual(snap.own_text(0), "slot light")


class FingerprintContract(unittest.TestCase):
    def test_gap_anchor_slots_match_contract(self):
        self.assertEqual(list(ANCHORS), ["heading", "body", "cta", "nav", "card"])
        self.assertIsNotNone(gap)
        self.assertIsNotNone(fp)
        self.assertEqual(score.SLOT_OF_CONCEPT["heading_type"], "heading")


FIXTURE = REPO / "tests" / "fixtures" / "c1"
RECEIPT = "2026-01-01T00:00:00+00:00"


def _targets_from(fingerprint):
    """Locked-style targets for each anchor slot, read from one scrubbed fingerprint fixture."""
    out = {}
    for slot in score.SLOTS:
        anchor = fp.anchor(fingerprint, slot)
        if anchor is None:
            out[slot] = {"absent": True}
            continue
        out[slot] = {
            "tag": anchor["tag"],
            "text": anchor["text"],
            "box": [anchor["box"][k] for k in ("x", "y", "width", "height")],
            "style": {
                "font-size": anchor["font_size"],
                "line-height": anchor["line_height"],
                "letter-spacing": anchor["letter_spacing"],
            },
        }
    return out


class RealPairsCapture(unittest.TestCase):
    def test_capture_pair_runs_every_real_pairs_entry(self):
        def fake(url, *, screenshot, evidence, **_):
            Path(evidence).write_text(json.dumps({"page": {"title": "t", "text": "body"}}))
            return {"capture": {"status": 200, "final_url": url, "captured_at": RECEIPT}}

        data = capture.load_pairs()
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            for pair in data["primary"] + data["reserves"]:
                record = capture.capture_pair(pair, Path(tmp), measure_fn=fake)
                self.assertEqual(record["status"], "ok", pair["id"])
                self.assertEqual(record["sides"]["reference"]["requested_url"], pair["reference"])
                self.assertEqual(record["sides"]["build"]["requested_url"], pair["build"])


class OffSetPinsFromGap(unittest.TestCase):
    def setUp(self):
        self.fps = {
            side: json.loads((FIXTURE / f"{side}.json").read_text(encoding="utf-8"))
            for side in ("reference", "build")
        }

    def test_score_pair_skips_page_level_pins_and_scores_anchors(self):
        concepts = {pin.concept for pin in gap(self.fps["reference"], self.fps["build"])}
        self.assertTrue({"accent", "container"} <= concepts)  # real gap output, page-level
        targets = {side: _targets_from(self.fps[side]) for side in self.fps}
        receipts = _meta()
        pin_rows, _, anchor_rows, shown, correct, pinned, skipped = score.score_pair(
            "D-01", targets, self.fps, receipts
        )
        self.assertEqual(skipped, 2)  # accent and container are outside pin accuracy
        self.assertEqual([row[1] for row in anchor_rows], list(score.SLOTS))
        self.assertTrue({row[1] for row in pin_rows} <= set(score.SLOTS))
        self.assertLessEqual(correct, shown)
        self.assertLessEqual(pinned, len(score.SLOTS))

    def test_pin_with_no_locked_hand_value_raises_not_incorrect(self):
        targets = {side: _targets_from(self.fps[side]) for side in self.fps}
        for side in targets:
            for slot in score.SLOTS:
                if not targets[side][slot].get("absent"):
                    targets[side][slot]["style"].pop("line-height")  # hand value never locked
        receipts = _meta()
        with self.assertRaises(score.HarnessDefect):
            score.score_pair("D-01", targets, self.fps, receipts)


if __name__ == "__main__":
    with contextlib.redirect_stdout(io.StringIO()):
        unittest.main()
