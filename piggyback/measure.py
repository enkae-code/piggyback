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
import shutil
import signal
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
from .browser import ARGS, CHANNELS, NO_WEBRTC, launch
from .proxy import EgressProxy

SCRIPT = (Path(__file__).parent / "measure.js").read_text(encoding="utf-8").strip().rstrip(";")
BYPASS_ENV = "PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK"
TOTAL_S = 90  # whole run, browser start included
SCROLL_TOTAL_S = 240  # a scroll run: six wheel steps with samples
WORKER_GRACE_S = 5  # after SIGTERM, a worker still running gets this long before its tree is killed
REA_SLACK_S = 5  # the REA worker's start-up and teardown, on top of REA_STAGE_S
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
# The window covers the slowest wheel pass: REA arms, then each step waits for a move and a pause.
REA_WINDOW_MS = REA_ARM_MS + SCROLL_STEPS * (SCROLL_ARRIVE_MS + REA_STEP_MS)
REA_STAGE_S = 40  # REA version, target and observation together: a slow REA fails only REA
REA_BROWSER_ARGS = ["--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1"]
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
  return {scroll_y: num(scrollY), animations, elements, count: nodes.length};
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


def _temp_fd(path, mode):
    """(fd, name) of a new empty file beside path, created O_EXCL: never an existing file or link.
    mode is applied at creation (the umask narrows it), so no byte lands in a looser file."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    return fd, str(tmp)


def _mode_of(path):
    """Mode for a replacement of path: an existing regular file's, else 0o666 (umask applies)."""
    try:
        found = os.lstat(path)
    except FileNotFoundError:
        return 0o666
    return found.st_mode & 0o777 if stat.S_ISREG(found.st_mode) else 0o666


def _temp_beside(path):
    """Empty temp file beside path, for a writer that takes a file name (a screenshot)."""
    fd, tmp = _temp_fd(path, _mode_of(path))
    os.close(fd)
    return tmp


def publish_text(path, text):
    """Replace path with text in one rename: a crash never leaves a partial file. The text goes
    into the descriptor of the temp file this call created, so the name is never reopened."""
    fd, tmp = _temp_fd(path, _mode_of(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)  # replaces a link at path, never writes through it
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_plain(path, cap):
    """The bytes of a plain file, at most cap (ValueError beyond it), read through one descriptor.

    None for a link, a special file, a FIFO or a file with another name: a FIFO is refused by its
    type before any read can block. The descriptor is checked again after open, so a swap between
    the two checks is caught too.
    """
    if not stat.S_ISREG(os.lstat(path).st_mode):
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as handle:
        now = os.fstat(handle.fileno())
        if not stat.S_ISREG(now.st_mode) or now.st_nlink != 1:
            return None
        data = handle.read(cap + 1)
    if len(data) > cap:
        raise ValueError(f"{Path(path).name}: larger than {cap} bytes")
    return data


def publish_json(path, data):
    publish_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


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
    if result.get("screenshot"):
        result["screenshot"] = (
            shot.name
        )  # beside the fingerprint; an absolute path leaks and goes stale
    publish_json(fingerprint, result)  # last: its presence marks the folder complete
    return fingerprint, shot


def _path_or_none(path):
    return str(path) if path else None


def kill_tree(proc):
    """Kill a child started with start_new_session=True and everything it started: its process
    group, or its process tree on Windows. Call it only before the child is reaped, while its id
    is still ours."""
    if os.name == "nt":
        try:
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=10
                )
        finally:
            with contextlib.suppress(OSError):
                proc.kill()
    else:
        # macOS answers EPERM, not ESRCH, when the group's only member is an unreaped zombie leader.
        # Every member is our own child, so EPERM can only mean that.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)


def _stop_worker(proc):
    """Stop a timed-out worker: SIGTERM, a fixed grace for its own cleanup, then its whole tree.
    The kill is in finally, so nothing during the grace can skip it. The worker is never polled,
    so its id is still ours when the tree is killed; the caller reaps it after. Windows: taskkill
    walks the tree at once."""
    if os.name == "nt":
        kill_tree(proc)
        return
    try:
        with contextlib.suppress(ProcessLookupError):
            os.kill(proc.pid, signal.SIGTERM)
        time.sleep(WORKER_GRACE_S)
    finally:
        kill_tree(proc)


def _run_worker(request, timeout=None):
    """One JSON request to a Chromium child process; its JSON result, or an error dict."""
    # Playwright's bypass switch would let Chromium skip the proxy for loopback.
    env = {k: v for k, v in os.environ.items() if k != BYPASS_ENV}
    timeout = timeout or (SCROLL_TOTAL_S if request.get("scroll_dir") else TOTAL_S)
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
            start_new_session=True,
        )
        try:
            _, err = proc.communicate(json.dumps(request), timeout=timeout)
        except subprocess.TimeoutExpired:
            _stop_worker(proc)  # a worker alone left open would leave Chromium holding stderr
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
        if request["rea"]:
            result["rea"] = _rea_worker(request)
    return result


