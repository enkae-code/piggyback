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
import contextlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
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

if os.name == "nt":
    import msvcrt
else:
    import fcntl

ARM_FILES = {
    "piggyback-brief": ("BRIEF.md",),
    "screenshot": ("screenshot.png",),
    "skill-alone": ("SKILL.md", "screenshot.png"),
    "skill-plus-piggyback": ("SKILL.md", "screenshot.png", "BRIEF.md"),
}
PASS, FAILED_AFTER, NO_OUTPUT, CHECKER_FAULT, AGENT_ERROR, RUNNER_CRASH, MODEL_MISMATCH = (
    "pass",
    "failed_after_5",
    "no_output",
    "checker_fault",
    "agent_error",
    "runner_crash",
    "model_mismatch",
)
RUNNABLE = (checklist.PASS, checklist.FAIL)
USAGE_FIELDS = ("contextWindow", "maxOutputTokens", "provider")
# A file name is at most 255 bytes; Claude Code names the transcript folder after the project path.
SLUG_LIMIT = 255


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
    """Refuse further pages once any published page in this run was not model-conforming, or a
    claimed page was interrupted before it was published."""
    for folder in sorted((run / "pages").glob("*/*")):
        if not folder.is_dir():
            continue
        task_id, arm = folder.parent.name, folder.name
        path = folder / "run.json"
        if not path.is_file():
            raise SystemExit(
                f"refusing: {task_id}/{arm} was interrupted (no run.json); run "
                f"`python -m bench.c3.run --run {run.name} --reconcile {task_id} {arm}` "
                "to record it"
            )
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            raise SystemExit(
                f"refusing: {path} is not readable JSON; the gate state is unknown"
            ) from None
        if saved.get("model_conforms") is not True:
            raise SystemExit(
                f"refusing: {task_id}/{arm} is not model-conforming; the gate is halted"
            )


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
    """One agent turn, unparsed: (raw stdout, returncode, stderr, timeout error). A timeout keeps
    its partial output. A missing executable raises; run_page records that as runner_crash."""
    try:
        proc = agent_turn(argv, project)
    except subprocess.TimeoutExpired as exc:
        raw = exc.stdout or ""
        raw = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        return raw, None, "", f"turn timed out after {TURN_TIMEOUT_S} s"
    return proc.stdout, proc.returncode, proc.stderr, None


def turn_from_output(raw, started, returncode, stderr):
    """The record of one turn from its raw output. A live turn and reconcile both read it this way.
    reconcile passes returncode None: the exit status of an interrupted turn is not known."""
    try:
        data = json.loads(raw)
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
    if data is None or returncode != 0 or data.get("is_error"):
        error = f"exit {returncode}: {stderr.strip()[-500:] or 'no JSON result'}"
    elif usage is not None and not isinstance(usage, dict):
        error = "modelUsage is not an object"
    fields = model_usage_fields(usage) if isinstance(usage, dict) else {}
    return turn_record(
        error,
        started,
        returncode=returncode,
        session_id=data.get("session_id") if data else None,
        model_usage=fields,
        models=sorted(fields),
        model_usage_present=bool(fields),
        duration_ms=data.get("duration_ms") if data else None,
        permission_denials=denials,
    )


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


def round_conforms(agent, model):
    """False only when the turn reported a model other than the locked one, or another context
    window or output cap. A turn with no usage report (a timeout, a crash) is missing evidence,
    recorded in model_evidence, not a model change; halting the gate for it would stop the run."""
    usage = agent["model_usage"]
    if not usage:
        return True
    return (
        set(usage) == {MODEL}
        and usage[MODEL]["contextWindow"] == model["context_window"]
        and usage[MODEL]["maxOutputTokens"] == model["max_output_tokens"]
    )


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


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def check_page(project, task_id):
    """The frozen checklist on index.html, or None when there is no page file to check.

    The page loads from a loopback server that serves only the project folder, for the length of
    this check, so it cannot reach the evidence store beside the project. The server always stops.
    """
    page = project / "index.html"
    if not page.is_file() or page.is_symlink():
        return None
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(project)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        return checklist.run_bounded(f"http://127.0.0.1:{server.server_port}/index.html", task_id)
    finally:
        server.shutdown()
        server.server_close()


