"""Windows-native counterpart of Rethlas' generation/tests/run_example.sh.

Run with the project's virtual environment. This file and codex_runtime.py live
in <project-root>/scripts, while the official checkout is <project-root>/Rethlas.
The upstream AGENTS.md and its skills remain authoritative for the agent.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import signal
import subprocess
import sys
import threading
import time
from typing import TextIO

from rethlas_core.publication import check_or_publish


MAX_TURN_SECONDS = 900
REFERENCE_SUFFIXES = {".md", ".tex", ".txt"}
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
ANSI_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
LEGACY_SESSION_PATTERN = re.compile(r"session id:\s*(\S+)\s*$", re.IGNORECASE)


class RunnerError(Exception):
    """A recoverable preflight or execution failure, reported without a traceback."""


def _has_verified_proof(problem):
    try:
        return check_or_publish(problem)
    except ValueError as exc:
        raise RunnerError(str(exc)) from exc


@dataclass(frozen=True)
class Problem:
    generation_root: Path
    file: Path
    relative_file: str
    problem_id: str
    name: str
    reference_dir: Path
    relative_reference_dir: str
    result_dir: Path
    verified_path: Path


@dataclass(frozen=True)
class History:
    next_iteration: int
    session_id: str | None
    log_count: int


@dataclass
class RecordContext:
    """One invocation's outcome; only validated real runs arm the collector."""

    project_root: Path | None = None
    problem_file: str | None = None
    enabled: bool = False
    status: str = "failed"
    error: str | None = None

    def arm(self, project_root: Path, problem: Problem, args: argparse.Namespace) -> None:
        self.project_root = project_root
        self.problem_file = problem.relative_file
        self.enabled = not args.dry_run and not args.no_records


def _inside(path: Path, root: Path) -> bool:
    return path.is_relative_to(root)


def _validate_session_id(value: str, source: str) -> str:
    if not SESSION_ID_PATTERN.fullmatch(value):
        raise RunnerError(f"Invalid session ID in {source}.")
    return value


def validate_problem_path(generation_root: Path, problem_file: str) -> Problem:
    """Validate both path syntax and the final resolved location (including links)."""
    generation_root = generation_root.resolve(strict=True)
    windows_path = PureWindowsPath(problem_file)
    if windows_path.drive or windows_path.root or problem_file.startswith("/"):
        raise RunnerError("Problem file must be relative to agents/generation.")
    parts = problem_file.replace("\\", "/").split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise RunnerError("Problem file must not contain empty, '.' or '..' components.")
    if any(any(character in part for character in '<>:"|?*\x00') for part in parts):
        raise RunnerError("Problem file contains an invalid Windows path component.")
    if len(parts) < 2 or parts[0].lower() != "data" or Path(parts[-1]).suffix.lower() != ".md":
        raise RunnerError("Problem file must be a .md file under data/.")
    # Canonical spelling of data/ makes logs and result IDs consistent.
    parts[0] = "data"
    data_root = (generation_root / "data").resolve(strict=True)
    if not _inside(data_root, generation_root):
        raise RunnerError("The data directory resolves outside agents/generation.")
    target = generation_root.joinpath(*parts).resolve(strict=True)
    if not _inside(target, data_root) or not target.is_file():
        raise RunnerError("Problem file must resolve to a regular file inside data/.")
    relative_file = PurePosixPath(*parts).as_posix()
    problem_id = PurePosixPath(*parts[1:]).with_suffix("").as_posix()
    ref_relative = f"data/{problem_id}.refs"
    result_dir = (generation_root / "results" / Path(problem_id)).resolve()
    if not _inside(result_dir, generation_root):
        raise RunnerError("The result directory resolves outside agents/generation.")
    return Problem(
        generation_root=generation_root,
        file=target,
        relative_file=relative_file,
        problem_id=problem_id,
        name=PurePosixPath(problem_id).name,
        reference_dir=generation_root / Path(ref_relative),
        relative_reference_dir=ref_relative,
        result_dir=result_dir,
        verified_path=result_dir / "blueprint_verified.md",
    )


def _workspace_path(root: Path, value: str | None, default: Path, label: str) -> Path:
    path = Path(value) if value else default
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not _inside(path, root):
        raise RunnerError(f"{label} must stay inside agents/generation.")
    return path


