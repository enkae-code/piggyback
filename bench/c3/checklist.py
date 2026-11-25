"""Frozen C3 acceptance checklist: four checks, run in this order, on one page.

1. render: passes when the body has non-empty rendered text. Uncaught page errors and console
   errors are recorded and never fail on their own; a failed render names the first page error.
2. content: each predicate of the task, read from the rendered DOM (see tasks.json, predicates.py).
3. overflow: at 1440 px, then 390 px, after the page settles: scrollWidth must not exceed the
   viewport, and no rendered element may end more than 1 px past the viewport edge unless an
   ancestor with overflow hidden or clip clips it.
4. axe: axe-core, tags wcag2a and wcag2aa, run at 1440 px. Violations fail. Incomplete results
   are recorded and never fail.

Outcomes: pass (exit 0), fail (exit 1, one correction message), no_runnable_output (exit 1, no
correction message: the page never reached a state we can check), checker_fault (exit 2: a bad
target or task, no bundled Chromium, or a bug here). Every outcome prints the JSON report.
The checks run in a worker process killed after TOTAL_S: a page that blocks the browser
gives no_runnable_output with detail "checklist timed out".

Only file paths and loopback http(s) URLs are loaded. The browser goes through TargetProxy, which
connects only to the target's own origin; route() checks the page's requests as a second layer.
A file target has no origin, so it reaches no http(s) host at all.

Usage: PYTHONPATH=. python bench/c3/checklist.py PAGE --task T01
"""

import argparse
import contextlib
import ipaddress
import json
import os
import signal
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

from bench.c3.predicates import evaluate_predicate
from piggyback import netpolicy
from piggyback.browser import ARGS, NO_WEBRTC
from piggyback.measure import BYPASS_ENV
from piggyback.proxy import FORBIDDEN, EgressProxy

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
TASKS_PATH = HERE / "tasks.json"
AXE_PATH = HERE / "vendor" / "axe-core" / "axe.min.js"
AXE_VERSION = "4.14.1"
AXE_TAGS = ["wcag2a", "wcag2aa"]
OVERFLOW_WIDTHS = (1440, 390)
HEIGHT = 900
NAV_MS = 30_000
SETTLE_MS = 5_000
TOTAL_S = 90  # whole worker run, browser start included, as in measure.py
WINDOWS = os.name == "nt"
PASS, FAIL, NO_RUNNABLE, FAULT = "pass", "fail", "no_runnable_output", "checker_fault"
EXIT_CODES = {PASS: 0, FAIL: 1, NO_RUNNABLE: 1, FAULT: 2}
CONTROL_ROLES = (
    "button",
    "link",
    "textbox",
    "combobox",
    "checkbox",
    "radio",
    "switch",
    "tab",
    "menuitem",
)
RENDER_ITEM = "page renders: the body must show rendered text"
OVERFLOW_ITEM = (
    "horizontal overflow at {width} px: the page must not scroll sideways, and no element may "
    "end past the {width} px viewport edge unless an overflow hidden or clip ancestor clips it"
)
MESSAGE = (
    "The page failed these checklist items:\n{items}\n"
    "Fix each listed item and return the complete page."
)
SNAPSHOT_JS = """() => {
  const nodes = [];
  const visible = (el) => {
    const s = getComputedStyle(el);
    return s.display !== "none" && s.visibility !== "hidden" && el.getClientRects().length > 0;
  };
  const walk = (el, path) => {
    const index = nodes.length;
    const attrs = {};
    for (const a of el.attributes) attrs[a.name] = a.value;
    nodes.push({
      tag: el.tagName.toLowerCase(),
      attrs,
      visible: visible(el),
      path,
      text: (el.innerText || "").trim(),
    });
    for (const child of el.children) walk(child, [...path, index]);
  };
  walk(document.body, []);
  return { text: document.body.innerText, nodes };
}"""
OVERFLOW_JS = """async () => {
  // Settle: animations finish, or 1000 ms for pages that never go idle; then two frames.
  const finished = Promise.all(document.getAnimations().map((a) => a.finished.catch(() => null)));
  await Promise.race([finished, new Promise((done) => setTimeout(done, 1000))]);
  await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
  const width = window.innerWidth;
  // A fixed element is never clipped by an ancestor, and a fixed ancestor ends the walk: its own
  // overflow still clips its subtree, nothing above it does. An absolute ancestor ends the clip
  // chain too: an absolute element is clipped only by overflow ancestors at or above its containing
  // block, the nearest positioned ancestor.
  // ponytail: transform and filter ancestors make a fixed containing block; not modelled, so a
  // fixed element counts as unclipped there too.
  const clips = (s) => [s.overflowX, s.overflowY].some((v) => v === "hidden" || v === "clip");
  const clipped = (el) => {
    const position = getComputedStyle(el).position;
    if (position === "fixed") return false;
    let inside = position !== "absolute";
    for (let p = el.parentElement; p; p = p.parentElement) {
      const s = getComputedStyle(p);
      if (!inside && s.position !== "static") inside = true;
      if (inside && clips(s)) return true;
      if (s.position === "fixed") return false;
      if (s.position === "absolute") inside = false;
    }
    return false;
  };
  const offenders = [];
  for (const el of document.body.querySelectorAll("*")) {
    const s = getComputedStyle(el);
    if (s.display === "none" || s.visibility === "hidden") continue;
    if (el.getClientRects().length === 0) continue;
    if (el.getBoundingClientRect().right > width + 1 && !clipped(el)) {
      offenders.push(el.tagName.toLowerCase());
    }
  }
  return {
    scroll_width: document.documentElement.scrollWidth,
    client_width: document.documentElement.clientWidth,
    offenders: offenders.slice(0, 5),
    offender_count: offenders.length,
  };
}"""
AXE_RUN_JS = """async (tags) => {
  const r = await axe.run(document, { runOnly: { type: "tag", values: tags } });
  const brief = (v) => ({ id: v.id, help: v.help, impact: v.impact, nodes: v.nodes.length });
  return { violations: r.violations.map(brief), incomplete: r.incomplete.map(brief) };
}"""


