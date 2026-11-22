"""Generate one C3 page: one task in one arm, from the locked inputs, checked until it passes.

The page folder is claimed once; an existing one is a first attempt that stands. The project
folder holds only that arm's allowed inputs, the agent writes index.html there, and the frozen
checklist runs after every turn. A failed checklist sends one correction turn, up to five. Records
(run.json, the raw agent output and the sent message of each round, and the session transcript)
sit beside the project, outside its folder. Every turn runs the claude executable pinned at the
lock. A page whose turns are not all on the locked model and context window is not gate eligible,
and the runner refuses further pages while any such page is published.

Usage: python -m bench.c3.run --store STORE --run RUN_ID TASK ARM
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from bench.c1.manifest import clean_commit, run_folder, sha256_file
from bench.c3 import checklist, prompts
from bench.c3.manifest import (
    AGENT_FLAGS,
    DEFAULT_STORE,
    MAX_CORRECTIONS,
    MODEL,
    TURN_TIMEOUT_S,
    agent_env,
    checker_chromium,
    claude_version,
    harness_hashes,
    refuse_store_links,
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
PASS, FAILED_AFTER, NO_OUTPUT, CHECKER_FAULT, AGENT_ERROR, RUNNER_CRASH = (
    "pass",
    "failed_after_5",
    "no_output",
    "checker_fault",
    "agent_error",
    "runner_crash",
)
RUNNABLE = (checklist.PASS, checklist.FAIL)
USAGE_FIELDS = ("contextWindow", "maxOutputTokens", "provider")


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


def check_cli(manifest):
    """Refuse when the pinned claude is missing, or its --version differs from the lock."""
    pinned = manifest["claude_executable"]
    if not Path(pinned["path"]).is_file():
        raise SystemExit(f"refusing: pinned claude CLI {pinned['path']} is missing")
    now = claude_version(pinned["path"])
    if now != pinned["version"]:
        raise SystemExit(
            f"refusing: claude CLI at {pinned['path']} is {now!r}; "
            f"the manifest recorded {pinned['version']!r}"
        )


def check_checker(manifest):
    """Refuse when the checker's Chromium differs from the one the manifest recorded."""
    version, _ = checker_chromium()
    recorded = manifest["checker_browser"]["chromium_version"]
    if version != recorded:
        raise SystemExit(
            f"refusing: checker Chromium is {version!r}; the manifest recorded {recorded!r}"
        )


def check_conformance(run):
    """Refuse further pages once any published page in this run was not model-conforming."""
    for path in sorted((run / "pages").glob("*/*/run.json")):
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            raise SystemExit(
                f"refusing: {path} is not readable JSON; the gate state is unknown"
            ) from None
        if saved.get("model_conforms") is not True:
            name = f"{saved.get('task')}/{saved.get('arm')}"
            raise SystemExit(f"refusing: {name} is not model-conforming; the gate is halted")


def task_row(task_id):
    rows = [t for t in prompts.load_tasks() if t["id"] == task_id]
    if len(rows) != 1:
        raise SystemExit(f"refusing: unknown task {task_id!r}")
    return rows[0]


def refuse_links(run, task_id, arm):
    """Refuse a symbolic link anywhere from the run folder down to the page folder."""
    for part in (run, run / "pages", run / "pages" / task_id, run / "pages" / task_id / arm):
        if part.is_symlink():
            raise SystemExit(f"refusing: {part} is a symbolic link")


def claim(run, task_id, arm):
    """Create pages/TASK/ARM. An existing folder is refused."""
    out = run / "pages" / task_id / arm
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        out.mkdir(mode=0o700)
    except FileExistsError:
        raise SystemExit(f"refusing: {out} exists; the first attempt stands") from None
    return out


def copy_inputs(run, starter, arm, project):
    for name in ARM_FILES[arm]:
        folder = "impeccable" if name == "SKILL.md" else starter
        shutil.copyfile(run / "inputs" / folder / name, project / name)


