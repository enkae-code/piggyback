"""MOTION.md: the reference's measured scroll motion as one fixed table.

Built only from scroll-samples.json. A value is either read from the page's own samples
(observed/runtime) or not measured. Nothing is inferred or guessed, and no inferred value
drives the build.
"""

from . import fingerprint as fp

MAX_ROWS = 40
NOT_MEASURED = "not measured"
OBSERVED = "observed/runtime"
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


def _cell(value, unit=""):
    """One table cell: a value and its label, escaped. None is not measured."""
    if value is None:
        return NOT_MEASURED
    text = fp.fmt(value) if isinstance(value, (int, float)) else fp.clean(value)
    return f"{text}{unit} ({OBSERVED})"


def _matrix(values):
    return "matrix(" + ", ".join(NOT_MEASURED if v is None else fp.fmt(v) for v in values) + ")"


def _samples(data):
    """(step index, sample) for every step and every dense sample under it, in order."""
    for step in data.get("steps", []):
        yield step["step"], step
        for dense in step.get("dense", []):
            yield step["step"], dense


def _trigger(steps):
    low, high = min(steps), max(steps)
    return f"scroll {low}" if low == high else f"scroll {low}-{high}"


def _scroll_range(scrolls):
    seen = [y for y in scrolls if isinstance(y, (int, float))]
    if not seen:
        return NOT_MEASURED
    return f"{fp.fmt(min(seen))}-{fp.fmt(max(seen))} px ({OBSERVED})"


def _receipt(steps, count):
    low, high = min(steps), max(steps)
    return f"steps {low}-{high}, {count} samples"


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


def _element_rows(samples):
    seen = {}  # (element index, property) -> [(step, scroll position, value, tag)]
    for step, sample in samples:
        for element in sample.get("elements", []):
            for prop in ("transform", "opacity"):
                if element.get(prop) is not None:
                    key = (element["index"], prop)
                    seen.setdefault(key, []).append(
                        (step, sample.get("scroll_y"), element[prop], element["tag"])
                    )
    rows = []
    for (_, prop), obs in seen.items():
        first, last = obs[0][2], obs[-1][2]
        if first == last:
            continue
        show = _matrix if prop == "transform" else fp.fmt
        steps = {step for step, _, _, _ in obs}
        rows.append(
            [
                _cell(obs[0][3]),
                _trigger(steps),
                _cell(prop),
                f"{_cell(show(first))} → {_cell(show(last))}",
                NOT_MEASURED,
                NOT_MEASURED,
                NOT_MEASURED,
                _scroll_range([scroll for _, scroll, _, _ in obs]),
                _receipt(steps, len(obs)),
                "element timing not measured; rows matched by element index",
            ]
        )
    return rows


def build(data):
    """MOTION.md text from one cleaned scroll-samples.json."""
    samples = list(_samples(data))
    rows = (_animation_rows(samples) + _element_rows(samples))[:MAX_ROWS]
    coverage = (data.get("coverage") or {}).get("scroll", "none")
    viewport = data.get("viewport") or {}
    lines = [
        "# Motion",
        "",
        f"Coverage: {fp.clean(coverage)}. Viewport {fp.fmt(viewport.get('width', 0))}x"
        f"{fp.fmt(viewport.get('height', 0))}. Samples: {len(samples)}.",
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
    if not rows:
        lines += ["", "No motion observed in the samples."]
    return "\n".join(lines) + "\n"
