"""Register a C3 run before any generation: a dated manifest and its frozen inputs, locked once.

The manifest refuses a dirty Piggyback or Impeccable worktree, an existing manifest or a non-empty
run folder, a symbolic link on the store path, and a missing or blank DESIGN.md for any starter. It
records the commit, the sha256 of the harness, the starters, the prompts (with their text) and every
frozen input, the checker and capture Chromium versions, the axe-core version, the Impeccable skill,
commit and version, the model, the pinned claude executable and its version, the context window and
settings, the exact agent invocation and env, the status scoring and the rater seed. Each starter's
screenshot and brief come from Piggyback's own measure of its local index.html. Nothing here starts
a model. It is written once (never replaced).

Usage: python -m bench.c3.manifest --store STORE --run RUN_ID --design-md DIR --seed N
"""

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bench.c1.manifest import bundled_chromium, clean_commit, run_folder, sha256_file
from bench.c3 import checklist, prompts
from piggyback import brief as brief_mod
from piggyback import fingerprint as fingerprints
from piggyback.browser import ARGS
from piggyback.measure import write_measure

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
STARTERS = REPO / "starters"
DEFAULT_STORE = Path("/home/brodey/projects/piggyback-evidence/c3")
IMPECCABLE = Path("/home/brodey/repos/impeccable")
SKILL_PATH = IMPECCABLE / ".hermes/skills/impeccable/SKILL.md"
MODEL = "claude-opus-5-5"
# As reported by Claude Code 2.1.325 for claude-opus-5-5 in modelUsage.
CONTEXT_WINDOW = 1000000
MAX_OUTPUT_TOKENS = 128000
VIEWPORT = (1440, 900)
TURN_TIMEOUT_S = 1800
MAX_CORRECTIONS = 5
AGENT_TOOLS = "Read,Write,Edit,Glob,Grep"
AGENT_FLAGS = [
    "--model",
    MODEL,
    "--setting-sources",
    "project",
    "--strict-mcp-config",
    "--tools",
    AGENT_TOOLS,
    "--permission-mode",
    "acceptEdits",
    "--output-format",
    "json",
]
AGENT_ENV = {
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS": "1",
    "DISABLE_AUTOUPDATER": "1",
}
# The only host variables the agent gets. No ANTHROPIC_*, no other CLAUDE_CODE_*.
ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TERM",
    "USER",
    "XDG_RUNTIME_DIR",
    "XDG_CONFIG_HOME",
)
# Scoring, preregistered before any generation. turn_score is the number used for the turn total.
STATUS_SCORING = {
    "pass": {
        "turn_score": "actual correction count",
        "rated": True,
        "loss": False,
        "flagged": False,
    },
    "failed_after_5": {"turn_score": 6, "rated": True, "loss": False, "flagged": False},
    "agent_error": {"turn_score": 6, "rated": True, "loss": False, "flagged": False},
    "no_output": {"turn_score": 6, "rated": False, "loss": True, "flagged": False},
    "runner_crash": {"turn_score": 6, "rated": False, "loss": True, "flagged": False},
    "checker_fault": {"turn_score": 6, "rated": True, "loss": False, "flagged": True},
}


def starter_ids():
    return sorted({task["starter_id"] for task in prompts.load_tasks()})


def harness_hashes():
    names = sorted(p for p in HERE.iterdir() if p.is_file() and p.suffix in (".py", ".json"))
    hashes = {p.name: sha256_file(p) for p in names}
    hashes["vendor/axe-core/axe.min.js"] = sha256_file(checklist.AXE_PATH)
    return hashes


def starter_hashes():
    hashes = {}
    for sid in starter_ids():
        root = STARTERS / sid
        if not root.is_dir():
            raise SystemExit(f"refusing: starter {sid} is not in {STARTERS}")
        for path in sorted(root.rglob("*")):
            if path.is_file():
                hashes[f"{sid}/{path.relative_to(root).as_posix()}"] = sha256_file(path)
    return hashes


def frozen_hashes(run):
    """sha256 of every file under the run folder except the manifest itself."""
    return {
        p.relative_to(run).as_posix(): sha256_file(p)
        for p in sorted(run.rglob("*"))
        if p.is_file() and p.name != "manifest.json"
    }


