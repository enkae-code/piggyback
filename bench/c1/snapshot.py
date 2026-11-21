"""Read a saved DOM snapshot (evidence file from piggyback.measure): elements, styles, boxes, text.

Pure data reading, no browser. Boxes are viewport CSS px (the document offset by the scroll position
recorded at capture). Text boxes come from the DOM snapshot's text layout, not from Piggyback.
"""

import re
from dataclasses import dataclass, field

PX = re.compile(r"(-?\d+(?:\.\d+)?)px")
# ECMAScript WhiteSpace and LineTerminator: what JS \s and trim() match (not Python's str.split).
ES_CHARS = (
    "\t\n\x0b\x0c\r \u00a0\u1680"
    + "".join(chr(c) for c in range(0x2000, 0x200B))
    + "\u2028\u2029\u202f\u205f\u3000\ufeff"
)
ES_RUN = re.compile(f"[{ES_CHARS}]+")


def px(value):
    """CSS px from a computed length, or None for 'normal' and anything else."""
    found = PX.fullmatch(value) if isinstance(value, str) else None
    return float(found.group(1)) if found else None


def js_length(s):
    """String length in UTF-16 code units, as JS .length counts it."""
    return len(s.encode("utf-16-le", "surrogatepass")) // 2


def js_collapse(s):
    """replace(/\\s+/g, " ").trim() with ECMAScript whitespace."""
    return ES_RUN.sub(" ", s).strip(" ")


def js_trim(s):
    """trim() with ECMAScript whitespace."""
    return s.strip(ES_CHARS)


@dataclass
class El:
    idx: int
    tag: str
    attrs: dict
    parent: int
    style: dict | None  # computed styles; None when the element has no layout (not rendered)
    box: tuple | None  # x, y, width, height in viewport px
    boxes: list = field(default_factory=list)  # boxes of the text under this element
    shadow: bool = False  # shadow-tree element: transparent to walks, never a candidate


class Snapshot:
    def __init__(self, evidence):
        doc = evidence["documents"][0]
        nodes = doc["nodes"]
        page = evidence["page"]
        self.viewport = (page["viewport"][0], page["viewport"][1])
        self.scroll_x, self.scroll_y = page["scroll"]
        self.strings = evidence["strings"]
        count = len(nodes["nodeType"])
        self.kind = nodes["nodeType"]
        self.parents = nodes["parentIndex"]
        self.backend = nodes.get("backendNodeId", [-1] * count)
        # pseudoType is a rare-string table {"index": [node ids], "value": [...]}: ::before/::after
        self.pseudo = set(nodes.get("pseudoType", {}).get("index", []))
        # shadowRootType: same rare-string table; lists every shadow-tree node, not a root node.
        self.shadow = set(nodes.get("shadowRootType", {}).get("index", []))
        self.paragraph_text = evidence.get("inner_text", {})  # opt-in hook: innerText by backend id
        self.children = [[] for _ in range(count)]
        for idx, parent in enumerate(self.parents):
            if parent >= 0:
                self.children[parent].append(idx)
        values = nodes.get("nodeValue", [-1] * count)
        self.text_value = {i: self._text(values[i]) for i in range(count) if self.kind[i] == 3}
        self.els = self._elements(evidence, doc, count)
        self._attach_text_boxes(doc)

    def _text(self, index):
        return self.strings[index] if 0 <= index < len(self.strings) else ""

    def _shift(self, bounds):
        x, y, w, h = bounds
        return (x - self.scroll_x, y - self.scroll_y, w, h)

    def _elements(self, evidence, doc, count):
        nodes, layout = doc["nodes"], doc["layout"]
        layout_of = {node: li for li, node in enumerate(layout["nodeIndex"])}
        styles = evidence["styles"]
        flat_attrs = nodes.get("attributes", [[]] * count)
        els = {}
        for idx in range(count):
            if self.kind[idx] != 1 or idx in self.pseudo:
                continue
            flat = flat_attrs[idx]
            attrs = {
                self._text(flat[k]): self._text(flat[k + 1]) for k in range(0, len(flat) - 1, 2)
            }
            li = layout_of.get(idx)
            style = box = None
            if li is not None:
                style = dict(
                    zip(styles, (self._text(v) for v in layout["styles"][li]), strict=True)
                )
                box = self._shift(layout["bounds"][li])
            tag = self._text(nodes["nodeName"][idx]).lower()
            shadow = idx in self.shadow
            els[idx] = El(idx, tag, attrs, self.parents[idx], style, box, shadow=shadow)
        return els

    def _attach_text_boxes(self, doc):
        layout = doc["layout"]
        boxes = doc.get("textBoxes", {"layoutIndex": [], "bounds": []})
        for k, li in enumerate(boxes["layoutIndex"]):
            node = layout["nodeIndex"][li]
            if node in self.shadow:  # innerText/Range over the host leave shadow text out
                continue
            parent = self.parents[node]
            while parent >= 0:
                if parent in self.els:
                    self.els[parent].boxes.append(self._shift(boxes["bounds"][k]))
                parent = self.parents[parent]

    def elements(self):
        return [self.els[idx] for idx in sorted(self.els) if not self.els[idx].shadow]

    def light_parent(self, idx):
        """Nearest non-shadow ancestor: the light-DOM parent, -1 at root."""
        parent = self.parents[idx]
        while parent >= 0 and parent in self.shadow:
            parent = self.parents[parent]
        return parent

    def light_children(self, idx):
        """Children of idx with shadow nodes transparent: a shadow node's children take its place.

        Slotted light nodes sit under the shadow <slot> in the flat tree; shadow text is dropped.
        """
        for child in self.children[idx]:
            if child in self.shadow:
                yield from self.light_children(child)
            else:
                yield child

    def paragraph_inner_text(self, idx):
        """innerText of a <p> as the browser reported it (opt-in hook), else the approximation."""
        recorded = self.paragraph_text.get(str(self.backend[idx]))
        return js_trim(recorded) if recorded is not None else self.inner_text(idx)

    def own_text(self, idx):
        """Own text nodes of idx, child elements left out, whitespace collapsed (as measure.js)."""
        return js_collapse(
            "".join(self.text_value[c] for c in self.light_children(idx) if self.kind[c] == 3)
        )

    def inner_text(self, idx):
        """Approximates innerText: rendered text under idx, whitespace collapsed and trimmed."""
        parts = []
        for child in self.light_children(idx):
            if self.kind[child] == 3:
                parts.append(self.text_value[child])
            elif child in self.els and self._rendered(child):
                parts.append(f" {self.inner_text(child)} ")
        return js_collapse("".join(parts))

    def _rendered(self, idx):
        el = self.els[idx]
        return el.style is not None and el.style.get("visibility") != "hidden"
