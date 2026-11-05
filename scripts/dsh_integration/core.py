"""Project workflow integration for DSH (AI for education).

This module wraps the project's existing entry points; it never replaces them:

* DeepTutor 1.6.15 official CLI (``python -m deeptutor``) and its loopback
  service scripts;
* original Codex Rethlas launcher (``scripts/Run-Rethlas.ps1``);
* DeepSeek staged pipeline (``scripts/deepseek_rethlas_cli.py``);
* unified archiving (``scripts/experiment_records.py``);
* evaluation entry (``scripts/eval_cli.py``).

It is not a model client. Background tasks are started only by explicit
``task_start(..., mode="run", allow_model_calls=True)``; read-only checks and
dry-runs never call a model. No credentials are read, copied, or logged.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT_ROOT / "scripts"
STATE_ROOT = PROJECT_ROOT / "work" / "dsh-orchestration"
TASKS_ROOT = STATE_ROOT / "tasks"
SERVICES_ROOT = STATE_ROOT / "services"

DEEPTUTOR_ROOT = Path(os.environ.get("DEEPTUTOR_ROOT", Path.home() / ".local/share/deeptutor"))
DEEPTUTOR_PY = Path(os.environ.get("DEEPTUTOR_PYTHON", DEEPTUTOR_ROOT / (
    ".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")))
DEEPTUTOR_RUNTIME_HOME = DEEPTUTOR_ROOT / "runtime"
DEEPTUTOR_BACKEND = "http://127.0.0.1:8106"
DEEPTUTOR_FRONTEND = "http://127.0.0.1:3783"
RETHLA_VERIFY_URL = "http://127.0.0.1:8091"
RETHLA_GENERATION = PROJECT_ROOT / "Rethlas" / "agents" / "generation"
PROJECT_PY = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
POWERSHELL = shutil.which("powershell.exe") or "powershell.exe"
MATH_PROBE = PROJECT_ROOT / "work" / "dsh-integration" / "evidence" / "math_local_probe.json"

CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
)
RUNNING: dict[str, subprocess.Popen] = {}
STATE_LOCK = threading.RLock()
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259

SAFE_STATUSES = {
    "running", "completed", "failed", "timeout", "interrupted", "unverified",
    "paused", "pending_final_review", "dry_run", "check_completed", "stale",
    "stopped", "not_started", "degraded", "starting", "degraded", "unknown",
}


class WorkflowError(RuntimeError):
    """Explicit integration failure; surfaced to the model as a tool error."""


# ---------------------------------------------------------------------------
# small utilities
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + secrets.token_hex(4)


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(4) + ".tmp")
    try:
        temporary.write_text(_json(data), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError):
        return default


def _safe_project_path(value: str, label: str, *, must_exist: bool = True) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError(f"{label} 必须是非空项目内相对路径")
    candidate = Path(value)
    if candidate.is_absolute():
        resolved = candidate.resolve(strict=False)
    else:
        if ".." in candidate.parts:
            raise WorkflowError(f"{label} 不允许包含上级目录：{value}")
        resolved = (PROJECT_ROOT / candidate).resolve(strict=False)
    if not resolved.is_relative_to(PROJECT_ROOT):
        raise WorkflowError(f"{label} 必须位于项目内：{value}")
    if must_exist and not resolved.is_file():
        raise WorkflowError(f"{label} 不存在：{value}")
    return resolved


def _sha256(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bounded(value, default, low, high, label):
    try:
        number = int(value)
    except (TypeError, ValueError):
        if value in (None, ""):
            return default
        raise WorkflowError(f"{label} 必须是整数")
    return max(low, min(high, number))


def _http_json(url: str, timeout: float = 2.0) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as response:
        body = response.read(2_000_000).decode("utf-8", "replace")
    try:
        return {"ok": True, "status": response.status, "json": json.loads(body)}
    except ValueError:
        return {"ok": True, "status": response.status, "body": body[:2000]}


def _http_status(url: str, timeout: float = 2.0) -> dict:
    try:
        result = _http_json(url, timeout)
        return {"reachable": True, **result}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        return {"reachable": False, "error": type(error).__name__ + ": " + str(error)[:300]}


def _pid_state(pid) -> dict:
    if not isinstance(pid, int) or pid <= 0:
        return {"alive": False, "exit_code": None}
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return {"alive": True, "exit_code": None}
        except OSError:
            return {"alive": False, "exit_code": None}
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return {"alive": False, "exit_code": None}
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return {"alive": False, "exit_code": None}
        if code.value == STILL_ACTIVE:
            return {"alive": True, "exit_code": None}
        return {"alive": False, "exit_code": int(code.value)}
    finally:
        kernel32.CloseHandle(handle)


def _kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, check=False, timeout=30)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def _tail(path: Path, lines: int) -> dict:
    try:
        if not path.is_file():
            return {"exists": False, "path": str(path), "lines": []}
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as error:
        return {"exists": False, "path": str(path), "error": str(error), "lines": []}
    return {"exists": True, "path": str(path), "total_lines": len(text),
            "lines": text[-max(1, lines):]}


# ---------------------------------------------------------------------------
# task persistence and process supervision
# ---------------------------------------------------------------------------
def _task_dir(task_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", task_id or ""):
        raise WorkflowError(f"task_id 不合法：{task_id!r}")
    return TASKS_ROOT / task_id


def _load_task(task_id: str) -> dict:
    task = _read_json(_task_dir(task_id) / "task.json")
    if not isinstance(task, dict):
        raise WorkflowError(f"任务不存在或状态文件损坏：{task_id}")
    return task


def _save_task(task: dict) -> None:
    with STATE_LOCK:
        _write_json(_task_dir(task["task_id"]) / "task.json", task)


def _all_tasks() -> list[dict]:
    if not TASKS_ROOT.is_dir():
        return []
    tasks = []
    for path in TASKS_ROOT.glob("*/task.json"):
        data = _read_json(path)
        if isinstance(data, dict) and data.get("task_id"):
            tasks.append(data)
    return sorted(tasks, key=lambda item: item.get("created_at") or "", reverse=True)


def _running_task_for(system: str) -> dict | None:
    for task in _all_tasks():
        if task.get("system") != system:
            continue
        state = _pid_state(task.get("pid"))
        if state["alive"]:
            return task
    return None


def _spawn(argv: list, cwd: Path, env: dict | None, task: dict) -> subprocess.Popen:
    directory = _task_dir(task["task_id"])
    directory.mkdir(parents=True, exist_ok=True)
    stdout_path = directory / "stdout.log"
    stderr_path = directory / "stderr.log"
    stdout = stdout_path.open("ab")
    stderr = stderr_path.open("ab")
    try:
        process = subprocess.Popen(
            argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
            stdout=stdout, stderr=stderr, creationflags=CREATION_FLAGS,
        )
    finally:
        stdout.close()
        stderr.close()
    task["pid"] = process.pid
    task["started_at_epoch"] = time.time()
    task["started_at"] = _now()
    task["status"] = "running"
    task["stdout"] = str(stdout_path)
    task["stderr"] = str(stderr_path)
    _save_task(task)
    RUNNING[task["task_id"]] = process
    _watch(task, process)
    return process


def _watch(task: dict, process: subprocess.Popen) -> None:
    max_seconds = task.get("max_seconds")
    deadline = (task.get("started_at_epoch") or time.time()) + (max_seconds or 10**9)

    def run():
        timed_out = False
        try:
            remaining = max(0.1, deadline - time.time())
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            timed_out = True
            task["status"] = "timeout"
            task["error"] = f"超过 max_seconds={max_seconds}；已终止进程树，不重试"
            task["timed_out"] = True
            _kill_tree(process.pid)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
        finally:
            current = _read_json(_task_dir(task["task_id"]) / "task.json", {})
            current["exit_code"] = process.returncode
            current["finished_at"] = _now()
            current["finished_at_epoch"] = time.time()
            if timed_out:
                current["status"] = "timeout"
                current["timed_out"] = True
                current["error"] = task.get("error")
            elif current.get("status") == "running":
                current["status"] = "exited"  # resolved from evidence by status tool
            _write_json(_task_dir(task["task_id"]) / "task.json", current)
            RUNNING.pop(task["task_id"], None)

    threading.Thread(target=run, name=f"watch-{task['task_id']}", daemon=True).start()


def _sync_task(system: str, mode: str, argv: list, cwd: Path, env: dict | None,
               inputs: dict, max_seconds: int, timeout_seconds: int = 120,
               dry_status: str = "dry_run") -> dict:
    task_id = _now_id()
    directory = _task_dir(task_id)
    directory.mkdir(parents=True, exist_ok=True)
    task = {
        "task_id": task_id, "system": system, "mode": mode,
        "created_at": _now(), "status": "running", "pid": None,
        "argv": argv, "cwd": str(cwd), "inputs": inputs,
        "max_seconds": max_seconds, "allow_model_calls": False,
    }
    _save_task(task)
    try:
        completed = subprocess.run(
            argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout_seconds, creationflags=CREATION_FLAGS,
        )
        stdout, stderr, code = completed.stdout or "", completed.stderr or "", completed.returncode
    except subprocess.TimeoutExpired as error:
        stdout = (error.stdout or "") if isinstance(error.stdout, str) else ""
        stderr = (error.stderr or "") if isinstance(error.stderr, str) else ""
        code = None
        task.update({"status": "timeout", "error": f"同步检查超时（{timeout_seconds}s）",
                     "timed_out": True})
    (directory / "stdout.log").write_text(stdout, encoding="utf-8")
    (directory / "stderr.log").write_text(stderr, encoding="utf-8")
    parsed = _last_json_object(stdout) or {}
    if task.get("status") == "running":
        if code == 0:
            task["status"] = dry_status
        else:
            task["status"] = "failed"
            task["error"] = (stderr.strip().splitlines()[-1] if stderr.strip() else f"退出码 {code}")
    task.update({"exit_code": code, "finished_at": _now(),
                 "finished_at_epoch": time.time(), "parsed_output": parsed})
    _save_task(task)
    return {"task": _public_task(task), "stdout": stdout[-8000:], "stderr": stderr[-8000:],
            "parsed_output": parsed}


def _public_task(task: dict) -> dict:
    keys = ("task_id", "system", "mode", "status", "pid", "exit_code", "created_at",
            "started_at", "finished_at", "timed_out", "error", "max_seconds",
            "allow_model_calls", "artifacts", "inputs", "warnings")
    return {key: task.get(key) for key in keys}


def _last_json_object(text: str) -> dict | None:
    """Parse a JSON object from mixed stdout (whole text first, then brace scan)."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    positions = [index for index, char in enumerate(text) if char == "{"]
    for position in positions:
        try:
            value, _end = decoder.raw_decode(text[position:])
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    for position in reversed(positions):
        try:
            value, _end = decoder.raw_decode(text[position:])
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


# ---------------------------------------------------------------------------
# DeepSeek staged pipeline adapter
# ---------------------------------------------------------------------------
def _deepseek_inputs(problem_file: str, requirements_file: str, user_prompt_file: str) -> dict:
    problem = _safe_project_path(problem_file, "problem_file")
    requirements = _safe_project_path(requirements_file, "requirements_file")
    user_prompt = _safe_project_path(user_prompt_file, "user_prompt_file")
    for path, label in ((problem, "problem_file"), (requirements, "requirements_file"),
                        (user_prompt, "user_prompt_file")):
        if path.suffix.lower() not in {".md", ".txt", ".tex"}:
            raise WorkflowError(f"{label} 必须是 .md/.txt/.tex 文本文件：{path.name}")
    return {
        "problem_file": problem.relative_to(PROJECT_ROOT).as_posix(),
        "requirements_file": requirements.relative_to(PROJECT_ROOT).as_posix(),
        "user_prompt_file": user_prompt.relative_to(PROJECT_ROOT).as_posix(),
        "sha256": {"problem": _sha256(problem), "requirements": _sha256(requirements),
                   "user_prompt": _sha256(user_prompt)},
    }


def _deepseek_argv(inputs: dict, options: dict, dry: bool) -> list:
    architecture = options.get("architecture_profile") or "legacy"
    if architecture not in {"legacy", "verified-v2"}:
        raise WorkflowError("architecture_profile 必须是 legacy 或 verified-v2")
    config = options.get("config") or (
        "setup/deepseek-rethlas-v2.json" if architecture == "verified-v2"
        else "setup/deepseek-rethlas.json")
    config_path = _safe_project_path(config, "config", must_exist=True)
    argv = [str(PROJECT_PY), "-X", "utf8", str(SCRIPTS / "deepseek_rethlas_cli.py"),
            "--project-root", str(PROJECT_ROOT), "--config",
            config_path.relative_to(PROJECT_ROOT).as_posix(), "run",
            "--problem-file", inputs["problem_file"],
            "--requirements-file", inputs["requirements_file"],
            "--user-prompt-file", inputs["user_prompt_file"]]
    if options.get("question_id"):
        argv += ["--question-id", str(options["question_id"])]
    for tag in options.get("tags") or []:
        argv += ["--tag", str(tag)]
    mapping = {
        "max_rounds": "--max-rounds", "timeout_seconds": "--timeout-seconds",
        "max_model_calls": "--max-model-calls", "max_tool_calls": "--max-tool-calls",
        "max_total_seconds": "--max-total-seconds", "max_observed_tokens": "--max-observed-tokens",
        "record_kind": "--record-kind", "teaching_contract": "--teaching-contract",
        "architecture_profile": "--architecture-profile",
    }
    for key, flag in mapping.items():
        value = options.get(key)
        if value not in (None, ""):
            argv += [flag, str(value)]
    if dry:
        argv.append("--dry-run")
    return argv


def _deepseek_run_dir(task: dict) -> Path | None:
    recorded = (task.get("artifacts") or {}).get("run_dir")
    if recorded and Path(recorded).is_dir():
        return Path(recorded)
    return None


def _deepseek_status(task: dict) -> dict:
    state = _pid_state(task.get("pid"))
    stdout_tail = "\n".join(_tail(Path(task.get("stdout") or ""), 400)["lines"])
    parsed = _last_json_object(stdout_tail) or {}
    run_dir = _deepseek_run_dir(task)
    added_dirs = []
    if run_dir is None and task.get("started_at_epoch"):
        root = PROJECT_ROOT / "work" / "deepseek-rethlas"
        if root.is_dir():
            candidates = [path for path in root.iterdir()
                          if path.is_dir() and path.stat().st_mtime >= task["started_at_epoch"] - 10]
            if candidates:
                run_dir = max(candidates, key=lambda path: path.stat().st_mtime)
                added_dirs.append(str(run_dir))
                task.setdefault("artifacts", {})["run_dir"] = str(run_dir)
                _save_task(task)
    artifacts = dict(task.get("artifacts") or {})
    derived = None
    evidence = []
    if run_dir and run_dir.is_dir():
        artifacts["run_dir"] = str(run_dir)
        request = run_dir / "final-review-request.json"
        summary = run_dir / "run.summary.json"
        if summary.is_file():
            summary_data = _read_json(summary, {}) or {}
            evidence.append({"file": str(summary), "status": summary_data.get("status"),
                             "failure_kind": summary_data.get("failure_kind")})
            derived = summary_data.get("status")
        if request.is_file():
            request_data = _read_json(request, {}) or {}
            artifacts["final_review_request"] = str(request)
            artifacts["review_prompt"] = str(run_dir / "review.prompt.txt")
            evidence.append({"file": str(request), "status": request_data.get("status")})
            derived = "pending_final_review"
            review = request_data.get("record") or {}
            if review.get("record_dir"):
                artifacts["record_dir"] = review["record_dir"]
                record_file = Path(review["record_dir"]) / "record.json"
                if record_file.is_file():
                    record = _read_json(record_file, {}) or {}
                    artifacts["record"] = str(record_file)
                    if record.get("status"):
                        derived = record.get("status")
                    evidence.append({"file": str(record_file), "status": record.get("status")})
        for name, key in (("blueprint_verified.md", "proof"),
                          ("proof.verification.json", "proof_report"),
                          ("teaching.verification.json", "teaching_report")):
            path = run_dir / name
            if path.is_file():
                artifacts[key] = str(path)
        calls_dir = run_dir / "calls"
        if calls_dir.is_dir():
            teaching = sorted(calls_dir.glob("*-teaching-*/host.answer.txt"))
            if teaching:
                artifacts["teaching"] = str(teaching[-1])
            calls = _read_json(run_dir / "calls.json", {}) or {}
            artifacts["calls"] = str(run_dir / "calls.json")
        final_answer = artifacts.get("teaching") or artifacts.get("proof")
        if final_answer:
            artifacts["final_answer"] = final_answer
    if parsed and parsed.get("status"):
        derived = parsed["status"]
    if state["alive"]:
        status = "running"
    else:
        status = derived or "unknown"
        if task.get("timed_out"):
            status = "timeout"
    result = {
        "task": _public_task({**task, "status": status, "artifacts": artifacts}),
        "status": status,
        "alive": state["alive"],
        "process_exit_code": state["exit_code"] if state["exit_code"] is not None else task.get("exit_code"),
        "run_dir": str(run_dir) if run_dir else None,
        "evidence": evidence,
        "parsed_stdout": parsed,
        "needs_current_codex_final_review": status == "pending_final_review",
        "final_review_note": ("DeepSeek 内部链通过只到 pending_final_review；必须由当前 Codex "
                              "对话阅读完整材料并保存真实终审报告后，用 finalize 归档。"
                              if status == "pending_final_review" else None),
    }
    task["status"] = status
    task["artifacts"] = artifacts
    _save_task(task)
    return result


# ---------------------------------------------------------------------------
# original Codex Rethlas adapter
# ---------------------------------------------------------------------------
def _rethlas_inputs(problem_file: str) -> dict:
    if not isinstance(problem_file, str) or not problem_file.strip():
        raise WorkflowError("problem_file 必须是 Rethlas/agents/generation 下的 data/*.md")
    normalized = problem_file.replace("\\", "/")
    if normalized.startswith("Rethlas/agents/generation/"):
        normalized = normalized[len("Rethlas/agents/generation/"):]
    if not normalized.startswith("data/") or not normalized.endswith(".md"):
        raise WorkflowError("problem_file 必须是 data/*.md（相对 Rethlas/agents/generation）")
    path = (RETHLA_GENERATION / normalized).resolve(strict=False)
    if not path.is_relative_to(RETHLA_GENERATION.resolve()):
        raise WorkflowError("problem_file 越出 Rethlas generation 目录")
    if not path.is_file():
        raise WorkflowError(f"题面不存在：{path}")
    problem_id = Path(normalized).relative_to("data").with_suffix("").as_posix()
    return {"problem_file": normalized, "problem_id": problem_id,
            "problem_path": str(path), "sha256": _sha256(path)}


def _write_sidecar(problem_path: Path, identity: dict | None) -> dict | None:
    if not identity:
        return None
    allowed = ("question_id", "ai_id", "run_id", "record_kind", "tags",
               "requirements", "user_prompt", "name", "ai_name")
    payload = {key: identity[key] for key in allowed if key in identity}
    if not payload:
        return None
    target = problem_path.with_suffix(".experiment.json")
    if target.exists():
        existing = _read_json(target, {}) or {}
        if existing == payload:
            return {"path": str(target), "written": False}
        raise WorkflowError(f"实验身份 sidecar 已存在且内容不同，拒绝覆盖：{target}")
    _write_json(target, payload)
    return {"path": str(target), "written": True}


def _rethlas_argv(inputs: dict, options: dict, dry: bool) -> list:
    max_iterations = _bounded(options.get("max_iterations"), 3, 1, 20, "max_iterations")
    timeout = _bounded(options.get("timeout_seconds"), 900, 1, 900, "timeout_seconds")
    argv = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            str(SCRIPTS / "Run-Rethlas.ps1"), "-ProblemFile", inputs["problem_file"],
            "-MaxIterations", str(max_iterations), "-ProjectRoot", str(PROJECT_ROOT),
            "-TimeoutSeconds", str(timeout)]
    if options.get("model"):
        argv += ["-Model", str(options["model"])]
    if options.get("reasoning_effort"):
        argv += ["-ReasoningEffort", str(options["reasoning_effort"])]
    if dry:
        argv.append("-DryRun")
    return argv


