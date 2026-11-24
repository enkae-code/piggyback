"""External tools for `piggyback kit`: REA observation and Craft Anything evidence.

Each tool is a child process: argv list, no shell, stdin closed, a minimal environment, a deadline
and capped stdout, killed and reaped on every path. Tool output is untrusted and is validated here.
Only fixed fields, numbers and the text of scripts REA saw running leave this module; that text is
kept as inert files and is never executed or rendered.
"""

import contextlib
import hashlib
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .measure import _host, publish_json

ENV_KEYS = ("PATH", "HOME")
CRAFT_TIMEOUT_S = 60
CRAFT_OUT_MAX = 1024 * 1024
REA_OUT_MAX = 32 * 1024 * 1024  # REA may return 32 MiB of sources; over the cap fails the stage
SOURCE_DIR = "rea/sources"
SOURCE_MAX = 1024 * 1024  # one script's text; a larger script is dropped with a reason
SOURCES_TOTAL_MAX = 8 * 1024 * 1024  # all kept script text together
MAX_SCRIPTS = 40
MAX_FUNCTIONS = 40
MAX_RANGES = 40
MAX_COUNT = 10**9
CHANGE = 0.01  # ponytail: 1% of pixels, a fixed threshold; tune on fixtures before trusting it.
TOLERANCE = 8
FUNCTION_NAME = re.compile(r"[A-Za-z_$][A-Za-z0-9_$.]{0,80}")
EVIDENCE_ID = re.compile(r"ev_[0-9a-f]{64}")
ENDPOINT = re.compile(r"http://127\.0\.0\.1:\d{1,5}")
PORT = re.compile(r"\d{1,5}")
BROWSER_PATH = re.compile(r"/devtools/browser/[A-Za-z0-9_-]{1,128}")
TARGET_ID = re.compile(r"[A-Za-z0-9]{1,128}")
ORIGIN = re.compile(r"https?://[A-Za-z0-9.-]{1,253}(:\d{1,5})?")
CRAFT_VERSION = re.compile(r"craft-anything-cli (\d+\.\d+\.\d+)")
REA_VERSION = re.compile(r"\d+\.\d+\.\d+")
REA_TARGETS_MAX = 1024 * 1024
MCP_DEADLINE_S = 60  # the whole Craft stage: a hung Craft is killed, never waited on
MCP_LINE_MAX = 1024 * 1024  # one JSON-RPC line from Craft
MCP_TOTAL_MAX = 16 * 1024 * 1024  # all Craft output in one stage
MCP_PROTOCOL = "2025-06-18"
MCP_TOOLS = frozenset(
    {"core_brief_write", "web_pixels_compare", "core_hypothesis_record", "core_handoff_write"}
)


class ToolError(RuntimeError):
    """A failed tool stage. reason is one fixed phrase, safe to publish in kit.json."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def find(env_name, name):
    """The tool's absolute path: env_name if set (an executable file), else PATH."""
    configured = os.environ.get(env_name, "")
    if configured:
        usable = os.path.isabs(configured) and os.path.isfile(configured)
        return configured if usable and os.access(configured, os.X_OK) else None
    found = shutil.which(name)
    return os.path.abspath(found) if found else None