def call_turn(argv, project):
    """One agent turn: (record, raw stdout). A timeout keeps its partial output. A non-zero exit,
    an is_error result, output that is not a JSON object or a non-object modelUsage is an agent
    error. A missing executable raises; run_page records that as runner_crash."""
    started = time.monotonic()
    try:
        proc = agent_turn(argv, project)
    except subprocess.TimeoutExpired as exc:
        raw = exc.stdout or ""
        raw = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        error = f"turn timed out after {TURN_TIMEOUT_S} s"
        return turn_record(error, started), raw
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        data = None
    usage = data.get("modelUsage") if data else None
    denials = data.get("permission_denials") if data else None
    denials = (
        [denial_target(d) for d in denials if isinstance(d, dict)]
        if isinstance(denials, list)
        else []
    )
    error = None
    if data is None or proc.returncode != 0 or data.get("is_error"):
        error = f"exit {proc.returncode}: {proc.stderr.strip()[-500:] or 'no JSON result'}"
    elif usage is not None and not isinstance(usage, dict):
        error = "modelUsage is not an object"
    fields = model_usage_fields(usage) if isinstance(usage, dict) else {}
    record = turn_record(
        error,
        started,
        returncode=proc.returncode,
        session_id=data.get("session_id") if data else None,
        model_usage=fields,
        models=sorted(fields),
        model_usage_present=bool(fields),
        duration_ms=data.get("duration_ms") if data else None,
        permission_denials=denials,
    )
    return record, proc.stdout


def model_usage_fields(usage):
    """Per model: the context window, output cap and provider that Claude Code reports."""
    return {
        model: {k: (value.get(k) if isinstance(value, dict) else None) for k in USAGE_FIELDS}
        for model, value in usage.items()
    }


def denial_target(denial):
    """Tool name and target path of one permission denial. The rest of its input is not kept."""
    tool_input = denial.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    return {
        "tool_name": denial.get("tool_name"),
        "path": tool_input.get("file_path") or tool_input.get("path"),
    }


def round_conforms(agent, window):
    """True when the turn used only the locked model, with the locked context window."""
    usage = agent["model_usage"]
    return set(usage) == {MODEL} and usage[MODEL]["contextWindow"] == window


def agent_turn(argv, cwd):
    """The only place a model runs: argv list, no shell, the allowlisted env, a bounded wait."""
    return subprocess.run(
        argv,
        cwd=cwd,
        env=agent_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=TURN_TIMEOUT_S,
        check=False,
    )


def turn_record(error, started, **fields):
    record = {
        "error": error,
        "wall_s": round(time.monotonic() - started, 1),
        "returncode": None,
        "session_id": None,
        "model_usage": {},
        "models": [],
        "model_usage_present": False,
        "duration_ms": None,
        "permission_denials": [],
    }
    return {**record, **fields}


def check_page(project, task_id):
    """The frozen checklist on index.html, or None when there is no page file to check."""
    page = project / "index.html"
    if not page.is_file() or page.is_symlink():
        return None
    return checklist.run_bounded(str(page), task_id)


def generate(out, project, task_id, session, prompt, rounds, exe):
    """Run turns, appending to rounds in place so a crash keeps them. Returns the status.

    The checklist runs after every turn, errors included. Its result decides the page: a
    runnable passing page is pass whatever the agent did; an agent error ends the loop only when
    the page is runnable and does not pass.
    """
    (out / "rounds").mkdir(exist_ok=True)
    message = prompt
    argv = [exe, "-p", prompt, *AGENT_FLAGS, "--session-id", session]
    while True:
        n = len(rounds)
        (out / "rounds" / f"sent-{n}.txt").write_text(message, encoding="utf-8")
        record, raw = call_turn(argv, project)
        (out / "rounds" / f"agent-{n}.json").write_text(raw, encoding="utf-8")
        report = check_page(project, task_id)
        rounds.append({"round": n, "agent": record, "checklist": report})
        if report is None or report["outcome"] == checklist.NO_RUNNABLE:
            return NO_OUTPUT
        if report["outcome"] == checklist.FAULT:
            return CHECKER_FAULT
        if report["pass"]:
            return PASS
        if record["error"]:
            return AGENT_ERROR
        if n == MAX_CORRECTIONS:
            return FAILED_AFTER
        message = checklist.correction_message(report["failed"])
        argv = [exe, "-p", message, "--resume", session, *AGENT_FLAGS]


