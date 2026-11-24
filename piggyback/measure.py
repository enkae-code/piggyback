"""Page design fingerprint.

Chromium runs in a child process: Playwright's sync API is not thread-safe.
The child reads one JSON request on stdin and prints one JSON line on stdout. Nothing from the
page is ever put on a command line or evaluated as code: the page script is a fixed file.
"""

import contextlib
import ipaddress
import json
import math
import os
import platform
import re
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__, netpolicy
from .browser import NO_WEBRTC, launch
from .proxy import EgressProxy

SCRIPT = (Path(__file__).parent / "measure.js").read_text(encoding="utf-8").strip().rstrip(";")
BYPASS_ENV = "PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK"
TOTAL_S = 90  # whole run, browser start included
SCROLL_TOTAL_S = 180  # a scroll run also wheels through six steps, samples each one and runs REA
SCROLL_NUM_LIMIT = (
    1_000_000  # page numbers are clamped to this: no real px, ms or opacity is larger
)
WORKER_OUTPUT_MAX = 16 * 1024 * 1024  # the most worker stdout the parent reads
NAV_MS = 30_000
SETTLE_MS = 5_000  # a page that never goes idle is still measured
NAVIGATED = "the scroll page navigated during its capture; measure a page that stays put"
SCROLL_WAIT_MS = 3_000  # the scroll page settles before its first frame
SCROLL_ARRIVE_MS = 1_000  # a wheel move that never scrolls is recorded as null, then carried on
SCROLL_STEPS = 5  # six frames: the top, then five equal steps down to the bottom
DENSE_MS = (0, 200, 500, 900)  # samples after each wheel move: the first second of motion
REA_ARM_MS = 500  # REA attaches before the first wheel move
REA_STEP_MS = 700  # pause after each wheel move, so REA sees the page's own work
REA_WINDOW_MS = 6000  # REA's observation window; the wheel pass sits inside it
# Numbers and fixed words only: no text, HTML, URLs or class names leave the page.
SCROLL_SAMPLE = """(withAnimations) => {
  const EASES = ['linear', 'ease', 'ease-in', 'ease-out', 'ease-in-out', 'step-start', 'step-end'];
  const TAG = /^[a-z][a-z0-9]{0,19}$/;
  const num = (n) => (Number.isFinite(n) ? Math.round(n * 100) / 100 : null);
  const easing = (e) => (EASES.includes(e) ? e
    : e.startsWith('cubic-bezier(') ? 'cubic-bezier' : e.startsWith('steps(') ? 'steps' : 'other');
  const animations = withAnimations ? document.getAnimations().slice(0, 40).map((a) => {
    const t = a.effect ? a.effect.getComputedTiming() : {};
    const kind = a instanceof CSSTransition ? 'transition'
      : a instanceof CSSAnimation ? 'animation' : 'other';
    return {
      kind,
      duration: num(t.duration),
      delay: num(t.delay),
      iterations: Number.isFinite(t.iterations) ? t.iterations : 'infinite',
      easing: easing(String(t.easing)),
      play_state: a.playState,
    };
  }) : null;
  const elements = [];
  const nodes = document.body ? document.body.querySelectorAll('*') : [];
  for (let index = 0; index < nodes.length && elements.length < 40; index++) {
    const el = nodes[index];
    const r = el.getBoundingClientRect();
    if (r.bottom <= 0 || r.top >= innerHeight) continue;
    const s = getComputedStyle(el);
    const m = s.transform === 'none' ? null : new DOMMatrixReadOnly(s.transform);
    elements.push({
      index,
      tag: TAG.test(el.localName) ? el.localName : 'other',
      box: [r.x, r.y, r.width, r.height].map(num),
      transform: m ? [m.a, m.b, m.c, m.d, m.e, m.f].map(num) : null,
      opacity: num(Number(s.opacity)),
      clip_path: s.clipPath === 'none' ? 'none' : 'other',
      position: ['static', 'relative', 'absolute', 'fixed', 'sticky'].includes(s.position)
        ? s.position : 'other',
    });
  }
  return {scroll_y: num(scrollY), animations, elements};
}"""