def design_files(design_dir):
    """starter ID -> DESIGN.md path. Refuses unless every starter has a non-blank file."""
    root = Path(design_dir)
    if not root.is_dir():
        raise SystemExit(f"refusing: design dir {root} is not a directory")
    found = {}
    for sid in starter_ids():
        src = root / f"{sid}.md"
        if src.is_symlink() or not src.is_file() or not src.read_text(encoding="utf-8").strip():
            raise SystemExit(f"refusing: {src} is missing or blank")
        found[sid] = src
    return found


def refuse_store_links(store):
    """Refuse when the store path, or any folder above it, is a symbolic link."""
    path = Path(store).absolute()
    for part in (path, *path.parents):
        if part.is_symlink():
            raise SystemExit(f"refusing: {part} is a symbolic link")


def png_size(path):
    head = Path(path).read_bytes()[:24]
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit(f"refusing: {path} is not a PNG")
    return struct.unpack(">II", head[16:24])


def measure_starter(starter, folder):
    """Measure a starter's local index.html with Piggyback's bundled Chromium: writes
    screenshot.png (1440x900) and BRIEF.md into folder. A loopback server serves the starter."""
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(STARTERS / starter))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/index.html"
        with tempfile.TemporaryDirectory() as tmp:
            fingerprint, shot = write_measure(url, tmp, viewport=VIEWPORT, allow_loopback=True)
            if png_size(shot) != VIEWPORT:
                raise SystemExit(
                    f"refusing: {starter} screenshot is not {VIEWPORT[0]}x{VIEWPORT[1]}"
                )
            shutil.copyfile(shot, folder / "screenshot.png")
            brief = brief_mod.brief([fingerprints.load(fingerprint)])
    finally:
        server.shutdown()
        server.server_close()
    (folder / "BRIEF.md").write_text(brief, encoding="utf-8")


def agent_env():
    """The allowlisted host variables that are set, plus the frozen additions. Nothing else, so
    an ANTHROPIC_* key in the host environment never reaches the agent."""
    host = {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ}
    return {**host, **AGENT_ENV}


def claude_version(path):
    """The --version of the claude executable at path, run with the agent env."""
    try:
        out = subprocess.run(
            [path, "--version"],
            env=agent_env(),
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"refusing: the claude CLI did not answer --version: {exc}") from None
    return out.stdout.strip()


def claude_executable():
    """(realpath, version) of the claude CLI on PATH. The real file is pinned, so an auto-update
    that moves the link does not change the executable a run uses."""
    found = shutil.which("claude")
    if found is None:
        raise SystemExit("refusing: no claude CLI on PATH")
    path = os.path.realpath(found)
    return path, claude_version(path)


def checker_chromium():
    """(version, executable) of the Chromium the frozen checklist launches: bundled, no channel."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(chromium_sandbox=True, args=ARGS)
        try:
            return browser.version, p.chromium.executable_path
        finally:
            browser.close()


def impeccable_commit():
    return subprocess.run(
        ["git", "-C", str(IMPECCABLE), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def impeccable_version():
    """The 'version' of the Impeccable checkout's package.json, or None when there is none."""
    pkg = IMPECCABLE / "package.json"
    if not pkg.is_file():
        return None
    return json.loads(pkg.read_text(encoding="utf-8")).get("version")


