"""Gap between a reference and a build: numbered pins on anchors both pages have.

Page-level comparison only. Pins never claim that one element matches another; each anchor is
compared with itself, and a missing anchor is reported as not found, never guessed.
"""

from dataclasses import dataclass

from . import fingerprint as fp
from .glossary import note

# anchor -> (label, glossary concept): each anchor explains its own type, never another's.
ANCHORS = {
    "heading": ("Heading", "heading_type"),
    "body": ("Body text", "body_type"),
    "cta": ("Primary button", "button_type"),
    "nav": ("Navigation", "nav_type"),
    "card": ("Repeated card", "card_type"),
}
PX_TOLERANCE = 1.0  # the plan's pin gate: a value within 1px is right
SHARE_TOLERANCE = 0.02  # ... and a share within 2% is right
TRACKING_TOLERANCE = 0.01  # em


@dataclass(frozen=True)
class Pin:
    number: int | None  # None for a "not found" entry
    concept: str | None  # None for a "not found" entry: nothing to explain, nothing to compare
    detail: str
    ref_box: dict | None
    build_box: dict | None


def _absent(label, ref_value, build_value):
    """Not-found text naming the side(s) that lack the value. Call only when one side is None."""
    sides = [
        side for side, value in (("reference", ref_value), ("build", build_value)) if value is None
    ]
    return f"{label}: not found on the {' and the '.join(sides)} — can't compare"


def _type_gap(label, location, ref_anchor, build_anchor, sizes):
    """Pin detail when an anchor's type or tracking differs by more than tolerance, else None.

    The pin reads current build -> reference target, as the plan's examples do.
    """
    rf, bf = sizes
    rl, bl = fp.px(ref_anchor.get("line_height")), fp.px(build_anchor.get("line_height"))
    rt = fp.px(ref_anchor.get("letter_spacing")) or 0.0
    bt = fp.px(build_anchor.get("letter_spacing")) or 0.0
    size_differs = abs(rf - bf) > PX_TOLERANCE
    line_differs = rl is not None and bl is not None and abs(rl - bl) > PX_TOLERANCE
    tracking_differs = abs(rt / rf - bt / bf) > TRACKING_TOLERANCE
    if not (size_differs or line_differs or tracking_differs):
        return None

    def type_text(size, line):
        return f"{fp.fmt(size)}/{'normal' if line is None else fp.fmt(line)}"

    detail = f"{label} at {location}: {type_text(bf, bl)} → {type_text(rf, rl)}"
    if tracking_differs:
        detail += f", tracking {bt / bf:.2f}em → {rt / rf:.2f}em"
    return detail


def _anchor_rows(ref, build):
    """Type gaps on anchors both pages have, and (None, text) not-found entries for the rest."""
    rows, absent = [], []
    for name, (label, concept) in ANCHORS.items():
        ref_anchor, build_anchor = fp.anchor(ref, name), fp.anchor(build, name)
        if ref_anchor is None or build_anchor is None:
            absent.append((None, _absent(label, ref_anchor, build_anchor)))
            continue
        rf, bf = fp.px(ref_anchor.get("font_size")), fp.px(build_anchor.get("font_size"))
        if rf is None or bf is None or rf <= 0 or bf <= 0:
            absent.append((None, f"{label}: font size not measured — can't compare"))
            continue
        detail = _type_gap(label, name + " anchor", ref_anchor, build_anchor, (rf, bf))
        if detail:
            boxes = (fp.box(ref_anchor.get("box")), fp.box(build_anchor.get("box")))
            rows.append((concept, detail, *boxes))
    return rows, absent