def _rethlas_status(task: dict) -> dict:
    state = _pid_state(task.get("pid"))
    inputs = task.get("inputs") or {}
    problem_id = inputs.get("problem_id") or ""
    generation = RETHLA_GENERATION
    logs = generation / "logs" / problem_id / "iter"
    run_files = sorted(logs.glob("*.run.json")) if logs.is_dir() else []
    latest = _read_json(run_files[-1], {}) if run_files else {}
    proof = generation / "results" / problem_id / "blueprint_verified.md"
    artifacts = dict(task.get("artifacts") or {})
    if proof.is_file():
        artifacts["proof"] = str(proof)
    if latest:
        artifacts["latest_run"] = str(run_files[-1])
        artifacts["model"] = latest.get("model")
        artifacts["reasoning_effort"] = latest.get("reasoning_effort")
        artifacts["timed_out"] = latest.get("timed_out")
        artifacts["exit_code"] = latest.get("exit_code")
    record = None
    sidecar = Path(inputs.get("problem_path", "")).with_suffix(".experiment.json")
    if sidecar.is_file():
        identity = _read_json(sidecar, {}) or {}
        if all(identity.get(key) for key in ("question_id", "ai_id", "run_id")):
            record = (PROJECT_ROOT / "docs" / "experiments" / identity["question_id"] /
                      identity["ai_id"] / identity["run_id"] / "record.json")
            if record.is_file():
                record_data = _read_json(record, {}) or {}
                artifacts["record"] = str(record)
                artifacts["record_status"] = record_data.get("status")
    exit_code = task.get("exit_code")
    if state["alive"]:
        status = "running"
    elif task.get("timed_out"):
        status = "timeout"
    elif exit_code == 0:
        status = "completed" if proof.is_file() else "failed"
    elif exit_code == 1:
        status = "unverified"
    elif exit_code == 130:
        status = "interrupted"
    elif exit_code in (2, None):
        status = "failed" if exit_code is not None else "unknown"
    else:
        status = "failed"
    task["status"] = status
    task["artifacts"] = artifacts
    _save_task(task)
    return {"task": _public_task(task), "status": status, "alive": state["alive"],
            "process_exit_code": exit_code, "problem_id": problem_id,
            "artifacts": artifacts, "latest_run": latest,
            "note": "Rethlas 自动记录钩子会归档真实运行；核验 verdict 是 Codex 核验智能体判定，"
                    "不是共同外审。"}


