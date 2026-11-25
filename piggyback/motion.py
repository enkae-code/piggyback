"""MOTION.md: the reference's measured scroll motion as one fixed table.

Built only from scroll-samples.json. A value is either read from the page's own samples
(observed/runtime) or not measured. Nothing is inferred or guessed, and no inferred value
drives the build. Rows: pinned fixed or sticky elements first, then CSS animations, then element
transform and opacity changes.
"""

from . import fingerprint as fp
from .measure import PINNED

MAX_ROWS = 40
PIN_ROWS = MAX_ROWS // 4  # pins get their own cap, so animations cannot crowd them out
ANIMATION_ROWS = MAX_ROWS // 2  # animations never take more than half the table
HELD_PX = 1  # a pin's top that moves this little between two samples is held
BAR_SHARE = 0.5  # a full-width bar is shorter than this share of the viewport height
PLACE_MAX = 120  # a pin's place text holds bounded numbers only; it is never cut short
FULL_WIDTH = 0.9  # a bar at least this share of the viewport wide, at the top, is full-width
EDGE_PX = 4  # a bar within this many px of the viewport's top or bottom is at that edge
NOT_MEASURED = "not measured"
OBSERVED = "observed/runtime"
IDENTITY = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]  # the computed transform of "none"
HEADER = (
    "ID",
    "Role",
    "Trigger",
    "Property",
    "Endpoints",
    "Duration",
    "Delay",
    "Easing",
    "Scroll range",
    "Receipt",
    "Limits",
)


def _num(value):
    """Two decimals: opacity 0.25 must not read as 0.2."""
    return f"{round(value, 2):g}"


def _cell(value, unit="", limit=fp.MAX_TEXT):
    """One table cell: a value and its label, escaped. None is not measured."""
    if value is None:
        return NOT_MEASURED
    text = _num(value) if isinstance(value, (int, float)) else fp.clean(value, limit)
    return f"{text}{unit} ({OBSERVED})"


def _matrix(values):
    return "matrix(" + ", ".join(NOT_MEASURED if v is None else _num(v) for v in values) + ")"


def _samples(data):
    """(step, sample, settled) in capture order: a step's dense samples, then its settled sample."""
    for step in data.get("steps", []):
        for dense in step.get("dense", []):
            yield step["step"], dense, False
        yield step["step"], step, True


def _trigger(steps):
    """Where the page showed the motion. It names no cause: nothing observed says one."""
    return f"seen at steps {min(steps)}-{max(steps)} ({OBSERVED})"


def _scroll_range(scrolls):
    seen = [y for y in scrolls if isinstance(y, (int, float))]
    if not seen:
        return NOT_MEASURED
    low, high = min(seen), max(seen)
    span = f"at {_num(low)} px" if low == high else f"{_num(low)}-{_num(high)} px"
    return f"{span} ({OBSERVED})"


def _receipt(steps, count):
    return f"steps {min(steps)}-{max(steps)}, {count} samples"


def _animation_rows(samples):
    seen = {}  # timing -> the steps and scroll positions where the page showed it
    for step, sample, _ in samples:
        for anim in sample.get("animations") or []:
            key = (anim["kind"], anim["duration"], anim["delay"], anim["easing"])
            steps, scrolls = seen.setdefault(key, (set(), []))
            steps.add(step)
            scrolls.append(sample.get("scroll_y"))
    rows = []
    for (kind, duration, delay, easing), (steps, scrolls) in seen.items():
        rows.append(
            [
                NOT_MEASURED,  # an animation is not linked to an element
                _trigger(steps),
                _cell(kind),
                NOT_MEASURED,
                _cell(duration, " ms"),
                _cell(delay, " ms"),
                _cell(easing),
                _scroll_range(scrolls),
                _receipt(steps, len(scrolls)),
                "animation not linked to an element",
            ]
        )
    return rows


def _pin_key(element):
    """What one pin is across samples: its tag, position, size and x, and a sticky one's CSS top.

    A fixed element's top is not in the key, so a header that moves a little stays one pin.
    # ponytail: geometry is the match key, not node identity; a same-size twin can join a run.
    # ponytail: a header that changes size is one row per size.
    None if a number is missing: such an element is not matched.
    """
    x, _, width, height = element["box"]
    if None in (x, width, height):
        return None
    return (
        element["tag"],
        element["position"],
        round(width),
        round(height),
        round(x),
        element["top"],
    )