def inspect_references(problem: Problem) -> tuple[list[str], list[str]]:
    """Use existing text references only. No PDF conversion or downloads occur."""
    reference_dir = problem.reference_dir
    if not reference_dir.exists():
        return [], []
    resolved = reference_dir.resolve(strict=True)
    if not _inside(resolved, problem.generation_root) or not resolved.is_dir():
        raise RunnerError("The reference directory must stay inside agents/generation.")
    text_files: list[str] = []
    pdf_files: list[str] = []
    for entry in sorted(resolved.rglob("*")):
        resolved_entry = entry.resolve()
        if not _inside(resolved_entry, resolved):
            raise RunnerError(f"Reference path escapes its reference directory: {entry}")
        if not entry.is_file():
            continue
        relative = entry.relative_to(resolved).as_posix()
        if entry.suffix.lower() in REFERENCE_SUFFIXES:
            text_files.append(relative)
        elif entry.suffix.lower() == ".pdf":
            pdf_files.append(relative)
    return text_files, pdf_files


def _session_from_event(line: str) -> str | None:
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(event, dict) or event.get("type") != "thread.started":
        return None
    thread_id = event.get("thread_id")
    if not isinstance(thread_id, str):
        raise RunnerError("A thread.started event is missing a valid thread_id.")
    return _validate_session_id(thread_id, "thread.started event")


def extract_session_ids(path: Path) -> set[str]:
    """Read current JSONL event logs and legacy Markdown CLI logs."""
    ids: set[str] = set()
    with path.open("r", encoding="utf-8-sig", errors="replace") as stream:
        for line in stream:
            clean_line = ANSI_PATTERN.sub("", line).strip()
            event_id = _session_from_event(clean_line)
            if event_id:
                ids.add(event_id)
            # Legacy logs put 'session id: ...' in the Codex startup header.
            if path.suffix.lower() == ".md":
                match = LEGACY_SESSION_PATTERN.search(clean_line)
                if match:
                    ids.add(_validate_session_id(match.group(1), str(path)))
    return ids


def load_history(log_dir: Path, problem_name: str, explicit_session_id: str | None = None) -> History:
    if explicit_session_id:
        _validate_session_id(explicit_session_id, "--session-id/SESSION_ID")
    if not log_dir.exists():
        return History(0, explicit_session_id, 0)
    if not log_dir.is_dir():
        raise RunnerError(f"Log directory is not a directory: {log_dir}")
    pattern = re.compile(rf"{re.escape(problem_name)}_iter_(\d+)\.(md|jsonl)\Z", re.IGNORECASE)
    iterations: dict[int, Path] = {}
    session_ids: set[str] = set()
    for path in sorted(log_dir.iterdir()):
        match = pattern.fullmatch(path.name)
        if not match:
            continue
        if not path.is_file() or not _inside(path.resolve(), log_dir.resolve()):
            raise RunnerError(f"Unsafe or non-file iteration log: {path}")
        iteration = int(match.group(1))
        if iteration in iterations:
            raise RunnerError(f"More than one log uses iteration {iteration}: {iterations[iteration]}, {path}")
        iterations[iteration] = path
        session_ids.update(extract_session_ids(path))
        # A resumed run may fail before its first JSON event. Its own metadata
        # retains the already-known thread ID for the next manual restart.
        metadata_path = path.with_suffix(".run.json")
        if metadata_path.is_file():
            if not _inside(metadata_path.resolve(), log_dir.resolve()):
                raise RunnerError(f"Unsafe metadata path: {metadata_path}")
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
            except (ValueError, OSError) as error:
                raise RunnerError(f"Cannot read run metadata: {metadata_path}") from error
            if not isinstance(metadata, dict):
                raise RunnerError(f"Invalid run metadata: {metadata_path}")
            for key in ("session_id_before", "session_id_after"):
                value = metadata.get(key)
                if value:
                    if not isinstance(value, str):
                        raise RunnerError(f"Invalid session ID in {metadata_path}")
                    session_ids.add(_validate_session_id(value, str(metadata_path)))
    if explicit_session_id:
        session_ids.add(explicit_session_id)
    if len(session_ids) > 1:
        raise RunnerError(f"Conflicting session IDs in {log_dir}; refusing to resume or override them.")
    session_id = next(iter(session_ids), None)
    if iterations and session_id is None:
        raise RunnerError(f"Could not recover a session ID from {log_dir}. Supply --session-id only if known.")
    return History(max(iterations, default=-1) + 1, session_id, len(iterations))


