"""Lock the expected anchor targets of each side from its saved DOM snapshot, before any scoring.

Reads only evidence.json (DOMSnapshot, computed styles, rects, text). Never reads a Piggyback
fingerprint, so the targets cannot follow Piggyback's choices. Rules follow the C1 spec ("Scoring")
and measure.js: a candidate is rendered when display is not none, visibility is not hidden,
opacity is not 0, it has nonzero geometry, and it intersects the viewport.

Usage: python -m bench.c1.targets --store STORE --run RUN_ID
"""

import argparse
import json
import math
import re
from datetime import datetime, timezone

from bench.c1.capture import SIDES, load_pairs, plan_retained
from bench.c1.manifest import check_registered, load_manifest, run_folder, sha256_file
from bench.c1.snapshot import Snapshot, js_length, px
from piggyback.measure import publish_json

SLOTS = ("heading", "body", "cta", "nav", "card")
# Every computed property piggyback.gap can report for an anchor (font size, line height, tracking).
TYPE_KEYS = ("font-size", "line-height", "letter-spacing")
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
CONTROL_TAGS = {"button", "input"}
RGBA = re.compile(r"rgba?\(([^)]*)\)")


def js_round(value):
    """JavaScript Math.round: halves round up."""
    return math.floor(value + 0.5)


def _hidden(style):
    return (
        style.get("display") == "none"
        or style.get("visibility") == "hidden"
        or style.get("opacity") == "0"
    )


def rendered(el):
    """Rendered for the captured checks: display, visibility, opacity and nonzero geometry."""
    if el.style is None or el.box is None or _hidden(el.style):
        return False
    return el.box[2] > 0 and el.box[3] > 0


def visible(el, vw, vh):
    """Rendered and intersecting the viewport. Cards use rendered() alone: they keep below-fold."""
    if not rendered(el):
        return False
    x, y, w, h = el.box
    return x < vw and x + w > 0 and y < vh and y + h > 0


def clipped_area(el, vw, vh):
    x, y, w, h = el.box
    width = min(x + w, vw) - max(x, 0)
    height = min(y + h, vh) - max(y, 0)
    return width * height if width > 0 and height > 0 else 0.0


def has_background(el):
    """A nontransparent computed background: rgba alpha above 0 (or an rgb() with no alpha)."""
    value = (el.style or {}).get("background-color", "")
    found = RGBA.fullmatch(value.strip()) if value else None
    if not found:
        return False
    parts = [p for p in re.split(r"[\s,/]+", found.group(1)) if p]
    return len(parts) == 3 or float(parts[3].rstrip("%")) > 0


def _descendant_boxes(snap, el):
    for child in snap.light_children(el.idx):
        if child in snap.els:
            yield snap.els[child].box
            yield from _descendant_boxes(snap, snap.els[child])


def ink_box(snap, el):
    """measure.js ink rect: Range.getBoundingClientRect over the element's contents, clipped to it.

    The range covers the text boxes under el (already attached to every ancestor) and the border
    boxes of every descendant element, so a block child widens the ink. Falls back to the element
    box when nothing is left.
    """
    x, y, w, h = el.box
    boxes = el.boxes + [b for b in _descendant_boxes(snap, el) if b]
    if not boxes:
        return el.box
    left = min(b[0] for b in boxes)
    top = min(b[1] for b in boxes)
    right = max(b[0] + b[2] for b in boxes)
    bottom = max(b[1] + b[3] for b in boxes)
    left, top = max(left, x), max(top, y)
    right, bottom = min(right, x + w), min(bottom, y + h)
    return (left, top, right - left, bottom - top) if right > left and bottom > top else el.box


def is_control(el):
    return el.tag in CONTROL_TAGS or el.attrs.get("role") == "button"


def in_svg(snap, el):
    """An SVG element, as measure.js `instanceof SVGElement`: HTML inside <foreignObject> is not,
    the <foreignObject> itself is."""
    node = el
    while node is not None:
        if not node.shadow:
            if node.tag == "svg":
                return True
            if node.tag == "foreignobject" and node is not el:
                return False
        node = snap.els.get(node.parent)
    return False


