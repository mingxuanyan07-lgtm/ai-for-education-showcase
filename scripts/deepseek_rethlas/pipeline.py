"""Host orchestration for DeepSeek Rethlas and optional DeepTutor teaching.

Original Codex/Rethlas entry points are untouched. This module never invokes
Codex: the final review is explicitly handed back to the current Codex chat.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import time

from jsonschema import Draft202012Validator

from experiment_records import record_manifest
from rethlas_core.verification import make_binding, validate_math_report
from rethlas_core import computation
from rethlas_core.budget import RunBudget, BudgetLimit
from rethlas_core.memory import ClaimMemory
from . import prompts
from . import teaching_contract as pedagogy
from .statement_contract import compare_statement


DEFAULTS = {
    "schema_version": 1,
    "provider": "deepseek-account", "model": "deepseek-flash",
    "reasoning_effort": "max", "max_rounds": 3, "timeout_seconds": 900,
    "final_review": "codex_current_chat", "record_kind": "math_experiment",
    "workflow_adapter": "Rethlas DeepSeek 分阶段适配",
    "tool_use": "none;host manages artifacts",
    "configured_model_source": "未记录",
    "workflow_variant": "Rethlas DeepSeek 分阶段适配",
    "tools": "none; host orchestrates artifacts and independent sessions",
    "search": "disabled", "allow_provider_fallback": False,
    "teaching_contract": pedagogy.LEGACY,
    "architecture_profile": "legacy",
    "max_model_calls": 10, "max_tool_calls": 12,
    "max_total_seconds": 3600, "max_observed_tokens": 500000,
}
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")
_DEVICE = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?\Z", re.I)
SCHEMA_PATH = "Rethlas/agents/verification/schemas/verification_output.schema.json"
SOURCE_PATHS = (
    "Rethlas/agents/generation/AGENTS.md", "Rethlas/agents/verification/AGENTS.md",
    "Rethlas/agents/generation/.agents/skills/direct-proving/SKILL.md",
    "Rethlas/agents/verification/.agents/skills/verify-sequential-statements/SKILL.md",
    SCHEMA_PATH,
    "scripts/deepseek_rethlas/prompts.py", "scripts/deepseek_rethlas/pipeline.py",
    "scripts/deepseek_rethlas/statement_contract.py",
    "scripts/deepseek_rethlas/teaching_contract.py",
    "scripts/rethlas_core/verification.py",
    "scripts/rethlas_core/claims.py",
    "scripts/rethlas_core/computation.py",
    "scripts/rethlas_core/budget.py",
    "scripts/rethlas_core/memory.py",
    "scripts/rethlas_core/computation_worker.py",
    "scripts/runtime_metrics.py",
)
HYBRID_VERSION = "hybrid-rethlas-deeptutor-v1"
HYBRID_TEACHING_VARIANT = "deeptutor-chat-teaching-v1"
HYBRID_SOURCE_PATHS = (
    "scripts/hybrid_tutor_cli.py", "scripts/hybrid_tutor/deeptutor_runtime.py",
    "scripts/hybrid_tutor/deeptutor_worker.py", "scripts/deepseek_rethlas/final_review.py",
)


class PipelineError(ValueError):
    """Input, output-contract, identity or orchestration failure."""
    def __init__(self, message, kind="format_failure"):
        super().__init__(message)
        self.kind = kind


class BudgetExpired(PipelineError):
    pass


class UserInterrupted(PipelineError):
    pass


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _id(value, label: str) -> str:
    if (not isinstance(value, str) or not _SAFE_ID.fullmatch(value)
            or _DEVICE.fullmatch(value) or value.endswith(".")):
        raise PipelineError(f"{label} 必须为安全 ASCII 标识")
    return value


def _inside(root: Path, value, *, exists: bool = True) -> Path:
    candidate = Path(value)
    candidate = candidate if candidate.is_absolute() else root / candidate
    if ".." in candidate.parts:
        raise PipelineError("输入路径不能包含上级穿越")
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise PipelineError("输入与证据必须位于项目目录内") from exc
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise PipelineError("输入与证据不能通过符号链接或目录联接")
    result = candidate.resolve(strict=exists)
    if not result.is_relative_to(root):
        raise PipelineError("路径解析后位于项目之外")
    return result


def _read(root: Path, value, label: str) -> tuple[Path, str]:
    path = _inside(root, value)
    if not path.is_file() or path.suffix.lower() not in {".md", ".tex", ".txt"}:
        raise PipelineError(f"{label} 必须是项目内 .md/.tex/.txt 文本文件")
    if any(part.lower() in {".git", ".codex", ".venv", "node_modules"}
           for part in path.relative_to(root).parts):
        raise PipelineError(f"{label} 不能读取配置或安装目录")
    raw = path.read_bytes()
    if len(raw) > 4 * 1024 * 1024:
        raise PipelineError(f"{label} 超过 4 MiB")
    text = raw.decode("utf-8-sig")
    if not text.strip():
        raise PipelineError(f"{label} 不能为空")
    return path, text


def validate_config(config=None) -> dict:
    if config is not None and not isinstance(config, dict):
        raise PipelineError("config 必须是对象")
    result = dict(DEFAULTS)
    result.update(config or {})
    if result["architecture_profile"] not in ("legacy", "verified-v2"):
        raise PipelineError("未知架构版本")
    if result["architecture_profile"] == "verified-v2":
        if config and config.get("teaching_contract", pedagogy.VERSION) != pedagogy.VERSION:
            raise PipelineError("verified-v2 必须启用知识准备与主线教学合同")
        result["teaching_contract"] = pedagogy.VERSION
    # Never retain arbitrary config keys (which could contain credential data).
    unexpected = set(result) - set(DEFAULTS)
    if unexpected:
        raise PipelineError("不支持的配置字段：" + ", ".join(sorted(unexpected)))
    if result["provider"] != "deepseek-account":
        raise PipelineError("本适配目前只支持明确的 deepseek-account 提供方，不作跨模型回退")
    if (not isinstance(result["model"], str) or not result["model"].startswith("deepseek-")
            or not _SAFE_ID.fullmatch(result["model"])):
        raise PipelineError("必须明确指定 DeepSeek model")
    if result["reasoning_effort"] not in {"low", "medium", "high", "max"}:
        raise PipelineError("不支持的 DeepSeek reasoning_effort")
    if type(result["schema_version"]) is not int or result["schema_version"] != 1:
        raise PipelineError("schema_version 必须为 1")
    upgraded_config = result["architecture_profile"] == "verified-v2"
    improved_tools = "host-safe-v1: exact finite checks and math-local allowlist; no model-facing tools"
    variants = (DEFAULTS["workflow_variant"], DEFAULTS["workflow_variant"] + " / verified-v2") if upgraded_config else (DEFAULTS["workflow_variant"],)
    tools = (DEFAULTS["tools"], improved_tools) if upgraded_config else (DEFAULTS["tools"],)
    if (result["search"] != "disabled" or result["allow_provider_fallback"] is not False
            or result["workflow_variant"] not in variants
            or result["tools"] not in tools
            or not isinstance(result["configured_model_source"], str)):
        raise PipelineError("搜索、工具、提供方回退与工作流范围不能被隐式改变")
    if upgraded_config:
        result["tools"] = improved_tools
        result["workflow_variant"] = DEFAULTS["workflow_variant"] + " / verified-v2"
    for key, ceiling in (("max_rounds", 3), ("timeout_seconds", 900)):
        if type(result[key]) is not int or not 1 <= result[key] <= ceiling:
            raise PipelineError(f"{key} 必须在 1 至 {ceiling} 内")
    for key, ceiling in (("max_model_calls", 10), ("max_tool_calls", 12),
                         ("max_total_seconds", 3600), ("max_observed_tokens", 500000)):
        if type(result[key]) is not int or not 1 <= result[key] <= ceiling:
            raise PipelineError(f"{key} 必须在 1 至 {ceiling} 内", "configuration_failure")
    if result["record_kind"] not in {"math_experiment", "installation_test"}:
        raise PipelineError("record_kind 无效")
    if result["final_review"] != "codex_current_chat":
        raise PipelineError("本流程最终复核必须交回当前 Codex 会话")
    if result["tool_use"] != DEFAULTS["tool_use"]:
        raise PipelineError("本适配只允许无模型工具的文本角色")
    if result["workflow_adapter"] != DEFAULTS["workflow_adapter"]:
        raise PipelineError("workflow_adapter 不能冒充原生 Rethlas 调用")
    if result["teaching_contract"] not in (pedagogy.LEGACY, pedagogy.VERSION):
        raise PipelineError("未知教学合同版本")
    return result


def validate_teaching_config(config) -> dict:
    """Explicit, credential-free teaching dispatch; no provider fallback."""
    required = {"provider", "model", "workflow_variant"}
    optional = {"max_output_tokens", "thinking_mode", "output_profile"}
    if not isinstance(config, dict) or not required.issubset(config) or set(config) - required - optional:
        raise PipelineError("组合教学配置需要 provider/model/workflow_variant；仅允许已声明的预算与教学选项", "configuration_failure")
    if config["provider"] != "deepseek-api" or config["workflow_variant"] != HYBRID_TEACHING_VARIANT:
        raise PipelineError("组合教学仅支持已声明的 DeepTutor DeepSeek API 文本执行器", "configuration_failure")
    if (not isinstance(config["model"], str) or not config["model"].startswith("deepseek-")
            or not _SAFE_ID.fullmatch(config["model"])):
        raise PipelineError("必须明确指定组合教学的 DeepSeek 模型", "configuration_failure")
    if (type(config.get("max_output_tokens", 8192)) is not int
            or not 1 <= config.get("max_output_tokens", 8192) <= 32768):
        raise PipelineError("组合教学 max_output_tokens 必须为 1..32768 整数；上调仍需本次运行授权", "configuration_failure")
    if config.get("thinking_mode", "provider-default") not in ("provider-default", "disabled"):
        raise PipelineError("未知组合教学 thinking_mode", "configuration_failure")
    if config.get("output_profile", "standard") not in ("standard", "concise-v1"):
        raise PipelineError("未知组合教学 output_profile", "configuration_failure")
    return dict(config)


def validate_proof(proof: str, problem: str) -> dict:
    """Check structure and an audited delimiter-only compatibility policy.

    A successful structure check never changes the raw proof or establishes its
    mathematics. The same raw candidate still enters the independent verifier.
    """
    audit = {"schema_version": 1, "scope": "structure_only", "accepted": False,
             "proof_sha256": _sha(proof.encode("utf-8")),
             "problem_input_sha256": _sha(problem.encode("utf-8")),
             "proof_text_rewritten": False, "mathematical_verdict": "not_run"}

    def reject(message):
        audit.update(accepted=False, error=message)
        error = PipelineError(message)
        error.structural_report = audit
        raise error

    targets = list(re.finditer(r"(?mi)^#\s+(?:theorem\b|定理(?:\s|$))[^\n]*", proof))
    if not targets:
        reject("候选证明缺少最后主定理 # theorem")
    tail = proof[targets[-1].end():]
    statement = re.search(r"(?mi)^##\s+statement\s*$", tail)
    proof_start = re.search(r"(?mi)^##\s+proof\s*$", tail)
    if not statement or not proof_start or statement.end() >= proof_start.start():
        reject("主定理必须有按顺序的 ## statement 与 ## proof")
    audit.update(compare_statement(problem, tail[statement.end():proof_start.start()]))
    if not audit["accepted"]:
        reject("主定理 statement 未逐字保留完整原题，且不符合仅数学定界符的兼容合同")
    body = tail[proof_start.end():].strip()
    if not body or re.search(r"(?m)^#\s+[^#]", body):
        reject("主定理必须为最后命题且有完整 proof")
    return audit


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PipelineError(f"核验 JSON 有重复键：{key}")
        result[key] = value
    return result


def validate_report(text: str, schema: dict, *, teaching: str | None = None,
                    contract: str = pedagogy.LEGACY, proof: str | None = None,
                    require_claims: bool = False) -> dict:
    try:
        payload = json.loads(text, object_pairs_hook=_unique_object)
        check_schema = pedagogy.report_schema(schema) if contract == pedagogy.VERSION else schema
        validate_math_report(payload, check_schema, proof=proof, require_claims=require_claims)
        if contract == pedagogy.VERSION:
            if teaching is None:
                raise ValueError("教学检查缺少对应稿件")
            pedagogy.validate_review(payload["teaching_review"], teaching)
    except PipelineError:
        raise
    except Exception as exc:
        raise PipelineError(f"独立核验输出不符合 JSON 合同：{exc}") from exc
    return payload


def _bound(root: Path, path: Path) -> dict:
    return {"file": path.relative_to(root).as_posix(), "sha256": _sha(path.read_bytes())}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _not_run_final() -> dict:
    return {"stage": "final_review", "verdict": "not_run",
            "verifier": {"verifier_id": "codex-current-chat", "model": "未记录"},
            "verification_report": {"summary": "待当前 Codex 会话复核"}}


def run_pipeline(project_root, problem_file, requirements_file, user_prompt_file,
                 question_id=None, tags=None, config=None, dry_run=False, caller=None,
                 teaching_caller=None, teaching_config=None, math_runtime_file=None,
                 on_run_created=None) -> dict:
    """Run a bounded chain; return a separate Codex review handoff.

    dry_run performs input validation only and creates no directories or records.
    A caller can be injected for offline tests; real calls use runtime.run_call.
    """
    root = Path(project_root).resolve(strict=True)
    config = validate_config(config)
    hybrid = teaching_caller is not None or teaching_config is not None
    if hybrid:
        if not callable(teaching_caller) or config["architecture_profile"] != "verified-v2":
            raise PipelineError("组合流程需要 verified-v2 与显式 teaching_caller", "configuration_failure")
        teaching_config = validate_teaching_config(teaching_config)
    math_runtime_path = None
    if math_runtime_file is not None:
        if not hybrid:
            raise PipelineError("计算入口覆写只用于显式组合工作流", "configuration_failure")
        math_runtime_path = _inside(root, math_runtime_file)
        if math_runtime_path.suffix.lower() != ".json" or math_runtime_path.stat().st_size > 65536:
            raise PipelineError("计算入口必须是项目内不超过 64 KiB 的 JSON", "configuration_failure")
        math_runtime = json.loads(math_runtime_path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_object)
        shared = math_runtime.get("shared_mcp", {}) if isinstance(math_runtime, dict) else {}
        engine = shared.get("math-local", {}) if isinstance(shared, dict) else {}
        if (not isinstance(math_runtime, dict) or set(math_runtime) != {"shared_mcp"}
                or not isinstance(shared, dict) or set(shared) != {"math-local"}
                or not isinstance(engine, dict) or set(engine) != {"command", "args"}
                or not isinstance(engine.get("command"), str) or not engine["command"].strip()
                or not isinstance(engine.get("args"), list)
                or any(not isinstance(arg, str) for arg in engine["args"])):
            raise PipelineError("计算配置只接受 shared_mcp.math-local 的 command/args", "configuration_failure")
    problem_path, problem = _read(root, problem_file, "题面")
    requirements_path, requirements = _read(root, requirements_file, "教学要求")
    user_path, user_prompt = _read(root, user_prompt_file, "完整用户请求")
    question_id = _id(question_id or "q_" + _sha(" ".join(problem.split()).encode())[:12], "question_id")
    if tags is None:
        tags = []
    if (not isinstance(tags, list) or any(not isinstance(x, str) or re.search(r"[\x00-\x1f\x7f]", x)
                                          for x in tags)):
        raise PipelineError("tags 必须是无控制字符的字符串数组")
    tags = list(dict.fromkeys(x.strip() for x in tags if x.strip()))
    schema_path = _inside(root, SCHEMA_PATH)
    schema = json.loads(schema_path.read_text(encoding="utf-8-sig"))
    Draft202012Validator.check_schema(schema)
    ai_id = _id("rethlas-dsh-" + config["model"], "ai_id")
    contract = config["teaching_contract"]
    teaching_round_limit = 2 if contract == pedagogy.VERSION else 1
    if contract == pedagogy.VERSION:
        ai_id = _id(ai_id + "-kr1", "ai_id")
    upgraded = config["architecture_profile"] == "verified-v2"
    if upgraded:
        ai_id = _id("rethlas-dsh-" + config["model"] + "-v2", "ai_id")
    if hybrid:
        ai_id = _id("hybrid-rethlas-deeptutor-" + config["model"] + "-v1", "ai_id")
    workflow = HYBRID_VERSION if hybrid else config["workflow_variant"]
    if dry_run:
        return {"status": "dry_run", "question_id": question_id, "ai_id": ai_id,
                "config": config, "wrote": False, "called_model": False,
                "workflow_variant": workflow,
                "teaching_config": teaching_config if hybrid else None,
                "math_runtime": _bound(root, math_runtime_path) if math_runtime_path else None,
                "inputs": {"problem": _bound(root, problem_path),
                           "requirements": _bound(root, requirements_path),
                           "user_prompt": _bound(root, user_path)},
                "teaching_plan": {"contract": contract, "max_rounds": teaching_round_limit,
                                  "shared_timeout_seconds": config["timeout_seconds"]},
                "budget_plan": {key: config[key] for key in ("max_model_calls", "max_tool_calls",
                    "max_total_seconds", "max_observed_tokens")},
                "notice": "只校验输入；未运行 DeepSeek、Codex 或真实实验。"}
    if caller is None:
        from .runtime import run_call
        caller = run_call
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + secrets.token_hex(4)
    work = _inside(root, root / "work/deepseek-rethlas" / run_id, exists=False)
    work.mkdir(parents=True, exist_ok=False)
    copied = {}
    for name, value in (("problem", problem), ("requirements", requirements), ("user_prompt", user_prompt)):
        path = work / (name + ".txt")
        _write(path, value)
        copied[name] = path
    _write(work / "config.json", _json(config))
    if hybrid:
        _write(work / "teaching.config.json", _json(teaching_config))
    if math_runtime_path:
        math_runtime_snapshot = work / "math.runtime.json"
        with math_runtime_snapshot.open("xb") as handle:
            handle.write(math_runtime_path.read_bytes())
        math_runtime_path = math_runtime_snapshot
    source_paths = SOURCE_PATHS + (HYBRID_SOURCE_PATHS if hybrid else ())
    source_files = [_inside(root, root / path) for path in source_paths if (root / path).is_file()]
    source_snapshots, source_versions = [], []
    for source in source_files:
        relative = source.relative_to(root)
        snapshot = work / "source-snapshots" / relative
        # The record evidence contract accepts text, not executable .py files.
        # Copy bytes (not read_text/write_text) so CRLF, BOM and all original
        # source bytes remain auditable instead of being normalized on Windows.
        if snapshot.suffix.lower() == ".py":
            snapshot = snapshot.with_name(snapshot.name + ".txt")
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        source_bytes = source.read_bytes()
        with snapshot.open("xb") as handle:
            handle.write(source_bytes)
        source_snapshots.append(snapshot)
        source_versions.append({"file": relative.as_posix(), "sha256": _sha(source_bytes),
                                "snapshot": snapshot.relative_to(root).as_posix(),
                                "snapshot_sha256": _sha(snapshot.read_bytes())})
    evidence = [*copied.values(), work / "config.json", *source_snapshots]
    if hybrid:
        evidence.append(work / "teaching.config.json")
    if math_runtime_path:
        evidence.append(math_runtime_path)
    iterations, verifications, calls, sessions = [], [], [], set()
    tool_checks, current_computation = [], []
    computation_file = None
    proof = teaching = ""
    proof_file = proof_report_file = teaching_file = teaching_report_file = None
    teaching_source_file = None
    status, error = "unverified", None
    failure_kind = None
    budget = RunBudget(seconds=config["max_total_seconds"], model_calls=config["max_model_calls"],
                       tool_calls=config["max_tool_calls"], observed_tokens=config["max_observed_tokens"], clock=time.monotonic)
    observed_model = "未记录"
    record_result = None
    teaching_review = None
    _write(work / "memory.init.json", _json({"identity": {"question_id": question_id, "ai_id": ai_id, "run_id": run_id},
           "scope": "host artifact memory, no original Rethlas memory MCP invoked",
           "source_versions": source_versions}))
    evidence.append(work / "memory.init.json")
    claim_memory = ClaimMemory(work / "claim-memory.sqlite3", {"question_id": question_id,
        "ai_id": ai_id, "run_id": run_id}) if upgraded else None
    memory_file = None

    def save_record():
        nonlocal record_result
        for iteration in iterations:
            iteration["settings"]["run_budget"] = budget.snapshot()
            iteration["settings"]["run_failure_kind"] = failure_kind
        record_result = record_manifest({
            "question_id": question_id, "ai_id": ai_id, "run_id": run_id,
            "record_kind": config["record_kind"], "tags": tags,
            "question": problem, "requirements": requirements, "user_prompt": user_prompt,
            "model": observed_model, "framework": workflow
                + (" / " + contract if contract != pedagogy.LEGACY else ""),
            "ai_name": ("Rethlas 分阶段求解与独立核验、DeepTutor 教学；当前 Codex 会话另作终审"
                        if hybrid else "DeepSeek 分阶段求解、核验、教学整理；当前 Codex 会话另作终审"),
            "status": status, "error": error, "iterations": iterations,
            "verification": verifications, "final_answer": proof or "未完成",
            "tool_checks": tool_checks if upgraded else [{"tool": "math-local MCP / Rethlas MCP / search",
                             "status": "not_run", "scope": "文本适配不挂载这些工具，不能把模型检查当计算核验"}],
            "task_review": (("自动检查说明（需人工确认）：verified-v2 的模型角色无工具，宿主独立运行允许列表内的计算请求，实际结果和限制单列。"
                if upgraded else "自动检查说明（需人工确认）：本次为 DeepSeek 纯文本分阶段适配，")
                +
                "宿主保存输入输出和错误，不声称运行原 Rethlas MCP/技能。各角色使用不同会话，"
                "各阶段实际模型与提供方单列，可能存在共同盲点。教学稿数学复核与原证明分别保存；"
                "关键思路、条件与方法的具体教学表现需当前 Codex 最终复核，不代填人工感受。"
                "当前状态：" + status + "。没有真人学习测验或效果比较。"
                + ("\n" + pedagogy.summary(teaching_review) if teaching_review is not None else "")),
            "evidence_files": list(dict.fromkeys(str(path) for path in evidence)),
        }, root)

    def invoke(stage: str, prompt: str, deadline: float):
        nonlocal error, observed_model
        external_teaching = hybrid and stage.startswith("teaching-")
        role_config = teaching_config if external_teaching else config
        role_caller = teaching_caller if external_teaching else caller
        remaining = min(config["timeout_seconds"], budget.admit_model(deadline))
        if remaining < 1:
            raise BudgetExpired("本轮共享时间预算已耗尽；未启动额外调用")
        call_dir = work / "calls" / f"{len(calls) + 1:02d}-{stage}"
        call_dir.mkdir(parents=True, exist_ok=False)
        # Save complete actual input before entering any model runtime.
        prompt_path = call_dir / "host.prompt.txt"
        _write(prompt_path, prompt)
        evidence.append(prompt_path)
        try:
            result = role_caller(project_root=root, call_dir=call_dir / "runtime", prompt=prompt,
                                 config=role_config, timeout_seconds=remaining)
        except KeyboardInterrupt:
            partial = call_dir / "runtime"
            result = {"status": "interrupted", "answer": "", "error": "用户中断当前角色调用",
                      "actual_model": "未记录", "actual_provider": "未记录", "session_id": None,
                      "tool_events": [], "evidence_files": [str(path) for path in partial.rglob("*")
                          if path.is_file() and path.suffix.lower() in {".txt", ".json", ".jsonl", ".log"}]}
        except Exception as exc:
            result = {"status": "failed", "answer": "", "error": f"运行器异常：{type(exc).__name__}: {exc}",
                      "actual_model": "未记录", "actual_provider": "未记录", "session_id": None,
                      "evidence_files": [], "tool_events": []}
        if not isinstance(result, dict):
            result = {"status": "failed", "answer": "", "error": "运行器未返回对象"}
        result = dict(result)
        answer = result.get("answer")
        if not isinstance(answer, str):
            answer = ""
            result.update(status="failed", error="运行器没有文本回答")
        model = result.get("actual_model", result.get("model", "未记录"))
        provider = result.get("actual_provider", result.get("provider", "未记录"))
        if isinstance(model, str) and model.strip() and model != "未记录":
            observed_model = model
        session = result.get("session_id")
        if result.get("status") == "completed":
            if model != role_config["model"] or provider != role_config["provider"]:
                result.update(status="failed", error="实际模型/提供方与已指定的角色配置不一致或未记录")
            elif not isinstance(session, str) or not session.strip() or session in sessions:
                result.update(status="failed", error="角色缺少独立会话证据或复用了其他角色会话")
            elif not answer.strip():
                result.update(status="failed", error="退出成功但缺少最终文本回答")
            elif result.get("tool_events"):
                result.update(status="failed", error="无工具文本角色出现了工具调用，不能作为本流程已完成结果")
            elif external_teaching and (result.get("provider_request_attempts") != 1
                    or type(result.get("provider_request_attempts")) is not int
                    or result.get("provider_request_observed") is not True
                    or result.get("response_observed") is not True):
                result.update(status="failed", failure_kind="runtime_contract_failure",
                              error="DeepTutor 教学必须提供一次实际 API 请求及响应的证据，不接受未知或隐藏重试")
        if result.get("status") == "completed" and time.monotonic() > deadline:
            result.update(status="timeout", error="本轮求解与核验共享时间预算已耗尽")
        if result.get("status") != "completed" and not result.get("failure_kind"):
            result["failure_kind"] = ("timeout" if result.get("status") == "timeout" else
                "interrupted" if result.get("status") == "interrupted" else "runtime_failure")
        if isinstance(session, str) and session:
            sessions.add(session)
        budget.observe(stage, result)
        answer_path = call_dir / "host.answer.txt"
        _write(answer_path, answer)
        result["answer"] = answer
        result_path = call_dir / "host.result.json"
        _write(result_path, _json(result))
        evidence.extend([answer_path, result_path])
        for path in result.get("evidence_files") or []:
            evidence.append(_inside(root, path))
        calls.append({"stage": stage, "session_id": session, "result_file": _bound(root, result_path),
                      "invocation_started": result.get("invocation_started"),
                      "provider_request_observed": result.get("provider_request_observed"),
                      "response_observed": result.get("response_observed")})
        iteration = {"iteration_id": stage, "prompt_file": str(prompt_path), "answer_file": str(answer_path),
                     "settings": {"stage": "post_verification_teaching" if stage.startswith("teaching-") else stage,
                         "role": stage, "ai": ("DeepTutor / DeepSeek API 教学角色" if external_teaching
                                                 else "DeepSeek / DSH 文本角色"), "model": model,
                         "actual_model": model, "actual_provider": provider, "session_id": session,
                         "configured_model": role_config["model"], "configured_provider": role_config["provider"],
                         "reasoning_effort": (result.get("reasoning_effort", "未记录") if external_teaching
                                              else config["reasoning_effort"]), "timeout_seconds": remaining,
                         "elapsed_seconds": result.get("elapsed_seconds"), "usage": result.get("usage"),
                         "status": result.get("status"), "timed_out": result.get("status") == "timeout",
                         "error": result.get("error"), "tool_use": config["tool_use"],
                         "failure_kind": result.get("failure_kind"),
                         "teaching_contract": contract, "teaching_round_limit": teaching_round_limit}}
        iteration["settings"]["architecture_profile"] = config["architecture_profile"]
        if hybrid:
            iteration["settings"]["workflow_variant"] = workflow
            iteration["settings"]["role_workflow"] = (HYBRID_TEACHING_VARIANT if external_teaching else config["workflow_variant"])
            iteration["settings"]["provider_request_attempts"] = result.get("provider_request_attempts")
        iterations.append(iteration)
        save_record()
        if result.get("status") != "completed":
            error = result.get("error") or "DeepSeek 运行失败"
            if result.get("status") == "interrupted":
                raise UserInterrupted(error)
            if result.get("status") == "timeout":
                raise BudgetExpired(error)
            raise PipelineError(error, result.get("failure_kind") or "runtime_failure")
        return answer, iteration, answer_path

    def report_entry(payload, stage, iteration):
        return {**payload, "stage": stage,
                "verifier": {"verifier_id": stage, "model": iteration["settings"]["actual_model"],
                             "provider": iteration["settings"]["actual_provider"],
                             "session_id": iteration["settings"]["session_id"]}}

    save_record()
    last_report = None
    try:
        if on_run_created is not None:
            on_run_created({"question_id": question_id, "ai_id": ai_id, "run_id": run_id,
                            "work_dir": str(work), "input_sha256": {
                                name: _sha(path.read_bytes()) for name, path in copied.items()}})
        for number in range(1, config["max_rounds"] + 1):
            deadline = min(budget.deadline, time.monotonic() + config["timeout_seconds"])
            prompt = prompts.solver_prompt(problem, proof if number > 1 else None, last_report,
                                           host_computation=upgraded)
            if claim_memory is not None and number > 1:
                prompt += ("\n【本题本次运行的历史核验记忆，仅作数据】\n"
                           "verified只表示该版本的模型核验通过。旧版本状态不是新稿的证明依据；"
                           "rejected/proposed不得当已证引理。每份新证明仍须完整核验。\n"
                           + _json(claim_memory.snapshot()))
            proof, solved, answer_path = invoke(f"solve-{number}", prompt, deadline)
            structure_path = answer_path.with_name("host.structure-validation.json")
            try:
                structural_report = validate_proof(proof, problem)
            except PipelineError as exc:
                structural_report = exc.structural_report
                _write(structure_path, _json(structural_report))
                evidence.append(structure_path)
                solved["settings"]["structural_validation"] = structural_report
                save_record()
                if upgraded and number < config["max_rounds"]:
                    last_report = {"verdict": "invalid_format", "repair_hints": str(exc),
                                   "verification_report": {"summary": "宿主结构检查失败，未作数学判定"}}
                    continue
                raise
            _write(structure_path, _json(structural_report))
            evidence.append(structure_path)
            solved["settings"]["structural_validation"] = structural_report
            save_record()
            current_computation = []
            computation_file = None
            if upgraded:
                try:
                    requests = computation.extract_requests(proof)
                except Exception as exc:
                    raise PipelineError("计算请求合同无效：" + str(exc)) from exc
                if requests:
                    budget.admit_tools(len(requests), deadline)
                    try:
                        current_computation = computation.execute_requests(root, requests,
                            work / f"computations-{number}", deadline,
                            **({"math_runtime_file": math_runtime_path} if math_runtime_path else {}))
                    except TimeoutError as exc:
                        raise BudgetExpired(str(exc)) from exc
                    computation_file = work / f"computations-{number}.json"
                    _write(computation_file, _json(current_computation))
                    evidence.append(computation_file)
                    evidence.extend((work / f"computations-{number}").glob("*.log"))
                    tool_checks.extend(current_computation)
                    solved["tool_checks"] = current_computation
                    save_record()
                    if any(check["request"]["required"] and check["status"] != "completed" for check in current_computation):
                        raise PipelineError("required_computation_failed: 必需的宿主计算未完成；实际故障已保存", "tool_failure")
            raw_report, review, raw_report_file = invoke(
                f"proof-review-{number}", prompts.verification_prompt(problem, proof, claim_review=upgraded,
                    computation_evidence=current_computation), deadline)
            last_report = validate_report(raw_report, schema, proof=proof, require_claims=upgraded)
            entry = report_entry(last_report, f"proof-review-{number}", review)
            verifications.append(entry)
            if claim_memory is not None:
                claim_memory.record(problem, proof, last_report, make_binding(problem, proof, last_report,
                    run_id + "-memory-" + str(number)))
                memory_file = work / f"claim-memory-{number}.json"
                _write(memory_file, _json(claim_memory.snapshot()))
                evidence.append(memory_file)
            solved["verification"] = entry
            review["verification"] = entry
            save_record()
            if last_report["verdict"] == "correct":
                if any(check["result"].get("matches_expected") is False
                       or check["result"].get("status") == "counterexample_found" for check in current_computation):
                    raise PipelineError("computation_conflict: 核验correct与实际计算反例冲突，不能发布", "computation_conflict")
                if re.search(r"https?://|arxiv\s*:\s*\d|\bdoi\s*:|\b10\.\d{4,9}/", proof, re.I):
                    raise PipelineError("文本模式发现外部文献标识；没有检索证据，不能接受其已完成核验")
                proof_file = work / "blueprint_verified.md"
                proof_report_file = work / "proof.verification.json"
                _write(proof_file, proof)
                _write(proof_report_file, _json(last_report))
                evidence.extend([proof_file, proof_report_file])
                proof_binding_file = work / "proof.binding.json"
                _write(proof_binding_file, _json(make_binding(problem, proof, last_report,
                                                            run_id + "-proof-" + str(number))))
                evidence.append(proof_binding_file)
                break
        if proof_file is None:
            status = "unverified"
            failure_kind = "mathematical_unverified"
            error = "已达到证明修订轮次上限，仍有关键错误或缺口"
        else:
            # Both teaching attempts and their independent reviews share the
            # original teaching-pair budget; a repair cannot reset the clock.
            deadline = min(budget.deadline, time.monotonic() + config["timeout_seconds"])
            teaching_report = None
            for number in range(1, teaching_round_limit + 1):
                teaching_prompt = prompts.teaching_prompt(problem, proof, requirements,
                    contract=contract, previous_teaching=teaching if number > 1 else None,
                    previous_report=teaching_report)
                if hybrid:
                    teaching_source_file = work / f"teaching-{number}.source.json"
                    _write(teaching_source_file, _json({"schema_version": 1, "workflow_variant": workflow,
                        "identity": {"question_id": question_id, "ai_id": ai_id, "run_id": run_id},
                        "stage": f"teaching-{number}", "teaching_version": number,
                        "problem": _bound(root, copied["problem"]), "proof": _bound(root, proof_file),
                        "proof_report": _bound(root, proof_report_file),
                        "requirements": _bound(root, copied["requirements"]),
                        "prompt_sha256": _sha(teaching_prompt.encode("utf-8")),
                        "teaching_config": teaching_config,
                        "previous_teaching_sha256": _sha(teaching.encode("utf-8")) if number > 1 else None,
                        "previous_review": _bound(root, teaching_report_file) if number > 1 else None,
                        "input_scope": "Bound verified proof and explicit teaching requirements; no evaluator references or cross-run memory."}))
                    evidence.append(teaching_source_file)
                teaching, taught, teaching_file = invoke(
                    f"teaching-{number}", teaching_prompt, deadline)
                review_stage = "review-teaching" if number == 1 else f"review-teaching-{number}"
                raw_report, reviewed, raw_report_file = invoke(
                    review_stage, prompts.verification_prompt(problem, teaching, teaching=True,
                        requirements=requirements, original_proof=proof, contract=contract,
                        computation_evidence=current_computation), deadline)
                if contract == pedagogy.VERSION:
                    # Save comparison facts even if the unchanged literal
                    # contract rejects this report. Never replace a quotation
                    # or use this diagnostic as a mathematical verdict.
                    try:
                        unvalidated = json.loads(raw_report, object_pairs_hook=_unique_object)
                        comparison = pedagogy.quote_comparison_report(
                            unvalidated.get("teaching_review") if isinstance(unvalidated, dict) else None,
                            teaching)
                    except Exception as exc:
                        comparison = {"schema_version": 1, "quote_policy": pedagogy.QUOTE_POLICY,
                                      "prompt_revision": pedagogy.PROMPT_REVISION,
                                      "comparison_only_not_a_verdict": True,
                                      "original_report_modified": False,
                                      "parse_error": type(exc).__name__ + ": " + str(exc)[:500]}
                    comparison["raw_report"] = _bound(root, raw_report_file)
                    comparison_file = work / f"teaching-{number}.quote-comparison.json"
                    _write(comparison_file, _json(comparison))
                    evidence.append(comparison_file)
                    reviewed["settings"]["teaching_quote_prompt_revision"] = pedagogy.PROMPT_REVISION
                    reviewed["settings"]["teaching_quote_policy"] = pedagogy.QUOTE_POLICY
                    reviewed["settings"]["teaching_quote_comparison"] = _bound(root, comparison_file)
                    save_record()
                teaching_report = validate_report(raw_report, schema, teaching=teaching, contract=contract)
                entry = report_entry(teaching_report, review_stage, reviewed)
                taught["verification"] = entry
                reviewed["verification"] = entry
                verifications.append(entry)
                report_name = "teaching.verification.json" if number == 1 else f"teaching-{number}.verification.json"
                teaching_report_file = work / report_name
                _write(teaching_report_file, _json(teaching_report))
                evidence.append(teaching_report_file)
                teaching_review = teaching_report.get("teaching_review")
                if teaching_review is not None:
                    review_file = work / f"teaching-{number}.pedagogy.json"
                    _write(review_file, _json(teaching_review))
                    evidence.append(review_file)
                save_record()
                if (teaching_report["verdict"] == "correct"
                        and (teaching_review is None or teaching_review["verdict"] == "pass")):
                    status = "pending_final_review"
                    verifications.append(_not_run_final())
                    break
            if status != "pending_final_review":
                status = "unverified"
                failure_kind = "teaching_unverified"
                error = ("独立教学数学复核或知识与主线检查未通过；已达到教学轮次上限"
                         if contract == pedagogy.VERSION else
                         "DeepSeek 教学数学复核未通过；未进入 Codex 最终通过状态")
    except BudgetLimit as exc:
        status, error, failure_kind = "timeout", str(exc), "budget_exhausted"
    except BudgetExpired as exc:
        status, error = "timeout", str(exc)
        failure_kind = "timeout"
    except UserInterrupted as exc:
        status, error = "interrupted", str(exc)
        failure_kind = "interrupted"
    except (PipelineError, OSError, ValueError) as exc:
        status, error = "failed", str(exc)
        failure_kind = getattr(exc, "kind", "artifact_failure")
    _write(work / "calls.json", _json(calls))
    evidence.append(work / "calls.json")
    _write(work / "run.summary.json", _json({"status": status, "failure_kind": failure_kind,
        "budget": budget.snapshot(), "routing": "Structure failure repairs in next proof round; mathematical wrong repairs proof; teaching revise repairs teaching; runtime/tool failures stop without fallback."}))
    evidence.append(work / "run.summary.json")
    save_record()
    result = {**(record_result or {}), "status": status, "error": error,
              "question_id": question_id, "ai_id": ai_id, "run_id": run_id,
              "work_dir": str(work), "role_attempts": len(calls),
              "called_model": (True if any(row["response_observed"] is True for row in calls)
                               else None if any(row["invocation_started"] is not False for row in calls) else False),
              "failure_kind": failure_kind, "budget": budget.snapshot(),
              "completed_deepseek_chain": status == "pending_final_review" and not hybrid,
              "final_review_status": "not_run"}
    if hybrid:
        result.update(workflow_variant=workflow, completed_hybrid_chain=status == "pending_final_review")
    if status == "pending_final_review":
        request_file = work / "final-review-request.json"
        review_prompt_file = work / "review.prompt.txt"
        final_prompt = prompts.final_review_prompt(str(request_file), contract=contract,
                                                   architecture_profile=config["architecture_profile"])
        if hybrid:
            final_prompt += ("\n本次为 hybrid-rethlas-deeptutor-v1：证明和独立核验使用 DSH，"
                "教学稿来自 DeepTutor 的独立 DeepSeek API 调用。请阅读绑定的 teaching_source、"
                "teaching_config 和各阶段实际记录，核对题面/证明版本、教学改写与新增数学，"
                "不要把 DeepTutor 教学调用描述成 DSH 或原版 Rethlas。\n")
        _write(review_prompt_file, final_prompt)
        request = {"schema_version": 1, "status": status,
                   "teaching_contract": contract,
                   "architecture_profile": config["architecture_profile"],
                   "identity": {"question_id": question_id, "ai_id": ai_id, "run_id": run_id},
                   "inputs": {key: _bound(root, path) for key, path in copied.items()},
                   "artifacts": {"proof": _bound(root, proof_file),
                                 "proof_report": _bound(root, proof_report_file),
                                 "proof_binding": _bound(root, proof_binding_file),
                                 "teaching": _bound(root, teaching_file),
                                 "teaching_report": _bound(root, teaching_report_file)},
                   "reviewer": {"kind": "codex_current_chat", "actual_model": "未记录", "status": "not_run",
                                "prompt": _bound(root, review_prompt_file)},
                   "record": record_result,
                   "limitations": ["DeepSeek 各阶段会话独立，仍使用同一模型，可能有共同盲点。",
                                   ("模型无工具；宿主计算仅支持其实际对象与定义域，原 Rethlas 记忆MCP和搜索未运行。"
                                    if upgraded else "文本适配未运行原 Rethlas MCP、搜索或 math-local。"),
                                   "原证明和教学稿的 DeepSeek correct 不等于 Codex 已复核或 Lean 形式化。"]}
        if computation_file is not None:
            request["artifacts"]["computation_evidence"] = _bound(root, computation_file)
        if memory_file is not None:
            request["artifacts"]["claim_memory"] = _bound(root, memory_file)
        if hybrid:
            request["workflow_variant"] = workflow
            request["artifacts"]["teaching_source"] = _bound(root, teaching_source_file)
            request["artifacts"]["teaching_config"] = _bound(root, work / "teaching.config.json")
            request["limitations"][0] = "Rethlas 使用 DSH 独立文本会话，教学使用 DeepTutor 的独立 API 会话；各阶段模型、提供方和消耗分别记录，可能存在共同盲点。"
        if math_runtime_path:
            request["artifacts"]["math_runtime"] = _bound(root, math_runtime_path)
        _write(request_file, _json(request))
        evidence.extend([request_file, review_prompt_file])
        save_record()
        result.update(record_result or {})
        result.update(final_review_request_file=str(request_file), review_prompt_file=str(review_prompt_file))
    return result
