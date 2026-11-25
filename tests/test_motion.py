import unittest

from piggyback import measure, motion


def sample(scroll_y, *, animations=None, elements=None, count=10):
    return {
        "scroll_y": scroll_y,
        "animations": animations,
        "elements": elements or [],
        "count": count,
    }


def anim(duration=300.0, delay=0.0, easing="ease-out"):
    return {
        "kind": "transition",
        "duration": duration,
        "delay": delay,
        "iterations": 1.0,
        "easing": easing,
        "play_state": "running",
    }


def element(index=0, tag="div", transform=None, opacity=1.0, box=None, position="static", top=None):
    return {
        "index": index,
        "tag": tag,
        "box": box or [0.0, 0.0, 10.0, 10.0],
        "transform": transform,
        "opacity": opacity,
        "clip_path": "none",
        "position": position,
        "top": top,
    }


def data(steps, coverage="done"):
    return {
        "viewport": {"width": 1440, "height": 900},
        "scrollable_px": 1000,
        "scrollable_end_px": 1000,
        "coverage": {"scroll": coverage},
        "steps": steps,
    }


def step(index, scroll_y, *, dense=None, **kwargs):
    """A step as measure.py records it: its dense samples first, then its settled sample."""
    return {
        "step": index,
        "target_scroll_y": scroll_y,
        "scroll_onset_ms": 12.0,
        **sample(scroll_y, **kwargs),
        "dense": dense or [],
    }


def rows_with(text, prop):
    return [line for line in text.splitlines() if line.startswith("| M") and prop in line]