class MeasureError(RuntimeError):
    """A refused or failed measure. status and final_url are set when the refusal knows them."""

    def __init__(self, message="", *, status=None, final_url=None, screenshot=None):
        super().__init__(message)
        self.status = status
        self.final_url = final_url
        self.screenshot = screenshot  # set only when a failure screenshot was requested and taken


def check_url(url, allow_loopback=False):
    """The URL to fetch (localhost rewritten to 127.0.0.1), or MeasureError."""
    url = netpolicy.local_name(url)
    if netpolicy.url_allowed(url, allow_loopback):
        return url
    hint = ""
    if not allow_loopback and netpolicy.url_allowed(url, allow_loopback=True):
        hint = " (use --allow-loopback for your own local build)"
    raise MeasureError(f"url is not a public http(s) address{hint}")


def receipt_refusal(status, requested, final):
    """Why the answer cannot stand as the reference, or None. Only 2xx on the same host passes."""
    final_host = urlsplit(final).hostname or "unknown host"
    if status is None:
        return "page gave no HTTP response"
    if not 200 <= status < 300:
        return f"page answered HTTP {status} from {final_host}: measure a page that loads with 2xx"
    if final_host != urlsplit(requested).hostname:
        return f"page redirected to {final}, not the host you gave: measure that address"
    return None


def _refuse_if_moved(status, req, page, tmp=None):
    refusal = receipt_refusal(status, req["url"], page.url)
    if not refusal:
        return
    shot = None
    if tmp and req.get("failure_screenshot") and req["screenshot"]:  # opt-in: keep the page
        page.screenshot(path=tmp, type="png")
        os.replace(tmp, req["screenshot"])
        shot = req["screenshot"]
    raise MeasureError(refusal, status=status, final_url=page.url, screenshot=shot)


def refuse_links(*paths):
    """MeasureError if a path is a symbolic link: output never follows a link out of its folder."""
    for path in paths:
        if os.path.islink(path):
            raise MeasureError(
                f"{Path(path).name} is a symbolic link; refusing to write through it"
            )