def transcript_path(project, session):
    """The Claude Code session file for this project and session under the current HOME.
    Claude Code 2.1.325 maps every character outside [A-Za-z0-9] to '-' in the folder name."""
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(project.resolve()))
    return Path.home() / ".claude" / "projects" / slug / f"{session}.jsonl"


def keep_transcript(out, project, session):
    src = transcript_path(project, session)
    if not src.is_file() or src.is_symlink():
        return "missing"
    try:
        shutil.copyfile(src, out / "rounds" / "transcript.jsonl")
    except OSError as exc:
        return f"copy_failed: {type(exc).__name__}"
    return "kept"


def publish(out, task_id, arm, session, rounds, started, status, window, error=None):
    """Write run.json. Used for a finished loop and for a crash, with the rounds made so far."""
    (out / "rounds").mkdir(exist_ok=True)
    models = sorted({m for r in rounds for m in r["agent"]["models"]})
    runnable = [
        r["round"] for r in rounds if r["checklist"] and r["checklist"]["outcome"] in RUNNABLE
    ]
    evidence = bool(rounds) and all(r["agent"]["model_usage_present"] for r in rounds)
    conforms = bool(rounds) and all(round_conforms(r["agent"], window) for r in rounds)
    denials = sum(len(r["agent"]["permission_denials"]) for r in rounds)
    if denials:
        print(
            f"FLAG: {task_id}/{arm} has {denials} permission denial(s); see run.json",
            file=sys.stderr,
        )
    if not conforms:
        print(f"STOP: {task_id}/{arm} is not model-conforming; halt the gate", file=sys.stderr)
    result = {
        "task": task_id,
        "arm": arm,
        "session_id": session,
        "status": status,
        "error": error,
        "turns": max(len(rounds) - 1, 0),
        "last_runnable_round": runnable[-1] if runnable else None,
        "model_ids": models,
        "model_flag": [m for m in models if m != MODEL],
        "model_evidence": "confirmed" if evidence else "missing",
        "model_conforms": conforms,
        "gate_eligible": conforms and status not in (NO_OUTPUT, RUNNER_CRASH),
        "denials_total": denials,
        "audit_flag": "permission_denials" if denials else None,
        "transcript": keep_transcript(out, out / "project", session),
        "total_wall_s": round(time.monotonic() - started, 1),
        "rounds": rounds,
    }
    publish_json(out / "run.json", result)
    return result


def run_page(store, run_id, task_id, arm):
    refuse_store_links(store)
    run = run_folder(store, run_id)
    manifest = load_manifest(run)
    check_registered(run, manifest)
    check_cli(manifest)
    check_checker(manifest)
    check_conformance(run)
    task = task_row(task_id)
    if arm not in ARM_FILES:
        raise SystemExit(f"refusing: unknown arm {arm!r}")
    refuse_links(run, task_id, arm)
    prompt = (run / "prompts" / task_id / f"{arm}.txt").read_text(encoding="utf-8")
    out = claim(run, task_id, arm)
    session = str(uuid.uuid4())
    rounds = []
    started = time.monotonic()
    window = manifest["model"]["context_window"]
    exe = manifest["claude_executable"]["path"]
    try:
        (out / "project").mkdir()
        copy_inputs(run, task["starter_id"], arm, out / "project")
        status = generate(out, out / "project", task_id, session, prompt, rounds, exe)
    except BaseException as exc:
        error = {"type": type(exc).__name__, "message": str(exc)}
        publish(out, task_id, arm, session, rounds, started, RUNNER_CRASH, window, error)
        raise
    return publish(out, task_id, arm, session, rounds, started, status, window)


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