def pick_first_best(candidates, key):
    """Highest key, earliest in DOM order on a tie. Input is in DOM order."""
    best = None
    for el in candidates:
        if best is None or key(el) > key(best):
            best = el
    return best


def _target(snap, el, box):
    """Locked target for one element: its box, text and the type values of every slot."""
    return {
        "tag": el.tag,
        "dom_index": el.idx,
        "text": snap.inner_text(el.idx)[:80],
        "box": [js_round(v) for v in box],
        "style": {k: el.style.get(k) for k in TYPE_KEYS},
    }


def _heading(snap, els, vw, vh):
    pool = [el for el in els if el.tag in HEADINGS and snap.inner_text(el.idx) != ""]
    heading = pick_first_best(
        pool, lambda el: (px(el.style["font-size"]) or 0, clipped_area(el, vw, vh))
    )
    return _target(snap, heading, ink_box(snap, heading)) if heading else None


BODY_FALLBACK_TAGS = {"div", "span", "li", "blockquote"}
BODY_OUTSIDE_TAGS = {"nav", "footer"} | HEADINGS
BODY_OUTSIDE_ROLES = {"navigation", "contentinfo", "dialog", "alertdialog"}


def in_first_screen(box, vw, vh):
    """Fully inside the first screen: the only place a pin shows on both sides."""
    x, y, w, h = box
    return x >= 0 and y >= 0 and x + w <= vw and y + h <= vh


def _outside_body(snap, el):
    """Inside nav, footer, a heading, dialog or cookie banner (measure.js BODY_OUTSIDE).

    The cookie match is ASCII-case-insensitive, as CSS [attr*=cookie i] is.
    """
    while el is not None:
        names = f"{el.attrs.get('class', '')} {el.attrs.get('id', '')}"
        if not el.shadow and (
            el.tag in BODY_OUTSIDE_TAGS
            or el.attrs.get("role") in BODY_OUTSIDE_ROLES
            or re.search("cookie", names, re.IGNORECASE | re.ASCII)
        ):
            return True
        el = snap.els.get(el.parent)
    return False


def _body_text(snap, el):
    return snap.paragraph_inner_text(el.idx) if el.tag == "p" else snap.own_text(el.idx)


def _body_ok(snap, el, vw, vh):
    return (
        in_first_screen(el.box, vw, vh)
        and js_length(_body_text(snap, el)) >= 40
        and not _outside_body(snap, el)
    )


def _body(snap, els, vw, vh):
    """First <p> in DOM order that qualifies; else the first fallback block whose own text does."""
    p = next((el for el in els if el.tag == "p" and _body_ok(snap, el, vw, vh)), None)
    body = p or next(
        (el for el in els if el.tag in BODY_FALLBACK_TAGS and _body_ok(snap, el, vw, vh)), None
    )
    return _target(snap, body, ink_box(snap, body)) if body else None


def _nav(snap, els):
    nav = next((el for el in els if el.tag == "nav" or el.attrs.get("role") == "navigation"), None)
    return _target(snap, nav, nav.box) if nav else None


def lock_targets(evidence):
    """Expected target per slot for one side: a dict, or {"absent": True} when none qualifies."""
    snap = Snapshot(evidence)
    vw, vh = snap.viewport
    els = [el for el in snap.elements() if visible(el, vw, vh)]
    targets = {
        "heading": _heading(snap, els, vw, vh),
        "body": _body(snap, els, vw, vh),
        "cta": _cta(snap, els, vw, vh),
        "nav": _nav(snap, els),
        "card": _card(snap, [el for el in snap.elements() if rendered(el)], vw, vh),
    }
    return {slot: targets[slot] or {"absent": True} for slot in SLOTS}


def _is_cta_candidate(el):
    if el.tag == "button":
        return True
    if el.tag == "input":
        return el.attrs.get("type") in ("button", "submit")
    if el.tag != "a":
        return False
    classes = el.attrs.get("class", "").lower()
    return el.attrs.get("role") == "button" or "btn" in classes or "button" in classes