def _pinned_by_key(sample):
    """key -> element for one sample's fixed and sticky elements.

    A key found twice is ambiguous and dropped. An opacity-0 element is not on screen, so not a pin.
    """
    found = {}
    for element in sample.get("elements") or []:
        if element["position"] in PINNED and element["opacity"] != 0:
            found.setdefault(_pin_key(element), []).append(element)
    return {key: group[0] for key, group in found.items() if key is not None and len(group) == 1}


def _pin_runs(samples):
    """Each pin's run: its samples in capture order. A sample without the key ends the run."""
    runs, open_runs = [], {}
    for step, sample, settled in samples:
        found = _pinned_by_key(sample)
        for key in [key for key in open_runs if key not in found]:
            runs.append(open_runs.pop(key))
        for key, element in found.items():
            open_runs.setdefault(key, []).append((step, sample.get("scroll_y"), element, settled))
    return runs + list(open_runs.values())


def _pairs(run):
    """(held, moving): adjacent samples of a run where the scroll moved: (before, after, element).

    A pair is held when the top moved HELD_PX or less. Missing numbers count for neither.
    """
    held, moving = [], []
    for (_, before, prev, _), (_, scroll, element, _) in zip(run, run[1:], strict=False):
        top, prev_top = element["box"][1], prev["box"][1]
        if None in (scroll, before, top, prev_top) or scroll == before:
            continue
        (held if abs(top - prev_top) <= HELD_PX else moving).append((before, scroll, element))
    return held, moving


def _stuck(element):
    """A sticky element is stuck when its box top sits at its computed top (CSS top)."""
    top, y = element["top"], element["box"][1]
    return top is not None and y is not None and abs(y - top) <= HELD_PX


def _role(element, viewport):
    """A phrase from geometry only: a full-width bar at the top or bottom, else a box."""
    _, top, width, height = element["box"]
    vw, vh = viewport.get("width") or 0, viewport.get("height") or 0
    if vw > 0 and vh > 0 and None not in (top, width, height):
        if width >= FULL_WIDTH * vw and height < BAR_SHARE * vh:
            if top <= EDGE_PX:
                return f"full-width bar at the top, {element['tag']}"
            if abs(top + height - vh) <= EDGE_PX:
                return f"full-width bar at the bottom, {element['tag']}"
    return f"box, {element['tag']}"


def _px(value):
    return NOT_MEASURED if value is None else _num(value)


def _stuck_view(run, floor):
    """(scrolls, element, single) for a sticky pin, or None.

    Stuck at two or more scrolls is "stuck". One stuck scroll above the smallest scroll seen is
    single: its sticking is not measured. Stuck only at the smallest scroll is no row.
    """
    shown = [(s, el) for _, s, el, settled in run if settled and _stuck(el) and s is not None]
    scrolls = sorted({s for s, _ in shown})
    if len(scrolls) >= 2:
        return scrolls, shown[0][1], False
    if scrolls and scrolls[0] > floor:
        return scrolls, shown[0][1], True
    return None


def _held_view(held):
    """(scrolls, element, single) for a fixed pin with a held pair, or None."""
    if not held:
        return None
    scrolls = sorted({s for before, after, _ in held for s in (before, after)})
    return scrolls, held[0][2], False


def _pin_row(run, viewport, floor):
    sticky = run[0][2]["position"] == "sticky"
    held, moving = _pairs(run)
    view = _stuck_view(run, floor) if sticky else _held_view(held)
    if view is None:
        return None
    scrolls, element, single = view
    x, top, width, height = element["box"]
    if not sticky:
        place = f"held at top {_px(top)} px"
    elif single:
        place = f"at its CSS top {_px(element['top'])} px at scroll {_num(scrolls[0])} px"
    else:
        place = f"stuck at top {_px(element['top'])} px"
    place += f", {_px(width)} x {_px(height)} px, at left {_px(x)} px"
    limits = "pin start/release between samples; element matched by tag, position, size and x"
    if single:
        limits += "; sticking across scroll not measured"
    if moving:
        limits += f"; moving seen {_scroll_range([s for b, a, _ in moving for s in (b, a)])}"
    return [
        _cell(_role(element, viewport)),
        _cell(f"scroll (position: {element['position']})"),
        _cell("position"),
        _cell(place, limit=PLACE_MAX),
        NOT_MEASURED,
        NOT_MEASURED,
        NOT_MEASURED,
        _scroll_range(scrolls),
        _receipt([step for step, _, _, _ in run], len(run)),
        limits,
    ]


