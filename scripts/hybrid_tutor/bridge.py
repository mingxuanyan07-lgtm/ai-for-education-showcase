"""Restricted asynchronous task bridge. No model execution is authorized by a tool argument.

The host-created authorization binds a frozen config and a finite question set.
Every worker owns only the child process it starts; cancellation never selects a
process by a port or by an unverified PID from disk.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")
TASK = re.compile(r"hyb_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{16}\Z")
RESERVED = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?\Z", re.I)
MAX_TEXT = 256 * 1024
MAX_JSON = 32 * 1024 * 1024
WINDOWS_COMMIT_DELAYS = (0.02, 0.04, 0.08, 0.16, 0.20)
PARTIAL_FILE_LIMIT = 128 * 1024
PARTIAL_TOTAL_LIMIT = 512 * 1024
CALL_DIRECTORY = re.compile(r"[0-9]{2,3}-(?:solve-[1-3]|proof-review-[1-3]|teaching-[1-2]|review-teaching(?:-[1-2])?)\Z")
# This deliberately excludes configuration, auth, environment, command and
# arbitrary runtime files. DSH may not flush an in-flight response before hard
# termination; absent bytes are never reconstructed or presented as observed.
PARTIAL_FILES = (
    "runtime/capture-state.json", "runtime/worker-result.json",
    "runtime/provider-response.chunks.jsonl", "host.answer.txt", "host.result.json",
    "host.prompt.txt", "runtime/result.json", "runtime/actual-input-messages.json",
    "runtime/prompt.txt",
)
TERMINAL = {"dry_run", "failed", "timeout", "interrupted", "unverified", "pending_final_review", "completed", "host_lost", "artifact_failure"}


class BridgeError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def safe_id(value, label="标识"):
    if not isinstance(value, str) or not SAFE.fullmatch(value) or RESERVED.fullmatch(value) or value.endswith("."):
        raise BridgeError(label + "不是安全标识")
    return value


def inside(root, path, *, exists=True):
    root = Path(root).resolve(strict=True)
    path = Path(path)
    path = path if path.is_absolute() else root / path
    if ".." in path.parts:
        raise BridgeError("不允许上级路径")
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise BridgeError("路径不在允许目录") from exc
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise BridgeError("不允许符号链接或目录联接")
    resolved = path.resolve(strict=exists)
    if not resolved.is_relative_to(root):
        raise BridgeError("路径不在允许目录")
    return resolved


def read_json(path):
    raw = Path(path).read_bytes()
    if len(raw) > MAX_JSON:
        raise BridgeError("JSON 超过 32 MiB 可读取范围；原文件保留")
    obj = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(obj, dict):
        raise BridgeError("需要 JSON 对象")
    return obj


def _commit_file_with_retry(temporary, path, *, replace_existing=True):
    """Commit the same closed temp file, retrying only brief Windows locks.

    Six attempts / 500 ms of scheduled waiting at most. Failed snapshots remain
    on disk, and permanent errors retain their original exception. This never
    retries a model, serialization or write. Create-only receipts/outcomes use a
    no-clobber operation even if another publisher races the initial check.
    """
    for delay in (*WINDOWS_COMMIT_DELAYS, None):
        try:
            if replace_existing:
                os.replace(temporary, path)
            elif os.name == "nt":
                os.rename(temporary, path)  # Windows rename refuses an existing target.
            else:
                os.link(temporary, path)  # Atomic create-only publication on POSIX.
                Path(temporary).unlink()
            return
        except OSError as exc:
            if (os.name != "nt" or getattr(exc, "winerror", None) not in {5, 32, 33}
                    or delay is None):
                raise
            time.sleep(delay)


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + "." + secrets.token_hex(4) + ".tmp")
    temp.write_bytes(json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))
    _commit_file_with_retry(temp, path)


@contextmanager
def store_lock(folder):
    """Use an OS-owned lock, automatically released after a crash."""
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".lock").open("a+b") as handle:
        handle.seek(0)
        if not handle.read(1):
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + 10
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise BridgeError("任务存储忙，请稍后重试")
                time.sleep(.05)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)


def process_identity(pid):
    """Creation time prevents a recycled PID being mistaken for this worker."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return None
            try:
                times = [wintypes.FILETIME() for _ in range(4)]
                if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                    return None
                return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
            finally:
                kernel.CloseHandle(handle)
        stat = Path(f"/proc/{int(pid)}/stat").read_text()
        return stat[stat.rfind(")") + 2:].split()[19]
    except (OSError, ValueError, IndexError):
        return None


