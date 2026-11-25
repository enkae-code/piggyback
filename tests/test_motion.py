import unittest

from piggyback import motion


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


def element(index=0, tag="div", transform=None, opacity=1.0):
    return {
        "index": index,
        "tag": tag,
        "box": [0.0, 0.0, 10.0, 10.0],
        "transform": transform,
        "opacity": opacity,
        "clip_path": "none",
        "position": "fixed",
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