def is_loopback_host(host):
    # A literal IP only: a name would need DNS, and DNS is a lookup we do not make here.
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def origin_of(url):
    """(host, port) of an http or https URL, or None for anything else (file, data, ...)."""
    try:
        parts = urlsplit(url)
        if parts.scheme not in netpolicy.SCHEMES or not parts.hostname:
            return None
        return parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return None


def _origin_url(origin):
    host, port = origin
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


class TargetProxy(EgressProxy):
    """The enforcement point for every connection Chromium makes (http, ws, CONNECT, preconnect).

    Only the target's own (host, port) is connected to. A file target has no origin, so nothing
    is connected to at all. `shown` is the request target for the record, never parsed here."""

    def __init__(self, origin):
        self.origin = origin
        # The base proxy then reaches loopback only at this target's own address and port.
        super().__init__(loopback=None if origin is None else netpolicy.origin(_origin_url(origin)))

    def _open(self, client, host, port, shown):
        if self.origin is None or (host.lower(), port) != self.origin:
            self.record(shown)
            client.sendall(FORBIDDEN)
            return None
        return super()._open(client, host, port, shown)


def target_url(target):
    if Path(target).is_file():
        return Path(target).resolve().as_uri()
    url = netpolicy.local_name(target)
    parts = urlsplit(url)
    if parts.scheme not in netpolicy.SCHEMES or not is_loopback_host(parts.hostname):
        raise ValueError(f"refusing non-loopback target: {target}")
    return url


def allowed_request(url, origin):
    if urlsplit(url).scheme in ("file", "data", "blob"):
        return True
    return origin is not None and origin_of(url) == origin


def first_line(text):
    lines = str(text).strip().splitlines()
    return lines[0] if lines else repr(text)


def render_item(page_errors):
    if not page_errors:
        return RENDER_ITEM
    return f"{RENDER_ITEM}; first page error: {first_line(page_errors[0])}"


def has_horizontal_overflow(scroll_width, client_width):
    return scroll_width > client_width


def failed_items(checks):
    items = []
    if not checks["render"]["pass"]:
        items.append(render_item(checks["render"]["page_errors"]))
    items += [
        f"content item missing: {c['item']} ({c['rule']})"
        for c in checks["content"]
        if not c["pass"]
    ]
    items += [OVERFLOW_ITEM.format(width=o["width"]) for o in checks["overflow"] if not o["pass"]]
    axe = checks["axe"]
    if axe["error"] is not None:
        items.append(f"axe-core could not run: {axe['error']}")
    items += [f"axe-core violation {v['id']}: {v['help']}" for v in axe["violations"]]
    return items