# ---------------------------------------------------------------------------
# DeepTutor adapter
# ---------------------------------------------------------------------------
def _deeptutor_env() -> dict:
    environment = dict(os.environ)
    environment.update({
        "DEEPTUTOR_HOME": str(DEEPTUTOR_RUNTIME_HOME),
        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8:replace",
        "PYTHONUNBUFFERED": "1", "NO_PROXY": "127.0.0.1,localhost,::1",
        "TIKTOKEN_CACHE_DIR": str(DEEPTUTOR_ROOT / "cache/tiktoken"),
    })
    return environment


def _deeptutor_argv(mode: str, options: dict) -> list:
    argv = [str(DEEPTUTOR_PY), "-I", "-X", "utf8", "-m", "deeptutor"]
    if mode == "check":
        return argv + ["doctor", "--format", "json"]
    prompt = options.get("prompt")
    prompt_file = options.get("prompt_file")
    if prompt_file and not prompt:
        path = _safe_project_path(prompt_file, "prompt_file")
        prompt = path.read_text(encoding="utf-8-sig")
    if not isinstance(prompt, str) or not prompt.strip():
        raise WorkflowError("DeepTutor 任务需要非空 prompt 或项目内 prompt_file")
    if len(prompt) > 30000:
        raise WorkflowError("DeepTutor 命令行 prompt 长度上限 30000 字符；请缩短或拆分任务")
    capability = options.get("capability") or "deep_solve"
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", capability):
        raise WorkflowError("capability 必须是安全标识")
    language = options.get("language") or "zh"
    argv += ["run", capability, prompt, "--format", "json", "--language", language]
    for item in options.get("config") or []:
        argv += ["--config", str(item)]
    if options.get("config_json"):
        argv += ["--config-json", str(options["config_json"])]
    return argv


