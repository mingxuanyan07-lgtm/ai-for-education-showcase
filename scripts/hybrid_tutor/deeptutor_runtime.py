"""Project-side process adapter for the isolated DeepTutor teaching role."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from .deeptutor_worker import validate_config as validate_worker_config

DEEPTUTOR_ROOT = Path(os.environ.get("DEEPTUTOR_ROOT", Path.home() / ".local/share/deeptutor"))
PYTHON = Path(os.environ.get("DEEPTUTOR_PYTHON", DEEPTUTOR_ROOT / (
    ".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")))
EXPECTED = {"provider": "deepseek-api", "model": "deepseek-flash",
            "workflow_variant": "deeptutor-chat-teaching-v1"}
RECOVERY_LIMIT = 16 * 1024 * 1024


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_bounded(path, errors):
    if not path.is_file():
        return None
    try:
        if path.stat().st_size > RECOVERY_LIMIT:
            raise ValueError("Bound exceeded")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Expected object")
        return value
    except (OSError, ValueError, UnicodeError) as exc:
        errors.append({"file": path.name, "class": type(exc).__name__})
        return None


def recover_result(directory):
    """Read complete snapshots; salvage an explicitly incomplete JSONL prefix."""
    errors = []
    terminal = _load_bounded(directory / "worker-result.json", errors)
    capture = _load_bounded(directory / "capture-state.json", errors)
    if terminal is not None:
        return terminal
    if capture is not None:
        result = capture
    else:
        result = {}
        chunks = directory / "provider-response.chunks.jsonl"
        if chunks.is_file():
            with chunks.open("rb") as handle:
                raw = handle.read(RECOVERY_LIMIT + 1)
            truncated = len(raw) > RECOVERY_LIMIT
            raw = raw[:RECOVERY_LIMIT]
            parts, models, usage, count = [], set(), None, 0
            for line in raw.splitlines(keepends=True):
                if not line.endswith(b"\n"):
                    truncated = True
                    break
                try:
                    chunk = json.loads(line)
                    if not isinstance(chunk, dict):
                        raise ValueError("Expected object")
                except (ValueError, UnicodeError):
                    truncated = True
                    break
                count += 1
                if isinstance(chunk.get("model"), str) and chunk["model"]:
                    models.add(chunk["model"])
                if chunk.get("usage") is not None:
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if not isinstance(choices, list):
                    truncated = True
                    break
                for choice in choices:
                    if not isinstance(choice, dict):
                        truncated = True
                        break
                    delta = choice.get("delta") or choice.get("message") or {}
                    if not isinstance(delta, dict):
                        truncated = True
                        break
                    if isinstance(delta.get("content"), str):
                        parts.append(delta["content"])
            result.update(raw_answer="".join(parts), usage=usage,
                actual_model=next(iter(models)) if len(models) == 1 else "未记录",
                response_observed=count > 0,
                recovery={"source": chunks.name, "complete_chunk_count": count,
                          "truncated": truncated, "incomplete": True, "byte_limit": RECOVERY_LIMIT})
    role = _load_bounded(directory / "role.json", errors) or {}
    result.setdefault("session_id", role.get("session_id"))
    result.setdefault("evidence_kind", "test_double" if role.get("execution_kind") == "test_double"
                      else "actual_provider" if role.get("execution_kind") == "actual_model_call" else "unknown")
    result.update(status="failed", failure_kind="incomplete_evidence",
                  error="Worker did not leave a complete terminal result; partial original evidence retained")
    result["evidence_read_errors"] = errors
    result.setdefault("recovery", {"source": "capture-state.json", "incomplete": True, "truncated": False})
    return result


def _execute(argv, cwd, environment, timeout):
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    started = time.monotonic()
    process = subprocess.Popen(argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        # The Windows venv launcher has a child interpreter. Kill precisely the
        # owned process tree, otherwise that child can keep making requests.
        from deepseek_rethlas.runtime import _stop_and_collect
        stdout, stderr, _ = _stop_and_collect(process)
    except BaseException:
        from deepseek_rethlas.runtime import _stop_and_collect
        _stop_and_collect(process)
        raise
    return {"exit_code": process.returncode, "timed_out": timed_out,
            "elapsed_seconds": time.monotonic() - started, "pid": process.pid,
            "stdout": stdout, "stderr": stderr}


def run_call(project_root, call_dir, prompt, config, timeout_seconds):
    root = Path(project_root).resolve(strict=True)
    directory = Path(call_dir).absolute()
    try:
        config = validate_worker_config(config)
    except RuntimeError as exc:
        raise ValueError("Unsupported DeepTutor teaching configuration") from exc
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Teaching prompt must be nonempty")
    if not directory.is_relative_to(root) or ".." in directory.parts:
        raise ValueError("Teaching artifacts must stay within project")
    checked = root
    for part in directory.relative_to(root).parts:
        checked /= part
        if checked.is_symlink() or (hasattr(checked, "is_junction") and checked.is_junction()):
            raise ValueError("Teaching artifacts cannot traverse a link")
    directory.mkdir(parents=True, exist_ok=False)
    if directory.resolve(strict=True) != directory:
        raise ValueError("Teaching artifacts cannot traverse a link")
    if not 0 < timeout_seconds <= 3600:
        raise ValueError("Teaching timeout outside approved bounds")
    (directory / "prompt.txt").write_bytes(prompt.encode("utf-8"))
    _write(directory / "config.json", config)
    worker = root / "scripts/hybrid_tutor/deeptutor_worker.py"
    source = worker.read_bytes()
    (directory / "deeptutor_worker.py.txt").write_bytes(source)
    (directory / "deeptutor_runtime.py.txt").write_bytes(Path(__file__).read_bytes())
    argv = [str(PYTHON), "-I", "-u", "-X", "utf8", str(worker),
            "--directory", str(directory), "--timeout", str(timeout_seconds)]
    _write(directory / "command.json", {"argv": argv, "cwd": str(directory),
        "worker_sha256": hashlib.sha256(source).hexdigest(), "environment_names_only": [
            "DEEPTUTOR_HOME", "TIKTOKEN_CACHE_DIR", "PYTHONUTF8", "PYTHONIOENCODING"]})
    env = dict(os.environ)
    env.update({"DEEPTUTOR_HOME": str(DEEPTUTOR_ROOT / "runtime"),
                "TIKTOKEN_CACHE_DIR": str(DEEPTUTOR_ROOT / "cache/tiktoken"),
                "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    try:
        outcome = _execute(argv, directory, env, timeout_seconds)
    except OSError as exc:
        outcome = {"exit_code": None, "timed_out": False, "elapsed_seconds": 0,
                   "pid": None, "stdout": b"", "stderr": b""}
        _write(directory / "launch-error.json", {"class": type(exc).__name__,
            "message_saved": False, "invocation_started": False})
    # Child deliberately emits only a status envelope. Arbitrary library stderr
    # can contain secrets, so retain byte counts / hashes rather than its bytes.
    _write(directory / "process.json", {**{key: outcome[key] for key in
        ("exit_code", "timed_out", "elapsed_seconds", "pid")},
        "stdout_sha256": hashlib.sha256(outcome["stdout"]).hexdigest(),
        "stderr_sha256": hashlib.sha256(outcome["stderr"]).hexdigest(),
        "stdout_bytes": len(outcome["stdout"]), "stderr_bytes": len(outcome["stderr"]),
        "raw_process_logs_saved": False, "scope": "Actual inputs/outputs captured at provider boundary; process logs may expose credentials"})
    path = directory / "worker-result.json"
    result = recover_result(directory)
    if outcome["timed_out"]:
        result.update(status="timeout", failure_kind="timeout", error="DeepTutor teaching subprocess deadline exceeded")
    elif outcome["exit_code"] != 0 or not path.is_file():
        result["original_worker_status"] = result.get("status")
        # A worker reports timeout/interrupted with exit 1 by design. Preserve
        # that failure category; only a success claim or unknown state downgrades.
        if result.get("status") not in {"timeout", "interrupted", "failed"}:
            result["status"] = "failed"
        result["failure_kind"] = result.get("failure_kind") or "runtime_failure"
        result["error"] = result.get("error") or "DeepTutor worker failed; process evidence retained"
    if result.get("status") == "completed" and (result.get("evidence_kind") != "actual_provider"
            or result.get("provider_request_attempts") != 1 or not result.get("response_observed")
            or result.get("actual_model") != config["model"]
            or result.get("actual_provider") != config["provider"]
            or result.get("tool_events") or result.get("violations")):
        result.update(status="failed", failure_kind="format_failure", error="DeepTutor evidence contract failed")
    result.setdefault("answer", result.get("raw_answer", ""))
    result.setdefault("actual_model", "未记录")
    result.setdefault("actual_provider", "未记录")
    result.setdefault("session_id", None)
    result.setdefault("tool_events", [])
    result.setdefault("provider_request_attempts", 0)
    result.setdefault("provider_request_observed", False)
    result.setdefault("response_observed", False)
    result["invocation_started"] = outcome["pid"] is not None
    result["elapsed_seconds"] = outcome["elapsed_seconds"]
    result["evidence_files"] = [str(item) for item in sorted(directory.rglob("*")) if item.is_file()
        and item.suffix in {".txt", ".json", ".jsonl"}]
    return result