def generate(out, project, task_id, session, prompt, rounds, exe, model):
    """Run turns, appending to rounds in place so a crash keeps them. Returns the status.

    The raw output is written before it is parsed, and the round is kept even if that write fails.
    Each turn's round is recorded before the checklist runs, so a crash in the checker keeps it.
    The checklist runs after every turn, errors included. A turn on another model or context
    window or output cap returns model_mismatch at once, with no further agent call. Otherwise the
    checklist result decides the page: a runnable passing page is pass whatever the agent did; an
    agent error ends the loop only when the page is runnable and does not pass.
    """
    (out / "rounds").mkdir(exist_ok=True)
    (out / "rounds" / "session.txt").write_text(session, encoding="utf-8")
    message = prompt
    argv = [exe, "-p", prompt, *AGENT_FLAGS, "--session-id", session]
    while True:
        n = len(rounds)
        (out / "rounds" / f"sent-{n}.txt").write_text(message, encoding="utf-8")
        started = time.monotonic()
        raw, returncode, stderr, timeout = call_turn(argv, project)
        entry = {"round": n, "agent": None, "checklist": None}
        try:
            (out / "rounds" / f"agent-{n}.json").write_text(raw, encoding="utf-8")
        finally:
            entry["agent"] = (
                turn_record(timeout, started)
                if timeout
                else turn_from_output(raw, started, returncode, stderr)
            )
            rounds.append(entry)
        report = check_page(project, task_id)
        entry["checklist"] = report
        if not round_conforms(entry["agent"], model):
            return MODEL_MISMATCH
        if report is None or report["outcome"] == checklist.NO_RUNNABLE:
            return NO_OUTPUT
        if report["outcome"] == checklist.FAULT:
            return CHECKER_FAULT
        if report["pass"]:
            return PASS
        if entry["agent"]["error"]:
            return AGENT_ERROR
        if n == MAX_CORRECTIONS:
            return FAILED_AFTER
        message = checklist.correction_message(report["failed"])
        argv = [exe, "-p", message, "--resume", session, *AGENT_FLAGS]