def _deeptutor_events(task: dict) -> list[dict]:
    stdout = Path(task.get("stdout") or "")
    if not stdout.is_file():
        return []
    events = []
    for line in stdout.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _deeptutor_status(task: dict) -> dict:
    state = _pid_state(task.get("pid"))
    events = _deeptutor_events(task)
    done = next((event for event in reversed(events) if event.get("type") == "done"), None)
    result = next((event for event in reversed(events) if event.get("type") == "result"), None)
    session = next((event for event in events if event.get("type") == "session"), None)
    answer = ""
    if result and isinstance(result.get("metadata"), dict):
        response = result["metadata"].get("response")
        if isinstance(response, str):
            answer = response
    usage = (done or {}).get("metadata", {}).get("usage_summary") if isinstance(done, dict) else None
    turn_status = (done or {}).get("metadata", {}).get("status") if isinstance(done, dict) else None
    artifacts = dict(task.get("artifacts") or {})
    if session:
        artifacts["session_id"] = session.get("session_id")
        artifacts["turn_id"] = session.get("turn_id")
    if answer:
        answer_path = _task_dir(task["task_id"]) / "final-answer.md"
        answer_path.write_text(answer + "\n", encoding="utf-8")
        artifacts["final_answer"] = str(answer_path)
        artifacts["final_answer_sha256"] = _sha256(answer_path)
    if state["alive"]:
        status = "running"
    elif task.get("timed_out"):
        status = "timeout"
    elif done is None:
        status = "failed"
        artifacts["failure_reason"] = "事件流没有 done 终态（可能中断、worker_lost 或命令失败）"
    elif turn_status == "completed" and answer:
        status = "completed"
    else:
        status = "failed" if turn_status != "completed" else "failed"
        if turn_status and turn_status != "completed":
            artifacts["failure_reason"] = f"turn status={turn_status}"
        elif not answer:
            artifacts["failure_reason"] = "turn completed 但没有最终回答"
    task["status"] = status
    task["artifacts"] = artifacts
    _save_task(task)
    return {"task": _public_task(task), "status": status, "alive": state["alive"],
            "process_exit_code": state["exit_code"] if state["exit_code"] is not None else task.get("exit_code"),
            "turn_status": turn_status, "artifacts": artifacts,
            "usage": usage,
            "configured_route": {"provider": "deepseek", "model": "deepseek-flash"},
            "actual_model": "未记录",
            "note": "DeepTutor 原生上游 response.model 未保留；configured_route 是配置，"
                    "不能当作实际响应模型。"}


# ---------------------------------------------------------------------------
# services
# ---------------------------------------------------------------------------
def _service_path(service: str) -> Path:
    if service not in {"deeptutor", "rethlas_verification"}:
        raise WorkflowError("service 只能是 deeptutor 或 rethlas_verification")
    return SERVICES_ROOT / (service + ".json")


def _deeptutor_service_status() -> dict:
    status_script = DEEPTUTOR_ROOT / "scripts" / "Status-DeepTutor.ps1"
    try:
        completed = subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(status_script)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=45, creationflags=CREATION_FLAGS,
        )
        parsed = _last_json_object(completed.stdout) or {}
        result = {"ok": completed.returncode == 0, "exit_code": completed.returncode,
                  "status": parsed.get("status") or "unknown", "state": parsed,
                  "stderr": completed.stderr[-1000:] if completed.returncode else None}
    except (OSError, subprocess.TimeoutExpired) as error:
        result = {"ok": False, "status": "unknown", "error": str(error)}
    result["backend"] = _http_status(DEEPTUTOR_BACKEND + "/health/ready")
    result["frontend"] = _http_status(DEEPTUTOR_FRONTEND + "/chat")
    return result