def _rea_worker(request):
    """REA in its own worker after the capture's: a hung REA call costs only that worker, and the
    capture is kept. A timeout is the rea stage; an error from the page refuses the kit."""
    try:
        result = _run_worker({**request, "mode": "rea"}, timeout=REA_STAGE_S + REA_SLACK_S)
    except MeasureError as exc:
        return {"state": "failed", "reason": "timeout" if "timed out" in str(exc) else "page error"}
    if "error" in result:
        if result.get("refused"):
            raise MeasureError(
                result["error"],
                status=result.get("status"),
                final_url=result.get("final_url"),
                screenshot=result.get("screenshot"),
            )
        return {"state": "failed", "reason": "page error"}
    return result["rea"]


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


def _guard(egress, allow_loopback):
    """Second layer for page requests, redirect hops included; the proxy is authoritative."""

    def guard(route):
        url = route.request.url
        if netpolicy.url_allowed(url, allow_loopback):
            route.continue_()
        else:
            egress.record(url)
            route.abort("blockedbyclient")

    return guard


def _capture_page(req, tmp):
    from playwright.sync_api import sync_playwright

    width, height = req["viewport"]
    allow_loopback = req["allow_loopback"]
    # The proxy is the enforcement point: every connection Chromium makes goes through it.
    with closing(EgressProxy(allow_loopback)) as egress, sync_playwright() as p:
        guard = _guard(egress, allow_loopback)
        proxy = {"server": f"http://127.0.0.1:{egress.port}"}  # loopback is proxied by Playwright
        browser, label = launch(
            p, proxy=proxy
        )  # fresh temporary profile per launch, removed on close
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
            scroll = _capture_scroll(ctx, req, page) if req.get("scroll_dir") else None
        finally:
            browser.close()
        captured_blocked = list(egress.blocked)  # the capture's own blocks, before REA's pass
    result = {
        **fingerprint,
        "url": req["url"],
        "captured_with": f"piggyback {__version__}",
        "browser": label,
        "captured_at": captured_at,
        "capture": receipt,
        "blocked_requests": captured_blocked,
        "screenshot": req["screenshot"],
    }
    if scroll is not None:
        result["scroll"] = scroll
    return result


def _capture_rea(req):
    """The REA worker: a fresh load in its own browser, after the capture worker has returned."""
    from playwright.sync_api import sync_playwright

    with closing(EgressProxy(req["allow_loopback"])) as egress, sync_playwright() as p:
        return {"rea": _rea_pass(p, req, egress)}


def _ms(deadline):
    """Playwright's timeout in ms for the REA stage: time left, rounded up. Playwright reads 0 as no
    limit, so a budget under 1 ms ends the stage as a timeout instead of being sent as 0."""
    from .tools import ToolError  # tools imports this module: a top-level import would be circular

    ms = math.ceil((deadline - time.monotonic()) * 1000)
    if ms < 1:
        raise ToolError("timeout")
    return ms


def _pause(page, ms, deadline):
    """A wait that stops at the stage deadline: no wait runs past the time left."""
    page.wait_for_timeout(min(ms, _ms(deadline)))


def _launch_rea(p, profile, egress, req, deadline):
    """The REA browser: a persistent profile and a loopback debugging port the OS picks (0, read
    back from DevToolsActivePort). Same sandbox, WebRTC switch, proxy, request guard and init
    script as capture. browser.launch() takes no profile, so its channel loop is repeated here.
    Each launch gets the time left as its timeout; a failure after the deadline is a timeout."""
    from .tools import ToolError  # tools imports this module: a top-level import would be circular

    width, height = req["viewport"]
    proxy = {"server": f"http://127.0.0.1:{egress.port}"}
    for channel in CHANNELS:
        timeout = _ms(deadline)  # raises once the deadline has passed: no channel is tried then
        try:
            ctx = p.chromium.launch_persistent_context(
                str(profile),
                channel=channel,
                chromium_sandbox=True,
                args=[*ARGS, *REA_BROWSER_ARGS],
                proxy=proxy,
                viewport={"width": width, "height": height},
                service_workers="block",
                accept_downloads=False,
                timeout=timeout,
            )
        except Exception:  # the next channel
            continue
        ctx.route("**/*", _guard(egress, req["allow_loopback"]))
        ctx.add_init_script(NO_WEBRTC)
        return ctx
    raise ToolError("browser unavailable")