def impeccable_clean():
    """Refuse when the Impeccable checkout has any uncommitted change."""
    status = subprocess.run(
        ["git", "-C", str(IMPECCABLE), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    if status.strip():
        raise SystemExit(f"refusing: impeccable checkout {IMPECCABLE} is dirty")


def agent_record():
    """The agent invocation, as the runner makes it. Recorded so a re-run can audit it."""
    return {
        "argv_first_turn": [
            "<claude_executable>",
            "-p",
            "<prompt>",
            *AGENT_FLAGS,
            "--session-id",
            "<uuid4>",
        ],
        "argv_correction_turn": [
            "<claude_executable>",
            "-p",
            "<correction>",
            "--resume",
            "<session_id>",
            *AGENT_FLAGS,
        ],  # fmt: skip
        "env_allowlist": list(ENV_ALLOWLIST),
        "env_adds": sorted(AGENT_ENV),
        "cwd": "pages/<task>/<arm>/project",
        "turn_timeout_s": TURN_TIMEOUT_S,
        "max_correction_turns": MAX_CORRECTIONS,
    }


def prompt_texts():
    """task ID -> arm -> the exact text of that prompt file."""
    return {
        task["id"]: {arm: prompts.expand(task, arm) + "\n" for arm in prompts.ARM_BLOCKS}
        for task in prompts.load_tasks()
    }


def write_inputs(run, designs, texts):
    inputs = run / "inputs"
    for sid, design in designs.items():
        folder = inputs / sid
        folder.mkdir(parents=True)
        measure_starter(sid, folder)
        shutil.copyfile(design, folder / "DESIGN.md")
    (inputs / "impeccable").mkdir()
    shutil.copyfile(SKILL_PATH, inputs / "impeccable" / "SKILL.md")
    for task_id, arms in texts.items():
        for arm, text in arms.items():
            path = run / "prompts" / task_id / f"{arm}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")


def prompt_hashes(run):
    return {
        f"{task['id']}/{arm}": sha256_file(run / "prompts" / task["id"] / f"{arm}.txt")
        for task in prompts.load_tasks()
        for arm in prompts.ARM_BLOCKS
    }


def publish_once(path, data):
    """Create path with the JSON text. An existing path, file or link, is refused, never replaced.
    The text goes to a temp file beside it, hard-linked into place, so no partial file shows."""
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        os.link(tmp, path)
    except FileExistsError:
        raise SystemExit(f"manifest already locked: {path}") from None
    finally:
        os.unlink(tmp)


def build(store, run_id, design_dir, seed):
    refuse_store_links(store)
    run = run_folder(store, run_id)
    path = run / "manifest.json"
    if path.exists() or path.is_symlink():
        raise SystemExit(f"manifest already locked: {path}")
    if run.exists() and any(run.iterdir()):
        raise SystemExit(f"refusing: run folder {run} is not empty; a run ID is used once")
    commit = clean_commit()
    impeccable_clean()
    designs = design_files(design_dir)
    chromium, label = bundled_chromium()
    checker_version, checker_exe = checker_chromium()
    exe, cli_version = claude_executable()
    run.mkdir(mode=0o700, parents=True, exist_ok=True)
    texts = prompt_texts()
    write_inputs(run, designs, texts)
    manifest = {
        "locked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "piggyback_commit": commit,
        "harness_sha256": harness_hashes(),
        "starter_ids": starter_ids(),
        "starters_sha256": starter_hashes(),
        "checker_browser": {
            "browser": "bundled chromium, no channel (launched by the frozen checklist)",
            "chromium_version": checker_version,
            "executable": checker_exe,
        },
        "capture_browser": {"browser": label, "chromium_version": chromium},
        "axe_core": {"version": checklist.AXE_VERSION, "sha256": sha256_file(checklist.AXE_PATH)},
        "impeccable": {
            "skill_path": str(SKILL_PATH),
            "skill_sha256": sha256_file(SKILL_PATH),
            "commit": impeccable_commit(),
            "version": impeccable_version(),
        },
        "claude_executable": {"path": exe, "version": cli_version},
        "model": {
            "provider": "anthropic via Claude Code (Claude Max)",
            "model": MODEL,
            "claude_cli_version": cli_version,
            "context_window": CONTEXT_WINDOW,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "settings": (
                "CLI defaults: no model, effort, thinking or temperature flags; "
                "--setting-sources project; the env allowlist in agent.env_allowlist"
            ),
        },
        "agent": agent_record(),
        "status_scoring": STATUS_SCORING,
        "rater_seed": seed,
        "prompts": texts,
        "prompts_sha256": prompt_hashes(run),
        "inputs_sha256": frozen_hashes(run),
    }
    publish_once(path, manifest)
    return path, manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--store", default=str(DEFAULT_STORE))
    parser.add_argument("--run", required=True)
    parser.add_argument("--design-md", required=True, metavar="DIR")
    parser.add_argument("--seed", type=int, required=True)
    args = parser.parse_args(argv)
    path, manifest = build(args.store, args.run, args.design_md, args.seed)
    print(json.dumps({"manifest": str(path), "inputs": len(manifest["inputs_sha256"])}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