def _rethlas_verification_status() -> dict:
    health = _http_status(RETHLA_VERIFY_URL + "/health")
    state = _read_json(_service_path("rethlas_verification"), {}) or {}
    pid = state.get("pid")
    process = _pid_state(pid)
    return {"health": health, "service_state": state or None,
            "pid": pid, "pid_alive": process["alive"],
            "started_by_orchestrator": bool(state)}


def _clean_env(base: dict | None = None) -> dict:
    """Collapse case-insensitive duplicate env keys.

    Windows can carry both NO_PROXY and no_proxy; PowerShell Start-Process
    builds a case-insensitive dictionary and aborts on such duplicates.
    Keep the upper-case spelling and drop the lower-case duplicate.
    """
    source = dict(os.environ if base is None else base)
    cleaned: dict = {}
    seen: dict[str, str] = {}
    for key, value in source.items():
        upper = key.upper()
        if upper in seen:
            existing = seen[upper]
            if key.isupper() and not existing.isupper():
                cleaned.pop(existing, None)
                cleaned[key] = value
                seen[upper] = key
            continue
        cleaned[key] = value
        seen[upper] = key
    return cleaned


def _start_service(service: str, wait_seconds: int) -> dict:
    if service == "deeptutor":
        current = _deeptutor_service_status()
        if current.get("status") == "ready" and current.get("backend", {}).get("reachable"):
            return {"ok": True, "service": service, "status": "ready",
                    "reused_existing": True, "detail": current}
        start_script = DEEPTUTOR_ROOT / "scripts" / "Start-DeepTutor.ps1"
        task_id = "svc-" + _now_id()
        log_dir = TASKS_ROOT / task_id
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout = (log_dir / "stdout.log").open("ab")
        stderr = (log_dir / "stderr.log").open("ab")
        try:
            process = subprocess.Popen(
                [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(start_script),
                 "-TimeoutSeconds", str(max(30, min(600, wait_seconds or 300)))],
                cwd=str(DEEPTUTOR_ROOT), stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                env=_clean_env(), creationflags=CREATION_FLAGS,
            )
        finally:
            stdout.close()
            stderr.close()
        _write_json(_service_path(service), {
            "service": service, "pid": process.pid, "started_at": _now(),
            "launcher_argv": [str(POWERSHELL), "-File", str(start_script)],
            "task_id": task_id, "stdout": str(log_dir / "stdout.log"),
            "stderr": str(log_dir / "stderr.log"),
        })
        return {"ok": True, "service": service, "status": "starting", "pid": process.pid,
                "task_id": task_id, "wait_seconds": wait_seconds,
                "detail": "启动脚本在后台运行；用 service_control(action=status) 有界检查。"}
    if service == "rethlas_verification":
        health = _http_status(RETHLA_VERIFY_URL + "/health")
        if health.get("reachable"):
            return {"ok": True, "service": service, "status": "already_running",
                    "owned_by_orchestrator": bool(_service_path(service).is_file()),
                    "detail": health}
        start_script = SCRIPTS / "Start-Verification.ps1"
        task_id = "svc-" + _now_id()
        log_dir = TASKS_ROOT / task_id
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout = (log_dir / "stdout.log").open("ab")
        stderr = (log_dir / "stderr.log").open("ab")
        try:
            process = subprocess.Popen(
                [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(start_script)],
                cwd=str(PROJECT_ROOT), stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                env=_clean_env(), creationflags=CREATION_FLAGS,
            )
        finally:
            stdout.close()
            stderr.close()
        _write_json(_service_path(service), {
            "service": service, "pid": process.pid, "started_at": _now(),
            "task_id": task_id, "stdout": str(log_dir / "stdout.log"),
            "stderr": str(log_dir / "stderr.log"),
        })
        return {"ok": True, "service": service, "status": "starting", "pid": process.pid,
                "task_id": task_id, "detail": "核验服务在后台启动；用 service_control(action=status) 检查 /health。"}
    raise WorkflowError("未知服务")


def _stop_service(service: str) -> dict:
    if service == "deeptutor":
        stop_script = DEEPTUTOR_ROOT / "scripts" / "Stop-DeepTutor.ps1"
        completed = subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(stop_script)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=90, creationflags=CREATION_FLAGS,
        )
        return {"ok": completed.returncode == 0, "service": service,
                "exit_code": completed.returncode,
                "stdout": completed.stdout[-3000:], "stderr": completed.stderr[-3000:],
                "note": "DeepTutor 停止脚本只按自身 state.json 验证身份，不按端口或进程名终止其他程序。"}
    if service == "rethlas_verification":
        state = _read_json(_service_path(service), {}) or {}
        pid = state.get("pid")
        if not state or not isinstance(pid, int):
            return {"ok": False, "service": service, "status": "not_owned",
                    "error": "核验服务不是本次编排启动的，拒绝停止；请由启动者自行停止。"}
        process = _pid_state(pid)
        if not process["alive"]:
            _service_path(service).unlink(missing_ok=True)
            return {"ok": True, "service": service, "status": "already_stopped", "pid": pid}
        _kill_tree(pid)
        time.sleep(1.0)
        after = _http_status(RETHLA_VERIFY_URL + "/health")
        if after.get("reachable"):
            return {"ok": False, "service": service, "status": "stop_failed",
                    "error": "/health 仍可访问；可能还有另一个服务实例，请人工检查，未再终止其他进程。"}
        _service_path(service).unlink(missing_ok=True)
        return {"ok": True, "service": service, "status": "stopped", "pid": pid}
    raise WorkflowError("未知服务")