def _rea_pass(p, req, egress):
    """REA observes the scroll page in its own short-lived browser: the capture browser has no
    debugging port. One deadline covers the whole stage, started before the launch. A REA or browser
    failure is the rea stage; a page that was replaced or moved refuses the kit (MeasureError).
    The profile is removed on every path. blocked counts the requests this pass alone refused."""
    from . import tools  # tools imports this module: a top-level import would be circular

    profile = Path(tempfile.mkdtemp(prefix="piggyback-rea-"))  # mkdtemp makes it owner-only
    deadline = time.monotonic() + REA_STAGE_S
    before = len(egress.blocked)
    result = {"version": None}
    try:
        ctx = _launch_rea(p, profile, egress, req, deadline)
        try:
            raw = _rea_observe(ctx, req, profile, result, deadline)
        finally:
            ctx.close()
        _ms(deadline)  # closing takes time too: an expired pass publishes nothing
        tools.publish_rea(Path(req["scroll_dir"]).parent, raw, result["version"], REA_WINDOW_MS)
    except MeasureError:
        raise
    except Exception as exc:  # a REA or browser failure is a stage, never a failed kit
        reason = "timeout" if time.monotonic() >= deadline else getattr(exc, "reason", "page error")
        state = {"state": "failed", **result, "reason": reason}
    else:
        state = {"state": "captured", **result}
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    return {**state, "blocked": len(egress.blocked) - before}


def _rea_observe(ctx, req, profile, result, deadline):
    """Version, target and observation under one deadline. The wheel pass runs in observation."""
    from . import tools  # tools imports this module: a top-level import would be circular

    binary = req["rea"]
    width, height = req["viewport"]
    endpoint, browser_path = tools.devtools_endpoint(profile, deadline)
    tools.verify_browser(endpoint, browser_path, deadline)  # the listener must be this browser
    result["version"] = tools.rea_version(binary, _ms(deadline) / 1000)
    page = ctx.new_page()
    response = page.goto(req["url"], wait_until="load", timeout=_ms(deadline))
    status = response.status if response else None
    _refuse_if_moved(status, req, page)
    origin = _document_origin(page)
    navigations = []
    page.on("domcontentloaded", lambda _page: navigations.append(True))
    _pause(page, SCROLL_WAIT_MS, deadline)
    _refuse_if_replaced(page, origin, navigations, status, req)
    scrollable = page.evaluate("document.documentElement.scrollHeight - innerHeight")
    targets = [round(scrollable * k / SCROLL_STEPS) for k in range(SCROLL_STEPS + 1)]
    url = page.url  # read here: the observer thread must not call Playwright
    target = tools.rea_target(binary, endpoint, url, _ms(deadline) / 1000)
    outcome = {}

    def observe():  # only REA's subprocess runs in this thread
        try:
            outcome["raw"] = tools.rea_observe(
                binary,
                endpoint=endpoint,
                target_id=target,
                url=url,
                window_ms=REA_WINDOW_MS,
                timeout_s=_ms(deadline) / 1000,
            )
        except Exception as exc:
            outcome["error"] = exc

    observer = threading.Thread(target=observe, daemon=True)
    observer.start()
    _pause(page, REA_ARM_MS, deadline)
    page.mouse.move(width / 2, height / 2)
    for k in range(1, SCROLL_STEPS + 1):
        _ms(deadline)  # no wheel move after the deadline
        _scroll_to(page, targets[k] - targets[k - 1])
        _pause(page, REA_STEP_MS, deadline)
    observer.join(max(deadline - time.monotonic(), 0))
    if observer.is_alive():
        raise tools.ToolError("timeout")
    if "error" in outcome:
        raise outcome["error"]
    _refuse_if_replaced(page, origin, navigations, status, req)
    return outcome["raw"]


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


def _capture_scroll(ctx, req, first_page):
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
    }


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
        "count": _num(raw.get("count")),
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


def _stop(_signum, _frame):
    """SIGTERM from the parent: with and finally blocks run, so browsers close and tools reap."""
    raise SystemExit(1)


def _worker():
    req = json.loads(sys.stdin.read())
    signal.signal(signal.SIGTERM, _stop)
    try:
        result = _capture_rea(req) if req.get("mode") == "rea" else _capture(req)
    except Exception as exc:  # Playwright and launch errors
        text = str(exc).strip()
        out: dict = {"error": text or repr(exc)}
        if isinstance(exc, MeasureError):  # a page refusal, not a setup failure
            out["refused"] = True
            out["status"], out["final_url"] = exc.status, exc.final_url
            out["screenshot"] = exc.screenshot
        print(json.dumps(out))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(_worker())