def _cta(snap, els, vw, vh):
    cands = [el for el in els if _is_cta_candidate(el)]
    painted = [el for el in cands if has_background(el)]
    pool = painted or cands
    best = pick_first_best(pool, lambda el: clipped_area(el, vw, vh))
    return _target(snap, best, best.box) if best else None


def _qualifies_as_card(snap, el, vw):
    x, y, w, h = el.box
    has_content = snap.inner_text(el.idx) != "" or any(
        c in snap.els for c in snap.light_children(el.idx)
    )
    return (
        w >= 120
        and h >= 80
        and x >= 0
        and x + w <= vw
        and has_content
        and not is_control(el)
        and not in_svg(snap, el)
    )


def _sibling_groups(snap, els):
    """Rendered elements sharing a light parent, tag and class, in DOM order per group.

    Walked as measure.js builds them: parents in first-seen order, then signatures within each
    parent in first-seen order, so a tie goes to the same group.
    """
    by_parent = {}
    for el in els:
        cls = el.attrs.get("class")
        parent = snap.light_parent(el.idx)
        if not cls or parent < 0:  # an empty class is no class, as in measure.js
            continue
        by_parent.setdefault(parent, {}).setdefault((el.tag, cls), []).append(el)
    return [group for sigs in by_parent.values() for group in sigs.values()]


def _card_group(snap, els, vw, vh):
    """Qualifying members of the group of three or more whose first member fits the first screen,
    then the largest; the first group wins a tie. Mirrors measure.js.
    """
    groups = []
    for members in _sibling_groups(snap, els):
        qualifying = [el for el in members if _qualifies_as_card(snap, el, vw)]
        if len(qualifying) >= 3:
            groups.append(qualifying)
    return max(
        groups,
        key=lambda g: (in_first_screen(g[0].box, vw, vh), len(g)),
        default=None,
    )


def _card(snap, els, vw, vh):
    best = _card_group(snap, els, vw, vh)
    if best is None:
        return None
    first = best[0]
    target = _target(snap, first, first.box)
    target["count"] = len(best)
    return target


def lock(store, run_id):
    """Write targets.json and its lock record once per run. Refuses to overwrite a lock.

    Locks the retained set only (kept primaries and chosen reserves). Refuses before any write,
    so an early call cannot burn the one lock.
    """
    run = run_folder(store, run_id)
    manifest = load_manifest(run)
    lock_path = run / "targets.lock.json"
    if lock_path.exists() or lock_path.is_symlink():
        raise SystemExit(f"targets already locked: {lock_path}")
    plan = plan_retained(run, load_pairs())
    check_registered(manifest)
    pairs = {}
    for chain in plan:
        pid = chain[-1]
        if pid is None:
            continue  # no reserve left: unresolved, nothing to lock
        pair = json.loads((run / pid / "pair.json").read_text(encoding="utf-8"))
        if pair["status"] not in ("ok", "suspected_challenge"):
            continue  # a failed pair has no snapshot to lock; it is reported unresolved
        pairs[pid] = {}
        for side in SIDES:
            attempt = pair["sides"][side]["attempt"]
            evidence = run / pid / side / f"attempt-{attempt}" / "evidence.json"
            data = json.loads(evidence.read_text(encoding="utf-8"))
            pairs[pid][side] = lock_targets(data)
    targets_path = run / "targets.json"
    publish_json(targets_path, pairs)  # replaces a link at the path, never writes through it
    targets_path.chmod(0o600)
    record = {
        "locked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "targets_sha256": sha256_file(targets_path),
        "pairs": sorted(pairs),
        "source": "evidence.json snapshots only; no fingerprint read",
    }
    publish_json(lock_path, record)
    lock_path.chmod(0o600)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--store", required=True)
    parser.add_argument("--run", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(lock(args.store, args.run), indent=2))


if __name__ == "__main__":
    main()