def make_prompt(problem: Problem, iteration: int, fresh: bool, web_mode: str) -> str:
    reference_prompt = (
        f"Use reference_dir={problem.relative_reference_dir} if it exists. "
        "Read only .md, .tex and .txt reference files, including previously extracted "
        ".txt files under .extracted/. Do not read PDF binaries: this Windows runner "
        "does not extract PDFs; those references must be preprocessed to text first."
    )
    if fresh:
        introduction = (
            f"Use AGENTS.md exactly to solve the math problem in {problem.relative_file}. "
            f"Use problem_id={problem.problem_id}. {reference_prompt}"
        )
    else:
        introduction = (
            "Please continue from the persisted memory and working draft. "
            "Follow AGENTS.md exactly. "
            f"The problem is {problem.relative_file}; use problem_id={problem.problem_id}. "
            f"{reference_prompt}"
        )
    if web_mode == "disabled":
        search_prompt = (
            "Do not use search tools such as arXiv theorem search or web search during "
            "this turn; think deeply by yourself."
        )
    else:
        search_prompt = (
            "You may use search tools such as arXiv theorem search and web search "
            "during this turn, but also think deeply by yourself."
        )
    return f"{introduction}\n\nIteration: {iteration}. {search_prompt}\n"


def _terminate_owned_process_tree(process: subprocess.Popen[str]) -> None:
    """Terminate this runner's child and descendants; never kill by image name."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        # Popen(start_new_session=True) creates a group owned by this child.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_iteration(
    problem: Problem,
    log_dir: Path,
    iteration: int,
    session_id: str | None,
    model: str,
    effort: str,
    timeout_seconds: int,
) -> str | None:
    from codex_runtime import build_command, child_environment

    web_mode = "disabled" if iteration % 2 else "live"
    # The initial turn has no previous research context, so use the explicitly
    # bounded no-search mode; subsequent even turns enable live search.
    if iteration == 0:
        web_mode = "disabled"
    prompt = make_prompt(problem, iteration, session_id is None, web_mode)
    base_path = log_dir / f"{problem.name}_iter_{iteration}"
    paths = {
        "events": base_path.with_name(base_path.name + ".jsonl"),
        "stderr": base_path.with_name(base_path.name + ".stderr.log"),
        "prompt": base_path.with_name(base_path.name + ".prompt.txt"),
        "metadata": base_path.with_name(base_path.name + ".run.json"),
    }
    # Also protect an upstream Markdown log at the same index.
    for path in [*paths.values(), base_path.with_name(base_path.name + ".md")]:
        if path.exists() or path.is_symlink():
            raise RunnerError(f"Refusing to overwrite existing log/artifact: {path}")
    command = build_command(problem.generation_root, model, effort, session_id=session_id, web_mode=web_mode)
    if not isinstance(command, (list, tuple)) or not command or not all(isinstance(arg, str) for arg in command):
        raise RunnerError("codex_runtime.build_command must return a nonempty argv sequence.")
    command = list(command)
    if "--dangerously-bypass-approvals-and-sandbox" in command or "--yolo" in command:
        raise RunnerError("Refusing to run Codex with sandbox/approval bypass.")
    # The shared command builder owns CLI compatibility and the required
    # --sandbox workspace-write / approval_policy=never / --json flags.
    # '-' passes the prompt through stdin instead of Windows command-line quoting.
    command.append("-")
    metadata: dict[str, object] = {
        "format_version": 1,
        "problem_file": problem.relative_file,
        "problem_id": problem.problem_id,
        "iteration": iteration,
        "web_mode": web_mode,
        "model": model,
        "reasoning_effort": effort,
        "started_at": _utc_now(),
        "finished_at": None,
        "session_id_before": session_id,
        "session_id_after": None,
        "exit_code": None,
        "timed_out": False,
        "error": None,
    }
    process: subprocess.Popen[str] | None = None
    discovered_ids: set[str] = set()
    reader_errors: list[str] = []
    state_lock = threading.Lock()
    readers: list[threading.Thread] = []
    with ExitStack() as stack:
        handles: dict[str, TextIO] = {}
        for key, path in paths.items():
            handles[key] = stack.enter_context(path.open("x", encoding="utf-8", newline="\n"))
        handles["prompt"].write(prompt)
        handles["prompt"].flush()
        json.dump(metadata, handles["metadata"], ensure_ascii=False, indent=2)
        handles["metadata"].write("\n")
        handles["metadata"].flush()

        def copy_stream(stream: TextIO, destination: TextIO, parse_events: bool) -> None:
            try:
                for line in stream:
                    destination.write(line)
                    destination.flush()
                    if parse_events:
                        found = _session_from_event(line.strip())
                        if found:
                            with state_lock:
                                discovered_ids.add(found)
            except (OSError, ValueError, RunnerError) as error:
                with state_lock:
                    reader_errors.append(str(error))

        try:
            kwargs: dict[str, object] = {}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            process = subprocess.Popen(
                command,
                cwd=problem.generation_root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=child_environment(),
                **kwargs,
            )
            assert process.stdin is not None and process.stdout is not None and process.stderr is not None
            for stream, key, parse_events in (
                (process.stdout, "events", True),
                (process.stderr, "stderr", False),
            ):
                reader = threading.Thread(target=copy_stream, args=(stream, handles[key], parse_events), daemon=True)
                reader.start()
                readers.append(reader)
            process.stdin.write(prompt)
            process.stdin.close()
            started = time.monotonic()
            next_progress = started + 30
            deadline = started + timeout_seconds
            while process.poll() is None:
                now = time.monotonic()
                with state_lock:
                    current_ids = set(discovered_ids)
                    current_errors = list(reader_errors)
                if current_errors:
                    raise RunnerError(f"Failed while recording Codex output: {current_errors[0]}")
                if len(current_ids) > 1 or (session_id and current_ids and current_ids != {session_id}):
                    raise RunnerError("Codex returned a conflicting thread ID; refusing to continue.")
                if now >= deadline:
                    metadata["timed_out"] = True
                    raise RunnerError(f"Iteration {iteration} exceeded {timeout_seconds} seconds; its process tree was stopped.")
                if now >= next_progress:
                    print(f"  Iteration {iteration}: still running ({int(now - started)} seconds).", flush=True)
                    next_progress = now + 30
                try:
                    process.wait(timeout=min(1.0, deadline - now))
                except subprocess.TimeoutExpired:
                    pass
            metadata["exit_code"] = process.returncode
        except KeyboardInterrupt:
            metadata["error"] = "Interrupted by user."
            raise
        except (OSError, RunnerError) as error:
            metadata["error"] = str(error)
            if isinstance(error, RunnerError):
                raise
            raise RunnerError(f"Could not execute Codex: {error}") from error
        finally:
            if process is not None:
                if process.poll() is None:
                    _terminate_owned_process_tree(process)
                metadata["exit_code"] = process.returncode
                for reader in readers:
                    reader.join(timeout=5)
                with state_lock:
                    current_ids = set(discovered_ids)
                if len(current_ids) == 1:
                    metadata["session_id_after"] = next(iter(current_ids))
                elif not current_ids:
                    metadata["session_id_after"] = session_id
            metadata["finished_at"] = _utc_now()
            handles["metadata"].seek(0)
            handles["metadata"].truncate()
            json.dump(metadata, handles["metadata"], ensure_ascii=False, indent=2)
            handles["metadata"].write("\n")
            handles["metadata"].flush()
        with state_lock:
            current_ids = set(discovered_ids)
            current_errors = list(reader_errors)
        if current_errors:
            raise RunnerError(f"Failed while recording Codex output: {current_errors[0]}")
        if any(reader.is_alive() for reader in readers):
            raise RunnerError("Codex output streams did not close; logs are incomplete and require review.")
        if len(current_ids) > 1 or (session_id and current_ids and current_ids != {session_id}):
            raise RunnerError("Codex returned a conflicting thread ID; refusing to continue.")
        if metadata["exit_code"] != 0:
            raise RunnerError(f"Codex exited with code {metadata['exit_code']}; see {paths['stderr']} and {paths['events']}.")
        recovered_id = next(iter(current_ids), session_id)
        if recovered_id is None and not problem.verified_path.is_file():
            raise RunnerError(f"No thread.started.thread_id was recorded in {paths['events']}; cannot resume safely.")
        return recovered_id


def _positive_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _turn_timeout(value: str) -> int:
    number = _positive_integer(value)
    if number > MAX_TURN_SECONDS:
        raise argparse.ArgumentTypeError(f"must not exceed {MAX_TURN_SECONDS} seconds")
    return number


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--problem-file", default=os.environ.get("PROBLEM_FILE", "data/windows_smoke.md"))
    parser.add_argument("--model", default=os.environ.get("MODEL"))
    parser.add_argument("--reasoning-effort", "--effort", dest="effort", default=os.environ.get("REASONING_EFFORT"))
    parser.add_argument("--max-iterations", type=_positive_integer, default=os.environ.get("MAX_ITERATIONS", "3"))
    parser.add_argument("--session-id", default=os.environ.get("SESSION_ID") or None)
    parser.add_argument("--log-dir", default=os.environ.get("LOG_DIR") or None)
    parser.add_argument("--pause-file", default=os.environ.get("PAUSE_FILE") or None)
    parser.add_argument("--timeout-seconds", "--iteration-timeout-seconds", dest="timeout_seconds", type=_turn_timeout, default="900")
    parser.add_argument("--dry-run", action="store_true", default=os.environ.get("DRY_RUN", "0") == "1")
    parser.add_argument("--no-records", action="store_true", help="Skip automatic experiment folder records and index updates.")
    return parser.parse_args(argv)


def _run_main(argv: list[str] | None, record: RecordContext) -> int:
    args = parse_args(argv)
    if os.environ.get("DRY_RUN", "0") not in {"0", "1"}:
        raise RunnerError("DRY_RUN must be 0 or 1.")
    project_root = args.project_root.resolve(strict=True)
    from codex_runtime import load_settings
    settings = load_settings(project_root)
    args.model = args.model or settings["model"]
    args.effort = args.effort or settings["reasoning_effort"]
    generation_root = project_root / "Rethlas" / "agents" / "generation"
    if not generation_root.is_dir():
        raise RunnerError(f"Missing Rethlas generation directory: {generation_root}")
    generation_root = generation_root.resolve(strict=True)
    if not _inside(generation_root, project_root):
        raise RunnerError("The Rethlas generation directory resolves outside --project-root.")
    instructions = generation_root / "AGENTS.md"
    if not instructions.is_file() or not _inside(instructions.resolve(), generation_root):
        raise RunnerError(f"Missing or unsafe AGENTS.md: {instructions}")
    problem = validate_problem_path(generation_root, args.problem_file)
    log_dir = _workspace_path(
        generation_root, args.log_dir,
        generation_root / "logs" / Path(problem.problem_id) / "iter", "Log directory",
    )
    pause_path = _workspace_path(
        generation_root, args.pause_file,
        problem.result_dir / "PAUSE_AFTER_ITERATION", "Pause file",
    )
    # An unreadable or malformed question is a preflight error, not a test.
    # Validate the session argument even when an existing proof short-circuits.
    problem.file.read_text(encoding="utf-8-sig")
    if args.session_id:
        _validate_session_id(args.session_id, "--session-id/SESSION_ID")
    if problem.verified_path.is_file():
        record.arm(project_root, problem, args)
        if not args.dry_run:
            _has_verified_proof(problem)
        record.status = "reused"
        print(f"Verified blueprint already exists: {problem.verified_path}")
        return 0
    if pause_path.exists() or pause_path.is_symlink():
        raise RunnerError(f"Pause marker already exists: {pause_path}. Review and remove it before resuming.")
    references, pdfs = inspect_references(problem)
    history = load_history(log_dir, problem.name, args.session_id)
    print(f"Generation directory: {generation_root}")
    print(f"Problem: {problem.relative_file} (problem_id={problem.problem_id})")
    print(f"Model: {args.model}; reasoning effort: {args.effort}")
    print(f"Mode: {'resume' if history.session_id else 'new'}; next iteration: {history.next_iteration}")
    if history.session_id:
        print(f"Session: {history.session_id}")
    print(f"Additional iterations: {args.max_iterations}; turn limit: {args.timeout_seconds} seconds")
    print(f"Logs: {log_dir}")
    print(f"Stop file: {problem.verified_path}")
    print(f"Pause file: {pause_path}")
    print(f"Text references: {len(references)} (.md/.tex/.txt only)")
    if pdfs:
        print(f"WARNING: {len(pdfs)} PDF reference(s) will be ignored. Extract them to .txt before using their contents.")
    if args.dry_run:
        print("Dry run: no model, subprocess, health request, or log write was started.")
        return 0
    record.arm(project_root, problem, args)
    from urllib.request import ProxyHandler, build_opener
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open("http://127.0.0.1:8091/health", timeout=5) as response:
            health = json.load(response)
        if health.get("status") != "ok":
            raise ValueError("Unexpected health response")
    except Exception as error:
        raise RunnerError("Verification service is unavailable. Run scripts/Start-Verification.ps1 in another terminal first.") from error
    log_dir.mkdir(parents=True, exist_ok=True)
    lock_path = log_dir / ".runner.lock"
    try:
        lock = lock_path.open("x", encoding="utf-8", newline="\n")
    except FileExistsError as error:
        raise RunnerError(f"Runner lock already exists: {lock_path}. Check the previous run before removing a stale lock.") from error
    try:
        with lock:
            json.dump({"pid": os.getpid(), "started_at": _utc_now(), "problem_id": problem.problem_id}, lock)
            lock.write("\n")
            lock.flush()
            # Recheck after acquiring the lock so parallel starts cannot reuse
            # stale history or overwrite another runner's completed iteration.
            if pause_path.exists() or pause_path.is_symlink():
                raise RunnerError(f"Pause marker appeared before execution: {pause_path}")
            history = load_history(log_dir, problem.name, args.session_id)
            session_id = history.session_id
            for iteration in range(history.next_iteration, history.next_iteration + args.max_iterations):
                if _has_verified_proof(problem):
                    record.status = "completed"
                    print(f"Verified blueprint exists: {problem.verified_path}")
                    return 0
                if pause_path.exists() or pause_path.is_symlink():
                    record.status = "paused"
                    print(f"Paused before iteration {iteration}: {pause_path}")
                    return 0
                print(f"Starting iteration {iteration}.", flush=True)
                session_id = run_iteration(
                    problem, log_dir, iteration, session_id,
                    args.model, args.effort, args.timeout_seconds,
                )
                print(f"Finished iteration {iteration}.", flush=True)
                if _has_verified_proof(problem):
                    record.status = "completed"
                    print(f"Verified blueprint exists: {problem.verified_path}")
                    return 0
                if pause_path.exists() or pause_path.is_symlink():
                    record.status = "paused"
                    print(f"Paused after iteration {iteration}: {pause_path}")
                    return 0
    finally:
        lock_path.unlink(missing_ok=True)
    print(f"Completed {args.max_iterations} iterations without a verified blueprint. Run again to resume.")
    record.status = "unverified"
    return 1


def _record_outcome(record: RecordContext) -> None:
    if not record.enabled:
        return
    try:
        from experiment_records import record_rethlas_run

        assert record.project_root is not None and record.problem_file is not None
        result = record_rethlas_run(
            record.project_root, record.problem_file, record.status, error=record.error,
        )
        print(f"Experiment record: {result['record_path']}", flush=True)
        print(f"Experiment folder: {result['record_dir']}", flush=True)
        print(f"Experiment ID: {result['experiment_id']}", flush=True)
    except (Exception, KeyboardInterrupt) as error:
        # A collection failure must not hide or replace the solver's outcome.
        print(
            f"WARNING: AUTOMATIC EXPERIMENT RECORD FAILED: {error}. "
            "The original run outcome is retained; review the preserved source logs.",
            file=sys.stderr, flush=True,
        )


def main(argv: list[str] | None = None) -> int:
    """Run and collect exactly once, including when imported by another script."""
    record = RecordContext()
    try:
        return _run_main(argv, record)
    except KeyboardInterrupt:
        record.status = "interrupted"
        record.error = "Interrupted by user."
        raise
    except Exception as error:
        record.status = "failed"
        record.error = str(error)
        raise
    finally:
        _record_outcome(record)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Run interrupted; its Codex child process tree was stopped. Logs were preserved.", file=sys.stderr)
        sys.exit(130)
    except (RunnerError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(2)