class Bridge:
    def __init__(self, root=ROOT, launcher=None):
        self.root = Path(root).resolve(strict=True)
        self.tasks = inside(self.root, "work/hybrid-tutor/tasks", exists=False)
        self.config_path = inside(self.root, "setup/hybrid-tutor.json", exists=False)
        self.authorization_path = inside(self.root, "setup/hybrid-tutor-authorization.json", exists=False)
        self.launcher = launcher or self._launch

    def _load(self, task_id):
        if not isinstance(task_id, str) or not TASK.fullmatch(task_id):
            raise BridgeError("非法任务 ID")
        folder = inside(self.root, self.tasks / task_id)
        task = read_json(folder / "task.json")
        if task.get("task_id") != task_id or task.get("project_root") != str(self.root):
            raise BridgeError("任务身份或项目不匹配")
        for name, digest in task["input_sha256"].items():
            if name not in {"problem", "requirements", "user_prompt"}:
                raise BridgeError("非法任务材料")
            if sha(inside(self.root, folder / (name + ".txt")).read_bytes()) != digest:
                raise BridgeError("任务输入已变化：" + name)
        return folder, task

    def _authorize(self, question_id, config_hash, input_hashes):
        if not self.authorization_path.is_file():
            raise BridgeError("真实模型调用尚未授权；先确认模型和预算。可进行无模型预检。")
        auth = read_json(inside(self.root, self.authorization_path))
        required = {"schema_version", "enabled", "config_sha256", "expires_at", "max_runs", "allowed_question_ids", "allowed_input_sha256", "authorization_id"}
        if set(auth) != required or auth["schema_version"] != 1 or auth["enabled"] is not True:
            raise BridgeError("宿主授权合同不匹配")
        safe_id(auth["authorization_id"], "授权 ID")
        if auth["config_sha256"] != config_hash:
            raise BridgeError("配置已变化，旧授权不再适用")
        try:
            expires = datetime.fromisoformat(auth["expires_at"].replace("Z", "+00:00"))
        except (ValueError, AttributeError) as exc:
            raise BridgeError("授权截止时间非法") from exc
        if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
            raise BridgeError("宿主授权已过期")
        allowed = auth["allowed_question_ids"]
        if not isinstance(allowed, list) or any(safe_id(item) != item for item in allowed) or question_id not in allowed:
            raise BridgeError("本题不在宿主授权范围")
        if not isinstance(auth["allowed_input_sha256"], dict) or auth["allowed_input_sha256"].get(question_id) != input_hashes:
            raise BridgeError("题面、教学要求或完整用户请求与授权材料不匹配")
        if type(auth["max_runs"]) is not int or not 1 <= auth["max_runs"] <= 100:
            raise BridgeError("授权运行数量非法")
        count = 0
        if self.tasks.exists():
            for folder in self.tasks.iterdir():
                if TASK.fullmatch(folder.name) and (folder / "task.json").is_file():
                    prior = read_json(inside(self.root, folder / "task.json"))
                    count += prior.get("authorization_id") == auth["authorization_id"]
        if count >= auth["max_runs"]:
            raise BridgeError("本批已授权运行数已耗尽（失败尝试也计入）")
        return auth

    def start(self, *, problem, requirements, user_prompt, idempotency_key,
              question_id=None, tags=None, mode="dry_run"):
        if mode not in {"dry_run", "run"}:
            raise BridgeError("mode 仅允许 dry_run/run")
        safe_id(idempotency_key, "幂等键")
        inputs = dict(problem=problem, requirements=requirements, user_prompt=user_prompt)
        for name, text in inputs.items():
            if not isinstance(text, str) or not text.strip() or "\x00" in text or len(text.encode("utf-8")) > MAX_TEXT:
                raise BridgeError(name + "须为非空 UTF-8 文本且不超过 256 KiB")
        question_id = safe_id(question_id or "q_" + sha(" ".join(problem.split()).encode("utf-8"))[:12], "question_id")
        tags = [] if tags is None else tags
        if not isinstance(tags, list) or len(tags) > 20 or any(not isinstance(t, str) or len(t) > 80 or re.search(r"[\x00-\x1f]", t) for t in tags):
            raise BridgeError("tags 必须是最多 20 项的短字符串数组")
        request = {**inputs, "question_id": question_id, "tags": tags, "mode": mode}
        request_sha = sha(canonical(request))
        config_bytes = inside(self.root, self.config_path).read_bytes()
        config_hash = sha(config_bytes)
        with store_lock(self.tasks):
            for folder in self.tasks.iterdir():
                if TASK.fullmatch(folder.name) and (folder / "task.json").is_file():
                    old = read_json(inside(self.root, folder / "task.json"))
                    if old.get("idempotency_key") == idempotency_key:
                        if old.get("request_sha256") != request_sha or old.get("config_sha256") != config_hash:
                            raise BridgeError("同一幂等键的材料或配置发生冲突")
                        return {**self.status(old["task_id"]), "reused": True}
            input_hashes = {name: sha(text.encode("utf-8")) for name, text in inputs.items()}
            auth = self._authorize(question_id, config_hash, input_hashes) if mode == "run" else None
            if mode == "run":
                for folder in self.tasks.iterdir():
                    if TASK.fullmatch(folder.name) and (folder / "task.json").is_file():
                        old = read_json(inside(self.root, folder / "task.json"))
                        if old.get("mode") == "run" and self.status(old["task_id"])["status"] not in TERMINAL:
                            raise BridgeError("已有组合模型任务运行中；不并行启动")
            task_id = "hyb_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + secrets.token_hex(8)
            folder = inside(self.root, self.tasks / task_id, exists=False)
            folder.mkdir()
            (folder / "hybrid-config.json").write_bytes(config_bytes)
            for name, text in inputs.items():
                (folder / (name + ".txt")).write_bytes(text.encode("utf-8"))
            task = {"schema_version": 1, "task_id": task_id, "project_root": str(self.root),
                    "question_id": question_id, "tags": tags, "mode": mode, "created_at": now(),
                    "idempotency_key": idempotency_key, "request_sha256": request_sha,
                    "config_sha256": config_hash, "input_sha256": {name: sha(text.encode("utf-8")) for name, text in inputs.items()},
                    "authorization_id": auth["authorization_id"] if auth else None,
                    "authorization_expires_at": auth["expires_at"] if auth else None}
            atomic_json(folder / "task.json", task)
            atomic_json(folder / "state.json", {"status": "queued", "updated_at": now(), "called_model": False})
            try:
                self.launcher(task_id, folder)
            except Exception as exc:
                atomic_json(folder / "state.json", {"status": "failed", "failure_kind": "worker_start_failure", "error": str(exc), "called_model": False, "updated_at": now()})
            return {**self.status(task_id), "reused": False}

    def _launch(self, task_id, folder):
        python = inside(self.root, ".venv/Scripts/python.exe") if os.name == "nt" else Path(sys.executable)
        flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
        with (folder / "worker.stdout.log").open("ab") as out, (folder / "worker.stderr.log").open("ab") as err:
            proc = subprocess.Popen([str(python), str(self.root / "scripts/hybrid_tutor/bridge.py"), "--worker", task_id,
                                     "--project-root", str(self.root)], cwd=self.root, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=err, creationflags=flags, start_new_session=os.name != "nt")
            atomic_json(folder / "worker-launch.json", {"pid": proc.pid, "creation_id": process_identity(proc.pid), "started_at": now()})

    def status(self, task_id):
        folder, task = self._load(task_id)
        state = read_json(folder / "state.json")
        status = state.get("status", "unknown")
        final_review_validated = False
        if status in {"queued", "running", "cancelling"}:
            worker = state.get("worker") or (read_json(folder / "worker-launch.json") if (folder / "worker-launch.json").is_file() else {})
            if worker.get("pid") and process_identity(worker["pid"]) != worker.get("creation_id"):
                status = "host_lost"
                state = {**state, "failure_kind": "worker_lost", "error": "工作进程已消失，保留原材料；未自动重试"}
            elif not worker.get("pid") and (datetime.now(timezone.utc) - datetime.fromisoformat(task["created_at"].replace("Z", "+00:00"))).total_seconds() > 30:
                status = "host_lost"
                state = {**state, "failure_kind": "worker_not_started", "error": "提交超过 30 秒仍无工作进程身份，原材料保留；未自动重试"}
            elif (folder / "cancel.request.json").exists():
                status = "cancelling"
        if status == "pending_final_review" and (folder / "outcome.json").is_file():
            try:
                outcome = read_json(folder / "outcome.json")
                record_id = self._outcome_record(outcome, task)
                record = read_json(self.record_path(record_id)) if record_id else {}
                if record.get("status") in {"completed", "unverified"}:
                    status = self._finalized_status(outcome, record)
                    final_review_validated = True
            except (OSError, ValueError, KeyError, TypeError) as exc:
                status = "artifact_failure"
                state = {**state, "failure_kind": "final_review_binding_failure", "error": str(exc)}
        return {"task_id": task_id, "question_id": task["question_id"], "mode": task["mode"],
                "status": status, "created_at": task["created_at"], "updated_at": state.get("updated_at"),
                "error": state.get("error"), "failure_kind": state.get("failure_kind"),
                "called_model": state.get("called_model"), "elapsed_seconds": state.get("elapsed_seconds"),
                "final_review_validated": final_review_validated,
                "needs_current_codex_final_review": status == "pending_final_review"}

    def _finalized_status(self, outcome, record):
        # Reuse the same dry-run validator as the authoritative finalizer. It
        # checks request/report/source/record identity and hashes, writes nothing
        # and never calls a model. A status string alone cannot promote a task.
        from deepseek_rethlas.final_review import finalize_review
        request_path = inside(self.root, outcome["final_review_request_file"])
        request = read_json(request_path)
        reviews = [r for r in record.get("verification", []) if isinstance(r, dict)
                   and r.get("stage") == "final_review" and r.get("verdict") in {"correct", "wrong"}
                   and r.get("verifier", {}).get("source") == "current_codex_chat"]
        if not reviews:
            raise BridgeError("记录状态已变化，但没有真实当前 Codex 终审报告")
        review = reviews[-1]
        validated = finalize_review(self.root, request_path, request["reviewer"]["prompt"]["file"], review["report_file"], dry_run=True)
        expected = validated.get("expected_status")
        if expected not in {"completed", "unverified"} or record.get("status") != expected:
            raise BridgeError("记录状态与终审判定不一致")
        return expected

    def list_tasks(self):
        result = []
        if self.tasks.is_dir():
            for folder in sorted(self.tasks.iterdir(), reverse=True):
                if TASK.fullmatch(folder.name) and (folder / "task.json").is_file():
                    try:
                        result.append(self.status(folder.name))
                    except (OSError, ValueError) as exc:
                        result.append({"task_id": folder.name, "status": "artifact_failure", "error": str(exc)})
                    if len(result) >= 100:
                        break
        return result

    def results(self, task_id):
        folder, task = self._load(task_id)
        result = {**self.status(task_id), "provenance": "hybrid_task", "input_sha256": task["input_sha256"],
                  "inputs": {name: (folder / (name + ".txt")).read_bytes().decode("utf-8") for name in task["input_sha256"]}}
        outcome = None
        if (folder / "outcome.json").is_file():
            outcome = read_json(inside(self.root, folder / "outcome.json"))
            result["outcome"] = outcome
        receipt = None
        if (folder / "run-receipt.json").is_file():
            receipt = read_json(inside(self.root, folder / "run-receipt.json"))
            if receipt.get("input_sha256") != task["input_sha256"]:
                raise BridgeError("运行登记与原始提交哈希不匹配")
            result["run_receipt"] = receipt
        record_source = outcome if outcome and outcome.get("run_id") else receipt
        if record_source:
            record_id = self._outcome_record(record_source, task)
            if record_id:
                result["record_id"] = record_id
                replay = self.replay(record_id)
                result["record"] = replay["record"]
                result["runtime_summary"] = replay["runtime_summary"]
                result["partial_record"] = not bool(outcome and outcome.get("run_id"))
                if result["partial_record"]:
                    result["partial_notice"] = "根据模型调用前的宿主运行登记读取已保存阶段；最终结果未返回，不能据部分记录宣称完成。"
        if receipt:
            receipt_record_id = self._outcome_record(receipt, task)
            if receipt_record_id != result.get("record_id"):
                raise BridgeError("运行登记与结果对应不同记录")
            result["raw_partials"] = self._raw_partials(receipt, task, result["record"])
        for name in ("stdout", "stderr"):
            path = folder / (name + ".log")
            if path.is_file():
                with path.open("rb") as handle:
                    size = path.stat().st_size
                    handle.seek(max(0, size - 64000))
                    result[name + "_tail"] = handle.read().decode("utf-8", errors="replace")
                    result[name + "_truncated"] = size > 64000
        return result

    def _raw_partials(self, receipt, task, record):
        """Read bounded, unreviewed material only from this exact registered run.

        The receipt's work_dir is intentionally ignored. Paths are constructed
        from the validated run ID and a fixed filename allowlist, never supplied
        by a browser, model response, or arbitrary artifact field.
        """
        run_id = safe_id(receipt["run_id"], "run_id")
        run_root = inside(self.root, Path("work/deepseek-rethlas") / run_id, exists=False)
        envelope = {"status": "raw_partial", "review_status": "unreviewed", "files": [],
                    "file_byte_limit": PARTIAL_FILE_LIMIT, "total_byte_limit": PARTIAL_TOTAL_LIMIT,
                    "captured_bytes": 0, "omitted_files": 0, "read_errors": [],
                    "notice": "仅展示已落盘且尚未归档阶段的允许列表文件；原始片段未经核验，不代表完整回答。单文件最多128 KiB、合计512 KiB；哈希绑定实际读取字节。未落盘或不在允许列表的内容不会补造。"}
        if not run_root.is_dir():
            return envelope
        for name, digest in task["input_sha256"].items():
            path = inside(self.root, run_root / (name + ".txt"))
            if sha(path.read_bytes()) != digest:
                raise BridgeError("在途材料目录的原始输入不匹配：" + name)
        calls = inside(self.root, run_root / "calls", exists=False)
        if not calls.is_dir():
            return envelope
        archived = {item.get("iteration_id") for item in record.get("iterations", []) if isinstance(item, dict)}
        remaining = PARTIAL_TOTAL_LIMIT
        directories = sorted((p for p in calls.iterdir() if CALL_DIRECTORY.fullmatch(p.name)), key=lambda p: p.name, reverse=True)
        for directory in directories[:20]:
            stage = directory.name.split("-", 1)[1]
            if stage in archived:
                continue
            for relative in PARTIAL_FILES:
                try:
                    path = inside(self.root, directory / relative, exists=False)
                    if not path.is_file():
                        continue
                    if remaining <= 0:
                        envelope["omitted_files"] += 1
                        continue
                    with path.open("rb") as handle:
                        before = os.fstat(handle.fileno())
                        source_size = before.st_size
                        raw = handle.read(min(PARTIAL_FILE_LIMIT, remaining, source_size))
                        after = os.fstat(handle.fileno())
                    remaining -= len(raw)
                    changed = after.st_size != source_size or after.st_mtime_ns != before.st_mtime_ns
                    truncated = len(raw) < source_size or changed
                    envelope["files"].append({"call_id": directory.name, "stage": stage,
                        "file": path.relative_to(self.root).as_posix(), "status": "raw_partial",
                        "review_status": "unreviewed", "source_bytes_at_open": source_size,
                        "source_changed_during_read": changed,
                        "captured_bytes": len(raw), "truncated": truncated,
                        "captured_sha256": sha(raw), "source_sha256": None if truncated else sha(raw),
                        "sha256_scope": "captured raw byte prefix; text display decodes UTF-8 with replacement",
                        "text": raw.decode("utf-8", errors="replace")})
                    envelope["captured_bytes"] += len(raw)
                except (OSError, ValueError) as exc:
                    envelope["read_errors"].append({"call_id": directory.name, "file": relative, "error": str(exc)})
        return envelope

    def _outcome_record(self, outcome, task):
        if not outcome.get("run_id"):
            return None
        for key in ("question_id", "ai_id", "run_id"):
            safe_id(outcome.get(key), key)
        if outcome["question_id"] != task["question_id"] or not outcome["ai_id"].startswith("hybrid-rethlas-deeptutor-"):
            raise BridgeError("结果身份不属于本次组合任务")
        record_id = "/".join(outcome[key] for key in ("question_id", "ai_id", "run_id"))
        record = read_json(self.record_path(record_id))
        for key in ("question_id", "ai_id", "run_id"):
            if record.get(key) != outcome[key]:
                raise BridgeError("归档记录身份不匹配")
        for key, field in (("problem", "question"), ("requirements", "requirements"), ("user_prompt", "user_prompt")):
            if sha(record.get(field, "").encode("utf-8")) != task["input_sha256"][key]:
                raise BridgeError("归档记录原始输入不匹配")
        return record_id

    def cancel(self, task_id):
        folder, _ = self._load(task_id)
        current = self.status(task_id)
        if current["status"] in TERMINAL:
            return {**current, "cancelled": False}
        flag = folder / "cancel.request.json"
        try:
            with flag.open("x", encoding="utf-8") as handle:
                json.dump({"requested_at": now(), "task_id": task_id}, handle)
        except FileExistsError:
            pass
        return {**self.status(task_id), "cancelled": None,
                "notice": "已请求取消，仅由任务工作进程回收其自身子进程；等待确认。"}

    def record_path(self, record_id, filename="record.json"):
        if not isinstance(record_id, str) or len(record_id.split("/")) != 3:
            raise BridgeError("历史记录 ID 必须为 question_id/ai_id/run_id")
        for part in record_id.split("/"):
            safe_id(part, "历史记录 ID")
        return inside(self.root, Path("docs/experiments") / record_id / filename)

    def replay(self, record_id):
        from runtime_metrics import runtime_summary
        path = self.record_path(record_id)
        record = read_json(path)
        if record_id != "/".join(str(record.get(k)) for k in ("question_id", "ai_id", "run_id")):
            raise BridgeError("历史文件与记录身份不一致")
        return {"provenance": "historical_replay", "called_model": False, "record_id": record_id,
                "source_sha256": sha(path.read_bytes()), "record": record, "runtime_summary": runtime_summary(record),
                "notice": "只读历史材料，不是新的组合系统模型运行；不据此计算组合效果。"}

    def history(self):
        base = self.root / "docs/experiments"
        results = []
        if not base.is_dir():
            return results
        for question in base.iterdir():
            if not question.is_dir() or not SAFE.fullmatch(question.name):
                continue
            for ai in question.iterdir():
                if not ai.is_dir() or not SAFE.fullmatch(ai.name):
                    continue
                for run in ai.iterdir():
                    if not run.is_dir() or not SAFE.fullmatch(run.name) or not (run / "record.json").is_file():
                        continue
                    record_id = "/".join((question.name, ai.name, run.name))
                    try:
                        record = self.replay(record_id)["record"]
                    except (OSError, ValueError):
                        continue
                    if record.get("record_kind") == "installation_test":
                        continue
                    results.append({"record_id": record_id, "question_id": question.name, "ai_id": ai.name,
                                    "run_id": run.name, "status": record.get("status"), "tags": record.get("tags", [])})
        return sorted(results, key=lambda row: row["run_id"], reverse=True)[:300]