def _temp_beside(path):
    """Empty temp file beside path under a random name, created O_EXCL so it is never an existing
    file or link. The umask sets its mode; an existing regular file at path only narrows it."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    os.close(
        os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o666)
    )
    try:
        try:
            existing = os.lstat(path).st_mode
        except FileNotFoundError:
            existing = 0
        if stat.S_ISREG(existing):
            os.chmod(tmp, os.lstat(tmp).st_mode & existing & 0o777)
    except BaseException:
        os.unlink(tmp)
        raise
    return str(tmp)


def publish_json(path, data):
    """Replace path with JSON text in one rename: a crash never leaves a partial file."""
    tmp = _temp_beside(path)
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, path)  # replaces a link at path, never writes through it
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write_measure(
    url, out_dir, *, viewport=(1440, 900), allow_loopback=False, scroll=False, rea_binary=None
):
    """Measure one page into out_dir: fingerprint.json and screenshot.png. Raises MeasureError.

    scroll: also write scroll/frame-0..5.png and scroll-samples.json, the kit's motion evidence.
    """
    check_url(url, allow_loopback)
    out_dir = Path(out_dir)
    shot = out_dir / "screenshot.png"
    fingerprint = out_dir / "fingerprint.json"
    samples = out_dir / "scroll-samples.json"
    scroll_dir = out_dir / "scroll"
    refuse_links(shot, fingerprint)  # before the browser runs
    if scroll:
        frames = [scroll_dir / f"frame-{i}.png" for i in range(SCROLL_STEPS + 1)]
        refuse_links(samples, scroll_dir, *frames)
    out_dir.mkdir(parents=True, exist_ok=True)
    if scroll:
        scroll_dir.mkdir(exist_ok=True)
    result = measure(
        url,
        viewport=viewport,
        allow_loopback=allow_loopback,
        screenshot=shot,
        scroll_dir=scroll_dir if scroll else None,
        rea_binary=rea_binary,
    )
    if "scroll" in result:
        publish_json(samples, result.pop("scroll"))
    publish_json(fingerprint, result)  # last: its presence marks the folder complete
    return fingerprint, shot


def _path_or_none(path):
    return str(path) if path else None


def _run_worker(request):
    """One JSON request to a Chromium child process; its JSON result, or an error dict."""
    # Playwright's bypass switch would let Chromium skip the proxy for loopback.
    env = {k: v for k, v in os.environ.items() if k != BYPASS_ENV}
    timeout = SCROLL_TOTAL_S if request.get("scroll_dir") else TOTAL_S
    # stdout goes to a file: the parent reads at most WORKER_OUTPUT_MAX + 1 bytes of it.
    # ponytail: one giant page.evaluate result inside the worker is bounded only by the deadline.
    with tempfile.TemporaryFile() as out_file:
        proc = subprocess.Popen(
            [sys.executable, "-m", "piggyback.measure"],
            stdin=subprocess.PIPE,
            stdout=out_file,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=env,
        )
        try:
            _, err = proc.communicate(json.dumps(request), timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise MeasureError(f"measure timed out after {timeout}s") from None
        out_file.seek(0)
        raw = out_file.read(WORKER_OUTPUT_MAX + 1)
    if len(raw) > WORKER_OUTPUT_MAX:
        raise MeasureError("the page produced too much measurement data")
    lines = raw.decode("utf-8", errors="replace").strip().splitlines()
    try:
        return json.loads(lines[-1]) if lines else {"error": err.strip() or "no output"}
    except json.JSONDecodeError:
        return {"error": "worker output was not JSON"}


def measure(
    url,
    *,
    viewport=(1440, 900),
    allow_loopback=False,
    screenshot=None,
    evidence=None,
    failure_screenshot=False,
    scroll_dir=None,
    rea_binary=None,
):
    """Fingerprint one page. evidence: optional path for a DOM snapshot (Python API only).

    failure_screenshot: opt-in (C1 harness). A refused page is screenshotted to `screenshot`
    before the refusal, and the MeasureError carries the path.
    scroll_dir: opt-in (kit). Frames and scroll samples are captured; the result carries "scroll".
    """
    url = check_url(url, allow_loopback)
    request = {
        "url": url,
        "viewport": list(viewport),
        "allow_loopback": allow_loopback,
        "screenshot": _path_or_none(screenshot),
        "evidence": _path_or_none(evidence),
        "failure_screenshot": bool(failure_screenshot),
        "scroll_dir": _path_or_none(scroll_dir),
        "rea": rea_binary if scroll_dir else None,
    }
    result = _run_worker(request)
    if "error" in result:
        raise MeasureError(
            result["error"],
            status=result.get("status"),
            final_url=result.get("final_url"),
            screenshot=result.get("screenshot"),
        )
    if "scroll" in result:
        result["scroll"] = clean_scroll(
            result["scroll"]
        )  # the page's own output: rebuilt before publish
    return result


EVIDENCE_STYLES = [
    "display",
    "visibility",
    "opacity",
    "font-size",
    "line-height",
    "letter-spacing",
    "font-weight",
    "font-family",
    "color",
    "background-color",
    "border-top-left-radius",
    "box-shadow",
    "padding-top",
    "padding-right",
    "padding-bottom",
    "padding-left",
]
PAGE_STATE = (
    "() => ({url: location.href, title: document.title, scroll: [scrollX, scrollY],"
    " viewport: [innerWidth, innerHeight],"
    " text: (document.body ? document.body.innerText : '').slice(0, 4000)})"
)


def _paragraph_texts(session, snapshot):
    """innerText of each <p>, keyed by backend node id, read in the same CDP session.

    A paragraph that fails to resolve is left out: the snapshot falls back to its own text for it.
    """
    nodes = snapshot["documents"][0]["nodes"]
    pseudo = set(nodes.get("pseudoType", {}).get("index", []))
    texts = {}
    for idx, kind in enumerate(nodes["nodeType"]):
        if kind != 1 or idx in pseudo or snapshot["strings"][nodes["nodeName"][idx]] != "P":
            continue
        backend = nodes["backendNodeId"][idx]
        try:
            node = session.send("DOM.resolveNode", {"backendNodeId": backend})["object"]
            result = session.send(
                "Runtime.callFunctionOn",
                {
                    "objectId": node["objectId"],
                    "functionDeclaration": "function () { return this.innerText; }",
                    "returnByValue": True,
                },
            )
        except Exception:  # one node (detached, replaced) must not fail the whole side
            continue
        texts[str(backend)] = result["result"].get("value", "")
    return texts


def _save_evidence(page, path):
    """DOM snapshot with computed styles and rects, from the same session, to a JSON file."""
    refuse_links(path)
    session = page.context.new_cdp_session(page)
    try:
        snapshot = session.send(
            "DOMSnapshot.captureSnapshot",
            {
                "computedStyles": EVIDENCE_STYLES,
                "includeDOMRects": True,
                "includePaintOrder": False,
            },
        )
        inner_text = _paragraph_texts(session, snapshot)
    finally:
        session.detach()
    publish_json(
        path,
        {
            "styles": EVIDENCE_STYLES,
            "page": page.evaluate(PAGE_STATE),
            "inner_text": inner_text,
            **snapshot,
        },
    )


def _capture_motion(page, screenshot, evidence=None):
    fingerprint = page.evaluate(f"({SCRIPT})()")
    if evidence:  # opt-in: before the screenshot and reduced-motion emulation, same session
        _save_evidence(page, evidence)
    if screenshot:
        page.screenshot(path=screenshot, type="png")
    page.emulate_media(reduced_motion="reduce")
    fingerprint["reduced_motion"] = {
        "still_running": page.evaluate(
            "() => document.getAnimations()"
            ".filter((animation) => animation.playState === 'running').length"
        )
    }
    return fingerprint


def _capture(req):
    shot = req["screenshot"]
    # Playwright writes a fresh temp file beside the final name. os.replace publishes it last, so
    # a link or hardlink at the final name is replaced, never written through.
    tmp = _temp_beside(shot) if shot else None
    try:
        result = _capture_page(req, tmp)
        if tmp:
            os.replace(tmp, shot)
        return result
    finally:
        if tmp:
            Path(tmp).unlink(missing_ok=True)


def _capture_page(req, tmp):
    from playwright.sync_api import sync_playwright

    width, height = req["viewport"]
    allow_loopback = req["allow_loopback"]
    # The proxy is the enforcement point: every connection Chromium makes goes through it.
    with closing(EgressProxy(allow_loopback)) as egress, sync_playwright() as p:

        def guard(route):
            # Second layer for page requests, redirect hops included; the proxy is authoritative.
            url = route.request.url
            if netpolicy.url_allowed(url, allow_loopback):
                route.continue_()
            else:
                egress.record(url)
                route.abort("blockedbyclient")

        proxy = {"server": f"http://127.0.0.1:{egress.port}"}  # loopback is proxied by Playwright
        browser, label, port = _launch_for(p, req, proxy)
        try:
            ctx = browser.new_context(
                viewport={"width": width, "height": height},
                service_workers="block",
                accept_downloads=False,
            )
            ctx.route("**/*", guard)
            ctx.add_init_script(NO_WEBRTC)
            page = ctx.new_page()
            response = page.goto(req["url"], wait_until="load", timeout=NAV_MS)
            status = response.status if response else None
            _refuse_if_moved(status, req, page, tmp)
            page.evaluate("() => document.fonts.ready")
            with contextlib.suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=SETTLE_MS)
            fingerprint = _capture_motion(page, tmp, req.get("evidence"))
            try:
                _refuse_if_moved(status, req, page, tmp)  # a script may redirect while it settles
            except MeasureError as exc:
                if req["screenshot"] and exc.screenshot is None:
                    Path(req["screenshot"]).unlink(missing_ok=True)
                raise
            captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            receipt = {
                "status": status,
                "final_url": page.url,
                "browser": label,
                "browser_version": browser.version,
                "os": f"{platform.system()} {platform.release()}",
                "viewport": {"width": width, "height": height},
                "captured_at": captured_at,
            }
            scroll, rea = (
                _capture_scroll(ctx, req, page, port) if req.get("scroll_dir") else (None, None)
            )
        finally:
            browser.close()
    result = {
        **fingerprint,
        "url": req["url"],
        "captured_with": f"piggyback {__version__}",
        "browser": label,
        "captured_at": captured_at,
        "capture": receipt,
        "blocked_requests": egress.blocked,
        "screenshot": req["screenshot"],
    }
    if scroll is not None:
        result["scroll"] = scroll
    if rea is not None:
        result["rea"] = rea
    return result


class _DebugPort:
    """The Playwright object with --remote-debugging-port added to every launch. browser.py's
    argument list is shared, so the switch is added here, and only for a run that uses REA."""

    def __init__(self, playwright, port):
        self._playwright = playwright
        self._switch = f"--remote-debugging-port={port}"
        self.chromium = self

    def launch(self, **options):
        options["args"] = [*options["args"], self._switch]
        return self._playwright.chromium.launch(**options)


def _launch_for(playwright, req, proxy):
    """(browser, channel label, REA port or None). A port is picked only for a run that uses REA."""
    port = _free_port() if req.get("rea") else None
    browser, label = launch(
        _DebugPort(playwright, port) if port else playwright, proxy=proxy
    )  # fresh temporary profile per launch, removed on close
    return browser, label, port


def _free_port():
    """A free loopback port for REA's debugging endpoint.
    # ponytail: bind 0 then close: another process can take the port before Chromium binds it."""
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _capture_rea(page, req, port, targets):
    """REA observes the scroll page after the timing samples. A failure is a stage: the caller
    still checks that the page was not replaced, and the kit is still published."""
    from . import tools  # tools imports this module: a top-level import would be circular

    binary = req["rea"]
    endpoint = f"http://127.0.0.1:{port}"
    version = None
    try:
        version = tools.rea_version(binary)
        page.evaluate("scrollTo(0, 0)")
        url = page.url  # read here: the observer thread must not call Playwright
        target = tools.rea_target(binary, endpoint, url)
        outcome = {}

        def observe():  # only REA's subprocess runs in this thread
            try:
                outcome["raw"] = tools.rea_observe(
                    binary,
                    endpoint=endpoint,
                    target_id=target,
                    url=url,
                    window_ms=REA_WINDOW_MS,
                )
            except Exception as exc:
                outcome["error"] = exc

        observer = threading.Thread(target=observe, daemon=True)
        observer.start()
        page.wait_for_timeout(REA_ARM_MS)
        for k in range(1, SCROLL_STEPS + 1):
            _scroll_to(page, targets[k] - targets[k - 1])
            page.wait_for_timeout(REA_STEP_MS)
        observer.join()
        if "error" in outcome:
            raise outcome["error"]
        tools.publish_rea(Path(req["scroll_dir"]).parent, outcome["raw"], version, REA_WINDOW_MS)
    except Exception as exc:
        return {
            "state": "failed",
            "version": version,
            "reason": getattr(exc, "reason", "page error"),
        }
    return {"state": "captured", "version": version}


