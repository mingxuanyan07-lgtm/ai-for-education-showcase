"""评测规格（spec）、导入元数据与独立核验结果的读取和校验。

本模块只做本地文件校验，不推测实际模型，不调用任何模型或网络服务。
所有路径必须是项目内文件，以便 experiment_records 生成不可变证据快照。
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
from benchmark_evaluation import validate_evaluation, validate_reviews

CATEGORIES = ("teaching_comparison", "solving_comparison", "pipeline_comparison")
CATEGORY_LABELS = {
    "teaching_comparison": "第 1 类：教学能力比较",
    "solving_comparison": "第 2 类：解题能力比较",
    "pipeline_comparison": "第 3 类：完整流程比较",
}
RUN_STATUSES = ("completed", "failed", "timeout", "interrupted", "unverified")
VERDICTS = ("correct", "wrong", "partial", "not_run")
VERIFIER_KINDS = ("human", "model", "workflow", "未记录")
ROUND_TYPES = ("initial", "verification_revision", "prompt_iteration")
MODE_MANUAL = "manual_import"
MODE_DSH_HEADLESS = "dsh_headless"
INVOCATION_MODES = (MODE_MANUAL, MODE_DSH_HEADLESS)

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")
DEVICE = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?\Z", re.I)
# 工作台/框架名称不是模型名；这些取值不能直接写入 configured_model。
RESERVED_MODEL_NAMES = {"dsh", "deepseek harness", "codex", "rethlas", "agent 工作台", "工作台"}
EXTENSIONS = {".md", ".html", ".txt", ".tex", ".json", ".jsonl", ".log", ".csv",
              ".png", ".jpg", ".jpeg", ".svg", ".pdf"}
TEXT_EXTENSIONS = {".md", ".html", ".txt", ".tex", ".json", ".jsonl", ".log", ".csv"}
BLOCKED_PARTS = {".git", ".venv", ".uv-cache", ".codex", "node_modules", "__pycache__"}
MAX_TEXT_BYTES = 16 * 1024 * 1024


class EvalSpecError(ValueError):
    """规格、导入元数据或核验结果不满足接入约定。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text(value, label) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvalSpecError(f"{label} 必须是非空字符串")
    return value


def _optional_text(value, label, default="未记录") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise EvalSpecError(f"{label} 必须是字符串")
    value = value.strip()
    return value or default


def safe_id(value, label) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value) or DEVICE.fullmatch(value) or value.endswith("."):
        raise EvalSpecError(f"{label} 必须是安全 ASCII 标识（字母数字、下划线、短横线、点），且不能是 Windows 设备名：{value!r}")
    return value


def normalize_question(text: str) -> str:
    return " ".join(str(text).split())


def question_id_for(text: str) -> str:
    return "q_" + _digest(normalize_question(text).encode("utf-8"))[:12]