def _page_rows(ref, build):
    """Accent share and container width, compared at page level, so they carry no boxes."""
    rows, absent = [], []
    ref_share, build_share = (fp.share(fp.get(d, "palette", "accent_share")) for d in (ref, build))
    if ref_share is None or build_share is None:
        absent.append((None, _absent("Accent share", ref_share, build_share)))
    elif abs(round(ref_share - build_share, 4)) > SHARE_TOLERANCE:
        now, target = round(build_share * 100, 1), round(ref_share * 100, 1)
        rows.append(("accent", f"accent share at first screen: {now:g}% → {target:g}%", None, None))

    ref_width, build_width = (fp.number(fp.get(d, "container_width")) for d in (ref, build))
    if ref_width is None or build_width is None:
        absent.append((None, _absent("Container width", ref_width, build_width)))
    elif abs(ref_width - build_width) > PX_TOLERANCE:
        rows.append(
            (
                "container",
                f"Container width at main content column: {fp.fmt(build_width)}px "
                f"→ {fp.fmt(ref_width)}px",
                None,
                None,
            )
        )
    return rows, absent


def _bounded(value, default):
    return value if isinstance(value, str) and len(value) <= 4096 else default


def _identity(row):
    """Kind, properties and timing: what a motion row is. Its count is shown, not compared."""
    raw_props = row.get("props") if isinstance(row.get("props"), list) else []
    props = tuple(sorted(prop for prop in raw_props if isinstance(prop, str) and len(prop) <= 4096))
    return (
        _bounded(row.get("kind", "unknown"), "unknown"),
        props,
        fp.number(row.get("duration")),
        fp.number(row.get("delay")),
        _bounded(row.get("easing"), None),
    )


def _counts(rows):
    """Element count per identity; rows sharing an identity add up. None when a count is missing."""
    totals = {}
    for row in rows:
        key, count = _identity(row), fp.number(row.get("count"))
        prior = totals.get(key, 0)
        totals[key] = None if count is None or prior is None else prior + count
    return totals


def _signature_base(identity):
    """A motion signature as plain text without its count: kind, properties, timing, easing."""
    kind, props, duration, delay, easing = identity
    if duration is None or delay is None:
        timing = "timing not measured"
    else:
        timing = (
            f"{fp.fmt(duration)}ms duration, {fp.fmt(delay)}ms delay, "
            f"{fp.easing(easing) or 'easing not measured'}"
        )
    shown = fp.motion_props(list(props)) or "properties not measured"
    return f"{fp.clean(kind, 30)}, {shown}, {timing}"


def _signature_text(identity, count):
    count_text = "count not measured" if count is None else f"{fp.fmt(count)} elements"
    return f"{_signature_base(identity)}, {count_text}"


def _valid_motion(page):
    """The page's motion rows that are objects; anything else counts as no motion."""
    rows = fp.get(page, "motion")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _absent_rows(counts, other, side):
    """Signatures in `counts` that `other` lacks, as motion rows labelled with `side`."""
    rows = []
    for identity in sorted(counts.keys() - other.keys(), key=str):
        text = _signature_text(identity, counts[identity])
        rows.append(("motion_only_one_page", f"motion signature in whole document, {side}: {text}"))
    return rows


def _count_rows(build_counts, ref_counts):
    """Signatures on both pages whose element counts differ; a missing count is not compared."""
    rows = []
    for identity in sorted(build_counts.keys() & ref_counts.keys(), key=str):
        build_n, ref_n = build_counts[identity], ref_counts[identity]
        if build_n is not None and ref_n is not None and build_n != ref_n:
            rows.append(
                (
                    "motion_reach",
                    f"motion count differs in whole document: {_signature_base(identity)}, "
                    f"{fp.fmt(build_n)} → {fp.fmt(ref_n)} elements",
                )
            )
    return rows


def _still(page):
    return fp.number(fp.get(page, "reduced_motion", "still_running"))


def _still_rows(ref, build):
    """Animations still running under reduced motion; only more than the reference is a fix."""
    ref_still, build_still = _still(ref), _still(build)
    if ref_still is not None and build_still is not None and build_still > ref_still:
        return [
            (
                "reduced_motion",
                "fix the build: more animations still running with reduced motion requested — "
                f"build: {fp.fmt(build_still)}, reference: {fp.fmt(ref_still)} (whole document)",
            )
        ]
    return []


