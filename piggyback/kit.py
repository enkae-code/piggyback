"""`piggyback kit`: one folder from one reference URL, for an agent to build from.

Stages run in order: capture (screenshot, scroll frames, scroll samples, fingerprint), then
BRIEF.md, DESIGN.md and BUILD-PROMPT.md, then kit.json last. kit.json present = kit complete.
"""

import hashlib
import os
from pathlib import Path

from . import __version__
from . import brief as brief_mod
from . import fingerprint as fingerprints
from .measure import check_url, publish_json, refuse_links, write_measure
from .server import write_new

SCHEMA = 1
MAX_REQUEST = 500
FRAMES = tuple(f"scroll/frame-{i}.png" for i in range(6))
ARTIFACTS = (
    "screenshot.png",
    "fingerprint.json",
    "scroll-samples.json",
    *FRAMES,
    "BRIEF.md",
    "DESIGN.md",
    "BUILD-PROMPT.md",
)
PROMPT_START = (
    "Build one complete, responsive single-page website in this folder as index.html "
    "(plus any local CSS/JS files): "
)
PROMPT_END = (
    ". The kit folder holds images of a reference site: screenshot.png (first screen, "
    "1440x900) and scroll/frame-0.png to scroll/frame-5.png (the page at six scroll positions). "
    "Use the images for layout. Use BRIEF.md (measured type, colour, spacing and motion of the "
    "reference) for exact values. If MOTION.md is present, use it for the reference's measured "
    "scroll and motion; rows labelled inferred or not measured are not values. "
    "Match the reference's design direction and its scroll and motion feel for this new content. "
    "Write your own text, code and placeholder visuals; do not copy anything from the reference. "
    "Use accessible semantic HTML and respect prefers-reduced-motion. "
    "Keep every script, font and image inside this folder (the page is viewed offline). "
    "Do not ask questions. Stop when the page is runnable."
)


def check_request(request):
    """The user's request, stripped, or ValueError. It is stored and printed as data only."""
    text = request.strip()
    if not 1 <= len(text) <= MAX_REQUEST:
        raise ValueError(f"--request must be 1 to {MAX_REQUEST} characters")
    return text


def build_prompt(request):
    return PROMPT_START + request + PROMPT_END


def _fresh_folder(out):
    refuse_links(out)
    if os.path.lexists(out) and (not os.path.isdir(out) or os.listdir(out)):
        if not os.path.isdir(out):
            state = "is not a folder"
        elif os.path.isfile(os.path.join(out, "kit.json")):
            state = "holds a complete kit"
        else:
            state = "holds an incomplete kit (no kit.json): remove it before re-running"
        raise ValueError(f"{out} must be a new or empty folder; it {state}")


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make(url, out, request, *, allow_loopback=False):
    """Write one kit into out (new or empty). Returns kit.json's path; raises on a failed stage."""
    text = check_request(request)
    url = check_url(url, allow_loopback)
    _fresh_folder(out)
    out_dir = Path(out)
    # Before any browser runs. An empty folder holds no link, so this is the cheap guard.
    refuse_links(*(out_dir / name for name in (*ARTIFACTS, "kit.json")))
    out_dir.mkdir(parents=True, exist_ok=True)
    write_measure(url, out_dir, allow_loopback=allow_loopback, scroll=True)
    fp = fingerprints.load(out_dir / "fingerprint.json")
    write_new(out_dir / "BRIEF.md", brief_mod.brief([fp]))
    write_new(out_dir / "DESIGN.md", brief_mod.design_md(fp))
    write_new(out_dir / "BUILD-PROMPT.md", build_prompt(text))
    scroll_coverage = fingerprints.load(out_dir / "scroll-samples.json")["coverage"]["scroll"]
    kit = {
        "schema": SCHEMA,
        "url": fp["url"],
        "request": text,
        "piggyback": __version__,
        "browser": fp["capture"],
        "stages": {
            "measure": "done",
            "scroll": scroll_coverage,
            "brief": "done",
            "design": "done",
            "prompt": "done",
            "motion": "not run",
        },
        "artifacts": {name: _digest(out_dir / name) for name in ARTIFACTS},
    }
    kit_path = out_dir / "kit.json"
    publish_json(kit_path, kit)  # last: its presence marks the kit complete
    return kit_path