def _spawn(argv, *, stdin, stdout):
    """A child with PATH and HOME only and stderr discarded, leading its own process group so a
    kill reaches whatever it starts. A start failure is a stage error."""
    env = {key: os.environ[key] for key in ENV_KEYS if key in os.environ}
    try:
        return subprocess.Popen(
            argv,
            stdin=stdin,
            stdout=stdout,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
    except OSError:
        raise ToolError("cannot start") from None


def _reap(proc):
    """Kill the child's whole process group unless it was already reaped, then wait for it: no
    path leaves a child or anything it started behind. Unreaped, the group id is still ours."""
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


def run(argv, *, timeout, cap):
    """Stdout of one child process, read while it runs. ToolError on timeout, on output over cap
    (the child is killed as soon as the cap is passed), on a nonzero exit or a start failure."""
    proc = _spawn(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
    deadline = time.monotonic() + timeout
    out = bytearray()
    done = threading.Event()

    def pump():
        try:
            while len(out) <= cap:
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                out.extend(chunk)
        except OSError:
            pass
        finally:
            proc.stdout.close()
            done.set()

    threading.Thread(target=pump, daemon=True).start()
    try:
        if not done.wait(max(deadline - time.monotonic(), 0)):
            raise ToolError("timeout")
        if len(out) > cap:
            raise ToolError("output too large")
        try:
            code = proc.wait(timeout=max(deadline - time.monotonic(), 0))
        except subprocess.TimeoutExpired:
            raise ToolError("timeout") from None
    finally:
        _reap(proc)
    if code != 0:
        raise ToolError("exit status")
    return bytes(out)


def _unsafe_entry(entry):
    """True for a link, a special file, or a file with another name (a hard link)."""
    if entry.is_symlink() or not (entry.is_file() or entry.is_dir()):
        return True
    return entry.is_file() and entry.stat().st_nlink > 1


def unsafe_tree(path):
    """True unless path is a real folder and everything under it is plain: a tool may have left a
    link out of the tree, or a file where a folder should be."""
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        return True
    return any(_unsafe_entry(entry) for entry in path.rglob("*"))


def remove_tree(path):
    """Remove a folder tree without following links. A link or file at path is unlinked itself."""
    if os.path.lexists(path) and (os.path.islink(path) or not os.path.isdir(path)):
        os.unlink(path)
        return
    shutil.rmtree(path, ignore_errors=True)


def _no_constant(name):
    raise ValueError(name)


def _json(raw):
    try:
        data = json.loads(raw, parse_constant=_no_constant)
    except (ValueError, RecursionError):  # bad text, non-finite numbers, nesting too deep
        raise ToolError("bad output") from None
    return _dict(data)


def _dict(value):
    if not isinstance(value, dict):
        raise ToolError("bad output")
    return value


def _list(value):
    if not isinstance(value, list):
        raise ToolError("bad output")
    return value


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _host_of(url):
    try:
        host = urlsplit(url).hostname if isinstance(url, str) else None
    except ValueError:
        host = None
    return _host(host) if host else "other"


def _clamp(value):
    if not _is_int(value):
        return 0
    return min(max(value, 0), MAX_COUNT)


def _function(raw):
    raw = _dict(raw)
    name = raw.get("name")
    if not (isinstance(name, str) and FUNCTION_NAME.fullmatch(name)):
        name = "(anonymous)"
    ranges = [_dict(r) for r in _list(raw.get("ranges", []))]
    counts = [_clamp(r.get("count")) for r in ranges]
    spans = [
        {"start": _clamp(r.get("start_offset")), "end": _clamp(r.get("end_offset"))} for r in ranges
    ]
    return {"name": name, "count": max(counts, default=0), "ranges": spans[:MAX_RANGES]}


def _script_id(raw):
    value = raw.get("script_id")
    if isinstance(value, str) or _is_int(value):
        return str(value)
    return None


def _script(raw):
    raw = _dict(raw)
    functions = [_function(f) for f in _list(raw.get("functions"))]
    ran = [f for f in functions if f["count"] > 0]
    ran.sort(key=lambda f: -f["count"])
    return {
        "script_id": _script_id(raw),
        "host": _host_of(raw.get("reported_url")),
        "functions": ran[:MAX_FUNCTIONS],
        "truncated": len(ran) > MAX_FUNCTIONS,
    }


def _texts_by_script(normalized):
    """script id -> text REA reported for it. Entries without an id or text are skipped."""
    texts = {}
    for entry in _list(normalized.get("sources", [])):
        entry = _dict(entry)
        source = _dict(entry.get("source", {}))
        script_id = _script_id(entry)
        if script_id is not None and isinstance(source.get("text"), str):
            texts.setdefault(script_id, source["text"])
    return texts


def _keep_source(text, index, total):
    """(source record, stored bytes or None). The file name is the index, never page-chosen."""
    if text is None:
        return {"dropped": "no text"}, None
    data = text.encode("utf-8", "replace")
    if len(data) > SOURCE_MAX:
        return {"dropped": "over cap"}, None
    if total + len(data) > SOURCES_TOTAL_MAX:
        return {"dropped": "total cap"}, None
    record = {
        "file": f"{SOURCE_DIR}/{index}.txt",
        "utf16_length": len(text.encode("utf-16-le", "replace")) // 2,
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    return record, data


def clean_rea(raw, version, window_ms):
    """(rea-observation.json content, [(relative path, bytes)]) from REA's stdout.

    Kept per script: host, sanitized function names with counts and clamped offset ranges, and
    the text of every script that ran. Dropped: request URLs, URL paths and every other REA string.
    truncated is true when a cap cut any entry.
    """
    normalized = _dict(_json(raw).get("normalized_result"))
    texts = _texts_by_script(normalized)
    scripts = [_script(s) for s in _list(_dict(normalized.get("coverage")).get("scripts"))]
    scripts = [s for s in scripts if s["functions"]]
    scripts.sort(key=lambda s: -sum(f["count"] for f in s["functions"]))
    truncated = len(scripts) > MAX_SCRIPTS
    observed, files, total = [], [], 0
    for script in scripts[:MAX_SCRIPTS]:
        truncated = truncated or script.pop("truncated")
        record, data = _keep_source(texts.get(script.pop("script_id")), len(files), total)
        if data is not None:
            files.append((record["file"], data))
            total += len(data)
        observed.append({**script, "source": record})
    observation = {
        "tool": "rea",
        "version": version,
        "state": "captured",
        "window_ms": window_ms,
        "scripts": observed,
        "requests": len(_list(normalized.get("requests"))),
        "truncated": truncated,
    }
    return observation, files


def publish_rea(folder, raw, version, window_ms):
    """Create rea/ and rea/sources/ exclusively, write the kept sources and rea-observation.json."""
    observation, files = clean_rea(raw, version, window_ms)
    root = Path(folder)
    (root / "rea").mkdir()
    (root / "rea" / "sources").mkdir()
    for name, data in files:
        (root / name).write_bytes(data)
    publish_json(root / "rea-observation.json", observation)
    return {"state": "captured", "version": version}


def _origin(url):
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    if not ORIGIN.fullmatch(origin):
        raise ValueError("bad rea arguments")
    return origin


def rea_version(binary, timeout_s):
    text = run([binary, "--version"], timeout=timeout_s, cap=4096).decode("utf-8", "replace")
    text = text.strip()
    if not REA_VERSION.fullmatch(text):
        raise ToolError("bad output")
    return text


def rea_target(binary, endpoint, url, timeout_s):
    """The one page target at url, from REA's list. Zero or several matches fail the stage."""
    if not ENDPOINT.fullmatch(endpoint):
        raise ValueError("bad rea arguments")
    raw = run(
        [binary, "list-browser-targets", endpoint, "--format", "json"],
        timeout=timeout_s,
        cap=REA_TARGETS_MAX,
    )
    targets = _list(_dict(_json(raw).get("normalized_result")).get("targets"))
    pages = [t for t in targets if isinstance(t, dict) and t.get("url") == url]
    pages = [t for t in pages if t.get("type") == "page"]
    if len(pages) != 1:
        raise ToolError("no target")
    target_id = pages[0].get("target_id")
    if not (isinstance(target_id, str) and TARGET_ID.fullmatch(target_id)):
        raise ToolError("bad output")
    return target_id


def rea_observe(binary, *, endpoint, target_id, url, window_ms, timeout_s):
    """REA's observation stdout for one page. The endpoint is the run's own loopback CDP."""
    if not (ENDPOINT.fullmatch(endpoint) and TARGET_ID.fullmatch(target_id)):
        raise ValueError("bad rea arguments")
    argv = [
        binary,
        "observe-web-execution",
        endpoint,
        target_id,
        "--observation-ms",
        str(int(window_ms)),
        "--allowed-origins",
        _origin(url),
        "--format",
        "json",
    ]
    return run(argv, timeout=timeout_s, cap=REA_OUT_MAX)


def devtools_endpoint(profile, deadline):
    """(endpoint, browser path) from the DevToolsActivePort that Chromium wrote into its profile.

    Polls until the stage deadline (monotonic); a file still missing then is a timeout.
    ponytail: the file is read once it exists; a half-written file reads as bad output.
    """
    path = Path(profile) / "DevToolsActivePort"
    while not path.exists():
        left = deadline - time.monotonic()
        if left <= 0:
            raise ToolError("timeout")
        time.sleep(min(0.1, left))
    try:
        lines = path.read_text(encoding="utf-8").split("\n")
    except (OSError, UnicodeDecodeError):
        raise ToolError("bad output") from None
    if len(lines) < 2 or not PORT.fullmatch(lines[0]) or not BROWSER_PATH.fullmatch(lines[1]):
        raise ToolError("bad output")
    port = int(lines[0])
    if not 0 < port <= 65535:
        raise ToolError("bad output")
    return f"http://127.0.0.1:{port}", lines[1]


def verify_browser(endpoint, path, deadline):
    """Raise unless the listener on endpoint is the browser that wrote path (loopback, no proxy)."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise ToolError("timeout")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"{endpoint}/json/version", timeout=min(5, left)) as reply:
            data = json.loads(reply.read(65536), parse_constant=_no_constant)
    except (OSError, ValueError, RecursionError):
        raise ToolError("not our browser") from None
    url = data.get("webSocketDebuggerUrl") if isinstance(data, dict) else None
    port = endpoint.rsplit(":", 1)[1]
    if url != f"ws://127.0.0.1:{port}{path}":
        raise ToolError("not our browser")


def craft_version(binary):
    text = run([binary, "--version"], timeout=CRAFT_TIMEOUT_S, cap=4096).decode("utf-8", "replace")
    match = CRAFT_VERSION.search(text)
    if not match:
        raise ToolError("bad output")
    return match[1]


class _McpSession:
    """One Craft MCP stdio session: a fixed tool allowlist, one child, one deadline.

    Craft keeps evidence in the process that wrote it, so every call of a kit runs in this session.
    The child is reaped on every exit: a stage that fails at start, mid-call or at close.
    ponytail: one reader thread and one writer thread per message; fine for one short run.
    """

    def __init__(self, binary, project):
        self.binary = binary
        self.project = str(project)
        self.proc = None
        self.lines = queue.Queue()
        self.total = 0
        self.next_id = 1
        self.deadline = time.monotonic() + MCP_DEADLINE_S

    def __enter__(self):
        try:
            self.proc = _spawn(
                [self.binary, "--project", self.project, "mcp"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
            )
            threading.Thread(target=self._read, daemon=True).start()
            self._request(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL,
                    "capabilities": {},
                    "clientInfo": {"name": "piggyback", "version": __version__},
                },
            )
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except BaseException:
            if self.proc is not None:
                self._stop()
            raise
        return self

    def __exit__(self, *_):
        self._stop()

    def _stop(self):
        """Kill and reap the child, then close its pipes. Always a kill: a graceful close would wait
        on a stdin write still blocked in another thread. Craft writes each evidence file before its
        reply, so nothing is lost.
        ponytail: a kill at exit; add a graceful shutdown if Craft defers writes."""
        _reap(self.proc)
        with contextlib.suppress(OSError):
            self.proc.stdin.close()
        self.proc.stdout.close()

    def _left(self):
        return max(self.deadline - time.monotonic(), 0)

    def _read(self):
        """Each stdout line goes on the queue. Reads stop at the line cap and at the total cap."""
        try:
            while True:
                line = self.proc.stdout.readline(MCP_LINE_MAX + 1)
                if not line:
                    break
                if len(line) > MCP_LINE_MAX:  # readline stopped at the cap: no newline came
                    self.lines.put(ToolError("output too large"))
                    return
                self.total += len(line)  # charged as the bytes arrive, not after queueing
                if self.total > MCP_TOTAL_MAX:
                    self.lines.put(ToolError("output too large"))
                    return
                self.lines.put(line)
        except (OSError, ValueError):  # stdout closed under us: the session is over
            pass
        self.lines.put(None)

    def _send(self, message):
        data = (json.dumps(message) + "\n").encode("utf-8")
        failed = []

        def write():
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (OSError, ValueError):
                failed.append(True)

        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        writer.join(self._left())
        if writer.is_alive():
            raise ToolError("timeout")
        if failed:
            raise ToolError("craft error")

    def _request(self, method, params):
        request_id = self.next_id
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            try:
                line = self.lines.get(timeout=self._left())
            except queue.Empty:
                raise ToolError("timeout") from None
            if isinstance(line, ToolError):
                raise line
            if line is None:
                raise ToolError("craft error")
            reply = _json(line)
            if not _is_int(reply.get("id")) or reply["id"] != request_id:
                continue
            if "error" in reply:
                raise ToolError("craft error")
            return _dict(reply.get("result"))

    def tool(self, name, arguments):
        """The structured result of one allowlisted tool, validated as an object."""
        if name not in MCP_TOOLS:
            raise ValueError("tool not allowed")
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError") is True:
            raise ToolError("craft error")
        if "structuredContent" in result:
            return _dict(result["structuredContent"])
        content = _list(result.get("content"))
        text = content[0].get("text") if content and isinstance(content[0], dict) else None
        if not isinstance(text, str):
            raise ToolError("bad output")
        return _json(text)


def _changed_box(result, fraction):
    """The changed bounding box, or None for identical frames (Craft sends null there)."""
    box = result.get("changed_bounding_box")
    if box is None:
        if fraction > 0:
            raise ToolError("bad output")
        return None
    box = _dict(box)
    if not all(_is_int(box.get(key)) for key in ("x", "y", "width", "height")):
        raise ToolError("bad output")
    return box


def _compare(found):
    """(evidence id, difference fraction, changed box or None) from web-pixels-compare."""
    evidence = found.get("id")
    result = _dict(found.get("result"))
    if not (isinstance(evidence, str) and EVIDENCE_ID.fullmatch(evidence)):
        raise ToolError("bad output")
    if result.get("comparable") is not True:
        raise ToolError("not comparable")
    fraction = result.get("difference_fraction")
    if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
        raise ToolError("bad output")
    if not 0 <= fraction <= 1:
        raise ToolError("bad output")
    return evidence, float(fraction), _changed_box(result, fraction)


def craft_steps(binary, folder, host, stops):
    """Compare each stop's arrival frame with its late frame in one Craft MCP session.

    stops: (step, scroll_y, arrival_png, late_png). Returns the step numbers whose frames changed.
    Creates folder exclusively, with Craft's project folder/recon.cany and folder/HANDOFF.md.
    """
    try:
        folder.mkdir()
    except OSError:  # something is already at the root: a link or a file, never a folder to use
        raise ToolError("cannot start") from None
    project = folder / "recon.cany"
    changed, lines = [], []
    with _McpSession(binary, project) as mcp:
        mcp.tool(
            "core_brief_write",
            {
                "question": f"What changes after arriving at each scroll stop on {host}?",
                "purpose": "motion recon for a build kit",
                "targets": [host],
                "constraints": ["offline", "frame pixels only"],
                "done_when": "every scroll stop has an arrival frame and a late frame compared",
            },
        )
        for step, scroll_y, arrival, late in stops:
            found = mcp.tool(
                "web_pixels_compare",
                {
                    "before": {"path": str(arrival)},
                    "after": {"path": str(late)},
                    "tolerance": TOLERANCE,
                },
            )
            evidence, fraction, box = _compare(found)
            lines.append(f"- stop {step} (scroll y {scroll_y}): {fraction:.2%} of pixels changed")
            if fraction < CHANGE:
                continue
            changed.append(step)
            mcp.tool(
                "core_hypothesis_record",
                {
                    "claim": (
                        f"Stop {step} (scroll y {scroll_y}) changes after arrival in box "
                        f"x={box['x']} y={box['y']} w={box['width']} h={box['height']}"
                    ),
                    "prediction": "the late frame differs from the arrival frame at that scroll",
                    "evidence_for": [evidence],
                },
            )
        text = "Stops that change after arriving (pixel comparison, not a motion oracle):\n" + (
            "\n".join(lines) + "\n"
        )
        mcp.tool("core_handoff_write", {"text": text})
    # Validate before anything is written or removed: Craft may have left links that point outside.
    if unsafe_tree(folder):
        raise ToolError("unsafe output")
    # The journal records every call's params, absolute frame paths included: it is not published.
    remove_tree(project / "journal")
    _write_exclusive(folder / "HANDOFF.md", text)
    return changed


def _write_exclusive(path, text):
    """A new file with mode 0600, never through a link: O_EXCL refuses any existing name."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError:
        raise ToolError("craft error") from None
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