# ---------------------------------------------------------------------------
# public operations (used by MCP tools and CLI)
# ---------------------------------------------------------------------------
def project_health() -> dict:
    dsh_install = Path(os.environ.get("DSH_INSTALL_ROOT") or
                       Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share")) /
                       "Programs" / "DeepSeek Harness")
    runtime = _read_json(dsh_install / "resources/runtime/primary-runtime/runtime.json", {}) or {}
    dsh_cli = Path(os.environ.get("DSH_CLI") or dsh_install / "resources/runtime/cli/bin/dsh.cmd")
    math = _read_json(MATH_PROBE, {}) or {}
    recent = _all_tasks()[:5]
    pending = []
    deepseek_root = PROJECT_ROOT / "work" / "deepseek-rethlas"
    if deepseek_root.is_dir():
        for run_dir in sorted(deepseek_root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:5]:
            request = run_dir / "final-review-request.json"
            if request.is_file():
                data = _read_json(request, {}) or {}
                record_dir = (data.get("record") or {}).get("record_dir")
                record_status = None
                record_file = Path(record_dir) / "record.json" if record_dir else None
                if record_file and record_file.is_file():
                    record_status = (_read_json(record_file, {}) or {}).get("status")
                if record_status == "pending_final_review" or record_file is None:
                    pending.append({"run_id": run_dir.name, "request": str(request),
                                    "record_status": record_status})
    services = {
        "deeptutor": _deeptutor_service_status(),
        "rethlas_verification": _rethlas_verification_status(),
    }
    warnings = []
    if services["deeptutor"].get("status") != "ready":
        warnings.append("DeepTutor 服务未就绪；任务前可用 service_control 启动并检查健康。")
    if not services["rethlas_verification"]["health"].get("reachable"):
        warnings.append("Rethlas 核验服务未运行；原版 Codex Rethlas 真实运行前需要它。")
    if not math:
        warnings.append("math-local 尚无本次验收探针记录；请先运行 math 探针。")
    return {
        "ok": True,
        "generated_at": _now(),
        "dsh": {
            "install_root": str(dsh_install), "desktop_runtime_version": runtime.get("desktopVersion") or "未记录",
            "node": runtime.get("node") or "未记录", "python": runtime.get("python") or "未记录",
            "cli": str(dsh_cli), "cli_exists": dsh_cli.is_file(),
            "profile_home": os.environ.get("DSH_HOME") or str(Path.home() / ".dsh"),
        },
        "project": {
            "root": str(PROJECT_ROOT), "python": str(PROJECT_PY), "python_exists": PROJECT_PY.is_file(),
            "agents_md": (PROJECT_ROOT / "AGENTS.md").is_file(),
            "docs": {name: (PROJECT_ROOT / "docs" / name).is_file() for name in
                     ("DeepSeek全流程.md", "Rethlas架构升级-v2.md", "模型与工作流评测接入.md",
                      "测试集指标说明.md", "数学教学AI测试记录.md")},
        },
        "services": services,
        "math_local": {
            "probe_evidence": str(MATH_PROBE), "probe_exists": MATH_PROBE.is_file(),
            "tools": math.get("tools"),
            "differentiate_2x": (math.get("symbolic_calculate") or {}).get("result"),
            "note": "math-local 已作为 DSH MCP server `math` 注册时，可直接使用 mcp__math__* 工具。",
        },
        "recent_tasks": [_public_task(task) | {"derived_status": _derive_status_quick(task)}
                         for task in recent],
        "pending_final_review_candidates": pending,
        "warnings": warnings,
        "model_calls_made": False,
    }


def _derive_status_quick(task: dict) -> str:
    if task.get("status") not in ("running", "exited"):
        return task.get("status") or "unknown"
    state = _pid_state(task.get("pid"))
    if state["alive"]:
        return "running"
    if task.get("timed_out"):
        return "timeout"
    if task.get("exit_code") is None:
        return "unknown"
    if task.get("system") == "rethlas_codex":
        return "pending_evidence_check"
    return "exited"


def service_control(service: str, action: str, *, confirm: bool = False,
                    wait_seconds: int = 0) -> dict:
    if service not in {"deeptutor", "rethlas_verification"}:
        raise WorkflowError("service 只能是 deeptutor 或 rethlas_verification")
    if action == "status":
        detail = _deeptutor_service_status() if service == "deeptutor" else _rethlas_verification_status()
        return {"ok": True, "service": service, "action": "status", "detail": detail,
                "model_calls_made": False}
    if action not in {"start", "stop"}:
        raise WorkflowError("action 只能是 status/start/stop")
    if not confirm:
        raise WorkflowError("启动/停止服务有副作用，请显式传 confirm=true（由用户确认后执行）")
    wait_seconds = _bounded(wait_seconds, 0, 0, 120, "wait_seconds")
    if action == "start":
        result = _start_service(service, wait_seconds)
        if wait_seconds > 0:
            deadline = time.time() + min(wait_seconds, 120)
            while time.time() < deadline:
                time.sleep(2.0)
                detail = (_deeptutor_service_status() if service == "deeptutor"
                          else _rethlas_verification_status())
                ready = (detail.get("status") == "ready" and detail.get("backend", {}).get("reachable")) \
                    if service == "deeptutor" else detail.get("health", {}).get("reachable")
                if ready:
                    result["status"] = "ready"
                    result["detail_after_wait"] = detail
                    break
                if service == "deeptutor" and detail.get("status") in {"failed", "stale"}:
                    break
            else:
                result["status"] = result.get("status") or "starting"
        result["model_calls_made"] = False
        return result
    result = _stop_service(service)
    result["model_calls_made"] = False
    return result


def _task_inputs(system: str, payload: dict) -> dict:
    if system == "deepseek":
        return _deepseek_inputs(payload.get("problem_file"), payload.get("requirements_file"),
                                payload.get("user_prompt_file"))
    if system == "rethlas_codex":
        return _rethlas_inputs(payload.get("problem_file"))
    if system == "deeptutor":
        prompt = payload.get("prompt")
        prompt_file = payload.get("prompt_file")
        if prompt_file and not prompt:
            path = _safe_project_path(prompt_file, "prompt_file")
            prompt = path.read_text(encoding="utf-8-sig")
        if not isinstance(prompt, str) or not prompt.strip():
            raise WorkflowError("DeepTutor 任务需要非空 prompt 或项目内 prompt_file")
        return {"prompt": prompt, "prompt_sha256": __import__("hashlib").sha256(
            prompt.encode("utf-8")).hexdigest()}
    raise WorkflowError("system 只能是 deepseek / rethlas_codex / deeptutor")