def _stop_owned_child(proc):
    if proc.poll() is not None:
        return
    if os.name == "nt":
        # proc is a live Popen object created by this exact worker, never a PID
        # recovered from a task document. /T includes its model subprocesses.
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/taskkill.exe"
        subprocess.run([str(taskkill), "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                       timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        import signal
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def worker(root, task_id):
    bridge = Bridge(root)
    folder, task = bridge._load(task_id)
    worker_info = {"pid": os.getpid(), "creation_id": process_identity(os.getpid())}
    start = time.monotonic()
    state = {"status": "running", "updated_at": now(), "started_at": now(), "worker": worker_info,
             "called_model": False, "elapsed_seconds": 0}
    atomic_json(folder / "state.json", state)
    proc = None
    try:
        frozen_config = inside(bridge.root, folder / "hybrid-config.json")
        if sha(frozen_config.read_bytes()) != task["config_sha256"]:
            raise BridgeError("冻结配置发生变化，未启动模型")
        config = read_json(frozen_config)
        if task["mode"] == "run":
            auth = read_json(inside(bridge.root, bridge.authorization_path))
            if auth.get("enabled") is not True or auth.get("authorization_id") != task["authorization_id"] or auth.get("config_sha256") != task["config_sha256"]:
                raise BridgeError("任务授权已撤回或变更")
            expires = datetime.fromisoformat(task["authorization_expires_at"].replace("Z", "+00:00"))
            if expires <= datetime.now(timezone.utc):
                raise BridgeError("任务授权已过期")
        max_seconds = int((config.get("proof") or {}).get("max_total_seconds", 3600)) + 120
        max_seconds = min(max(30, max_seconds), 7320) if task["mode"] == "run" else 120
        python = inside(bridge.root, ".venv/Scripts/python.exe") if os.name == "nt" else Path(sys.executable)
        command = [str(python), "-X", "utf8", str(bridge.root / "scripts/hybrid_tutor_cli.py"),
                   "--project-root", str(bridge.root), "--config", str(frozen_config), "run",
                   "--problem-file", str(folder / "problem.txt"), "--requirements-file", str(folder / "requirements.txt"),
                   "--user-prompt-file", str(folder / "user_prompt.txt"), "--question-id", task["question_id"],
                   "--result-file", str(folder / "outcome.json"), "--receipt-file", str(folder / "run-receipt.json")]
        for tag in task["tags"]:
            command.extend(["--tag", tag])
        command.append("--dry-run" if task["mode"] == "dry_run" else "--allow-model-calls")
        atomic_json(folder / "command.json", {"argv": command, "cwd": str(bridge.root), "config_sha256": task["config_sha256"]})
        if (folder / "cancel.request.json").exists():
            state.update(status="interrupted", failure_kind="cancelled_before_start")
            return
        with (folder / "stdout.log").open("wb") as out, (folder / "stderr.log").open("wb") as err:
            proc = subprocess.Popen(command, cwd=bridge.root, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                                    start_new_session=os.name != "nt")
            state["called_model"] = None if task["mode"] == "run" else False
            while proc.poll() is None:
                if (folder / "cancel.request.json").exists():
                    _stop_owned_child(proc)
                    state.update(status="interrupted", failure_kind="user_cancelled", error="用户取消；原始材料保留")
                    break
                if time.monotonic() - start > max_seconds:
                    _stop_owned_child(proc)
                    state.update(status="timeout", failure_kind="host_timeout", error="达到宿主时间预算；原始材料保留")
                    break
                state.update(elapsed_seconds=round(time.monotonic() - start, 3), updated_at=now())
                atomic_json(folder / "state.json", state)
                time.sleep(.5)
        state["exit_code"] = proc.returncode
        if state["status"] == "running":
            if not (folder / "outcome.json").is_file():
                state.update(status="failed", failure_kind="missing_outcome", error="进程退出但没有可信结果文件；不按退出码推断数学成功")
            else:
                outcome = read_json(folder / "outcome.json")
                status = outcome.get("status")
                if status not in TERMINAL or status == "completed":
                    raise BridgeError("运行器返回非法终态；新的模型链只能等待当前 Codex 终审")
                if task["mode"] == "dry_run" and (status != "dry_run" or outcome.get("called_model") is not False):
                    raise BridgeError("无模型预检返回合同错误")
                if task["mode"] == "run" and status == "dry_run":
                    raise BridgeError("真实运行不能被预检替代")
                if status == "pending_final_review":
                    bridge._outcome_record(outcome, task)
                state.update(status=status, error=outcome.get("error"), failure_kind=outcome.get("failure_kind"), called_model=outcome.get("called_model"))
    except Exception as exc:
        state.update(status="failed", failure_kind="bridge_failure", error=f"{type(exc).__name__}: {exc}")
    finally:
        if proc is not None and proc.poll() is None:
            _stop_owned_child(proc)
        state.update(updated_at=now(), elapsed_seconds=round(time.monotonic() - start, 3))
        atomic_json(folder / "state.json", state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--project-root", default=str(ROOT))
    args = parser.parse_args()
    worker(args.project_root, args.worker)