def correction_message(failed):
    if not failed:
        return None
    return MESSAGE.format(items="\n".join(f"- {item}" for item in failed))


def guard(route, origin, egress):
    # Second layer for page requests, as in measure.py; the proxy is authoritative. Refusals are
    # recorded in the same list as proxy refusals.
    if allowed_request(route.request.url, origin):
        route.continue_()
    else:
        egress.record(route.request.url)
        route.abort("blockedbyclient")


def predicates_for(task_id):
    tasks = json.loads(TASKS_PATH.read_text(encoding="utf-8"))["tasks"]
    matches = [t for t in tasks if t["id"] == task_id]
    if len(matches) != 1:
        raise ValueError(f"unknown task: {task_id}")
    return matches[0]["predicates"]


def control_names(page):
    session = page.context.new_cdp_session(page)
    nodes = session.send("Accessibility.getFullAXTree")["nodes"]
    return [
        node["name"]["value"]
        for node in nodes
        if node.get("role", {}).get("value") in CONTROL_ROLES
        and node.get("name", {}).get("value")
        and not node.get("ignored")
    ]


def run_axe(page):
    try:
        page.add_script_tag(path=str(AXE_PATH))
        result = page.evaluate(AXE_RUN_JS, AXE_TAGS)
    except Exception as exc:  # fails closed; keep the first line of the error
        page.evaluate("1")  # raises again when the renderer crashed: nothing was checked, so raise
        return {
            "version": AXE_VERSION,
            "tags": AXE_TAGS,
            "violations": [],
            "incomplete": [],
            "error": first_line(exc),
            "pass": False,
        }
    return {
        "version": AXE_VERSION,
        "tags": AXE_TAGS,
        **result,
        "error": None,
        "pass": not result["violations"],
    }


class NotRunnable(Exception):
    """The main document is missing, refused by the proxy, or an HTTP error: nothing to check."""

    def __init__(self, detail, main):
        super().__init__(detail)
        self.main = main


def main_document(response, url):
    """Status and final URL of the main document. Raises NotRunnable unless it is usable.

    A file target gets no HTTP response from Chromium, so a missing response is not a failure there.
    """
    if response is None:
        if urlsplit(url).scheme == "file":
            return {"status": None, "final_url": url}
        raise NotRunnable("no response for the main document", {"status": None, "final_url": None})
    main = {"status": response.status, "final_url": response.url}
    if response.status >= 400:
        raise NotRunnable(f"main document answered HTTP {response.status}", main)
    return main


def observe(page, url):
    """Everything that touches the page. Any exception raised here means no runnable output."""
    main = main_document(page.goto(url, wait_until="load", timeout=NAV_MS), url)
    with contextlib.suppress(Exception):
        page.wait_for_load_state("networkidle", timeout=SETTLE_MS)
    snap = page.evaluate(SNAPSHOT_JS)
    snap["controls"] = control_names(page)
    overflow = []
    for width in OVERFLOW_WIDTHS:
        page.set_viewport_size({"width": width, "height": HEIGHT})
        overflow.append({"width": width, **page.evaluate(OVERFLOW_JS)})
    page.set_viewport_size({"width": 1440, "height": HEIGHT})
    return main, snap, overflow, run_axe(page)


def build_checks(snap, overflow, axe, predicates, page_errors, console_errors):
    return {
        "render": {
            "pass": bool(snap["text"].strip()),
            "page_errors": page_errors,
            "console_errors": console_errors,
        },
        "content": [evaluate_predicate(pred, snap) for pred in predicates],
        "overflow": [
            {
                **o,
                "pass": not has_horizontal_overflow(o["scroll_width"], o["client_width"])
                and o["offender_count"] == 0,
            }
            for o in overflow
        ],
        "axe": axe,
    }


def report_for(url, task_id, outcome, failed=(), **fields):
    failed = list(failed)
    return {
        "target": url,
        "task": task_id,
        "outcome": outcome,
        "pass": outcome == PASS,
        "failed": failed,
        "correction_message": correction_message(failed) if outcome == FAIL else None,
        **fields,
    }