class MotionTableTests(unittest.TestCase):
    def test_animation_timing_is_observed_and_marked_as_such(self):
        steps = [step(0, 0), step(1, 200, animations=[anim(duration=300.0, delay=50.0)])]
        text = motion.build(data(steps))
        self.assertIn("300 ms (observed/runtime)", text)
        self.assertIn("50 ms (observed/runtime)", text)
        self.assertIn("ease-out (observed/runtime)", text)

    def test_the_trigger_says_where_it_was_seen_and_claims_no_cause(self):
        steps = [step(0, 0, animations=[anim()]), step(1, 200, animations=[anim()])]
        self.assertIn("seen at steps 0-1 (observed/runtime)", motion.build(data(steps)))

    def test_an_animation_without_a_linked_element_has_its_role_not_measured(self):
        steps = [step(0, 0), step(1, 200, animations=[anim()])]
        row = next(line for line in motion.build(data(steps)).splitlines() if "transition" in line)
        self.assertIn("| not measured |", row)

    def test_an_element_transform_that_changed_has_endpoints_and_no_guessed_duration(self):
        first = [element(transform=[1, 0, 0, 1, 0, 0])]
        last = [element(transform=[1, 0, 0, 1, 0, 40])]
        steps = [step(0, 0, elements=first), step(1, 200, elements=last)]
        text = motion.build(data(steps))
        endpoints = (
            "matrix(1, 0, 0, 1, 0, 0) (observed/runtime) → "
            "matrix(1, 0, 0, 1, 0, 40) (observed/runtime)"
        )
        self.assertIn(endpoints, text)
        self.assertIn("| not measured | not measured | not measured |", text)

    def test_an_unchanged_element_gets_no_row(self):
        steps = [step(0, 0, elements=[element(opacity=1.0)]), step(1, 200, elements=[element()])]
        self.assertNotIn("opacity", motion.build(data(steps)))

    def test_unobserved_animation_data_is_not_measured_and_never_a_guess(self):
        steps = [step(0, 0, animations=None), step(1, 200, animations=None)]
        text = motion.build(data(steps))
        self.assertIn("No motion observed", text)
        self.assertNotIn("observed/code", text)
        self.assertNotIn("inferred", text)

    def test_a_partial_scroll_is_stated_in_the_limits(self):
        text = motion.build(data([step(0, 0)], coverage="partial"))
        self.assertIn("Coverage: partial", text)

    def test_the_output_is_deterministic(self):
        steps = [step(0, 0), step(1, 200, animations=[anim()])]
        self.assertEqual(motion.build(data(steps)), motion.build(data(steps)))

    def test_a_table_cell_never_gains_a_pipe_or_newline_from_page_text(self):
        steps = [step(0, 0, elements=[element(tag="x|y\nz", transform=[1, 0, 0, 1, 0, 0])])]
        steps.append(step(1, 200, elements=[element(tag="x|y\nz", transform=[1, 0, 0, 1, 0, 9])]))
        for line in motion.build(data(steps)).splitlines():
            if line.startswith("| M"):
                self.assertEqual(line.replace("\\|", "").count("|"), 12)

    def test_a_transform_entry_the_page_did_not_send_as_a_number_is_not_measured(self):
        first = [element(transform=[1, 0, 0, 1, None, 0])]
        last = [element(transform=[1, 0, 0, 1, 0, 40])]
        steps = [step(0, 0, elements=first), step(1, 200, elements=last)]
        text = motion.build(data(steps))
        self.assertIn("matrix(1, 0, 0, 1, not measured, 0) (observed/runtime)", text)

    def test_a_computed_transform_of_none_is_the_identity_matrix(self):
        first = [element(transform=None)]
        last = [element(transform=[1, 0, 0, 1, 0, 40])]
        steps = [step(0, 0, elements=first), step(1, 9, elements=last)]
        text = motion.build(data(steps))
        self.assertIn("matrix(1, 0, 0, 1, 0, 0) (observed/runtime) → ", text)

    def test_dense_samples_come_before_their_steps_settled_sample(self):
        dense = [sample(100, elements=[element(opacity=0.5)])]
        steps = [
            step(0, 0, elements=[element(opacity=1.0)]),
            step(1, 200, dense=dense, elements=[element(opacity=0.25)]),
        ]
        text = motion.build(data(steps))
        self.assertIn("1 (observed/runtime) → 0.25 (observed/runtime)", text)

    def test_the_same_index_with_another_tag_is_another_element(self):
        steps = [
            step(0, 0, elements=[element(index=0, tag="div", opacity=1.0)]),
            step(1, 200, elements=[element(index=0, tag="span", opacity=0.5)]),
        ]
        self.assertEqual(rows_with(motion.build(data(steps)), "opacity"), [])

    def test_samples_with_different_element_counts_are_not_paired(self):
        steps = [
            step(0, 0, count=10, elements=[element(opacity=1.0)]),
            step(1, 200, count=11, elements=[element(opacity=0.5)]),
        ]
        self.assertEqual(rows_with(motion.build(data(steps)), "opacity"), [])

    def test_a_duplicate_index_within_one_sample_is_ambiguous_and_skipped(self):
        steps = [
            step(0, 0, elements=[element(opacity=1.0), element(opacity=0.2)]),
            step(1, 200, elements=[element(opacity=0.5)]),
        ]
        self.assertEqual(rows_with(motion.build(data(steps)), "opacity"), [])

    def test_a_value_that_goes_and_comes_back_is_still_a_row(self):
        steps = [
            step(0, 0, elements=[element(opacity=1.0)]),
            step(1, 200, elements=[element(opacity=0.5)]),
            step(2, 400, elements=[element(opacity=1.0)]),
        ]
        text = motion.build(data(steps))
        self.assertIn("1 (observed/runtime) → 1 (observed/runtime)", text)
        self.assertIn("min 0.5, max 1", text)

    def test_rows_over_the_cap_say_how_many_are_not_shown(self):
        steps = [step(0, 0, animations=[anim(duration=d) for d in range(50)])]
        self.assertIn("30 more rows not shown", motion.build(data(steps)))

    def test_element_rows_are_not_crowded_out_by_animations(self):
        first = [element(index=i, opacity=1.0) for i in range(30)]
        last = [element(index=i, opacity=0.5) for i in range(30)]
        steps = [
            step(0, 0, animations=[anim(duration=d) for d in range(40)], elements=first),
            step(1, 200, elements=last),
        ]
        self.assertTrue(rows_with(motion.build(data(steps)), "opacity"))

    def test_spread_counts_distinct_transforms_only_above_two(self):
        a = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
        b = [1.0, 0.0, 0.0, 1.0, 5.0, 0.0]
        c = [1.0, 0.0, 0.0, 1.0, 9.0, 0.0]
        self.assertEqual(motion._spread("transform", [a, b, c]), "; 3 distinct transforms")
        self.assertEqual(motion._spread("transform", [a, b, a]), "")


def pinned(box, position="fixed", tag="div", top=None):
    return element(tag=tag, box=box, position=position, top=top)


def pin_rows(text):
    return rows_with(text, "scroll (position:")