def _pin_rows(samples, viewport):
    scrolls = [sample.get("scroll_y") for _, sample, _ in samples]
    floor = min((s for s in scrolls if isinstance(s, (int, float))), default=None)
    rows = (_pin_row(run, viewport, floor) for run in _pin_runs(samples))
    return [row for row in rows if row]


def _elements(samples):
    """(element count, index, tag, property) -> [(step, scroll, value)] in capture order.

    An element is matched across samples only by its index and tag, and only between samples
    with the same element count. An index that repeats within one sample is ambiguous and skipped.
    """
    series = {}
    for step, sample, _ in samples:
        by_index = {}
        for element in sample.get("elements") or []:
            by_index.setdefault(element["index"], []).append(element)
        for index, found in by_index.items():
            if len(found) != 1:
                continue
            element = found[0]
            values = {
                "transform": element["transform"] or IDENTITY,
                "opacity": element["opacity"],
            }
            for prop, value in values.items():
                if value is not None:
                    key = (sample.get("count"), index, element["tag"], prop)
                    series.setdefault(key, []).append((step, sample.get("scroll_y"), value))
    return series


def _spread(prop, values):
    """What the endpoints leave out: the range of an opacity, or how many transforms there were."""
    if prop == "opacity":
        low, high = min(values), max(values)
        return f"; min {_num(low)}, max {_num(high)}" if low != high else ""
    distinct = len({tuple(v) for v in values})
    return f"; {distinct} distinct transforms" if distinct > 2 else ""


def _element_rows(samples):
    rows = []
    for (_, _, tag, prop), obs in _elements(samples).items():
        values = [value for _, _, value in obs]
        if len(values) < 2 or all(value == values[0] for value in values):
            continue
        show = _matrix if prop == "transform" else _num
        steps = {step for step, _, _ in obs}
        endpoints = f"{_cell(show(values[0]))} → {_cell(show(values[-1]))}"
        rows.append(
            [
                _cell(tag),
                _trigger(steps),
                _cell(prop),
                endpoints + _spread(prop, values),
                NOT_MEASURED,
                NOT_MEASURED,
                NOT_MEASURED,
                _scroll_range([scroll for _, scroll, _ in obs]),
                _receipt(steps, len(obs)),
                "element timing not measured; matched by index and tag",
            ]
        )
    return rows


def build(data):
    """MOTION.md text from one cleaned scroll-samples.json."""
    samples = list(_samples(data))
    coverage = (data.get("coverage") or {}).get("scroll", "none")
    viewport = data.get("viewport") or {}
    pins = _pin_rows(samples, viewport)
    animations = _animation_rows(samples)
    elements = _element_rows(samples)
    rows = pins[:PIN_ROWS] + animations[:ANIMATION_ROWS]
    rows += elements[: MAX_ROWS - len(rows)]
    hidden = len(pins) + len(animations) + len(elements) - len(rows)
    lines = [
        "# Motion",
        "",
        f"Coverage: {fp.clean(coverage)}. Viewport {_num(viewport.get('width', 0))}x"
        f"{_num(viewport.get('height', 0))}. Samples: {len(samples)}.",
        "",
        f"Labels: {OBSERVED} (read from the page while it scrolled) or {NOT_MEASURED}. "
        "Nothing is guessed.",
        "",
    ]
    if all(sample.get("animations") is None for _, sample, _ in samples):
        lines += ["Animations: not measured in any sample.", ""]
    lines.append("| " + " | ".join(HEADER) + " |")
    lines.append("|" + "---|" * len(HEADER))
    lines += [f"| M{n} | " + " | ".join(row) + " |" for n, row in enumerate(rows, 1)]
    if hidden:
        lines += ["", f"{hidden} more rows not shown."]
    if not rows:
        lines += ["", "No motion observed in the samples."]
    return "\n".join(lines) + "\n"
