"""Generate one C3 page: one task in one arm, from the locked inputs, checked until it passes.

The page folder is claimed once; an existing one is a first attempt that stands. The project
folder holds only that arm's allowed inputs, the agent writes index.html there, and the frozen
checklist runs after each turn. A failed checklist sends one correction turn, up to five. Records
(run.json and the raw agent output of each round) sit beside the project, outside its folder.

Usage: python -m bench.c3.run --store STORE --run RUN_ID TASK ARM
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid

from bench.c1.manifest import clean_commit, run_folder, sha256_file
from bench.c3 import checklist, prompts
from bench.c3.manifest import (
    AGENT_ENV,
    AGENT_FLAGS,
    DEFAULT_STORE,
    MAX_CORRECTIONS,
    MODEL,
    TURN_TIMEOUT_S,
    harness_hashes,
    starter_hashes,
)
from piggyback.measure import publish_json

ARM_FILES = {
    "piggyback-brief": ("BRIEF.md",),
    "screenshot": ("screenshot.png",),
    "hand-design-md": ("DESIGN.md",),
    "skill-alone": ("SKILL.md", "screenshot.png"),
    "skill-plus-piggyback": ("SKILL.md", "screenshot.png", "BRIEF.md"),
}
NO_OUTPUT, PASS, FAILED_AFTER, CHECKER_FAULT, AGENT_ERROR = (
    "no_output",
    "pass",
    "failed_after_5",
    "checker_fault",
    "agent_error_after_page",
)
RUNNABLE = (checklist.PASS, checklist.FAIL)


def load_manifest(run):
    path = run / "manifest.json"
    if not path.is_file() or path.is_symlink():
        raise SystemExit(f"refusing: no manifest in {run}; run bench.c3.manifest first")
    return json.loads(path.read_text(encoding="utf-8"))


def check_registered(run, manifest):
    """Refuse unless HEAD is the locked commit, the tree is clean, and harness, starters and
    frozen inputs are unchanged since the lock."""
    if clean_commit() != manifest["piggyback_commit"]:
        raise SystemExit("refusing: HEAD is not the manifest commit")
    if harness_hashes() != manifest["harness_sha256"]:
        raise SystemExit("refusing: harness files changed since the manifest was locked")
    if starter_hashes() != manifest["starters_sha256"]:
        raise SystemExit("refusing: starter files changed since the manifest was locked")
    for rel, digest in manifest["inputs_sha256"].items():
        if not (run / rel).is_file() or sha256_file(run / rel) != digest:
            raise SystemExit(f"refusing: frozen input {rel} changed since the manifest was locked")


def task_row(task_id):
    rows = [t for t in prompts.load_tasks() if t["id"] == task_id]
    if len(rows) != 1:
        raise SystemExit(f"refusing: unknown task {task_id!r}")
    return rows[0]


def claim(run, task_id, arm):
    """Create pages/TASK/ARM and its empty project folder. An existing folder is refused."""
    out = run / "pages" / task_id / arm
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        out.mkdir(mode=0o700)
    except FileExistsError:
        raise SystemExit(f"refusing: {out} exists; the first attempt stands") from None
    (out / "project").mkdir()
    return out


def copy_inputs(run, starter, arm, project):
    for name in ARM_FILES[arm]:
        folder = "impeccable" if name == "SKILL.md" else starter
        shutil.copyfile(run / "inputs" / folder / name, project / name)


def agent_turn(argv, cwd):
    """The only place a model runs: argv list, no shell, frozen env additions, a bounded wait."""
    return subprocess.run(
        argv,
        cwd=cwd,
        env={**os.environ, **AGENT_ENV},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=TURN_TIMEOUT_S,
        check=False,
    )


def call_turn(argv, project):
    """One agent turn: (record, raw stdout). A timeout, a non-zero exit, an error result or
    output that is not a JSON object is an agent error."""
    started = time.monotonic()
    try:
        proc = agent_turn(argv, project)
    except subprocess.TimeoutExpired:
        return {"error": f"turn timed out after {TURN_TIMEOUT_S} s", "models": []}, ""
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        data = None
    error = None
    if proc.returncode != 0 or data is None or data.get("is_error"):
        error = f"exit {proc.returncode}: {proc.stderr.strip()[-500:] or 'no JSON result'}"
    record = {
        "error": error,
        "returncode": proc.returncode,
        "session_id": data.get("session_id") if data else None,
        "models": sorted((data or {}).get("modelUsage") or {}),
        "duration_ms": (data or {}).get("duration_ms"),
        "wall_s": round(time.monotonic() - started, 1),
    }
    return record, proc.stdout


def check_page(project, task_id):
    """The frozen checklist on index.html, or None when there is no page file to check."""
    page = project / "index.html"
    if not page.is_file() or page.is_symlink():
        return None
    return checklist.run_bounded(str(page), task_id)


def generate(out, project, task_id, prompt):
    session = str(uuid.uuid4())
    argv = ["claude", "-p", prompt, *AGENT_FLAGS, "--session-id", session]
    rounds, corrections = [], 0
    while True:
        record, raw = call_turn(argv, project)
        (out / "rounds").mkdir(exist_ok=True)
        (out / "rounds" / f"agent-{len(rounds)}.json").write_text(raw, encoding="utf-8")
        # An agent error ends the loop, but only a page with no earlier runnable round is no_output.
        had_page = any(r["checklist"] and r["checklist"]["outcome"] in RUNNABLE for r in rounds)
        report = check_page(project, task_id) if not record["error"] or had_page else None
        rounds.append({"round": len(rounds), "agent": record, "checklist": report})
        if report is None or report["outcome"] == checklist.NO_RUNNABLE:
            return session, rounds, NO_OUTPUT, corrections
        if report["outcome"] == checklist.FAULT:
            return session, rounds, CHECKER_FAULT, corrections
        if record["error"]:
            return session, rounds, AGENT_ERROR, corrections
        if report["pass"]:
            return session, rounds, PASS, corrections
        if corrections == MAX_CORRECTIONS:
            return session, rounds, FAILED_AFTER, corrections
        corrections += 1
        message = checklist.correction_message(report["failed"])
        argv = ["claude", "-p", message, "--resume", session, *AGENT_FLAGS]


def run_page(store, run_id, task_id, arm):
    run = run_folder(store, run_id)
    manifest = load_manifest(run)
    check_registered(run, manifest)
    task = task_row(task_id)
    if arm not in ARM_FILES:
        raise SystemExit(f"refusing: unknown arm {arm!r}")
    prompt = (run / "prompts" / task_id / f"{arm}.txt").read_text(encoding="utf-8")
    out = claim(run, task_id, arm)
    project = out / "project"
    copy_inputs(run, task["starter_id"], arm, project)
    started = time.monotonic()
    session, rounds, status, corrections = generate(out, project, task_id, prompt)
    models = sorted({m for r in rounds for m in r["agent"]["models"]})
    result = {
        "task": task_id,
        "arm": arm,
        "session_id": session,
        "turns": corrections,
        "status": status,
        "model_ids": models,
        "model_flag": [m for m in models if m != MODEL],
        "total_wall_s": round(time.monotonic() - started, 1),
        "rounds": rounds,
    }
    publish_json(out / "run.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--store", default=str(DEFAULT_STORE))
    parser.add_argument("--run", required=True)
    parser.add_argument("task", help="task id, T01 to T10")
    parser.add_argument("arm", help="one of the five arm ids")
    args = parser.parse_args(argv)
    result = run_page(args.store, args.run, args.task, args.arm)
    print(json.dumps({k: result[k] for k in ("status", "turns", "model_flag")}, indent=2))
    return 2 if result["status"] == CHECKER_FAULT else 0


if __name__ == "__main__":
    sys.exit(main())