def task_start(*, system: str, mode: str = "run", allow_model_calls: bool = False,
               options: dict | None = None, identity: dict | None = None) -> dict:
    options = dict(options or {})
    if system not in {"deepseek", "rethlas_codex", "deeptutor"}:
        raise WorkflowError("system 只能是 deepseek / rethlas_codex / deeptutor")
    if mode not in {"run", "dry_run", "check"}:
        raise WorkflowError("mode 只能是 run / dry_run / check")
    running = _running_task_for(system)
    if running:
        raise WorkflowError(f"{system} 已有运行中的任务 {running['task_id']}；不并行启动，"
                            "先查询状态或取消后再试")
    if system == "deeptutor" and mode == "check":
        inputs = {}
    else:
        inputs = _task_inputs(system, options)
    # ---- synchronous no-model checks -------------------------------------
    if system == "deeptutor" and mode == "check":
        argv = _deeptutor_argv("check", {})
        return _sync_task(system, "check", argv, DEEPTUTOR_ROOT, _deeptutor_env(),
                          inputs, max_seconds=60, timeout_seconds=90,
                          dry_status="check_completed")
    if system == "deepseek" and mode == "dry_run":
        argv = _deepseek_argv(inputs, options, dry=True)
        return _sync_task(system, "dry_run", argv, PROJECT_ROOT, dict(os.environ),
                          inputs, max_seconds=60, timeout_seconds=120, dry_status="dry_run")
    if system == "rethlas_codex" and mode == "dry_run":
        argv = _rethlas_argv(inputs, options, dry=True)
        return _sync_task(system, "dry_run", argv, PROJECT_ROOT, dict(os.environ),
                          inputs, max_seconds=60, timeout_seconds=120, dry_status="dry_run")
    if mode != "run":
        raise WorkflowError(f"{system} 不支持 mode={mode}；可用 run 或 dry_run/check")
    # ---- real model-calling runs -----------------------------------------
    if not allow_model_calls:
        raise WorkflowError("真实运行会调用付费模型。请先 dry_run/check，并由用户确认后显式传 "
                            "allow_model_calls=true；本次未启动任何模型调用。")
    if system == "deepseek":
        argv = _deepseek_argv(inputs, options, dry=False)
        max_seconds = _bounded(options.get("max_total_seconds"), 3600, 60, 7200, "max_total_seconds")
        task = {
            "task_id": _now_id(), "system": system, "mode": "run",
            "created_at": _now(), "status": "starting", "pid": None,
            "argv": argv, "cwd": str(PROJECT_ROOT), "inputs": inputs,
            "max_seconds": max_seconds, "allow_model_calls": True,
            "artifacts": {}, "warnings": [],
        }
        _save_task(task)
        _spawn(argv, PROJECT_ROOT, dict(os.environ), task)
        # identify the freshly created run directory without invoking a model.
        deadline = time.time() + 15
        root = PROJECT_ROOT / "work" / "deepseek-rethlas"
        while time.time() < deadline:
            candidates = [path for path in root.iterdir()
                          if path.is_dir() and path.stat().st_mtime >= task["started_at_epoch"] - 5] if root.is_dir() else []
            if candidates:
                run_dir = max(candidates, key=lambda path: path.stat().st_mtime)
                task["artifacts"]["run_dir"] = str(run_dir)
                _save_task(task)
                break
            time.sleep(0.5)
        return {"ok": True, "task": _public_task(task), "model_calls_started": True,
                "note": "DeepSeek 分阶段链已后台启动；查询状态请用 task_status（有界、非阻塞轮询）。"}
    if system == "rethlas_codex":
        health = _http_status(RETHLA_VERIFY_URL + "/health")
        if not health.get("reachable"):
            raise WorkflowError("Rethlas 核验服务 8091 未运行或 /health 不可用；请先 "
                                "service_control(service=rethlas_verification, action=start, confirm=true)。")
        if options.get("model") or options.get("reasoning_effort"):
            task_warnings = ["已显式覆盖 Rethlas 模型/推理强度；实际值仍以 .run.json 为准。"]
        else:
            task_warnings = []
        argv = _rethlas_argv(inputs, options, dry=False)
        max_iterations = _bounded(options.get("max_iterations"), 3, 1, 20, "max_iterations")
        timeout = _bounded(options.get("timeout_seconds"), 900, 1, 900, "timeout_seconds")
        task = {
            "task_id": _now_id(), "system": system, "mode": "run",
            "created_at": _now(), "status": "starting", "pid": None,
            "argv": argv, "cwd": str(PROJECT_ROOT), "inputs": inputs,
            "max_seconds": max_iterations * timeout + 300, "allow_model_calls": True,
            "artifacts": {}, "warnings": task_warnings,
        }
        sidecar = _write_sidecar(Path(inputs["problem_path"]), identity)
        if sidecar:
            task["artifacts"]["sidecar"] = sidecar
        _save_task(task)
        _spawn(argv, PROJECT_ROOT, dict(os.environ), task)
        return {"ok": True, "task": _public_task(task), "model_calls_started": True,
                "note": "原版 Rethlas（Codex）已后台启动，内部模型是 Codex，不是 DeepSeek。"}
    if system == "deeptutor":
        argv = _deeptutor_argv("run", {**options, **inputs})
        max_seconds = _bounded(options.get("max_seconds"), 900, 60, 1800, "max_seconds")
        task = {
            "task_id": _now_id(), "system": system, "mode": "run",
            "created_at": _now(), "status": "starting", "pid": None,
            "argv": argv, "cwd": str(DEEPTUTOR_ROOT), "inputs": inputs,
            "max_seconds": max_seconds, "allow_model_calls": True,
            "artifacts": {}, "warnings": [],
        }
        _save_task(task)
        _spawn(argv, DEEPTUTOR_ROOT, _deeptutor_env(), task)
        return {"ok": True, "task": _public_task(task), "model_calls_started": True,
                "note": "DeepTutor 单轮任务已后台启动（deep_solve 会调用 DeepSeek 官方 API）；"
                        "查询状态请用 task_status。"}
    raise WorkflowError("unreachable")


def task_status(task_id: str) -> dict:
    task = _load_task(task_id)
    if task.get("mode") != "run":
        # Synchronous dry-run/check tasks have no process to re-derive status from.
        return {"task": _public_task(task), "status": task.get("status") or "unknown",
                "stdout_tail": _tail(Path(task.get("stdout") or ""), 40)["lines"],
                "stderr_tail": _tail(Path(task.get("stderr") or ""), 40)["lines"],
                "parsed_output": task.get("parsed_output")}
    # refresh exit code if the watcher did not record it
    if task.get("pid") and task.get("exit_code") is None:
        state = _pid_state(task["pid"])
        if not state["alive"] and state["exit_code"] is not None:
            task["exit_code"] = state["exit_code"]
            task["finished_at"] = task.get("finished_at") or _now()
            if task.get("status") == "running":
                task["status"] = "exited"
            _save_task(task)
    if task.get("system") == "deepseek":
        return _deepseek_status(task)
    if task.get("system") == "rethlas_codex":
        return _rethlas_status(task)
    if task.get("system") == "deeptutor":
        return _deeptutor_status(task)
    return {"task": _public_task(task), "status": task.get("status") or "unknown"}


def task_results(task_id: str) -> dict:
    task = _load_task(task_id)
    status = task_status(task_id)
    system = task.get("system")
    artifacts = status.get("artifacts") or task.get("artifacts") or {}
    result = {"task_id": task_id, "system": system, "status": status.get("status"),
              "artifacts": artifacts, "needs_current_codex_final_review":
                  bool(status.get("needs_current_codex_final_review"))}
    if system == "deepseek":
        request = artifacts.get("final_review_request")
        record_dir = artifacts.get("record_dir")
        excerpts = {}
        for key, path in (("proof", artifacts.get("proof")),
                          ("teaching", artifacts.get("teaching"))):
            if path and Path(path).is_file():
                text = Path(path).read_text(encoding="utf-8", errors="replace")
                excerpts[key] = {"path": path, "sha256": _sha256(Path(path)),
                                 "excerpt": text[:4000],
                                 "truncated": len(text) > 4000}
        result["excerpts"] = excerpts
        result["final_review"] = {
            "required_by_current_codex": status.get("status") == "pending_final_review",
            "request": request,
            "review_prompt": artifacts.get("review_prompt"),
            "record_dir": record_dir,
            "how_to_finish": ("当前 Codex 读取题面、证明、核验/修订记录和教学稿，独立复核后保存真实终审 "
                              "prompt 与报告，再运行 deepseek_rethlas_cli.py finalize；DSH 不能代替终审。")
            if status.get("status") == "pending_final_review" else None,
        }
    elif system == "rethlas_codex":
        proof = artifacts.get("proof")
        if proof and Path(proof).is_file():
            text = Path(proof).read_text(encoding="utf-8", errors="replace")
            result["excerpts"] = {"proof": {"path": proof, "sha256": _sha256(Path(proof)),
                                            "excerpt": text[:4000], "truncated": len(text) > 4000}}
        result["model_evidence"] = {"latest_run": artifacts.get("latest_run"),
                                    "model": artifacts.get("model"),
                                    "reasoning_effort": artifacts.get("reasoning_effort")}
        result["note"] = ("Rethlas/Codex 的实际模型与用量以 .run.json 与日志为准；"
                          "内部核验 correct 不是共同外审。")
    elif system == "deeptutor":
        answer_path = artifacts.get("final_answer")
        if answer_path and Path(answer_path).is_file():
            text = Path(answer_path).read_text(encoding="utf-8", errors="replace")
            result["final_answer"] = {"path": answer_path, "sha256": _sha256(Path(answer_path)),
                                      "text": text[:12000], "truncated": len(text) > 12000}
        result["session"] = {"session_id": artifacts.get("session_id"),
                             "turn_id": artifacts.get("turn_id")}
        result["actual_model"] = "未记录"
        result["note"] = ("DeepTutor 原生上游 response.model 未保留；configured route 只能记录为配置。"
                          "任务结果未经数学核验或共同外审。")
    return result


