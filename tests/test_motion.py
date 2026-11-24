import unittest

from piggyback import motion


def sample(scroll_y, *, animations=None, elements=None, dense=None):
    return {
        "scroll_y": scroll_y,
        "animations": animations,
        "elements": elements or [],
        "dense": dense or [],
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


def step(index, scroll_y, **kwargs):
    return {
        "step": index,
        "target_scroll_y": scroll_y,
        "scroll_onset_ms": 12.0,
        **sample(scroll_y, **kwargs),
    }


class MotionTableTests(unittest.TestCase):
    def test_animation_timing_is_observed_and_marked_as_such(self):
        steps = [step(0, 0), step(1, 200, animations=[anim(duration=300.0, delay=50.0)])]
        text = motion.build(data(steps))
        self.assertIn("300 ms (observed/runtime)", text)
        self.assertIn("50 ms (observed/runtime)", text)
        self.assertIn("ease-out (observed/runtime)", text)

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

    def test_a_transform_entry_the_page_did_not_send_as_a_number_is_not_measured(self):
        first = [element(transform=[1, 0, 0, 1, None, 0])]
        last = [element(transform=[1, 0, 0, 1, 0, 40])]
        steps = [step(0, 0, elements=first), step(1, 200, elements=last)]
        text = motion.build(data(steps))
        self.assertIn("matrix(1, 0, 0, 1, not measured, 0) (observed/runtime)", text)

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
