"""`piggyback mcp`: read-only MCP server on stdio, for the agent's own self-check.

Implements MCP protocol version 2025-06-18 (https://modelcontextprotocol.io/specification/2025-06-18:
lifecycle, tools) as newline-delimited JSON-RPC 2.0. stdout carries protocol messages only; logs go
to stderr. The URLs are fixed at startup by the human, and no tool takes a URL, a path or free text.
"""

import json
import re
import traceback

from . import __version__, server, starters
from . import brief as brief_mod
from . import defaults as defaults_mod
from . import fingerprint as fp
from . import gap as gap_mod
from . import kit as kit_mod
from .glossary import TERMS
from .measure import MeasureError, measure

PROTOCOL = "2025-06-18"  # the only version implemented, so initialize always answers with it
MAX_LINE = 64 * 1024
PARSE, INVALID, NOT_FOUND, BAD_PARAMS = -32700, -32600, -32601, -32602
FRAMING = brief_mod.FRAMING
TOOLS = [
    {
        "name": "get_brief",
        "description": "The measured design brief for the configured reference pages.",
        "inputSchema": {
            "type": "object",
            "properties": {"refresh": {"type": "boolean", "description": "Measure again."}},
            "additionalProperties": False,
        },
    },
    {
        "name": "check",
        "description": "Measure the configured build and list how it differs from the references.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_design_md",
        "description": "DESIGN.md for the first configured reference page (Impeccable reads it).",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_starters",
        "description": "The packaged starter references: their starter:<id> names and licences.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]
ARGUMENTS = {
    "get_brief": {"refresh"},
    "check": set(),
    "get_design_md": set(),
    "list_starters": set(),
}
KIT_TOOLS = [  # only in kit mode (piggyback mcp --kit)
    {
        "name": "get_kit",
        "description": "Readiness of the frozen kit: its files, and short summaries of its brief.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]
KIT_ARGUMENTS = {"get_kit": set()}
PIGGYBACK_VERSION = re.compile(r"\d+\.\d+\.\d+([.-][0-9A-Za-z]+)?", re.ASCII)
KIT_CHECK_IMPECCABLE = "not run (read-only check)"
REFRESH_HINT = "call get_brief with refresh: true to measure again"


class RefFailed(MeasureError):
    """A reference capture that failed earlier: measured again only by refresh."""


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


class Session:
    """The references are measured once per process; the build is measured on every check."""

    def __init__(self, refs, build, measure_page=measure, kit=None):
        self.refs, self.build = refs, build
        self.measure_page = measure_page
        # The frozen kit folder in kit mode: the reference is read from it, never measured.
        self.kit = kit
        self.kit_pin = None  # kit.json's digest at startup: a later rewrite is refused
        if kit is not None:
            files = kit_mod.verified(kit, kit_mod.FROZEN)  # the one read: every pin and summary
            kit_mod.readiness(kit, files)  # refuses a kit it cannot serve, before any call
            self.kit_pin = kit_mod.digest(files["kit.json"])
        self.ref_pages = None
        self.ref_failure = None  # the error of a reference capture that failed, until refresh
        self.differing = set()  # concepts that differed at the previous check
        self.kit_attempts = 0

    def _refs(self, refresh=False):
        if refresh:
            self.ref_failure = None
        if self.ref_failure is not None:
            raise RefFailed(str(self.ref_failure))
        if self.ref_pages is None or refresh:
            try:
                self.ref_pages = [self._read(url) for url in self.refs]
            except MeasureError as exc:
                self.ref_failure = exc
                raise
            self.differing = set()  # a moved reference is not a fix
        return self.ref_pages

    def _read(self, url):
        # A starter is read from the package: the agent can name one, never fetch a page.
        if starters.is_starter(url):
            return starters.fingerprint(url)
        return self.measure_page(url)

    def _frozen(self):
        # Every kit-mode call checks the digests and the startup pin: the folder is agent-writable.
        files = kit_mod.verified(self.kit, kit_mod.FROZEN)
        if kit_mod.digest(files["kit.json"]) != self.kit_pin:
            raise ValueError(
                "kit.json changed since the session started: this kit can't be checked"
            )
        return files

    def get_brief(self, refresh):
        if self.kit is not None:
            if refresh:
                raise ValueError("the reference is frozen in kit mode: refresh is not available")
            return kit_mod.utf8_text(self._frozen()["BRIEF.md"], "BRIEF.md")
        return brief_mod.brief(self._refs(refresh))

    def get_design_md(self):
        if self.kit is not None:
            return kit_mod.utf8_text(self._frozen()["DESIGN.md"], "DESIGN.md")
        if not self.refs:
            raise ValueError("no reference configured: start the server with a reference URL")
        return brief_mod.design_md(self._refs()[0])

    def get_kit(self):
        return kit_summary(kit_mod.readiness(self.kit, self._frozen()))

    def _kit_check(self):
        files = self._frozen()
        ref = kit_mod.parse_fingerprint(files["fingerprint.json"])
        viewport = kit_mod.ref_viewport(ref)
        if self.kit_attempts >= kit_mod.CAP:
            raise ValueError(kit_mod.CAP_REACHED)
        allow = server._on_this_machine(self.build)
        try:
            build = self.measure_page(self.build, viewport=viewport, allow_loopback=allow)
        except Exception as exc:  # the error's text may hold a path: a fixed sentence instead
            raise ValueError(kit_mod.measure_message(exc)) from None
        self.kit_attempts += 1
        found = kit_mod.compare(ref, build, self.differing)
        attempt = {
            "n": self.kit_attempts,
            **found,
            "impeccable": KIT_CHECK_IMPECCABLE,
            "warnings": [],
        }
        self.differing = set(found["open"])
        return kit_mod.attempt_text(attempt) + kit_mod.stop_line(attempt) + "\n"

    def check(self):
        if self.build is None:
            raise ValueError("no build configured: start the server with --build")
        if self.kit is not None:
            return self._kit_check()
        build = self.measure_page(self.build, allow_loopback=server._on_this_machine(self.build))
        if not self.refs:
            return defaults_mod.render(defaults_mod.find(build))
        ref = self._refs()[0]
        pins = gap_mod.gap(ref, build)
        differing = gap_mod.differing(pins)
        fixed = gap_mod.fixed(self.differing, differing, ref, build)
        self.differing = differing
        text = gap_mod.render(pins) + f"\n{server.NOTE}\n"
        if fixed:
            names = "\n".join(f"- {TERMS[concept][0]}" for concept in sorted(fixed))
            text += f"\n## Fixed since the last check\n\n{names}\n"
        hits = defaults_mod.find(build)
        return text + (f"\n{defaults_mod.render(hits)}" if hits else "")


def _text(text, error=False):
    return {"content": [{"type": "text", "text": f"{FRAMING}\n\n{text}"}], "isError": error}


def _tables(session):
    """(tools, arguments) for this session: get_kit exists only in kit mode."""
    if session.kit is None:
        return TOOLS, ARGUMENTS
    return TOOLS + KIT_TOOLS, ARGUMENTS | KIT_ARGUMENTS


def kit_summary(info):
    """get_kit's text from kit_mod.readiness: structured fields only, never kit text or a path."""
    piggyback = info["piggyback"]
    if not (
        isinstance(piggyback, str)
        and len(piggyback) <= 40
        and PIGGYBACK_VERSION.fullmatch(piggyback)
    ):
        piggyback = "unknown"
    schema = info["schema"] if type(info["schema"]) is int else "unknown"
    lines = [
        "Kit ready: fingerprint.json, DESIGN.md, BRIEF.md and MOTION.md match kit.json.",
        f"Piggyback {piggyback}, kit schema {schema}.",
        f"Other files present: {', '.join(info['artifacts']) or 'none'}.",
        f"BRIEF.md: {info['brief_lines']} lines, {info['brief_bytes']} bytes.",
        f"MOTION.md: {info['motion_rows']} rows.",
        f"Latest CHECK.md: {info['check'] or 'none yet'}.",
    ]
    return "\n".join(lines) + "\n"


def _call(session, params):
    name, args = params.get("name"), params.get("arguments", {})
    _, arguments = _tables(session)
    if not isinstance(name, str) or name not in arguments:
        raise RpcError(BAD_PARAMS, "unknown tool")
    if not isinstance(args, dict) or set(args) - arguments[name]:
        raise RpcError(
            BAD_PARAMS, "arguments must be an object with the tool's own parameters only"
        )
    refresh = args.get("refresh", False)
    if not isinstance(refresh, bool):
        raise RpcError(BAD_PARAMS, "refresh must be true or false")
    try:
        handlers = {
            "get_brief": lambda: session.get_brief(refresh),
            "check": session.check,
            "get_design_md": session.get_design_md,
            "list_starters": starters.listing,
            "get_kit": session.get_kit,
        }
        return _text(handlers[name]())
    except RefFailed as exc:  # the hint is ours, so it is added after the page text is cleaned
        return _text(f"{fp.clean(exc, 200)}; {REFRESH_HINT}", error=True)
    except (ValueError, MeasureError) as exc:
        if session.kit is not None:  # kit-mode errors may carry a path: redacted like CHECK.md
            return _text(kit_mod.redact(exc, 200), error=True)
        return _text(fp.clean(exc, 200), error=True)
    except Exception:  # noqa: BLE001 - answer the agent instead of dropping the connection
        traceback.print_exc()
        return _text("measure failed unexpectedly; see the terminal", error=True)


def _dispatch(method, params, session):
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "piggyback", "version": __version__},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": _tables(session)[0]}
    if method == "tools/call":
        return _call(session, params)
    raise RpcError(NOT_FOUND, "method not found")


def _valid_id(rid):
    # Strings and ints only: a float (NaN too) cannot round-trip as valid JSON.
    return isinstance(rid, str) or (isinstance(rid, int) and not isinstance(rid, bool))


def _invalid(msg):
    rid = msg.get("id") if isinstance(msg, dict) else None  # echo a readable id, if any
    return _error(rid if _valid_id(rid) else None, INVALID, "invalid request")


def _request(msg, session):
    rid = msg["id"]
    params = msg.get("params", {})
    if not isinstance(params, dict):
        return _error(rid, BAD_PARAMS, "params must be an object")
    try:
        return {"jsonrpc": "2.0", "id": rid, "result": _dispatch(msg["method"], params, session)}
    except RpcError as exc:
        return _error(rid, exc.code, exc.message)


def _reply(line, session):
    """The reply to one raw line, or None for a notification (which gets no reply)."""
    try:
        msg = json.loads(line.decode("utf-8"))
    except (ValueError, RecursionError):
        return _error(None, PARSE, "parse error")
    if (
        not isinstance(msg, dict)
        or msg.get("jsonrpc") != "2.0"
        or not isinstance(msg.get("method"), str)
    ):
        return _invalid(msg)
    if "id" not in msg:
        return None
    if not _valid_id(msg["id"]):
        return _error(None, INVALID, "invalid request")
    return _request(msg, session)


def _error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def serve(stdin, stdout, session):
    """Answer requests until stdin closes. Both streams are binary; one message per line."""
    # ponytail: synchronous loop, so one measure at a time and no rate limit. Each measure is
    # capped at measure's TOTAL_S (90s): a first check with 3 refs runs 4 measures (up to ~360s),
    # get_brief refresh runs 3 (up to ~270s). Add a per-minute cap if a client loops on check.
    while True:
        raw = stdin.readline(MAX_LINE + 1)
        if not raw:
            return 0
        if len(raw) > MAX_LINE:  # read the rest of the oversized line, then refuse it
            while not raw.endswith(b"\n") and (raw := stdin.readline(MAX_LINE)):
                pass
            reply = _error(None, INVALID, "message too large")
        else:
            reply = _reply(raw, session)
        if reply is not None:
            stdout.write(json.dumps(reply).encode("ascii") + b"\n")
            stdout.flush()