class PinRowTests(unittest.TestCase):
    def test_a_fixed_full_width_header_held_at_the_top_is_one_pin_row(self):
        header = pinned([0.0, 0.0, 1440.0, 60.0], tag="header")
        steps = [step(i, 200.0 * i, elements=[header]) for i in range(6)]
        text = motion.build(data(steps))
        rows = pin_rows(text)
        self.assertEqual(len(rows), 1)
        self.assertIn("full-width bar at the top, header (observed/runtime)", rows[0])
        self.assertIn("held at top 0 px, 1440 x 60 px, at left 0 px (observed/runtime)", rows[0])
        self.assertIn("0-1000 px (observed/runtime)", rows[0])

    def test_a_sticky_column_is_stuck_across_the_scrolls_it_holds_its_top(self):
        ys = [400.0, 200.0, 0.0, 0.0, 0.0, -200.0]
        steps = [
            step(i, 200.0 * i, elements=[pinned([1200.0, y, 200.0, 400.0], "sticky", "aside", 0.0)])
            for i, y in enumerate(ys)
        ]
        rows = pin_rows(motion.build(data(steps)))
        self.assertEqual(len(rows), 1)
        self.assertIn("box, aside (observed/runtime)", rows[0])
        self.assertIn("scroll (position: sticky) (observed/runtime)", rows[0])
        self.assertIn("stuck at top 0 px, 200 x 400 px, at left 1200 px", rows[0])
        self.assertIn("400-800 px (observed/runtime)", rows[0])
        self.assertIn("moving seen 0-1000 px (observed/runtime)", rows[0])

    def test_a_sticky_element_stuck_once_at_its_top_is_one_row(self):
        steps = [
            step(0, 0, elements=[pinned([0.0, 500.0, 300.0, 200.0], "sticky", "aside", 130.0)]),
            step(
                1, 2419.0, elements=[pinned([0.0, 130.0, 300.0, 200.0], "sticky", "aside", 130.0)]
            ),
            step(
                2, 3628.0, elements=[pinned([0.0, -580.0, 300.0, 200.0], "sticky", "aside", 130.0)]
            ),
        ]
        rows = pin_rows(motion.build(data(steps)))
        self.assertEqual(len(rows), 1)
        self.assertIn(
            "at its CSS top 130 px at scroll 2419 px, 300 x 200 px, at left 0 px "
            "(observed/runtime)",
            rows[0],
        )
        self.assertIn("sticking across scroll not measured", rows[0])

    def test_a_sticky_top_within_one_pixel_of_its_computed_top_is_stuck(self):
        steps = [
            step(0, 0, elements=[pinned([0.0, 500.0, 300.0, 200.0], "sticky", "aside", 130.0)]),
            step(
                1, 2419.0, elements=[pinned([0.0, 130.5, 300.0, 200.0], "sticky", "aside", 130.0)]
            ),
        ]
        self.assertEqual(len(pin_rows(motion.build(data(steps)))), 1)

    def test_a_sticky_element_whose_y_never_equals_its_top_gets_no_row(self):
        steps = [
            step(i, 200.0 * i, elements=[pinned([0.0, y, 300.0, 200.0], "sticky", "aside", 130.0)])
            for i, y in enumerate([500.0, 400.0, 300.0, 200.0])
        ]
        self.assertEqual(pin_rows(motion.build(data(steps))), [])

    def test_a_sticky_element_stuck_only_in_a_dense_sample_gets_no_row(self):
        dense = [
            sample(2419.0, elements=[pinned([0.0, 130.0, 300.0, 200.0], "sticky", "aside", 130.0)])
        ]
        steps = [
            step(0, 0, elements=[pinned([0.0, 500.0, 300.0, 200.0], "sticky", "aside", 130.0)]),
            step(
                1,
                2419.0,
                dense=dense,
                elements=[pinned([0.0, 500.0, 300.0, 200.0], "sticky", "aside", 130.0)],
            ),
        ]
        self.assertEqual(pin_rows(motion.build(data(steps))), [])

    def test_a_sticky_element_with_no_top_gets_no_stuck_row(self):
        steps = [
            step(i, 200.0 * i, elements=[pinned([0.0, 130.0, 300.0, 200.0], "sticky", "aside")])
            for i in range(3)
        ]
        self.assertEqual(pin_rows(motion.build(data(steps))), [])

    def test_a_fixed_header_across_a_count_change_is_one_row_over_both_ranges(self):
        header = pinned([0.0, 0.0, 1440.0, 60.0], tag="header")
        steps = [
            step(i, 200.0 * i, count=1103 if i < 3 else 1111, elements=[header]) for i in range(6)
        ]
        rows = pin_rows(motion.build(data(steps)))
        self.assertEqual(len(rows), 1)
        self.assertIn("0-1000 px (observed/runtime)", rows[0])

    def test_a_fixed_element_that_moves_gets_no_pin_row(self):
        steps = [
            step(i, 200.0 * i, elements=[pinned([0.0, 10.0 * i, 10.0, 10.0])]) for i in range(6)
        ]
        self.assertEqual(pin_rows(motion.build(data(steps))), [])

    def test_a_fixed_bottom_box_is_a_box_not_a_column(self):
        widget = pinned([0.0, 675.0, 392.0, 205.0])
        steps = [step(i, 200.0 * i, elements=[widget]) for i in range(6)]
        rows = pin_rows(motion.build(data(steps)))
        self.assertEqual(len(rows), 1)
        self.assertIn("box, div (observed/runtime)", rows[0])
        self.assertNotIn("column", rows[0])

    def test_a_full_width_bar_at_the_bottom_is_named_as_one(self):
        bar = pinned([0.0, 896.0, 1440.0, 4.0], tag="footer")
        steps = [step(i, 200.0 * i, elements=[bar]) for i in range(6)]
        rows = pin_rows(motion.build(data(steps)))
        self.assertIn("full-width bar at the bottom, footer (observed/runtime)", rows[0])

    def test_a_drift_within_one_pixel_still_counts_as_held(self):
        ys = [0.0, 0.5, 0.0, 0.5, 0.0, 0.5]
        steps = [
            step(i, 200.0 * i, elements=[pinned([0.0, y, 1440.0, 60.0])]) for i, y in enumerate(ys)
        ]
        self.assertEqual(len(pin_rows(motion.build(data(steps)))), 1)

    def test_an_element_seen_in_one_sample_only_is_not_a_pin(self):
        steps = [step(0, 0, elements=[pinned([0.0, 0.0, 1440.0, 60.0])]), step(1, 200)]
        self.assertEqual(pin_rows(motion.build(data(steps))), [])

    def test_pin_rows_come_before_animation_rows(self):
        header = pinned([0.0, 0.0, 1440.0, 60.0])
        steps = [
            step(0, 0, animations=[anim()], elements=[header]),
            step(1, 200, animations=[anim()], elements=[header]),
        ]
        lines = motion.build(data(steps)).splitlines()
        pin = next(i for i, line in enumerate(lines) if "scroll (position:" in line)
        animation = next(i for i, line in enumerate(lines) if "not linked" in line)
        self.assertLess(pin, animation)

    def test_pin_rows_have_their_own_cap_that_animations_cannot_take(self):
        pins = [
            element(index=i, box=[0.0, 0.0, 10.0 + i, 10.0], position="fixed") for i in range(30)
        ]
        steps = [
            step(0, 0, animations=[anim(duration=d) for d in range(40)], elements=pins),
            step(1, 200, animations=[anim(duration=d) for d in range(40)], elements=pins),
        ]
        text = motion.build(data(steps))
        self.assertEqual(len(pin_rows(text)), 10)
        self.assertEqual(len(rows_with(text, "not linked")), 20)
        self.assertIn("40 more rows not shown", text)


