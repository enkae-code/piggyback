"""Register a C3 run before any generation: a dated manifest and its frozen inputs, locked once.

The manifest refuses a dirty Piggyback worktree, an existing manifest or a non-empty run folder,
and a missing or blank DESIGN.md for any starter. It records the commit, the sha256 of the harness,
the starters and every frozen input, the Chromium and axe-core versions, the Impeccable skill and
commit, the model, the exact agent invocation and the rater seed. Each starter's screenshot and
brief come from Piggyback's own measure of its local index.html. Nothing here starts a model.
It is written once (never replaced).

Usage: python -m bench.c3.manifest --store STORE --run RUN_ID --design-md DIR --seed N
"""

import argparse
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bench.c1.manifest import bundled_chromium, clean_commit, run_folder, sha256_file
from bench.c3 import checklist, prompts
from piggyback import brief as brief_mod
from piggyback import fingerprint as fingerprints
from piggyback.measure import publish_json, write_measure

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
STARTERS = REPO / "starters"
DEFAULT_STORE = Path("/home/brodey/projects/piggyback-evidence/c3")
IMPECCABLE = Path("/home/brodey/repos/impeccable")
SKILL_PATH = IMPECCABLE / ".hermes/skills/impeccable/SKILL.md"
MODEL = "claude-opus-5-5"
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


def claude_version():
    try:
        out = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, check=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"refusing: the claude CLI did not answer --version: {exc}") from None
    return out.stdout.strip()


def impeccable_commit():
    return subprocess.run(
        ["git", "-C", str(IMPECCABLE), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def agent_record():
    """The agent invocation, as the runner makes it. Recorded so a re-run can audit it."""
    return {
        "argv_first_turn": ["claude", "-p", "<prompt>", *AGENT_FLAGS, "--session-id", "<uuid4>"],
        "argv_correction_turn": [
            "claude",
            "-p",
            "<correction>",
            "--resume",
            "<session_id>",
            *AGENT_FLAGS,
        ],  # fmt: skip
        "env_adds": AGENT_ENV,
        "cwd": "pages/<task>/<arm>/project",
        "turn_timeout_s": TURN_TIMEOUT_S,
        "max_correction_turns": MAX_CORRECTIONS,
    }


def write_inputs(run, designs):
    inputs = run / "inputs"
    for sid, design in designs.items():
        folder = inputs / sid
        folder.mkdir(parents=True)
        measure_starter(sid, folder)
        shutil.copyfile(design, folder / "DESIGN.md")
    (inputs / "impeccable").mkdir()
    shutil.copyfile(SKILL_PATH, inputs / "impeccable" / "SKILL.md")
    for task in prompts.load_tasks():
        for arm in prompts.ARM_BLOCKS:
            path = run / "prompts" / task["id"] / f"{arm}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(prompts.expand(task, arm) + "\n", encoding="utf-8")


def build(store, run_id, design_dir, seed):
    run = run_folder(store, run_id)
    path = run / "manifest.json"
    if path.exists() or path.is_symlink():
        raise SystemExit(f"manifest already locked: {path}")
    if run.exists() and any(run.iterdir()):
        raise SystemExit(f"refusing: run folder {run} is not empty; a run ID is used once")
    commit = clean_commit()
    designs = design_files(design_dir)
    chromium, label = bundled_chromium()
    cli_version = claude_version()
    run.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_inputs(run, designs)
    manifest = {
        "locked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "piggyback_commit": commit,
        "harness_sha256": harness_hashes(),
        "starter_ids": starter_ids(),
        "starters_sha256": starter_hashes(),
        "chromium_version": chromium,
        "browser": label,
        "axe_core": {"version": checklist.AXE_VERSION, "sha256": sha256_file(checklist.AXE_PATH)},
        "impeccable": {
            "skill_path": str(SKILL_PATH),
            "skill_sha256": sha256_file(SKILL_PATH),
            "commit": impeccable_commit(),
        },
        "model": {
            "provider": "anthropic via Claude Code (Claude Max)",
            "model": MODEL,
            "claude_cli_version": cli_version,
            "settings": "defaults",
        },
        "agent": agent_record(),
        "rater_seed": seed,
        "inputs_sha256": frozen_hashes(run),
    }
    publish_json(path, manifest)
    path.chmod(0o600)
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
