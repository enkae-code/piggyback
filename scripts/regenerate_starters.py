"""Measure each starter page in starters/ and package the result under piggyback/starters/<id>/.

Needs Chromium: pip install '.[measure]' and python -m playwright install chromium. Run it again
after any change to starters/<id>/index.html; tests/test_starters_packaged.py fails until you do.

Each page is served on 127.0.0.1 and measured with allow_loopback. Every loopback address in the
result is then replaced by its starter:<id> label, so the packaged data never names a local port.
"""

import functools
import hashlib
import json
import shutil
import sys
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from piggyback import starters  # noqa: E402
from piggyback.measure import write_measure  # noqa: E402

SOURCE = ROOT / "starters"
LICENSE = "CC0-1.0"
NAMES = {
    "field-journal": "Field Journal",
    "ink-and-rule": "INK / RULE",
    "northline-ops": "Northline Field Ops",
    "little-plot": "Little Plot",
    "mina-okafor": "Mina Okafor",
    "good-day-assembly": "Good Day Assembly",
}


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass


def relabel(value, base, label):
    """Replace the loopback page address `base` with `label` everywhere in the JSON value."""
    if isinstance(value, dict):
        return {key: relabel(item, base, label) for key, item in value.items()}
    if isinstance(value, list):
        return [relabel(item, base, label) for item in value]
    if isinstance(value, str):
        return value.replace(base, label)
    return value


def package(ident, port):
    """Measure one starter page on the loopback port and write its folder.

    meta.json is written last: it marks the folder as complete for starters.catalog().
    """
    label = f"{starters.PREFIX}{ident}"
    base = f"http://127.0.0.1:{port}/{ident}/index.html"
    page_sha = hashlib.sha256((SOURCE / ident / "index.html").read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory() as tmp:
        write_measure(base, tmp, allow_loopback=True)
        data = json.loads((Path(tmp) / "fingerprint.json").read_text(encoding="utf-8"))
        data = relabel(data, base, label)
        if f"127.0.0.1:{port}" in json.dumps(data):
            raise RuntimeError(f"{ident}: a loopback address is still in the fingerprint")
        folder = starters.PACKAGED / ident
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "meta.json").unlink(
            missing_ok=True
        )  # a crash below must not leave a listed starter
        shutil.copyfile(Path(tmp) / "screenshot.png", folder / "screenshot.png")
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        (folder / "fingerprint.json").write_text(text, encoding="utf-8")
    meta = {"id": ident, "name": NAMES[ident], "license": LICENSE, "sha256": page_sha}
    (folder / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"packaged {label}")


def main():
    ids = sorted(path.name for path in SOURCE.iterdir() if (path / "index.html").is_file())
    if set(ids) != set(NAMES):
        sys.exit(f"starters/ has {ids}; NAMES in this script must list the same ids")
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(SOURCE)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for ident in ids:
            package(ident, server.server_port)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