def run_checklist(target, task_id):
    url = target_url(target)
    predicates = predicates_for(task_id)
    origin = origin_of(url)
    # Imported here so this module loads, and its pure checks run, without Playwright.
    from playwright.sync_api import sync_playwright

    # Playwright's bypass switch would let Chromium skip the proxy for loopback.
    saved_bypass = os.environ.pop(BYPASS_ENV, None)
    try:
        with sync_playwright() as p, closing(TargetProxy(origin)) as egress:
            # Only the bundled Chromium, with no channel fallback: one pinned build.
            browser = p.chromium.launch(
                chromium_sandbox=True,
                args=ARGS,
                proxy={"server": f"http://127.0.0.1:{egress.port}"},
            )
            version = browser.version
            browser_info = {
                "browser": "bundled chromium",
                "browser_version": version,
                "browser_executable": p.chromium.executable_path,
            }
            try:
                ctx = browser.new_context(
                    viewport={"width": 1440, "height": HEIGHT},
                    service_workers="block",
                    accept_downloads=False,
                    bypass_csp=True,
                )
                ctx.route("**/*", lambda route: guard(route, origin, egress))
                ctx.add_init_script(NO_WEBRTC)
                page = ctx.new_page()
                page_errors, console_errors = [], []
                page.on("pageerror", lambda exc: page_errors.append(str(exc)))
                page.on(
                    "console",
                    lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
                )
                try:
                    main, snap, overflow, axe = observe(page, url)
                except Exception as exc:  # navigation timeout, not runnable, context destroyed
                    return report_for(
                        url,
                        task_id,
                        NO_RUNNABLE,
                        detail=first_line(exc),
                        main_document=getattr(exc, "main", None),
                        blocked_requests=egress.blocked,
                        **browser_info,
                    )
                checks = build_checks(snap, overflow, axe, predicates, page_errors, console_errors)
            finally:
                browser.close()
            failed = failed_items(checks)
            return report_for(
                url,
                task_id,
                FAIL if failed else PASS,
                failed=failed,
                checks=checks,
                main_document=main,
                blocked_requests=egress.blocked,
                **browser_info,
            )
    finally:
        if saved_bypass is not None:
            os.environ[BYPASS_ENV] = saved_bypass


def fault_report(target, task_id, exc):
    return report_for(target, task_id, FAULT, detail=first_line(exc))


def checked_in_process(target, task_id):
    try:
        return run_checklist(target, task_id)
    except Exception as exc:  # a bad target or task, no bundled Chromium, or a bug here
        return fault_report(target, task_id, exc)


def _kill_tree(pid):
    """Kill the worker and what it spawned: its process group, or its process tree on Windows."""
    if WINDOWS:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
    else:
        os.killpg(pid, signal.SIGKILL)


def run_bounded(target, task_id, total_s=TOTAL_S):
    """Run the checks in a worker process, so a page that blocks the browser cannot hang us.

    The worker leads its own process group, so the kill also reaches Chromium and its renderer.
    """
    if Path(target).is_file():
        # The worker runs in REPO: resolve a relative file path in the caller's cwd first.
        target = str(Path(target).resolve())
    worker = subprocess.Popen(
        [sys.executable, "-m", "bench.c3.checklist", "--worker", target, "--task", task_id],
        cwd=REPO,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        start_new_session=True,
    )
    try:
        out, err = worker.communicate(timeout=total_s)
    except subprocess.TimeoutExpired:
        _kill_tree(worker.pid)
        worker.communicate()
        return report_for(target, task_id, NO_RUNNABLE, detail="checklist timed out")
    except BaseException:  # Ctrl-C or SIGTERM: never leave Chromium orphaned
        with contextlib.suppress(ProcessLookupError):
            _kill_tree(worker.pid)
        raise
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return fault_report(
            target, task_id, RuntimeError(err.strip() or "worker output was not JSON")
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the frozen C3 checklist on one page.")
    parser.add_argument("target", help="file path or loopback http(s) URL")
    parser.add_argument("--task", required=True, help="task id, T01 to T10")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    check = checked_in_process if args.worker else run_bounded
    report = check(args.target, args.task)
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return EXIT_CODES[report["outcome"]]


if __name__ == "__main__":
    sys.exit(main())