def _motion_rows(ref, build):
    """Evidence-shaped motion differences; signatures are compared by kind and properties."""
    ref_rows, build_rows = _valid_motion(ref), _valid_motion(build)
    rows = []
    if len(ref_rows) != len(build_rows):
        rows.append(
            (
                "motion_total",
                f"motion signatures in whole document: {len(build_rows)} → {len(ref_rows)}",
            )
        )
    ref_counts, build_counts = _counts(ref_rows), _counts(build_rows)
    rows += _absent_rows(build_counts, ref_counts, "present in build, absent in reference")
    rows += _absent_rows(ref_counts, build_counts, "present in reference, absent in build")
    rows += _count_rows(build_counts, ref_counts)
    return rows + _still_rows(ref, build)


def gap(ref, build):
    """Pins for the differences between a reference and a build fingerprint."""
    anchor_rows, anchor_absent = _anchor_rows(ref, build)
    page_rows, page_absent = _page_rows(ref, build)
    motion_rows = [(concept, detail, None, None) for concept, detail in _motion_rows(ref, build)]
    pins = [
        Pin(number, *row) for number, row in enumerate(anchor_rows + page_rows + motion_rows, 1)
    ]
    not_found = anchor_absent + page_absent
    return pins + [Pin(None, None, text, None, None) for _, text in not_found]


def _sized(page, name):
    anchor = fp.anchor(page, name)
    size = fp.px(anchor.get("font_size")) if anchor else None
    return size is not None and size > 0


def _has_motion(page):
    """A page with no `motion` list is not comparable; an empty list is."""
    return isinstance(fp.get(page, "motion"), list)


def _counts_known(ref, build):
    """Every signature on both pages has a numeric count on both sides."""
    ref_counts, build_counts = _counts(_valid_motion(ref)), _counts(_valid_motion(build))
    return all(
        ref_counts[key] is not None and build_counts[key] is not None
        for key in ref_counts.keys() & build_counts.keys()
    )


def _motion_compared(ref, build):
    found = set()
    if _has_motion(ref) and _has_motion(build):
        found |= {"motion_total", "motion_only_one_page"}
        if _counts_known(ref, build):
            found.add("motion_reach")
    if _still(ref) is not None and _still(build) is not None:
        found.add("reduced_motion")
    return found


def _kept(ref, build, name):
    """Every line-height and letter-spacing the reference reports is still reported by the build.
    A metric that went missing is not comparable, so a difference it hid is never counted fixed."""
    ref_anchor, build_anchor = fp.anchor(ref, name), fp.anchor(build, name)
    return all(
        fp.px(build_anchor.get(metric)) is not None
        for metric in ("line_height", "letter_spacing")
        if fp.px(ref_anchor.get(metric)) is not None
    )


def compared(ref, build):
    """Concepts both pages can be compared on, whether or not they differ."""
    found = {
        concept
        for name, (_, concept) in ANCHORS.items()
        if _sized(ref, name) and _sized(build, name) and _kept(ref, build, name)
    }
    if fp.share(fp.get(ref, "palette", "accent_share")) is not None:
        if fp.share(fp.get(build, "palette", "accent_share")) is not None:
            found.add("accent")
    if fp.number(fp.get(ref, "container_width")) is not None:
        if fp.number(fp.get(build, "container_width")) is not None:
            found.add("container")
    return found | _motion_compared(ref, build)


def differing(pins):
    """Concepts with an open difference: what the next check compares against."""
    return {pin.concept for pin in pins if pin.concept is not None}


def fixed(before, now, ref, build):
    """Concepts open at the last check that are closed now, and can still be compared.

    A pin whose element vanished is not fixed: it is not in `compared`.
    """
    return (before - now) & compared(ref, build)


def render(pins):
    """The differences as markdown, numbered to match the pins. Each concept explains once."""
    lines, explained = [], set()
    for pin in pins:
        if pin.number is None:
            lines.append(f"- {pin.detail}")
            continue
        lines.append(note(pin.concept, pin.detail, pin.number, why=pin.concept not in explained))
        explained.add(pin.concept)
    body = "\n".join(lines) or "No differences above the thresholds."
    return f"## Differences (build → reference)\n\n{body}\n"