def stuck_nav(top, box_y):
    return pinned([0.0, box_y, 1440.0, 60.0], "sticky", "nav", top)


class PinRulingTests(unittest.TestCase):
    def header(self, y, x=0.0, width=1440.0, height=60.0, opacity=1.0):
        return element(tag="header", box=[x, y, width, height], position="fixed", opacity=opacity)

    def held(self, tops, scrolls=(0.0, 200.0, 400.0)):
        return [
            step(i, s, elements=[self.header(y)])
            for i, (s, y) in enumerate(zip(scrolls, tops, strict=True))
        ]

    def test_a_page_that_never_scrolls_gives_no_fixed_pin(self):
        steps = [step(i, 0.0, elements=[self.header(0.0)]) for i in range(3)]
        self.assertEqual(rows_with(motion.build(data(steps)), "held at top"), [])

    def test_an_ambiguous_duplicate_pin_key_gives_no_row(self):
        twins = [self.header(0.0), self.header(0.0)]
        steps = [step(0, 0.0, elements=twins), step(1, 200.0, elements=twins)]
        self.assertEqual(rows_with(motion.build(data(steps)), "held at top"), [])

    def test_same_size_buttons_at_different_x_are_two_rows(self):
        left = element(tag="button", box=[0.0, 10.0, 100.0, 40.0], position="fixed")
        right = element(tag="button", box=[200.0, 10.0, 100.0, 40.0], position="fixed")
        steps = [step(0, 0.0, elements=[left, right]), step(1, 200.0, elements=[left, right])]
        self.assertEqual(len(rows_with(motion.build(data(steps)), "held at top")), 2)

    def test_stacked_sticky_cards_with_tops_100_and_120_are_two_rows(self):
        def card(top, y):
            return pinned([0.0, y, 300.0, 200.0], "sticky", "div", top)

        steps = [
            step(0, 0.0, elements=[card(100.0, 300.0), card(120.0, 320.0)]),
            step(1, 200.0, elements=[card(100.0, 100.0), card(120.0, 120.0)]),
            step(2, 400.0, elements=[card(100.0, 100.0), card(120.0, 120.0)]),
        ]
        self.assertEqual(len(rows_with(motion.build(data(steps)), "stuck at top")), 2)

    def test_a_header_that_wobbles_under_a_pixel_is_one_row(self):
        rows = rows_with(motion.build(data(self.held([0.4, 0.6, 0.4]))), "held at top")
        self.assertEqual(len(rows), 1)

    def test_a_drift_across_the_rounding_boundary_is_one_row(self):
        rows = rows_with(
            motion.build(data(self.held([10.49, 10.51], scrolls=(0.0, 200.0)))), "held at top"
        )
        self.assertEqual(len(rows), 1)

    def test_a_missing_middle_sample_breaks_the_hold(self):
        steps = [
            step(0, 0.0, elements=[self.header(0.0)]),
            step(1, 200.0, elements=[]),
            step(2, 400.0, elements=[self.header(0.0)]),
        ]
        self.assertEqual(rows_with(motion.build(data(steps)), "held at top"), [])

    def test_a_held_pair_puts_both_of_its_scrolls_in_the_range(self):
        text = motion.build(data(self.held([0.0, 0.0], scrolls=(0.0, 200.0))))
        self.assertIn("0-200 px (observed/runtime)", rows_with(text, "held at top")[0])

    def test_a_sticky_top_only_at_scroll_zero_gives_no_row(self):
        steps = [
            step(0, 0.0, elements=[stuck_nav(0.0, 0.0)]),
            step(1, 200.0, elements=[stuck_nav(0.0, -200.0)]),
            step(2, 400.0, elements=[stuck_nav(0.0, -400.0)]),
        ]
        self.assertEqual(rows_with(motion.build(data(steps)), "CSS top"), [])
        self.assertEqual(rows_with(motion.build(data(steps)), "stuck at top"), [])

    def test_a_singleton_sticky_says_its_sticking_is_not_measured(self):
        steps = [
            step(0, 0.0, elements=[stuck_nav(0.0, 300.0)]),
            step(1, 200.0, elements=[stuck_nav(0.0, 0.0)]),
            step(2, 400.0, elements=[stuck_nav(0.0, -400.0)]),
        ]
        row = rows_with(motion.build(data(steps)), "CSS top")[0]
        self.assertIn("at its CSS top 0 px at scroll 200 px, 1440 x 60 px, at left 0 px", row)
        self.assertIn("sticking across scroll not measured", row)

    def test_a_sticky_stuck_at_two_scrolls_is_stuck_with_no_such_note(self):
        steps = [
            step(0, 0.0, elements=[stuck_nav(0.0, 300.0)]),
            step(1, 200.0, elements=[stuck_nav(0.0, 0.0)]),
            step(2, 400.0, elements=[stuck_nav(0.0, 0.0)]),
        ]
        row = rows_with(motion.build(data(steps)), "stuck at top")[0]
        self.assertNotIn("sticking across scroll not measured", row)

    def test_an_opacity_zero_overlay_is_not_a_pin(self):
        steps = [
            step(0, 0.0, elements=[self.header(0.0, opacity=0.0)]),
            step(1, 200.0, elements=[self.header(0.0, opacity=0.0)]),
        ]
        self.assertEqual(rows_with(motion.build(data(steps)), "held at top"), [])

    def test_a_full_height_fixed_panel_is_a_box_not_a_bar(self):
        panel = self.header(0.0, height=900.0)
        text = motion.build(
            data([step(0, 0.0, elements=[panel]), step(1, 200.0, elements=[panel])])
        )
        self.assertIn("box, header (observed/runtime)", rows_with(text, "held at top")[0])
        self.assertNotIn("full-width bar", text)

    def test_a_fixed_header_that_jumps_is_two_rows_and_no_row_spans_the_jump(self):
        text = motion.build(data(self.held([10.0, 10.0, 50.0, 50.0], scrolls=(0, 200, 400, 600))))
        rows = rows_with(text, "held at top")
        self.assertEqual(len(rows), 2)
        self.assertIn("held at top 10 px", rows[0])
        self.assertIn("0-200 px (observed/runtime)", rows[0])
        self.assertIn("held at top 50 px", rows[1])
        self.assertIn("400-600 px (observed/runtime)", rows[1])
        self.assertNotIn("| 0-600 px", text)
        self.assertIn("moving seen 200-400 px", rows[0])
        self.assertIn("moving seen 200-400 px", rows[1])

    def test_a_sticky_that_leaves_its_top_between_two_stuck_scrolls_is_two_singletons(self):
        steps = [
            step(0, 0.0, elements=[stuck_nav(200.0, 500.0)]),
            step(1, 200.0, elements=[stuck_nav(200.0, 200.0)]),
            step(2, 400.0, elements=[stuck_nav(200.0, 500.0)]),
            step(3, 600.0, elements=[stuck_nav(200.0, 200.0)]),
        ]
        text = motion.build(data(steps))
        rows = rows_with(text, "CSS top")
        self.assertEqual(len(rows), 2)
        self.assertIn("at its CSS top 200 px at scroll 200 px", rows[0])
        self.assertIn("at its CSS top 200 px at scroll 600 px", rows[1])
        self.assertNotIn("| 200-600 px", text)

    def test_an_unsettled_unstuck_sample_does_not_split_a_stuck_sticky(self):
        steps = [
            step(0, 0.0, elements=[stuck_nav(200.0, 200.0)]),
            step(
                1,
                200.0,
                dense=[sample(200.0, elements=[stuck_nav(200.0, 500.0)])],
                elements=[stuck_nav(200.0, 200.0)],
            ),
            step(2, 400.0, elements=[stuck_nav(200.0, 200.0)]),
        ]
        rows = rows_with(motion.build(data(steps)), "stuck at top 200 px")
        self.assertEqual(len(rows), 1)
        self.assertIn("| 0-400 px (observed/runtime)", rows[0])

    def test_a_hostile_sticky_top_is_dropped_before_the_table(self):
        def raw_nav(top, y=0.0):
            nav = {
                "index": 0,
                "tag": "nav",
                "box": [0.0, y, 1440.0, 60.0],
                "transform": None,
                "opacity": 1.0,
                "clip_path": "none",
                "position": "sticky",
                "top": top,
            }
            steps = [
                {
                    "step": i,
                    "target_scroll_y": 200.0 * i,
                    "scroll_onset_ms": 1.0,
                    "scroll_y": 200.0 * i,
                    "count": 5,
                    "animations": None,
                    "elements": [nav],
                    "dense": [],
                }
                for i in range(3)
            ]
            raw = {
                "viewport": {"width": 1440, "height": 900},
                "coverage": {"scroll": "done"},
                "steps": steps,
            }
            return measure.clean_scroll(raw)

        # At 1e6 px the box sits where the clamped top would be: only a dropped bound shows a row.
        for hostile, y in (("<img src=x onerror=1>", 0.0), (1e300, 1e6), ("1e9px", 0.0)):
            with self.subTest(top=hostile):
                text = motion.build(raw_nav(hostile, y))
                self.assertNotIn("<", text)
                self.assertEqual(rows_with(text, "stuck at top"), [])
                self.assertEqual(rows_with(text, "CSS top"), [])
        self.assertTrue(rows_with(motion.build(raw_nav(0.0)), "stuck at top"))


if __name__ == "__main__":
    unittest.main()