def _sample(page, with_animations):
    return page.evaluate(SCROLL_SAMPLE, with_animations)


def _await_scroll(page, before):
    """Wait until scrollY leaves `before`: ms waited, or None if it does not move in time."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    start = time.monotonic()
    try:
        page.wait_for_function("(y) => scrollY !== y", arg=before, timeout=SCROLL_ARRIVE_MS)
    except PlaywrightTimeout:
        return None
    return round((time.monotonic() - start) * 1000, 1)


def _scroll_to(page, delta):
    """Wheel by delta px; ms until the page has moved (0 if there was no move to make)."""
    if delta == 0:
        return 0
    before = page.evaluate("scrollY")
    page.mouse.wheel(0, delta)
    return _await_scroll(page, before)


def _dense_samples(page, origin, arrival):
    """Samples at DENSE_MS after origin. actual_ms is when Python requested the sample (Python's
    monotonic clock), not when the browser painted it. The arrival frame is taken right after
    the +0 sample; a screenshot that delays +200 shows in that sample's actual_ms."""
    dense = []
    for due in DENSE_MS:
        # The browser's timer can end a fraction early on our clock: wait until ours says due.
        while (wait := due - (time.monotonic() - origin) * 1000) > 0:
            page.wait_for_timeout(math.ceil(wait))
        actual = round((time.monotonic() - origin) * 1000, 1)
        dense.append({"due_ms": due, "actual_ms": actual, **_sample(page, False)})
        if due == 0:
            page.screenshot(path=arrival, type="png")
    return dense


