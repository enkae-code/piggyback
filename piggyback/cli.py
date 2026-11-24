"""`piggyback` command line: measure, check-refs and doctor."""

import argparse
import json
import os
import re
import sys
from pathlib import Path

from . import batch, mcp, server
from . import brief as brief_mod
from . import defaults as defaults_mod
from . import fingerprint as fingerprints
from . import format as ref_format
from . import gap as gap_mod
from . import kit as kit_mod
from .browser import BrowserUnavailable, launch
from .measure import MeasureError, write_measure

SANDBOX_HELP = (
    "Chromium would not start with its sandbox. Install Google Chrome, or run "
    "`python -m playwright install chromium`. On Linux, check unprivileged user "
    "namespaces (AppArmor can block them). Piggyback never falls back to --no-sandbox.\n"
    "Ubuntu 23.10+: install Google Chrome (its package ships an AppArmor profile), or add an "
    "AppArmor profile that allows unprivileged user namespaces for Playwright's chromium.\n"
    "With a uvx install, fetch the browser with: uvx --from 'piggyback-design[measure]' "
    "playwright install chromium"
)


def check_refs(directory):
    root = Path(directory)
    if not root.is_dir():
        print(f"{directory}: not a directory")
        return 1
    refs = root / "refs" if (root / "refs").is_dir() else root
    paths = sorted(refs.glob("*.md"))
    if not paths:
        print(f"{refs}: no refs found")
        return 1
    failures = 0
    for path in paths:
        try:
            meta, *_ = ref_format.parse_ref(path.read_text(encoding="utf-8"))
            problems = ref_format.validate(meta)
        except (ValueError, UnicodeDecodeError) as exc:
            problems = [str(exc)]
        for problem in problems:
            print(f"{path}: {problem}")
        failures += bool(problems)
    print(f"{len(paths)} refs checked, {failures} with problems")
    return 1 if failures else 0


def viewport_arg(value):
    match = re.fullmatch(r"(\d{3,5})x(\d{3,5})", value)
    if not match:
        raise argparse.ArgumentTypeError("viewport must look like 1440x900")
    return int(match[1]), int(match[2])


def run_measure(url, out, viewport, allow_loopback):
    try:
        fingerprint, shot = write_measure(
            url, out, viewport=viewport, allow_loopback=allow_loopback
        )
    except MeasureError as exc:
        print(f"measure failed: {fingerprints.clean(exc, 200)}", file=sys.stderr)
        return 1
    print(f"wrote {fingerprint} and {shot}")
    return 0


def run_kit(url, out, request, allow_loopback):
    try:
        kit_path = kit_mod.make(url, out, request, allow_loopback=allow_loopback)
    except (MeasureError, ValueError, OSError) as exc:
        print(f"kit failed: {fingerprints.clean(exc, 200)}", file=sys.stderr)
        return 1
    print(f"wrote {kit_path}")
    stages = json.loads(kit_path.read_text(encoding="utf-8"))["stages"]
    hints = {
        "craft": "craft-anything-cli not found; install it or set PIGGYBACK_CRAFT",
        "rea": "rea not found; install it or set PIGGYBACK_REA",
    }
    for name, hint in hints.items():
        if stages[name]["state"] == "missing":
            print(f"piggyback: {hint}", file=sys.stderr)
    return 0


def run_measure_list(list_path, out, viewport, allow_loopback):
    try:
        entries = batch.plan(list_path, out)
        return batch.run(entries, out, viewport=viewport, allow_loopback=allow_loopback)
    except (OSError, ValueError, MeasureError) as exc:
        print(f"measure-list failed: {batch.shown(exc)}", file=sys.stderr)
        return 1


def _write_outputs(text, out, design_path, ref):
    if design_path and out and Path(out).resolve() == Path(design_path).resolve():
        raise ValueError("--out and --design-md must be different files")
    # Both refusals come before any write; write_new's O_EXCL still guards the race.
    for path in (out, design_path):
        if path and os.path.lexists(path):
            raise FileExistsError(server.exists_message(path))
    # The brief first: a bad --out path then fails before any DESIGN.md exists.
    if out:
        server.write_new(out, text)
    if design_path:
        server.write_new(design_path, brief_mod.design_md(ref))


def run_brief(paths, build_path, out, design_path=None):
    try:
        refs = [fingerprints.load(path) for path in paths]
        text = brief_mod.brief(refs)
        if build_path:
            if len(refs) != 1:
                print(
                    "brief failed: --build compares one reference; pass exactly one",
                    file=sys.stderr,
                )
                return 1
            build = fingerprints.load(build_path)
            text += "\n" + gap_mod.render(gap_mod.gap(refs[0], build)) + f"\n{server.NOTE}\n"
        _write_outputs(text, out, design_path, refs[0])
    except (OSError, ValueError) as exc:
        print(f"brief failed: {exc}", file=sys.stderr)
        return 1
    if out:
        print(f"wrote {out}")
    else:
        print(text, end="")
    if design_path:
        print(
            f"wrote {design_path} (DESIGN.md for {brief_mod.source_title(refs[0])})",
            file=sys.stderr,
        )
    return 0


def run_defaults(path):
    try:
        hits = defaults_mod.find(fingerprints.load(path))
    except (OSError, ValueError) as exc:
        print(f"defaults failed: {exc}", file=sys.stderr)
        return 1
    print(defaults_mod.render(hits), end="")
    return 0


