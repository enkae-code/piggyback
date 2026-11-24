"""MOTION.md: the reference's measured scroll motion as one fixed table.

Built only from scroll-samples.json. A value is either read from the page's own samples
(observed/runtime) or not measured. Nothing is inferred or guessed, and no inferred value
drives the build.
"""

from . import fingerprint as fp

MAX_ROWS = 40
ANIMATION_ROWS = MAX_ROWS // 2  # animations never take more than half the table
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


def _cell(value, unit=""):
    """One table cell: a value and its label, escaped. None is not measured."""
    if value is None:
        return NOT_MEASURED
    text = _num(value) if isinstance(value, (int, float)) else fp.clean(value)
    return f"{text}{unit} ({OBSERVED})"


def _matrix(values):
    return "matrix(" + ", ".join(NOT_MEASURED if v is None else _num(v) for v in values) + ")"


def _samples(data):
    """(step, sample) in capture order: a step's dense samples, then its settled sample."""
    for step in data.get("steps", []):
        for dense in step.get("dense", []):
            yield step["step"], dense
        yield step["step"], step


def _trigger(steps):
    """Where the page showed the motion. It names no cause: nothing observed says one."""
    return f"seen at steps {min(steps)}-{max(steps)} ({OBSERVED})"


def _scroll_range(scrolls):
    seen = [y for y in scrolls if isinstance(y, (int, float))]
    if not seen:
        return NOT_MEASURED
    return f"{_num(min(seen))}-{_num(max(seen))} px ({OBSERVED})"


def _receipt(steps, count):
    return f"steps {min(steps)}-{max(steps)}, {count} samples"


def _animation_rows(samples):
    seen = {}  # timing -> the steps and scroll positions where the page showed it
    for step, sample in samples:
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


def _elements(samples):
    """(element count, index, tag, property) -> [(step, scroll, value)] in capture order.

    An element is matched across samples only by its index and tag, and only between samples
    with the same element count. An index that repeats within one sample is ambiguous and skipped.
    """
    series = {}
    for step, sample in samples:
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
    animations = _animation_rows(samples)
    shown = animations[:ANIMATION_ROWS]
    rows = shown + _element_rows(samples)[: MAX_ROWS - len(shown)]
    hidden = len(animations) + len(_element_rows(samples)) - len(rows)
    coverage = (data.get("coverage") or {}).get("scroll", "none")
    viewport = data.get("viewport") or {}
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
    if all(sample.get("animations") is None for _, sample in samples):
        lines += ["Animations: not measured in any sample.", ""]
    lines.append("| " + " | ".join(HEADER) + " |")
    lines.append("|" + "---|" * len(HEADER))
    lines += [f"| M{n} | " + " | ".join(row) + " |" for n, row in enumerate(rows, 1)]
    if hidden:
        lines += ["", f"{hidden} more rows not shown."]
    if not rows:
        lines += ["", "No motion observed in the samples."]
    return "\n".join(lines) + "\n"