def _document_origin(page):
    """The current document's timeOrigin; a failed read counts as a navigation."""
    try:
        return page.evaluate("performance.timeOrigin")
    except Exception as exc:
        raise MeasureError(NAVIGATED) from exc


def _replaced(page, origin, navigations):
    """True if the scroll page is no longer the document it loaded (an unreadable one counts)."""
    # A new document may commit without DOMContentLoaded: its timeOrigin still differs.
    if navigations:
        return True
    try:
        return page.evaluate("performance.timeOrigin") != origin
    except Exception:
        return True


def _refuse_if_replaced(page, origin, navigations, status, req):
    """MeasureError if the scroll page is no longer the document it loaded."""
    if _replaced(page, origin, navigations):
        raise MeasureError(NAVIGATED)
    _refuse_if_moved(status, req, page)


def _capture_scroll(ctx, req, first_page, port=None):
    """Frames from the top to the bottom on a fresh page in the same context, with motion samples.

    The first page closes first. Each frame is written beside its final name and published only
    once all are written. A navigation of the scroll page during capture refuses it.
    """
    frames_dir = Path(req["scroll_dir"])
    width, height = req["viewport"]
    first_page.close()
    page = ctx.new_page()  # normal motion: the first page's reduced-motion emulation is per page
    response = page.goto(req["url"], wait_until="load", timeout=NAV_MS)
    status = response.status if response else None
    _refuse_if_moved(status, req, page)
    origin = _document_origin(page)  # the document goto loaded, before anything can replace it
    navigations = []

    # Once per new main-frame document: pushState, replaceState and hash changes do not count.
    page.on("domcontentloaded", lambda _page: navigations.append(True))
    page.wait_for_timeout(SCROLL_WAIT_MS)
    _refuse_if_replaced(page, origin, navigations, status, req)
    scrollable = page.evaluate("document.documentElement.scrollHeight - innerHeight")
    targets = [round(scrollable * k / SCROLL_STEPS) for k in range(SCROLL_STEPS + 1)]
    page.mouse.move(width / 2, height / 2)
    steps, pending = [], []
    try:
        previous = 0
        for step, target in enumerate(targets):
            onset = _scroll_to(page, target - previous)  # cumulative targets: no drift
            previous = target
            arrival = frames_dir / f"frame-{step}-arrival.png"  # taken inside _dense_samples
            arrival_tmp = _temp_beside(arrival)
            pending.append((arrival_tmp, arrival))
            dense = _dense_samples(page, time.monotonic(), arrival_tmp)
            settled = _sample(page, True)
            final = frames_dir / f"frame-{step}.png"
            tmp = _temp_beside(final)
            pending.append((tmp, final))
            page.screenshot(path=tmp, type="png")
            steps.append(
                {
                    "step": step,
                    "frame": final.name,
                    "target_scroll_y": target,
                    "scroll_onset_ms": onset,
                    **settled,
                    "dense": dense,
                }
            )
        scrollable_end = page.evaluate("document.documentElement.scrollHeight - innerHeight")
        rea = _capture_rea(page, req, port, targets) if port else None
        _refuse_if_replaced(page, origin, navigations, status, req)
        for tmp, final in pending:
            os.replace(tmp, final)
    except Exception as exc:
        # A navigation mid-step usually breaks the next evaluate before its event arrives.
        if not isinstance(exc, MeasureError):
            with contextlib.suppress(Exception):
                page.wait_for_timeout(250)
            if _replaced(page, origin, navigations):
                raise MeasureError(NAVIGATED) from exc
        raise
    finally:
        for tmp, _ in pending:
            Path(tmp).unlink(missing_ok=True)
    return {
        "version": 1,
        "viewport": {"width": width, "height": height},
        "media": "no-preference",
        "scrollable_px": round(scrollable),
        "scrollable_end_px": round(scrollable_end),
        "status": status,
        "final_host": urlsplit(page.url).hostname,
        "coverage": {"scroll": scroll_coverage(scrollable, scrollable_end, steps)},
        "steps": steps,
    }, rea


