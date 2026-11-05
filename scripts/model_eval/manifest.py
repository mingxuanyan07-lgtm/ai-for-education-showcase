"""把评测规格、实际回答与独立核验构造成 experiment_records 的 manifest 并归档。

这里不调用模型；只复制真实输入输出、校验身份并调用现有记录接口。
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import experiment_records  # noqa: E402  现有统一记录接口
from benchmark_evaluation import merge_design

from .spec import (  # noqa: E402
    CATEGORY_LABELS,
    EvalSpecError,
    load_json_file,
    load_spec,
    new_run_id,
    normalize_metadata,
    normalize_verification,
    not_run_verification,
    safe_id,
)
from .staging import BatchStore  # noqa: E402

SECRET_KEY_PARTS = ("api_key", "apikey", "secret", "password", "credential", "authorization")
SECRET_KEY_SUFFIXES = ("_token", "_apikey")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _looks_like_secret_key(key) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    if normalized in ("token", "auth"):
        return True
    if any(part in normalized for part in SECRET_KEY_PARTS):
        return True
    return normalized.endswith(SECRET_KEY_SUFFIXES)


def assert_no_secrets(value, label: str = "导入元数据", *, _depth=0) -> None:
    """拒绝把凭据写入归档；只检查字段名，不读取、不回显任何密钥值。

    注意 usage 中的 input_tokens/output_tokens/total_tokens 是正常计量字段，
    不属于凭据；只有 token/api_key/secret 等凭据字段名会被拒绝。
    """
    if _depth > 20:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if _looks_like_secret_key(key):
                raise EvalSpecError(
                    f"{label} 含疑似凭据字段 {key!r}；不要导入、保存或复制 API key。"
                    "请删除该字段后重试，凭据只能由运行环境自身的环境变量提供。"
                )
            assert_no_secrets(item, label, _depth=_depth + 1)
    elif isinstance(value, list):
        for item in value:
            assert_no_secrets(item, label, _depth=_depth + 1)


def find_candidate(spec: dict, candidate_id: str) -> dict:
    candidate_id = safe_id(candidate_id, "candidate_id")
    for candidate in spec["candidates"]:
        if candidate["candidate_id"] == candidate_id:
            return candidate
    raise EvalSpecError(f"规格中没有候选：{candidate_id}")


def _requirements_text(spec: dict) -> str:
    materials = spec["materials"]
    parts = []
    if materials.get("student_background"):
        parts.append("学生背景：\n" + materials["student_background"]["text"].strip())
    if materials.get("requirements"):
        parts.append("回答要求：\n" + materials["requirements"]["text"].strip())
    if materials.get("teaching_requirements"):
        parts.append("教学要求：\n" + materials["teaching_requirements"]["text"].strip())
    resources = materials.get("allowed_resources") or []
    if resources:
        parts.append("允许资料：\n" + "\n".join(f"- {item['file']}" for item in resources))
    return "\n\n".join(parts) or "未记录"


def _material_hashes(spec: dict) -> dict:
    hashes = {"question": spec["question"]["sha256"], "prompt": spec["prompt"]["sha256"]}
    for key in ("verified_proof", "student_background", "teaching_requirements", "requirements"):
        entry = spec["materials"].get(key)
        if entry:
            hashes[key] = entry["sha256"]
    hashes["reference_files"] = [{"file": entry["file"], "sha256": entry["sha256"]}
                                 for entry in spec["materials"].get("reference_files") or []]
    hashes["user_request"] = spec["user_request"]["sha256"]
    return hashes


def _iteration_settings(spec: dict, candidate: dict, metadata: dict, *,
                        prompt_source: str, materials: dict) -> dict:
    round_info = metadata["round"]
    settings = {
        "stage": "teaching" if spec["category"] == "teaching_comparison" else "solving",
        "evaluation_category": spec["category"],
        "batch_id": spec["batch_id"],
        "evaluation_category_label": CATEGORY_LABELS[spec["category"]],
        "candidate_id": candidate["candidate_id"],
        "framework": candidate["framework"],
        "configured_model": candidate["configured_model"],
        "configured_provider": candidate["provider"],
        "actual_model": metadata["actual_model"],
        "actual_model_source": metadata["actual_model_source"],
        "actual_provider": metadata["provider"],
        "reported_framework": metadata["reported_framework"],
        "dsh_version": metadata["dsh_version"],
        "configured_tool_permissions": candidate["tool_permissions"],
        "actual_tool_permissions": metadata["tool_permissions"],
        "generation": metadata["generation"],
        "timing": metadata["timing"],
        "usage": metadata["usage"],
        "budget": candidate["budget"],
        "round": round_info,
        "prompt_source": prompt_source,
        "materials": materials,
        "verification_plan": spec["verification_plan"],
        "evaluation_plan": spec["evaluation_plan"],
        "design": spec["design"],
        "imported_at": _now(),
    }
    if spec["category"] == "pipeline_comparison":
        settings["tools"] = candidate.get("tools", [])
        settings["organization"] = candidate.get("organization", "未记录")
    return settings


def _answer_text_from_file(path: Path) -> str:
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise EvalSpecError(f"回答文件必须是 UTF-8 文本（原始字节仍会保存）：{path}") from error


def build_manifest(spec: dict, candidate: dict, identity: dict, metadata: dict, *,
                   prompt_file: str, answer_file: str | None, answer_text: str,
                   verification: list[dict], evidence_files: list[str],
                   staged_metadata: str, prompt_source: str,
                   extra_settings: dict | None = None) -> dict:
    item = {
        "iteration_id": metadata["round"]["iteration_id"],
        "prompt_file": prompt_file,
        "answer": answer_text if answer_text else "未记录",
        "change_reason": metadata["round"]["change_reason"] or "独立初稿；未参考其他模型回答",
        "settings": _iteration_settings(spec, candidate, metadata, prompt_source=prompt_source,
                                        materials=_material_hashes(spec)),
        "tool_checks": metadata["tool_events"],
        "verification": verification,
    }
    if extra_settings:
        item["settings"].update(extra_settings)
    if answer_file:
        item["answer_file"] = answer_file
    if metadata["round"]["type"] == "prompt_iteration":
        item["settings"]["prompt_iteration"] = metadata["round"]["prompt_iteration"]
    item["settings"]["staged_metadata"] = staged_metadata
    manifest = {
        "question_id": spec["question"]["question_id"],
        "ai_id": candidate["ai_id"],
        "run_id": identity["run_id"],
        "tags": spec["question"]["tags"],
        "question": spec["question"]["text"],
        "requirements": _requirements_text(spec),
        "user_prompt": spec["user_request"]["text"] or "未记录",
        "model": metadata["actual_model"],
        "framework": candidate["framework"],
        "status": metadata["status"],
        "error": metadata["error"],
        "task_review": (
            "自动检查说明（需人工确认）：本记录由模型与工作流评测接入导入；"
            "运行状态、实际模型与独立核验分别保存，��由程序判断教学或解题质量。"
        ),
        "iterations": [item],
        "evidence_files": evidence_files,
        "record_kind": "math_experiment",
    }
    if spec.get("test_set_id"):
        manifest["test_set_id"] = spec["test_set_id"]
    evaluation = merge_design(spec.get("evaluation", {}), candidate.get("evaluation", {}))
    evaluation = merge_design(evaluation, metadata.get("evaluation", {}))
    if evaluation:
        manifest["evaluation"] = evaluation
    if metadata.get("external_reviews"):
        manifest["external_reviews"] = metadata["external_reviews"]
    return manifest


def _evidence_list(*groups) -> list[str]:
    files: list[str] = []
    for group in groups:
        for value in group:
            if value and value not in files:
                files.append(value)
    return files


def prepare_batch(project_root, spec_path, *, dry_run: bool = False) -> dict:
    root = Path(project_root).resolve(strict=True)
    spec = load_spec(spec_path, root)
    store = BatchStore(root, spec["batch_id"])
    if dry_run:
        candidates = []
        for candidate in spec["candidates"]:
            candidates.append({"candidate_id": candidate["candidate_id"], "ai_id": candidate["ai_id"],
                               "run_id": "预览（未保存）", "configured_model": candidate["configured_model"],
                               "framework": candidate["framework"]})
        return {"dry_run": True, "wrote": False, "batch_id": spec["batch_id"],
                "category": spec["category"], "category_label": spec["category_label"],
                "question_id": spec["question"]["question_id"],
                "spec_file": spec["spec_file"], "spec_sha256": spec["spec_sha256"],
                "prompt_sha256": spec["prompt"]["sha256"],
                "materials": _material_hashes(spec), "candidates": candidates,
                "notice": "dry-run 只校验规格并显示身份，未写入批次目录，也未创建实验记录，未调用模型。"}
    staged = store.ensure_inputs(spec)
    store.ensure_plan(spec, staged)
    candidates = []
    for candidate in spec["candidates"]:
        identity = store.register_candidate(spec, candidate)
        prepared = store.prepare_candidate(spec, candidate, identity, staged)
        candidates.append({**prepared, "configured_model": candidate["configured_model"],
                           "framework": candidate["framework"]})
    return {"dry_run": False, "wrote": True, "batch_id": spec["batch_id"],
            "category": spec["category"], "category_label": spec["category_label"],
            "question_id": spec["question"]["question_id"],
            "spec_file": spec["spec_file"], "spec_sha256": spec["spec_sha256"],
            "prompt_sha256": spec["prompt"]["sha256"],
            "materials": _material_hashes(spec), "candidates": candidates,
            "directory": store.rel(store.base),
            "plan": store.rel(store.plan_path),
            "notice": "任务包已准备；未调用任何模型。"}


def _prepare_for_import(store: BatchStore, spec: dict, *, run_id: str | None = None) -> tuple[dict, dict, dict]:
    """导入前确保输入快照与候选身份存在；返回 staged、plan、各候选身份。"""
    staged = store.ensure_inputs(spec)
    plan = store.ensure_plan(spec, staged)
    identities = {}
    for candidate in spec["candidates"]:
        identity = store.register_candidate(spec, candidate, run_id=run_id)
        store.prepare_candidate(spec, candidate, identity, staged)
        identities[candidate["candidate_id"]] = identity
    return staged, plan, identities


def _resolve_import_inputs(spec: dict, candidate: dict, metadata_path, verification_path):
    metadata = load_json_file(metadata_path, "导入元数据")
    assert_no_secrets(metadata, "导入元数据")
    metadata = normalize_metadata(metadata, spec, candidate)
    verification_raw = None
    if verification_path is not None:
        verification_raw = load_json_file(verification_path, "核验结果")
        assert_no_secrets(verification_raw, "核验结果")
        verification_raw = normalize_verification(verification_raw, spec, candidate)
        if metadata["status"] != "completed" and verification_raw["verdict"] == "correct":
            raise EvalSpecError("失败、超时或中断的记录不能附带 correct 核验结论")
    return metadata, verification_raw


def import_candidate(project_root, spec_path, candidate_id, *, response_path, metadata_path,
                     verification_path=None, report_path=None, run_id=None,
                     dry_run: bool = False, extra_evidence: list[str] | None = None,
                     extra_settings: dict | None = None,
                     actual_input_text: str | None = None) -> dict:
    root = Path(project_root).resolve(strict=True)
    spec = load_spec(spec_path, root)
    candidate = find_candidate(spec, candidate_id)
    metadata, verification_raw = _resolve_import_inputs(spec, candidate, metadata_path, verification_path)
    response = Path(response_path) if response_path else None
    if metadata["status"] == "completed" and response is None:
        raise EvalSpecError("completed 记录必须提供完整回答文件；没有回答不能记为完成")
    if response is not None and not response.is_file():
        raise EvalSpecError(f"回答文件不存在：{response}")
    answer_text = _answer_text_from_file(response) if response is not None else ""
    if metadata["status"] == "completed" and not answer_text.strip():
        raise EvalSpecError("completed 记录的回答不能为空；空回答不能记为成功")
    if verification_raw is None and metadata["status"] != "completed":
        verification = not_run_verification(f"状态为 {metadata['status']}，未提供独立核验结果")
    elif verification_raw is None:
        verification = not_run_verification()
    else:
        verification = [verification_raw]

    store = BatchStore(root, spec["batch_id"])
    if dry_run:
        identity = {"ai_id": candidate["ai_id"], "run_id": run_id or "预览（未保存）"}
        return {"dry_run": True, "wrote": False, "batch_id": spec["batch_id"],
                "candidate_id": candidate["candidate_id"], "ai_id": candidate["ai_id"],
                "run_id": identity["run_id"], "status": metadata["status"],
                "actual_model": metadata["actual_model"],
                "verification_verdict": verification[0]["verdict"],
                "answer_sha256": hashlib.sha256(answer_text.encode("utf-8")).hexdigest() if answer_text else None,
                "actual_input_sha256": (hashlib.sha256(actual_input_text.encode("utf-8")).hexdigest()
                                        if actual_input_text else None),
                "extra_evidence_count": len(extra_evidence or []),
                "notice": "dry-run 未写入暂存文件，未创建实验记录，未调用模型。"}

    staged, _plan, identities = _prepare_for_import(store, spec, run_id=run_id)
    identity = identities[candidate["candidate_id"]]
    if run_id is not None and identity["run_id"] != run_id:
        raise EvalSpecError(
            f"候选 {candidate_id} 已保存 run_id={identity['run_id']}；续跑必须沿用原 run_id，"
            "独立重测请使用新的 batch_id 或新的 candidate 身份"
        )
    candidate_dir = store.candidate_dir(candidate["candidate_id"])
    prompt_staged = store.rel(store.inputs / _prompt_input_name(store))
    prompt_copy = candidate_dir / ("prompt" + Path(prompt_staged).suffix)
    if not prompt_copy.is_file():
        store.prepare_candidate(spec, candidate, identity, staged)
    prompt_file = store.rel(prompt_copy)
    prompt_source = "spec_prompt_copy"
    extra_settings = dict(extra_settings or {})
    if actual_input_text is not None:
        if not actual_input_text.strip():
            raise EvalSpecError("实际发送输入不能为空")
        staged_input = store.stage_text(candidate["candidate_id"], "actual-input", actual_input_text)
        prompt_file = staged_input["staged"]
        prompt_source = "session_actual_input"
        extra_settings.update({
            "prepared_prompt_sha256": spec["prompt"]["sha256"],
            "actual_input_sha256": staged_input["sha256"],
            "actual_input_matches_prepared_prompt": (
                " ".join(actual_input_text.split()) == " ".join(spec["prompt"]["text"].split())),
        })
    answer_file = None
    evidence = [spec["spec_file"], staged.get("spec", {}).get("staged"),
                store.rel(store.inputs / "materials.json")]
    for extra in extra_evidence or []:
        if not isinstance(extra, str) or not extra:
            raise EvalSpecError("extra_evidence 必须是项目内路径字符串列表")
        evidence.append(extra)
    for key in ("question", "verified_proof", "student_background", "teaching_requirements",
                "requirements", "prompt", "user_request"):
        entry = staged.get(key)
        if entry:
            evidence.append(entry["staged"])
    for entry in staged.get("reference_files") or []:
        evidence.append(entry["staged"])
    staged_metadata = store.stage_text(
        candidate["candidate_id"], "metadata-normalized",
        json.dumps({"candidate": candidate["candidate_id"], "ai_id": candidate["ai_id"],
                    "run_id": identity["run_id"], "metadata": metadata},
                   ensure_ascii=False, indent=2) + "\n", suffix=".json")
    evidence.append(staged_metadata["staged"])
    raw_metadata = store.stage_external_file(candidate["candidate_id"], "metadata",
                                             metadata_path, extension_hint=".json")
    evidence.append(raw_metadata["staged"])
    evidence.append(store.rel(candidate_dir / "imports" / "index.jsonl"))
    if response is not None:
        staged_response = store.stage_external_file(candidate["candidate_id"], "response", response)
        answer_file = staged_response["staged"]
        evidence.append(answer_file)
    verification_json = None
    if verification_raw is not None:
        verification_json = store.stage_text(
            candidate["candidate_id"], "verification-normalized",
            json.dumps(verification_raw, ensure_ascii=False, indent=2) + "\n", suffix=".json")
        evidence.append(verification_json["staged"])
        staged_verification = store.stage_external_file(candidate["candidate_id"], "verification",
                                                        verification_path, extension_hint=".json")
        evidence.append(staged_verification["staged"])
    if report_path is not None:
        staged_report = store.stage_external_file(candidate["candidate_id"], "verification-report", report_path)
        evidence.append(staged_report["staged"])
        if verification_raw is not None:
            verification_raw["report_file"] = staged_report["staged"]
    import_record = {"time": _now(), "candidate_id": candidate["candidate_id"],
                     "ai_id": candidate["ai_id"], "run_id": identity["run_id"],
                     "status": metadata["status"], "actual_model": metadata["actual_model"],
                     "response_origin": str(response) if response else None,
                     "metadata_origin": str(metadata_path),
                     "verification_origin": str(verification_path) if verification_path else None,
                     "verification_verdict": verification[0]["verdict"],
                     "prompt_source": prompt_source,
                     "extra_evidence": list(extra_evidence or [])}
    store.append_import_record(candidate["candidate_id"], import_record)
    evidence.append(store.rel(candidate_dir / "imports" / "import-records.jsonl"))
    manifest = build_manifest(spec, candidate, identity, metadata, prompt_file=prompt_file,
                              answer_file=answer_file, answer_text=answer_text,
                              verification=verification, evidence_files=_evidence_list(evidence),
                              staged_metadata=staged_metadata["staged"], prompt_source=prompt_source,
                              extra_settings=extra_settings)
    result = experiment_records.record_manifest(manifest, root)
    result.update({"candidate_id": candidate["candidate_id"], "status": metadata["status"],
                   "actual_model": metadata["actual_model"],
                   "verification_verdict": verification[0]["verdict"],
                   "prompt_source": prompt_source,
                   "batch_id": spec["batch_id"]})
    return result


def _prompt_input_name(store: BatchStore) -> str:
    matches = sorted(store.inputs.glob("prompt.*"))
    if len(matches) != 1:
        raise EvalSpecError("输入快照缺少唯一的 prompt 文件；请重新准备评测批次")
    return matches[0].name


def _existing_record(root: Path, spec: dict, candidate: dict, run_id: str) -> tuple[Path, dict]:
    run_dir = root / "docs" / "experiments" / spec["question"]["question_id"] / candidate["ai_id"] / run_id
    record_path = run_dir / "record.json"
    if not record_path.is_file():
        raise EvalSpecError(f"实验记录尚不存在，无法补充核验或分析：{run_dir}。请先导入回答。")
    try:
        record = json.loads(record_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise EvalSpecError(f"实验记录不可读取：{record_path}（{error}）") from error
    return run_dir, record


def verify_iteration(project_root, spec_path, candidate_id, *, verification_path, report_path=None,
                     run_id: str | None = None, dry_run: bool = False) -> dict:
    root = Path(project_root).resolve(strict=True)
    spec = load_spec(spec_path, root)
    candidate = find_candidate(spec, candidate_id)
    store = BatchStore(root, spec["batch_id"])
    plan = store.load_plan()
    if plan is None:
        raise EvalSpecError("评测批次尚未准备；请先导入回答，再导入核验结果")
    identity = plan.get("candidates", {}).get(candidate["candidate_id"])
    if identity is None:
        raise EvalSpecError(f"候选 {candidate_id} 尚无已保存的回答与运行身份")
    if run_id is not None and identity["run_id"] != run_id:
        raise EvalSpecError(f"核验必须指向已保存的 run_id={identity['run_id']}，而不是 {run_id}")
    raw = load_json_file(verification_path, "核验结果")
    assert_no_secrets(raw, "核验结果")
    verification = normalize_verification(raw, spec, candidate)
    round_id = safe_id(raw.get("round_id"), "核验结果 round_id")
    run_dir, record = _existing_record(root, spec, candidate, identity["run_id"])
    rounds = {item["iteration_id"]: item for item in record.get("iterations", [])}
    if round_id not in rounds:
        raise EvalSpecError(f"记录中不存在轮次 {round_id!r}；核验结果必须指向已保存的实际输入输出")
    if record.get("status") not in ("completed", "reused"):
        raise EvalSpecError(f"记录状态为 {record.get('status')!r}，不能把失败/超时记录补充为 correct 或成功核验")
    if dry_run:
        return {"dry_run": True, "wrote": False, "run_id": identity["run_id"],
                "iteration_id": round_id, "verdict": verification["verdict"],
                "verifier_id": verification["verifier"]["verifier_id"],
                "notice": "dry-run 未写入核验结果，未修改实验记录，未调用模型。"}
    staged = store.stage_external_file(candidate["candidate_id"], "verification", verification_path,
                                       extension_hint=".json")
    evidence = [spec["spec_file"], staged["staged"]]
    if report_path is not None:
        staged_report = store.stage_external_file(candidate["candidate_id"], "verification-report", report_path)
        verification["report_file"] = staged_report["staged"]
        evidence.append(staged_report["staged"])
    manifest = {"question_id": spec["question"]["question_id"], "ai_id": candidate["ai_id"],
                "run_id": identity["run_id"],
                "iterations": [{"iteration_id": round_id, "verification": [verification]}],
                "evidence_files": _evidence_list(evidence)}
    result = experiment_records.record_manifest(manifest, root)
    result.update({"iteration_id": round_id, "verdict": verification["verdict"],
                   "verifier_id": verification["verifier"]["verifier_id"],
                   "run_dir": str(run_dir), "batch_id": spec["batch_id"]})
    return result


def annotate_record(project_root, spec_path, candidate_id, *, task_review_path=None,
                    human_review_path=None, run_id: str | None = None,
                    dry_run: bool = False) -> dict:
    root = Path(project_root).resolve(strict=True)
    spec = load_spec(spec_path, root)
    candidate = find_candidate(spec, candidate_id)
    if task_review_path is None and human_review_path is None:
        raise EvalSpecError("补充记录需要 --task-review 或 --human-review（用户明确提供的文字）")
    store = BatchStore(root, spec["batch_id"])
    plan = store.load_plan()
    if plan is None:
        raise EvalSpecError("评测批次尚未准备；请先导入回答")
    identity = plan.get("candidates", {}).get(candidate["candidate_id"])
    if identity is None:
        raise EvalSpecError(f"候选 {candidate_id} 尚无已保存的回答与运行身份")
    if run_id is not None and identity["run_id"] != run_id:
        raise EvalSpecError(f"补充记录必须指向已保存的 run_id={identity['run_id']}")
    _run_dir, _record = _existing_record(root, spec, candidate, identity["run_id"])
    manifest = {"question_id": spec["question"]["question_id"], "ai_id": candidate["ai_id"],
                "run_id": identity["run_id"]}
    evidence = [spec["spec_file"]]
    if task_review_path is not None:
        text = Path(task_review_path).read_text(encoding="utf-8-sig")
        if not text.strip():
            raise EvalSpecError("task_review 文件为空")
        staged = store.stage_external_file(candidate["candidate_id"], "task-review", task_review_path)
        evidence.append(staged["staged"])
        manifest["task_review"] = text
    if human_review_path is not None:
        text = Path(human_review_path).read_text(encoding="utf-8-sig")
        if not text.strip():
            raise EvalSpecError("人工评价文件为空；不能代用户生成评价")
        staged = store.stage_external_file(candidate["candidate_id"], "human-review", human_review_path)
        evidence.append(staged["staged"])
        manifest["human_review"] = text
    if dry_run:
        return {"dry_run": True, "wrote": False, "run_id": identity["run_id"],
                "fields": sorted(manifest), "notice": "dry-run 未写入补充材料，未修改实验记录。"}
    manifest["evidence_files"] = _evidence_list(evidence)
    result = experiment_records.record_manifest(manifest, root)
    result.update({"run_id": identity["run_id"], "batch_id": spec["batch_id"]})
    return result
