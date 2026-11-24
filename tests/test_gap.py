import unittest

from piggyback import gap as gap_mod


def still(count):
    return {"reduced_motion": {"still_running": count}}


def reduced_pins(ref, build):
    return [pin for pin in gap_mod.gap(ref, build) if pin.concept == "reduced_motion"]


class ReducedMotionPinTests(unittest.TestCase):
    def test_build_that_respects_reduced_motion_is_not_pinned(self):
        self.assertEqual(reduced_pins(still(1), still(0)), [])

    def test_build_that_keeps_more_animations_running_is_pinned_as_a_build_fix(self):
        pins = reduced_pins(still(0), still(2))
        self.assertEqual(len(pins), 1)
        self.assertIn("build: 2", pins[0].detail)
        self.assertIn("reference: 0", pins[0].detail)

    def test_a_closed_difference_is_fixed_when_build_drops_to_reference(self):
        before = gap_mod.differing(gap_mod.gap(still(0), still(2)))
        now = gap_mod.differing(gap_mod.gap(still(0), still(0)))
        self.assertEqual(gap_mod.fixed(before, now, still(0), still(0)), {"reduced_motion"})

    def test_build_at_or_below_reference_counts_as_fixed_not_pinned(self):
        before = gap_mod.differing(gap_mod.gap(still(0), still(2)))
        now = gap_mod.differing(gap_mod.gap(still(3), still(1)))
        self.assertEqual(gap_mod.fixed(before, now, still(3), still(1)), {"reduced_motion"})


def heading(size, line):
    anchor = {"font_size": f"{size}px", "letter_spacing": "0px"}
    if line is not None:
        anchor["line_height"] = f"{line}px"
    return {"anchors": {"heading": anchor}}


class FixedNeedsEvidenceTests(unittest.TestCase):
    def test_a_heading_whose_line_height_goes_missing_is_not_fixed(self):
        ref = heading(56, 58)
        before = gap_mod.differing(gap_mod.gap(ref, heading(36, 44)))
        now = gap_mod.differing(gap_mod.gap(ref, heading(56, None)))
        self.assertEqual(gap_mod.fixed(before, now, ref, heading(56, None)), set())

    def test_a_heading_with_every_metric_present_and_matching_is_fixed(self):
        ref = heading(56, 58)
        before = gap_mod.differing(gap_mod.gap(ref, heading(36, 44)))
        now = gap_mod.differing(gap_mod.gap(ref, heading(56, 58)))
        self.assertEqual(gap_mod.fixed(before, now, ref, heading(56, 58)), {"heading_type"})


if __name__ == "__main__":
    unittest.main()