def task_logs(task_id: str, source: str = "stdout", lines: int = 80) -> dict:
    task = _load_task(task_id)
    lines = _bounded(lines, 80, 1, 500, "lines")
    source = source if source in {"stdout", "stderr"} else "stdout"
    path = Path(task.get(source) or "")
    return {"task_id": task_id, "source": source, "tail": _tail(path, lines)}


def task_cancel(task_id: str, *, confirm: bool = False) -> dict:
    task = _load_task(task_id)
    if not confirm:
        raise WorkflowError("取消任务会终止其进程树，请显式传 confirm=true")
    if _pid_state(task.get("pid"))["alive"]:
        _kill_tree(task["pid"])
        time.sleep(1.0)
    task = _load_task(task_id)
    task["status"] = "interrupted"
    task["cancelled"] = True
    task["finished_at"] = task.get("finished_at") or _now()
    _save_task(task)
    return {"ok": True, "task": _public_task(task), "note": "任务被中断；不是成功，也不会自动重试。"}


def prepare_final_review(task_id: str) -> dict:
    task = _load_task(task_id)
    if task.get("system") != "deepseek":
        raise WorkflowError("prepare_final_review 只适用于 DeepSeek 分阶段任务")
    status = _deepseek_status(task)
    if status.get("status") != "pending_final_review":
        return {"ok": False, "status": status.get("status"),
                "error": "当前任务不是 pending_final_review，不能进入当前 Codex 终审。",
                "task": status.get("task")}
    artifacts = status.get("artifacts") or {}
    request = artifacts.get("final_review_request")
    prompt = artifacts.get("review_prompt")
    checklist = [
        "阅读完整题面、教学要求与用户原始请求（final-review-request.json.inputs）",
        "独立复算证明，核对条件、定义域、边界与等号条件",
        "阅读 DeepSeek 证明核验与修订记录，但不得把其 correct 当作 Codex 结论",
        "独立复核中文教学稿：数学内容、新增例子/推广/练习、学生知识范围、要求落实",
        "v2/knowledge-route 时检查 claims、计算证据、claim-memory 与六项教学检查",
        "保存真实终审 prompt 与 JSON 报告，再调用 deepseek_rethlas_cli.py finalize",
    ]
    return {"ok": True, "task_id": task_id, "status": status.get("status"),
            "request": request, "review_prompt": prompt,
            "record_dir": artifacts.get("record_dir"),
            "checklist": checklist,
            "finalize_command": (
                f"& '{PROJECT_PY}' -X utf8 '{SCRIPTS / 'deepseek_rethlas_cli.py'}' finalize "
                f"--request '{request}' --prompt '<实际终审提示词文件>' --report '<实际终审报告.json>'"),
            "must_be_current_codex": True,
            "model_calls_made": False}


def archive_record(*, manifest: str, refresh: bool = True) -> dict:
    manifest_path = _safe_project_path(manifest, "manifest")
    if manifest_path.suffix.lower() != ".json":
        raise WorkflowError("manifest 必须是项目内 JSON 文件")
    argv = [str(PROJECT_PY), "-X", "utf8", str(SCRIPTS / "experiment_records.py"),
            "record", "--project-root", str(PROJECT_ROOT),
            "--manifest", manifest_path.relative_to(PROJECT_ROOT).as_posix()]
    completed = subprocess.run(argv, cwd=str(PROJECT_ROOT), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=180,
                               creationflags=CREATION_FLAGS)
    if completed.returncode != 0:
        raise WorkflowError("归档失败：" + (completed.stderr.strip()[-3000:] or f"退出码 {completed.returncode}"))
    result = _last_json_object(completed.stdout)
    if not result:
        raise WorkflowError("记录程序没有返回 JSON；不能宣称已归档：" + completed.stdout[-2000:])
    if refresh:
        subprocess.run([str(PROJECT_PY), "-X", "utf8", str(SCRIPTS / "experiment_records.py"),
                        "refresh", "--project-root", str(PROJECT_ROOT)],
                       cwd=str(PROJECT_ROOT), capture_output=True, timeout=180,
                       creationflags=CREATION_FLAGS)
    result["model_calls_made"] = False
    return result


def evaluation_prepare(*, spec: str, dry_run: bool = True) -> dict:
    spec_path = _safe_project_path(spec, "spec")
    if spec_path.suffix.lower() != ".json":
        raise WorkflowError("spec 必须是项目内 JSON 文件")
    argv = [str(PROJECT_PY), "-X", "utf8", str(SCRIPTS / "eval_cli.py"), "prepare",
            "--project-root", str(PROJECT_ROOT),
            "--spec", spec_path.relative_to(PROJECT_ROOT).as_posix()]
    if dry_run:
        argv.append("--dry-run")
    completed = subprocess.run(argv, cwd=str(PROJECT_ROOT), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=120,
                               creationflags=CREATION_FLAGS)
    if completed.returncode != 0:
        raise WorkflowError("评测准备失败：" + (completed.stderr.strip()[-3000:]
                                             or completed.stdout.strip()[-3000:] or f"退出码 {completed.returncode}"))
    result = _last_json_object(completed.stdout)
    if not result:
        raise WorkflowError("评测入口没有返回 JSON：" + completed.stdout[-2000:])
    result["model_calls_made"] = False
    return result


def evaluation_refresh() -> dict:
    completed = subprocess.run(
        [str(PROJECT_PY), "-X", "utf8", str(SCRIPTS / "eval_cli.py"), "refresh",
         "--project-root", str(PROJECT_ROOT)],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=180, creationflags=CREATION_FLAGS)
    if completed.returncode != 0:
        raise WorkflowError("刷新失败：" + (completed.stderr.strip()[-2000:] or f"退出码 {completed.returncode}"))
    result = _last_json_object(completed.stdout) or {"ok": True}
    result["model_calls_made"] = False
    return result