def run_open(port, out):
    try:
        server.serve(port, out)
    except OSError as exc:
        print(f"piggyback: could not start the local screen: {exc.strerror}", file=sys.stderr)
        return 1
    return 0


def run_mcp(refs, build):
    try:
        refs, build = server._slots({"references": refs, "build": build})
    except (ValueError, MeasureError) as exc:
        print(f"mcp failed: {exc}", file=sys.stderr)
        return 1
    return mcp.serve(sys.stdin.buffer, sys.stdout.buffer, mcp.Session(refs, build))


def doctor():
    print(
        f"python: {sys.version.split()[0]}",
        "(ok)" if sys.version_info >= (3, 11) else "(need 3.11+)",
    )
    if sys.version_info < (3, 11):
        return 1
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright: not installed; run: pip install 'piggyback-design[measure]'")
        return 1
    print("playwright: installed")
    try:
        with sync_playwright() as p:
            try:
                browser, label = launch(p)
            except BrowserUnavailable as exc:
                print(f"browser: FAILED {exc}")
                print(SANDBOX_HELP)
                return 1
            print(
                f"browser: starts with sandbox ({label}, "
                f"{browser.browser_type.name} {browser.version})"
            )
            browser.close()
    except Exception as exc:  # the Playwright driver itself did not start
        print(f"playwright: FAILED to start: {(str(exc).strip() or repr(exc)).splitlines()[0]}")
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="piggyback")
    sub = parser.add_subparsers(dest="command")
    o = sub.add_parser("open", help="open the local screen (also the default with no command)")
    o.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: any free port)")
    o.add_argument("--out", default=".", help="where BRIEF.md is written (default: current dir)")
    m = sub.add_parser("measure", help="fingerprint a page into fingerprint.json + screenshot.png")
    m.add_argument("url")
    m.add_argument("--out", default=".", help="output directory (default: current directory)")
    m.add_argument("--viewport", type=viewport_arg, default=(1440, 900), help="WIDTHxHEIGHT")
    m.add_argument("--allow-loopback", action="store_true", help="allow 127.0.0.0/8 and ::1 only")
    ml = sub.add_parser("measure-list", help="measure each URL in a list file, one at a time")
    ml.add_argument("list", metavar="LIST", help="text file: one URL per line, # lines ignored")
    ml.add_argument("--out", required=True, help="directory: one folder per URL, plus batch.json")
    ml.add_argument("--viewport", type=viewport_arg, default=(1440, 900), help="WIDTHxHEIGHT")
    ml.add_argument("--allow-loopback", action="store_true", help="allow 127.0.0.0/8 and ::1 only")
    k = sub.add_parser(
        "kit",
        help="build kit from one URL: screenshot, scroll frames, BRIEF.md, DESIGN.md, prompt",
    )
    k.add_argument("url")
    k.add_argument("--out", required=True, help="new or empty folder for the kit")
    k.add_argument("--request", required=True, help="what to build (1 to 500 characters)")
    k.add_argument("--allow-loopback", action="store_true", help="allow 127.0.0.0/8 and ::1 only")
    refs = sub.add_parser("check-refs", help="validate spec v1 reference files")
    refs.add_argument("directory")
    b = sub.add_parser("brief", help="write BRIEF.md from 1 to 3 reference fingerprints")
    b.add_argument("fingerprints", nargs="+", metavar="FINGERPRINT.json")
    b.add_argument(
        "--build",
        metavar="BUILD.json",
        help="add differences vs one reference (differences use reference 1)",
    )
    b.add_argument("--out", metavar="BRIEF.md", help="write to a file instead of stdout")
    b.add_argument(
        "--design-md",
        metavar="PATH",
        help="also write DESIGN.md for the first reference (Impeccable reads it)",
    )
    d = sub.add_parser("defaults", help="list generic defaults on one build fingerprint")
    d.add_argument("build", metavar="BUILD.json")
    sub.add_parser("doctor", help="check Python, Playwright and the sandboxed browser")
    mc = sub.add_parser(
        "mcp", help="read-only MCP server on stdio: get_brief, get_design_md and check"
    )
    mc.add_argument(
        "--ref",
        action="append",
        metavar="URL",
        help="public reference, or starter:<id> from list_starters (1 to 3)",
    )
    mc.add_argument("--build", metavar="URL", help="your build: localhost, or a public preview")
    args = parser.parse_args(argv)
    if args.command in (None, "open"):
        return run_open(getattr(args, "port", 0), getattr(args, "out", "."))
    if args.command == "measure":
        return run_measure(args.url, args.out, args.viewport, args.allow_loopback)
    if args.command == "measure-list":
        return run_measure_list(args.list, args.out, args.viewport, args.allow_loopback)
    if args.command == "kit":
        return run_kit(args.url, args.out, args.request, args.allow_loopback)
    if args.command == "check-refs":
        return check_refs(args.directory)
    if args.command == "brief":
        return run_brief(args.fingerprints, args.build, args.out, args.design_md)
    if args.command == "defaults":
        return run_defaults(args.build)
    if args.command == "mcp":
        return run_mcp(args.ref or [], args.build)
    return doctor()