def scroll_coverage(scrollable, scrollable_end, steps):
    """done, partial or none. partial: a wheel step timed out or settled away from its target, or
    the page stopped short of its live bottom (scrollable_end, read after the last step)."""
    if scrollable <= 0 or not any(step["scroll_y"] != steps[0]["scroll_y"] for step in steps):
        return "none"
    tolerance = max(2, 0.02 * scrollable)
    final = steps[-1]["scroll_y"]
    if final is None or scrollable_end - final > tolerance:
        return "partial"
    for step in steps:
        if step["scroll_onset_ms"] is None or step["scroll_y"] is None:
            return "partial"
        if abs(step["scroll_y"] - step["target_scroll_y"]) > tolerance:
            return "partial"
    return "done"


# Page output is untrusted: only these words and numbers pass the sanitizer; the rest is "other".
KINDS = frozenset({"animation", "transition"})
EASINGS = frozenset(
    {
        "linear",
        "ease",
        "ease-in",
        "ease-out",
        "ease-in-out",
        "step-start",
        "step-end",
        "cubic-bezier",
        "steps",
    }
)
PLAY_STATES = frozenset({"idle", "running", "paused", "finished"})
POSITIONS = frozenset({"static", "relative", "absolute", "fixed", "sticky"})
TAGS = frozenset(
    {
        "html", "body", "main", "header", "footer", "nav", "section", "article", "aside",
        "div", "span", "p", "a", "img", "picture", "video", "canvas", "svg", "h1", "h2",
        "h3", "h4", "h5", "h6", "ul", "ol", "li", "button", "form", "input", "figure",
        "figcaption", "blockquote", "label", "textarea", "select", "iframe",
    }
)  # fmt: skip
COVERAGE = frozenset({"done", "partial", "none"})
HOST = re.compile(r"[a-z0-9.-]{1,253}")
UNEXPECTED = "scroll result has an unexpected shape"


