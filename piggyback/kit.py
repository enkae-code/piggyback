"""`piggyback kit`: one folder from one reference URL, for an agent to build from.

The whole kit is built in a private staging folder beside --out, then renamed into place as one
step. --out holds a complete kit or nothing: a failed stage removes the staging folder.
"""

import contextlib
import hashlib
import os
import shutil
import tempfile
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
    # ponytail: PROMPT_END opens with a full stop; a request that already ends in one skips it.
    end = PROMPT_END[1:] if request.endswith(".") else PROMPT_END
    return PROMPT_START + request + end


def _fresh_folder(out):
    refuse_links(out)
    if os.path.lexists(out) and (not os.path.isdir(out) or os.listdir(out)):
        raise ValueError(f"{out} must be a new or empty folder")


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _build(url, folder, text, allow_loopback):
    """Every stage, into folder. A failed stage raises; the caller removes folder."""
    write_measure(url, folder, allow_loopback=allow_loopback, scroll=True)
    fp = fingerprints.load(folder / "fingerprint.json")
    write_new(folder / "BRIEF.md", brief_mod.brief([fp]))
    write_new(folder / "DESIGN.md", brief_mod.design_md(fp))
    write_new(folder / "BUILD-PROMPT.md", build_prompt(text))
    scroll_coverage = fingerprints.load(folder / "scroll-samples.json")["coverage"]["scroll"]
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
        "artifacts": {name: _digest(folder / name) for name in ARTIFACTS},
    }
    publish_json(folder / "kit.json", kit)  # last: its presence marks the kit complete


def make(url, out, request, *, allow_loopback=False):
    """Write one kit into out (new or empty), all at once. Returns kit.json's path."""
    text = check_request(request)
    url = check_url(url, allow_loopback)
    out_dir = Path(out)
    _fresh_folder(out_dir)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=out_dir.parent, prefix=".kit-"))  # owner-only: 0700
    try:
        _build(url, staging, text, allow_loopback)
        try:
            # rmdir removes only an empty folder, so a folder that filled up is never clobbered.
            # Windows' rename refuses any existing folder, so the empty one must go first.
            with contextlib.suppress(FileNotFoundError):
                os.rmdir(out_dir)
            os.rename(staging, out_dir)  # one step; fails if out_dir has become non-empty
        except OSError as exc:
            raise ValueError(f"{out} must be a new or empty folder ({exc.strerror})") from None
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return out_dir / "kit.json"