def slug_of(project):
    """The Claude Code folder name for this project path. Claude Code 2.1.325 maps every character
    outside [A-Za-z0-9] to '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(project.resolve()))


def transcript_path(project, session):
    """The Claude Code session file for this project and session under the current HOME."""
    return Path.home() / ".claude" / "projects" / slug_of(project) / f"{session}.jsonl"


def keep_transcript(out, project, session):
    try:
        src = transcript_path(project, session)
        if not src.is_file() or src.is_symlink():
            return "missing"
        shutil.copyfile(src, out / "rounds" / "transcript.jsonl")
    except OSError as exc:
        return f"lookup_failed: {type(exc).__name__}"
    return "kept"


def crash_status(rounds, model):
    """An interrupted page is model_mismatch when any round it kept was on another model."""
    off_model = any(not round_conforms(r["agent"], model) for r in rounds)
    return MODEL_MISMATCH if off_model else RUNNER_CRASH


def publish(
    out, task_id, arm, session, rounds, started, status, model, error=None, reconciled=False
):
    """Write run.json. Used for a finished loop, a crash with the rounds made so far, and a
    reconciled interrupted page."""
    (out / "rounds").mkdir(exist_ok=True)
    models = sorted({m for r in rounds for m in r["agent"]["models"]})
    runnable = [
        r["round"] for r in rounds if r["checklist"] and r["checklist"]["outcome"] in RUNNABLE
    ]
    evidence = bool(rounds) and all(r["agent"]["model_usage_present"] for r in rounds)
    conforms = all(round_conforms(r["agent"], model) for r in rounds)
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
        # A no-output page still counts for the gate, as a preregistered loss (status_scoring).
        "gate_eligible": conforms,
        "denials_total": denials,
        "audit_flag": "permission_denials" if denials else None,
        "transcript": keep_transcript(out, out / "project", session),
        "total_wall_s": round(time.monotonic() - started, 1),
        "reconciled": reconciled,
        "rounds": rounds,
    }
    publish_json(out / "run.json", result)
    return result


def locked_run(store, run_id):
    """The run folder and manifest, once the store, HEAD, harness, starters and inputs match the
    lock."""
    refuse_store_links(store)
    run = run_folder(store, run_id)
    manifest = load_manifest(run)
    check_registered(run, manifest)
    return run, manifest


@contextlib.contextmanager
def run_lock(run):
    """One exclusive lock per run, held from admission to publication. The kernel drops it when the
    process dies, so an interrupted run leaves no stale lock."""
    fd = _open_lock(run / ".runner.lock")
    locked = False
    try:
        try:
            if os.name == "nt":
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Only contention: flock raises EWOULDBLOCK, msvcrt EACCES. Anything else is a real fault.
        except (BlockingIOError, PermissionError):
            raise SystemExit(
                f"refusing: run {run.name} is active in another process; wait for it to finish"
            ) from None
        locked = True
        yield
    finally:
        try:
            if locked and os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # Windows may release late on close alone
        finally:
            os.close(fd)


def _open_lock(lock):
    """Open the lock file without following a link. Windows has no O_NOFOLLOW, so the file opened
    must be the one lstat sees, and not a link."""
    if hasattr(os, "O_NOFOLLOW"):
        return os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    if os.path.islink(lock):
        raise SystemExit(f"refusing: {lock.name} is a symbolic link")
    # ponytail: a link swapped in after that check can still make the open create an empty file
    # outside the run; the identity check below refuses to lock it. Upgrade: a reparse-point-aware
    # open (CreateFile with FILE_FLAG_OPEN_REPARSE_POINT).
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        seen, opened = os.lstat(lock), os.fstat(fd)
    except OSError:
        os.close(fd)
        raise
    if stat.S_ISLNK(seen.st_mode) or (seen.st_dev, seen.st_ino) != (opened.st_dev, opened.st_ino):
        os.close(fd)
        raise SystemExit(
            f"refusing: {lock.name} changed while it was opened (a link or another file)"
        )
    return fd


def check_page_id(run, task_id, arm):
    """Refuse an unknown task or arm, or a symbolic link on the way to the page folder."""
    task = task_row(task_id)
    if arm not in ARM_FILES:
        raise SystemExit(f"refusing: unknown arm {arm!r}")
    refuse_links(run, task_id, arm)
    return task


def run_page(store, run_id, task_id, arm):
    run, manifest = locked_run(store, run_id)
    check_cli(manifest)
    check_checker(manifest)
    with run_lock(run):
        check_conformance(run)
        task = check_page_id(run, task_id, arm)
        project = run / "pages" / task_id / arm / "project"
        if len(slug_of(project)) > SLUG_LIMIT:
            raise SystemExit(
                f"refusing: the transcript folder name for {project} is {len(slug_of(project))} "
                f"characters; the limit is {SLUG_LIMIT}"
            )
        prompt = (run / "prompts" / task_id / f"{arm}.txt").read_text(encoding="utf-8")
        out = claim(run, task_id, arm)
        session = str(uuid.uuid4())
        rounds = []
        started = time.monotonic()
        model = manifest["model"]
        exe = manifest["claude_executable"]["path"]
        try:
            (out / "project").mkdir()
            copy_inputs(run, task["starter_id"], arm, out / "project")
            status = generate(out, out / "project", task_id, session, prompt, rounds, exe, model)
        except BaseException as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
            status = crash_status(rounds, model)
            publish(out, task_id, arm, session, rounds, started, status, model, error)
            raise
        return publish(out, task_id, arm, session, rounds, started, status, model)


def reconcile(store, run_id, task_id, arm):
    """Record an interrupted page from the rounds it kept. Re-runs no turn and never touches
    project/: each agent-N.json is read as the turn it holds, then the page is published."""
    run, manifest = locked_run(store, run_id)
    with run_lock(run):
        check_page_id(run, task_id, arm)
        out = run / "pages" / task_id / arm
        if not out.is_dir():
            raise SystemExit(f"refusing: {out} does not exist; there is nothing to reconcile")
        if (out / "run.json").exists():
            raise SystemExit(f"refusing: {out} already has run.json; nothing was interrupted")
        started = time.monotonic()
        rounds = []
        while (out / "rounds" / f"agent-{len(rounds)}.json").is_file():
            n = len(rounds)
            raw = (out / "rounds" / f"agent-{n}.json").read_text(encoding="utf-8")
            agent = turn_from_output(raw, started, None, "")
            agent["error"] = "interrupted before publish"
            rounds.append({"round": n, "agent": agent, "checklist": None})
        persisted = out / "rounds" / "session.txt"
        session = persisted.read_text(encoding="utf-8") if persisted.is_file() else None
        session = session or next(
            (r["agent"]["session_id"] for r in rounds if r["agent"]["session_id"]), None
        )
        error = {
            "type": "Interrupted",
            "message": "runner stopped before publishing; reconciled from rounds/",
        }
        model = manifest["model"]
        status = crash_status(rounds, model)
        return publish(
            out, task_id, arm, session, rounds, started, status, model, error, reconciled=True
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--store", default=str(DEFAULT_STORE))
    parser.add_argument("--run", required=True)
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="record an interrupted page from its rounds/ instead of running it",
    )
    parser.add_argument("task", help="task id, T01 to T10")
    parser.add_argument("arm", help="one of the four arm ids")
    args = parser.parse_args(argv)
    if args.reconcile:
        result = reconcile(args.store, args.run, args.task, args.arm)
    else:
        result = run_page(args.store, args.run, args.task, args.arm)
    print(json.dumps({k: result[k] for k in ("status", "turns", "model_flag")}, indent=2))
    if not result["model_conforms"]:
        return 3
    return 2 if result["status"] == CHECKER_FAULT else 0


if __name__ == "__main__":
    sys.exit(main())