def _shape(value, kind):
    if not isinstance(value, kind):
        raise MeasureError(UNEXPECTED)
    return value


def _word(value, allowed):
    return value if isinstance(value, str) and value in allowed else "other"


def _num(value):
    """A finite number clamped to SCROLL_NUM_LIMIT, or None. A bool is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return round(min(max(value, -SCROLL_NUM_LIMIT), SCROLL_NUM_LIMIT), 2)


def _host(value):
    """A DNS name, or a validated IP literal in canonical form; anything else is "other"."""
    if not isinstance(value, str):
        return "other"
    if HOST.fullmatch(value):
        return value
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return "other"
    if getattr(address, "scope_id", None):  # a zone id is not a host we vetted
        return "other"
    return str(address)


def _box(value, count):
    values = _shape(value, list)
    if len(values) != count:
        raise MeasureError(UNEXPECTED)
    return [_num(v) for v in values]


def _list(value, cap, item):
    return [item(v) for v in _shape(value, list)[:cap]]


def _animation(raw):
    raw = _shape(raw, dict)
    iterations = raw.get("iterations")
    return {
        "kind": _word(raw.get("kind"), KINDS),
        "duration": _num(raw.get("duration")),
        "delay": _num(raw.get("delay")),
        "iterations": "infinite" if iterations == "infinite" else _num(iterations),
        "easing": _word(raw.get("easing"), EASINGS),
        "play_state": _word(raw.get("play_state"), PLAY_STATES),
    }


def _element(raw):
    raw = _shape(raw, dict)
    transform = raw.get("transform")
    return {
        "index": _num(raw.get("index")),
        "tag": _word(raw.get("tag"), TAGS),
        "box": _box(raw.get("box"), 4),
        "transform": None if transform is None else _box(transform, 6),
        "opacity": _num(raw.get("opacity")),
        "clip_path": _word(raw.get("clip_path"), {"none"}),
        "position": _word(raw.get("position"), POSITIONS),
    }


def _clean_sample(raw):
    raw = _shape(raw, dict)
    animations = raw.get("animations")  # null when the sample skips animations
    return {
        "scroll_y": _num(raw.get("scroll_y")),
        "animations": None if animations is None else _list(animations, 40, _animation),
        "elements": _list(raw.get("elements"), 40, _element),
    }


def _dense(raw):
    raw = _shape(raw, dict)
    return {
        "due_ms": _num(raw.get("due_ms")),
        "actual_ms": _num(raw.get("actual_ms")),
        **_clean_sample(raw),
    }


def _step(index, raw):
    raw = _shape(raw, dict)
    return {
        "step": index,
        "frame": f"frame-{index}.png",  # named here, never taken from the page
        "target_scroll_y": _num(raw.get("target_scroll_y")),
        "scroll_onset_ms": _num(raw.get("scroll_onset_ms")),
        **_clean_sample(raw),
        "dense": _list(raw.get("dense"), 4, _dense),
    }


def clean_scroll(raw):
    """Rebuild a scroll result from allowlisted keys and fixed words; MeasureError on other shapes.

    Page-forged numbers are accepted: they can only misstate the page's own motion.
    # ponytail: isolated-world sampling would stop the page forging numbers; not built here.
    """
    raw = _shape(raw, dict)
    viewport = _shape(raw.get("viewport"), dict)
    coverage = _word(_shape(raw.get("coverage"), dict).get("scroll"), COVERAGE)
    if coverage == "other":
        raise MeasureError(UNEXPECTED)
    status = raw.get("status")
    return {
        "version": 1,
        "viewport": {"width": _num(viewport.get("width")), "height": _num(viewport.get("height"))},
        "media": _word(raw.get("media"), {"no-preference"}),
        "scrollable_px": _num(raw.get("scrollable_px")),
        "scrollable_end_px": _num(raw.get("scrollable_end_px")),
        "status": status if isinstance(status, int) and 100 <= status <= 599 else None,
        "final_host": _host(raw.get("final_host")),
        "coverage": {"scroll": coverage},
        "steps": [_step(i, s) for i, s in enumerate(_shape(raw.get("steps"), list)[:6])],
    }


def _worker():
    req = json.loads(sys.stdin.read())
    try:
        result = _capture(req)
    except Exception as exc:  # Playwright and launch errors
        text = str(exc).strip()
        out: dict = {"error": text or repr(exc)}
        if isinstance(exc, MeasureError):
            out["status"], out["final_url"] = exc.status, exc.final_url
            out["screenshot"] = exc.screenshot
        print(json.dumps(out))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(_worker())