def new_run_id() -> str:
    """独立测试身份：UTC 时间 + 随机后缀，不以历史最大编号加一。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}_{secrets.token_hex(4)}"


def _resolve_inside(root: Path, value, label: str) -> tuple[Path, Path]:
    if not isinstance(value, str) or not value.strip():
        raise EvalSpecError(f"{label} 必须是非空路径字符串")
    candidate = Path(value)
    if candidate.is_absolute():
        absolute = candidate
        try:
            relative = absolute.resolve(strict=False).relative_to(root)
        except ValueError as error:
            raise EvalSpecError(f"{label} 必须位于项目目录内：{value}") from error
    else:
        if ".." in candidate.parts:
            raise EvalSpecError(f"{label} 不能包含上级目录穿越：{value}")
        absolute = root / candidate
        relative = candidate
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise EvalSpecError(f"{label} 不能是符号链接或目录联接：{value}")
    absolute = absolute.resolve(strict=False)
    if not absolute.is_relative_to(root):
        raise EvalSpecError(f"{label} 解析后仍在项目目录之外：{value}")
    if absolute.suffix.lower() not in EXTENSIONS:
        raise EvalSpecError(f"{label} 的文件类型不在允许的评测材料类型内：{value}")
    if not absolute.is_file():
        raise EvalSpecError(f"{label} 不存在或不是普通文件：{value}")
    return absolute.relative_to(root), absolute


def _read_file_entry(root: Path, value, label: str, *, require_text: bool) -> dict:
    relative, absolute = _resolve_inside(root, value, label)
    raw = absolute.read_bytes()
    if len(raw) > MAX_TEXT_BYTES:
        raise EvalSpecError(f"{label} 超过 {MAX_TEXT_BYTES} 字节上限：{value}")
    text = None
    if require_text:
        if absolute.suffix.lower() not in TEXT_EXTENSIONS:
            raise EvalSpecError(f"{label} 需要文本文件（.md/.txt/.tex/.json 等）：{value}")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise EvalSpecError(f"{label} 不是 UTF-8 文本：{value}") from error
    return {"file": relative.as_posix(), "sha256": _digest(raw), "text": text}


def _load_json(path: Path, label: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise EvalSpecError(f"{label} 不是可读取的 UTF-8 JSON：{path}（{error}）") from error
    if not isinstance(data, dict):
        raise EvalSpecError(f"{label} 顶层必须是 JSON 对象：{path}")
    return data


def load_json_file(path, label: str) -> dict:
    return _load_json(Path(path), label)


def _normalize_tags(raw, label="tags") -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise EvalSpecError(f"{label} 必须是字符串数组")
    tags: list[str] = []
    for item in raw:
        item = item.strip()
        if re.search(r"[\x00-\x1f\x7f]", item):
            raise EvalSpecError(f"{label} 不能包含控制字符")
        if item and item not in tags:
            tags.append(item)
    return tags


def _normalize_budget(raw, label: str) -> dict:
    if raw is None:
        return {"max_calls": None, "max_rounds": None, "timeout_seconds": None, "cost_limit": None}
    if not isinstance(raw, dict):
        raise EvalSpecError(f"{label} 必须是 JSON 对象")
    allowed = {"max_calls", "max_rounds", "timeout_seconds", "cost_limit", "notes"}
    unknown = set(raw) - allowed
    if unknown:
        raise EvalSpecError(f"{label} 含未知字段：{sorted(unknown)}")
    budget = {}
    for key in ("max_calls", "max_rounds", "timeout_seconds"):
        value = raw.get(key)
        if value is not None and (type(value) is not int or value < 1):
            raise EvalSpecError(f"{label}.{key} 必须是正整数或 null")
        budget[key] = value
    cost_limit = raw.get("cost_limit")
    if cost_limit is not None and not isinstance(cost_limit, str):
        raise EvalSpecError(f"{label}.cost_limit 必须是字符串或 null")
    budget["cost_limit"] = cost_limit
    notes = raw.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise EvalSpecError(f"{label}.notes 必须是字符串或 null")
    budget["notes"] = notes
    return budget


def _check_model_label(configured_model: str, framework: str, label: str) -> None:
    """工作台名称不能当模型名；配置模型与实际响应模型分别保存。"""
    normalized = configured_model.strip().lower()
    if normalized in RESERVED_MODEL_NAMES:
        raise EvalSpecError(
            f"{label}.configured_model 不能用工作台/框架名称当模型名：{configured_model!r}；"
            "实际模型无法确定时写“未记录”，并在响应元数据中保存 response 实际模型。"
        )
    if normalized and normalized == framework.strip().lower():
        raise EvalSpecError(
            f"{label}.configured_model 与 framework 相同（{configured_model!r}）；"
            "DSH/Codex/Rethlas 是工作台或工作流，不是模型名。"
        )


def _normalize_verifier(raw, label: str, *, required: bool) -> dict | None:
    if raw is None:
        if required:
            raise EvalSpecError(f"{label} 不能为空：第 2/3 类评测必须明确指定独立核验者")
        return None
    if not isinstance(raw, dict):
        raise EvalSpecError(f"{label} 必须是 JSON 对象")
    verifier = {
        "verifier_id": safe_id(raw.get("verifier_id"), f"{label}.verifier_id"),
        "name": _optional_text(raw.get("name"), f"{label}.name"),
        "kind": _optional_text(raw.get("kind"), f"{label}.kind", "未记录"),
        "model": _optional_text(raw.get("model"), f"{label}.model"),
        "provider": _optional_text(raw.get("provider"), f"{label}.provider"),
        "framework": _optional_text(raw.get("framework"), f"{label}.framework"),
        "notes": _optional_text(raw.get("notes"), f"{label}.notes", ""),
    }
    if verifier["kind"] not in VERIFIER_KINDS:
        raise EvalSpecError(f"{label}.kind 必须是 {VERIFIER_KINDS} 之一")
    return verifier


def _check_verifier_separation(verifier: dict | None, candidates: list[dict]) -> None:
    if not verifier:
        return
    for candidate in candidates:
        if verifier["verifier_id"] in {candidate["ai_id"], candidate["candidate_id"]}:
            raise EvalSpecError(
                "核验者身份必须与受测模型/工作流分开："
                f"verifier_id={verifier['verifier_id']!r} 与 candidate={candidate['candidate_id']!r} 冲突"
            )


def _material(root: Path, raw, label: str, *, require_text=True) -> dict | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        value = raw
    elif isinstance(raw, dict) and "file" in raw:
        value = raw["file"]
    else:
        raise EvalSpecError(f"{label} 必须是项目内文件路径字符串，或 {{\"file\": ...}} 对象")
    entry = _read_file_entry(root, value, label, require_text=require_text)
    if require_text and not entry["text"].strip():
        raise EvalSpecError(f"{label} 不能是空文本：{entry['file']}")
    return entry


def _material_list(root: Path, raw, label: str) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise EvalSpecError(f"{label} 必须是数组")
    return [_read_file_entry(root, item, f"{label}[{index}]", require_text=False)
            for index, item in enumerate(raw)]


def _prompt_block(root: Path, raw, label: str) -> dict:
    if not isinstance(raw, dict):
        raise EvalSpecError(f"{label} 必须是 JSON 对象，包含 file 或 text")
    has_file, has_text = "file" in raw, "text" in raw
    if has_file == has_text:
        raise EvalSpecError(f"{label} 必须且只能提供 file 或 text 之一")
    if has_file:
        entry = _read_file_entry(root, raw["file"], f"{label}.file", require_text=True)
        entry.pop("text", None)
        text = (root / entry["file"]).read_bytes().decode("utf-8-sig")
        return {"file": entry["file"], "sha256": entry["sha256"], "text": text}
    text = _text(raw["text"], f"{label}.text")
    return {"file": None, "sha256": _digest(text.encode("utf-8")), "text": text}


def _user_request_block(root: Path, raw) -> dict:
    if raw is None:
        return {"file": None, "sha256": None, "text": "未记录"}
    if isinstance(raw, str):
        return {"file": None, "sha256": _digest(raw.encode("utf-8")), "text": raw}
    if not isinstance(raw, dict):
        raise EvalSpecError("user_request 必须是字符串或含 file/text 的对象")
    has_file, has_text = "file" in raw, "text" in raw
    if has_file == has_text:
        raise EvalSpecError("user_request 必须且只能提供 file 或 text 之一")
    if has_file:
        entry = _read_file_entry(root, raw["file"], "user_request.file", require_text=True)
        text = (root / entry["file"]).read_bytes().decode("utf-8-sig")
        return {"file": entry["file"], "sha256": entry["sha256"], "text": text}
    text = _text(raw["text"], "user_request.text")
    return {"file": None, "sha256": _digest(text.encode("utf-8")), "text": text}


def load_spec(path, project_root) -> dict:
    """读取并校验评测规格；返回规范化字典（所有材料已读取并计算 SHA256）。"""
    root = Path(project_root).resolve(strict=True)
    spec_rel, spec_abs = _resolve_inside(root, str(path), "spec")
    raw = _load_json(spec_abs, "评测规格")
    if raw.get("schema_version") != 1:
        raise EvalSpecError("评测规格 schema_version 必须为 1")
    category = raw.get("category")
    if category not in CATEGORIES:
        raise EvalSpecError(f"category 必须是 {CATEGORIES} 之一")

    schema = {"schema_version": 1, "spec_file": spec_rel.as_posix(),
              "spec_sha256": _digest(spec_abs.read_bytes()),
              "category": category, "category_label": CATEGORY_LABELS[category],
              "description": _optional_text(raw.get("description"), "description", "")}

    batch = raw.get("batch") or {}
    evaluation = raw.get("evaluation", {})
    try:
        validate_evaluation(evaluation)
    except ValueError as error:
        raise EvalSpecError(str(error)) from error
    schema["evaluation"] = evaluation
    if not isinstance(batch, dict):
        raise EvalSpecError("batch 必须是 JSON 对象")
    batch_id = batch.get("batch_id")
    schema["batch_id"] = (safe_id(batch_id, "batch.batch_id") if batch_id is not None
                          else "eval-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4))
    schema["batch_notes"] = _optional_text(batch.get("notes"), "batch.notes", "")
    if "test_set_id" in batch:
        schema["test_set_id"] = safe_id(batch["test_set_id"], "batch.test_set_id")

    question_raw = raw.get("question")
    if not isinstance(question_raw, dict):
        raise EvalSpecError("question 必须是 JSON 对象")
    if "file" in question_raw:
        question = _read_file_entry(root, question_raw["file"], "question.file", require_text=True)
        question_text = question["text"]
    elif "text" in question_raw:
        question_text = _text(question_raw["text"], "question.text")
        question = {"file": None, "sha256": _digest(question_text.encode("utf-8")), "text": question_text}
    else:
        raise EvalSpecError("question 必须包含 file 或 text")
    if not question_text.strip():
        raise EvalSpecError("题面不能为空")
    question_id = question_raw.get("question_id")
    question_id = safe_id(question_id, "question.question_id") if question_id is not None else question_id_for(question_text)
    schema["question"] = {"question_id": question_id, "tags": _normalize_tags(question_raw.get("tags")),
                          "file": question["file"], "sha256": question["sha256"], "text": question_text}

    materials_raw = raw.get("materials") or {}
    if not isinstance(materials_raw, dict):
        raise EvalSpecError("materials 必须是 JSON 对象")
    for banned in ("prior_teaching_outputs", "prior_answers"):
        if materials_raw.get(banned):
            raise EvalSpecError(
                f"materials.{banned} 不能用于独立比较；本接入只做相互独立的初稿比较。"
                "包含旧回答的修改实验需要另行明确标注，不能混入独立模型比较。"
            )
    materials: dict = {}
    if category == "teaching_comparison":
        for key, label, required in (("verified_proof", "已核验证明", True),
                                     ("student_background", "学生背景", True),
                                     ("teaching_requirements", "教学要求", True),
                                     ("requirements", "回答要求", False)):
            field = key + "_file" if key + "_file" in materials_raw else key
            materials[key] = _material(root, materials_raw.get(field), f"materials.{field}", require_text=True)
            if required and materials[key] is None:
                raise EvalSpecError(f"第 1 类教学能力比较缺少 materials.{key}_file")
        proof_source = materials_raw.get("verified_proof_source") or {}
        if not isinstance(proof_source, dict):
            raise EvalSpecError("materials.verified_proof_source 必须是 JSON 对象")
        materials["verified_proof_source"] = {
            "kind": _optional_text(proof_source.get("kind"), "verified_proof_source.kind"),
            "reference": _optional_text(proof_source.get("reference"), "verified_proof_source.reference"),
            "verdict": _optional_text(proof_source.get("verdict"), "verified_proof_source.verdict"),
            "verified_by": _optional_text(proof_source.get("verified_by"), "verified_proof_source.verified_by"),
            "verified_at": _optional_text(proof_source.get("verified_at"), "verified_proof_source.verified_at", ""),
            "notes": _optional_text(proof_source.get("notes"), "verified_proof_source.notes", ""),
        }
    else:
        materials["allowed_resources"] = _material_list(root, materials_raw.get("allowed_resources"),
                                                        "materials.allowed_resources")
        materials["requirements"] = _material(root, materials_raw.get("requirements_file"),
                                              "materials.requirements_file", require_text=True)
        if category == "pipeline_comparison":
            materials["verified_proof"] = _material(root, materials_raw.get("verified_proof_file"),
                                                    "materials.verified_proof_file", require_text=True)
    materials["reference_files"] = _material_list(root, materials_raw.get("reference_files"), "materials.reference_files")
    schema["materials"] = materials
    schema["user_request"] = _user_request_block(root, raw.get("user_request"))
    schema["prompt"] = _prompt_block(root, raw.get("prompt"), "prompt")

    design_raw = raw.get("design") or {}
    if not isinstance(design_raw, dict):
        raise EvalSpecError("design 必须是 JSON 对象")
    independent = design_raw.get("independent_from_other_models", True)
    if independent is not True:
        raise EvalSpecError(
            "三类评测都要求各模型/流程独立生成；design.independent_from_other_models 必须为 true。"
            "包含旧回答的修改实验不属于本轮独立比较接口。"
        )
    schema["design"] = {
        "independent_from_other_models": True,
        "modification_rounds_allowed": bool(design_raw.get("modification_rounds_allowed", False)),
        "notes": _optional_text(design_raw.get("notes"), "design.notes", ""),
    }

    candidates_raw = raw.get("candidates")
    if not isinstance(candidates_raw, list) or not candidates_raw:
        raise EvalSpecError("candidates 必须是非空数组")
    candidates: list[dict] = []
    seen_ids, seen_ai = set(), set()
    for index, item in enumerate(candidates_raw):
        label = f"candidates[{index}]"
        if not isinstance(item, dict):
            raise EvalSpecError(f"{label} 必须是 JSON 对象")
        candidate_id = safe_id(item.get("candidate_id") or item.get("id"), f"{label}.candidate_id")
        ai_id = safe_id(item.get("ai_id"), f"{label}.ai_id")
        if candidate_id in seen_ids:
            raise EvalSpecError(f"{label}.candidate_id 重复：{candidate_id}")
        if ai_id in seen_ai:
            raise EvalSpecError(
                f"{label}.ai_id 重复：{ai_id}；不同模型或工作流必须使用不同 ai_id，独立运行必须使用不同 run_id"
            )
        seen_ids.add(candidate_id)
        seen_ai.add(ai_id)
        framework = _text(item.get("framework"), f"{label}.framework")
        configured_model = _optional_text(item.get("configured_model"), f"{label}.configured_model")
        _check_model_label(configured_model, framework, label)
        invocation = item.get("invocation") or {}
        if not isinstance(invocation, dict):
            raise EvalSpecError(f"{label}.invocation 必须是 JSON 对象")
        mode = _optional_text(invocation.get("mode"), f"{label}.invocation.mode", MODE_MANUAL)
        if mode not in INVOCATION_MODES:
            raise EvalSpecError(
                f"{label}.invocation.mode 必须是 {INVOCATION_MODES} 之一；"
                "其他模型没有可靠自动化接口时使用 manual_import 导入完整输入输出。"
            )
        invocation_normalized = {"mode": mode}
        budget = _normalize_budget(item.get("budget"), f"{label}.budget")
        if mode == MODE_DSH_HEADLESS:
            invocation_normalized["profile"] = _optional_text(
                invocation.get("profile"), f"{label}.invocation.profile", "headless")
            if budget["timeout_seconds"] is None:
                budget["timeout_seconds"] = 900
            if budget["max_calls"] not in (None, 1):
                raise EvalSpecError(
                    f"{label}.budget.max_calls 只能为 1：headless 每次运行一个任务，"
                    "自动适配器不做隐藏重试"
                )
            budget["max_calls"] = 1
        candidate = {
            "candidate_id": candidate_id, "ai_id": ai_id, "framework": framework,
            "configured_model": configured_model,
            "provider": _optional_text(item.get("provider"), f"{label}.provider"),
            "tool_permissions": _optional_text(item.get("tool_permissions"), f"{label}.tool_permissions"),
            "invocation": invocation_normalized,
            "budget": budget,
            "notes": _optional_text(item.get("notes"), f"{label}.notes", ""),
        }
        if category == "pipeline_comparison":
            tools = item.get("tools")
            if not isinstance(tools, list) or any(not isinstance(tool, str) or not tool.strip() for tool in tools):
                raise EvalSpecError(f"{label}.tools 必须是字符串数组：完整流程比较必须记录工具")
            candidate["tools"] = [tool.strip() for tool in tools]
            candidate["organization"] = _text(item.get("organization"), f"{label}.organization")
            budget = candidate["budget"]
            if all(budget[key] is None for key in ("max_calls", "max_rounds", "timeout_seconds", "cost_limit")):
                raise EvalSpecError(f"{label}.budget 不能为空：完整流程比较必须记录调用预算")
        candidates.append(candidate)
        if "evaluation" in item:
            try:
                validate_evaluation(item["evaluation"])
            except ValueError as error:
                raise EvalSpecError(str(error)) from error
            candidate["evaluation"] = item["evaluation"]
    schema["candidates"] = candidates

    plan_raw = raw.get("verification_plan")
    if not isinstance(plan_raw, dict):
        raise EvalSpecError("verification_plan 必须是 JSON 对象（每份回答需另行独立核验）")
    if plan_raw.get("required", True) is not True:
        raise EvalSpecError("verification_plan.required 必须为 true：评测接入不能把未核验回答记为数学正确")
    verifier = _normalize_verifier(plan_raw.get("verifier"), "verification_plan.verifier",
                                   required=category in ("solving_comparison", "pipeline_comparison"))
    _check_verifier_separation(verifier, candidates)
    schema["verification_plan"] = {
        "required": True,
        "method": _optional_text(plan_raw.get("method"), "verification_plan.method"),
        "verifier": verifier,
        "notes": _optional_text(plan_raw.get("notes"), "verification_plan.notes", ""),
    }

    evaluation_raw = raw.get("evaluation_plan") or {}
    if not isinstance(evaluation_raw, dict):
        raise EvalSpecError("evaluation_plan 必须是 JSON 对象")
    rubric = evaluation_raw.get("rubric_file")
    rubric_entry = _read_file_entry(root, rubric, "evaluation_plan.rubric_file", require_text=False) if rubric else None
    dimensions = evaluation_raw.get("dimensions") or []
    if not isinstance(dimensions, list) or any(not isinstance(item, str) or not item.strip() for item in dimensions):
        raise EvalSpecError("evaluation_plan.dimensions 必须是字符串数组")
    schema["evaluation_plan"] = {
        "rubric_file": rubric_entry["file"] if rubric_entry else None,
        "rubric_sha256": rubric_entry["sha256"] if rubric_entry else None,
        "dimensions": [item.strip() for item in dimensions],
        "human_review_note": _optional_text(evaluation_raw.get("human_review_note"),
                                            "evaluation_plan.human_review_note", ""),
    }
    return schema


def normalize_metadata(raw: dict, spec: dict, candidate: dict) -> dict:
    """规范化单轮导入元数据；实际模型缺失时写“未记录”，不从工作台名称推断。"""
    if not isinstance(raw, dict):
        raise EvalSpecError("导入元数据必须是 JSON 对象")
    try:
        validate_evaluation(raw.get("evaluation", {}))
        validate_reviews(raw.get("external_reviews", []))
    except ValueError as error:
        raise EvalSpecError(str(error)) from error
    status = raw.get("status")
    if status not in RUN_STATUSES:
        raise EvalSpecError(f"导入元数据 status 必须是 {RUN_STATUSES} 之一：{status!r}")
    actual_model = raw.get("actual_model")
    source = "actual_model"
    if actual_model is None and raw.get("model") is not None:
        actual_model = raw.get("model")
        source = "model"
    actual_model = _optional_text(actual_model, "导入元数据 actual_model")
    if actual_model == "未记录":
        source = "未记录"
    generation = raw.get("generation") or {}
    timing = raw.get("timing") or {}
    usage = raw.get("usage") or {}
    for key, value in (("generation", generation), ("timing", timing), ("usage", usage)):
        if not isinstance(value, dict):
            raise EvalSpecError(f"导入元数据 {key} 必须是 JSON 对象")
    tool_events = raw.get("tool_events") or []
    if not isinstance(tool_events, list):
        raise EvalSpecError("导入元数据 tool_events 必须是数组")
    error = raw.get("error")
    if error is not None and not isinstance(error, str):
        raise EvalSpecError("导入元数据 error 必须是字符串或 null")
    if status != "completed" and not error:
        error = f"导入状态：{status}（原记录未提供更具体原因）"

    round_raw = raw.get("round") or {}
    if not isinstance(round_raw, dict):
        raise EvalSpecError("导入元数据 round 必须是 JSON 对象")
    default_type = "initial"
    round_type = _optional_text(round_raw.get("type"), "round.type", default_type)
    if round_type not in ROUND_TYPES:
        raise EvalSpecError(f"round.type 必须是 {ROUND_TYPES} 之一")
    category = spec["category"]
    default_iteration = {"teaching_comparison": "teaching-initial",
                         "solving_comparison": "solving-initial",
                         "pipeline_comparison": "pipeline-initial"}[category]
    iteration_id = safe_id(round_raw.get("iteration_id") or default_iteration, "round.iteration_id")
    change_reason = _optional_text(round_raw.get("change_reason"), "round.change_reason", "")
    revision_of = round_raw.get("revision_of")
    if revision_of is not None:
        revision_of = safe_id(revision_of, "round.revision_of")
    prompt_iteration = round_raw.get("prompt_iteration")
    if prompt_iteration is not None and (type(prompt_iteration) is not int or prompt_iteration < 2):
        raise EvalSpecError("round.prompt_iteration 必须是 >= 2 的整数")
    if round_type == "verification_revision":
        if not revision_of:
            raise EvalSpecError("核验反馈修订轮必须提供 round.revision_of，首稿与修订稿不能混为同一结果")
        if not change_reason:
            raise EvalSpecError("核验反馈修订轮必须提供 round.change_reason")
    if round_type == "prompt_iteration":
        if prompt_iteration is None:
            raise EvalSpecError("明确的提示词迭代轮必须提供 round.prompt_iteration（从 2 起）")
        if not spec["design"].get("modification_rounds_allowed"):
            raise EvalSpecError(
                "该规格未声明 design.modification_rounds_allowed=true；"
                "独立模型比较不能沿用旧稿或追加提示词迭代。"
            )
    feedback_file = round_raw.get("verification_feedback_file")
    if feedback_file is not None and not isinstance(feedback_file, str):
        raise EvalSpecError("round.verification_feedback_file 必须是字符串路径")
    return {
        "status": status,
        "actual_model": actual_model,
        "actual_model_source": source,
        "evaluation": raw.get("evaluation", {}),
        "external_reviews": raw.get("external_reviews", []),
        "provider": _optional_text(raw.get("provider"), "导入元数据 provider"),
        "reported_framework": _optional_text(raw.get("framework"), "导入元数据 framework"),
        "dsh_version": _optional_text(raw.get("dsh_version"), "导入元数据 dsh_version"),
        "tool_permissions": _optional_text(raw.get("tool_permissions"), "导入元数据 tool_permissions"),
        "generation": generation,
        "timing": timing,
        "usage": usage,
        "tool_events": tool_events,
        "error": error,
        "notes": _optional_text(raw.get("notes"), "导入元数据 notes", ""),
        "round": {
            "iteration_id": iteration_id,
            "type": round_type,
            "change_reason": change_reason,
            "revision_of": revision_of,
            "prompt_iteration": prompt_iteration,
            "verification_feedback_file": feedback_file,
        },
    }


def normalize_verification(raw: dict, spec: dict, candidate: dict, *, report_file: str | None = None) -> dict:
    """规范化独立核验结果；核验者身份与受测模型分开检查。"""
    if not isinstance(raw, dict):
        raise EvalSpecError("核验结果必须是 JSON 对象")
    round_id = safe_id(raw.get("round_id"), "核验结果 round_id")
    verifier = _normalize_verifier(raw.get("verifier"), "核验结果 verifier", required=True)
    _check_verifier_separation(verifier, [candidate])
    declared = (spec.get("verification_plan") or {}).get("verifier")
    if declared and verifier["verifier_id"] != declared["verifier_id"]:
        raise EvalSpecError(
            "核验者身份与规格中声明的独立核验者不一致："
            f"声明 {declared['verifier_id']!r}，实际 {verifier['verifier_id']!r}"
        )
    verdict = raw.get("verdict")
    if verdict not in VERDICTS:
        raise EvalSpecError(f"核验结果 verdict 必须是 {VERDICTS} 之一：{verdict!r}")
    summary = raw.get("summary")
    if summary is not None and not isinstance(summary, str):
        raise EvalSpecError("核验结果 summary 必须是字符串或 null")
    findings = raw.get("findings") or []
    if not isinstance(findings, list) or any(not isinstance(item, dict) for item in findings):
        raise EvalSpecError("核验结果 findings 必须是对象数组")
    critical_errors, gaps, notes = [], [], []
    for index, item in enumerate(findings):
        finding_type = item.get("type", "note")
        text = item.get("text")
        if finding_type not in ("critical_error", "gap", "note"):
            raise EvalSpecError(f"findings[{index}].type 必须是 critical_error/gap/note")
        if not isinstance(text, str) or not text.strip():
            raise EvalSpecError(f"findings[{index}].text 必须是非空字符串")
        {"critical_error": critical_errors, "gap": gaps, "note": notes}[finding_type].append(text.strip())
    return {
        "verdict": verdict,
        "summary": summary or "未记录",
        "verifier": verifier,
        "verification_report": {
            "critical_errors": critical_errors,
            "gaps": gaps,
            "notes": notes,
            "source": "independent_verification_import",
            "recorded_at": _now_iso(),
        },
        "report_file": report_file,
        "source": "imported_verification_json",
    }


def not_run_verification(reason: str = "本次导入未提供独立核验结果") -> list[dict]:
    """未核验必须显式呈现为 not_run，不能由运行完成推断数学正确。"""
    return [{"verdict": "not_run", "reason": reason, "source": "evaluation_import"}]


TEACHING_PROMPT_LABEL = "【实际发送输入】"
