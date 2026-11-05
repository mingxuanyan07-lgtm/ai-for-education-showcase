"""评测批次的工作目录、输入快照与导入文件暂存。

暂存位置：work/evaluations/<batch_id>/。这里写入的是可复核的工作副本；正式记录仍由
experiment_records 生成。所有路径都保持在项目根目录内，便于证据快照按内容哈希保存。
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time

from .spec import EvalSpecError, safe_id
from benchmark_evaluation import merge_design

LOCK_TIMEOUT_SECONDS = 60
MATERIAL_DIR_NAMES = {
    "question": "question",
    "verified_proof": "verified-proof",
    "student_background": "student-background",
    "teaching_requirements": "teaching-requirements",
    "requirements": "requirements",
    "prompt": "prompt",
    "user_request": "user-request",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + os.urandom(4).hex() + ".tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path, label: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise EvalSpecError(f"{label} 不可读取：{path}（{error}）") from error
    if not isinstance(data, dict):
        raise EvalSpecError(f"{label} 必须是 JSON 对象：{path}")
    return data


class _Lock:
    """批次内的小范围互斥，避免两个导入同时改写 plan.json。"""

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                handle = self.path.open("x", encoding="utf-8")
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise EvalSpecError(f"评测批次锁被占用：{self.path}")
                time.sleep(0.05)
        with handle:
            handle.write(_json({"pid": os.getpid(), "time": _now()}))
            handle.flush()
        return self

    def __exit__(self, *exc_info):
        self.path.unlink(missing_ok=True)
        return False


class BatchStore:
    def __init__(self, project_root: Path, batch_id: str):
        self.root = Path(project_root).resolve(strict=True)
        self.batch_id = safe_id(batch_id, "batch_id")
        self.base = self.root / "work" / "evaluations" / self.batch_id
        self.inputs = self.base / "inputs"
        self.plan_path = self.base / "plan.json"

    # ---- 路径 -------------------------------------------------------------
    def rel(self, path: Path) -> str:
        return path.resolve(strict=False).relative_to(self.root).as_posix()

    def absolute(self, relative: str) -> Path:
        path = (self.root / relative).resolve(strict=False)
        if not path.is_relative_to(self.root) or ".." in Path(relative).parts:
            raise EvalSpecError(f"暂存路径超出项目目录：{relative}")
        return path

    def candidate_dir(self, candidate_id: str) -> Path:
        return self.base / safe_id(candidate_id, "candidate_id")

    # ---- 输入快照与计划 ---------------------------------------------------
    def _input_name(self, key: str, index: int | None = None) -> str:
        stem = MATERIAL_DIR_NAMES.get(key, key)
        return f"{stem}-{index:02d}" if index is not None else stem

    def _stage_entry(self, key: str, entry: dict | None, index: int | None = None) -> dict | None:
        if not entry:
            return None
        name = self._input_name(key, index)
        if entry.get("file"):
            extension = Path(entry["file"]).suffix.lower()
            target = self.inputs / (name + extension)
            _write_bytes(target, (self.root / entry["file"]).read_bytes())
            staged = target
        else:
            target = self.inputs / (name + ".md")
            _write_bytes(target, entry["text"].encode("utf-8"))
            staged = target
        return {"source": entry.get("file"), "staged": self.rel(staged),
                "sha256": entry.get("sha256") or _digest(staged.read_bytes())}

    def ensure_inputs(self, spec: dict) -> dict:
        """把规格引用的材料复制为不可变工作副本，并保存哈希清单。"""
        staged: dict = {"question": self._stage_entry("question", spec["question"])}
        for key in ("verified_proof", "student_background", "teaching_requirements", "requirements"):
            staged[key] = self._stage_entry(key, spec["materials"].get(key))
        staged["prompt"] = self._stage_entry("prompt", spec["prompt"])
        staged["user_request"] = self._stage_entry("user_request", spec["user_request"])
        references = []
        for index, entry in enumerate(spec["materials"].get("reference_files") or []):
            references.append(self._stage_entry("reference", entry, index))
        staged["reference_files"] = references
        spec_copy = self.inputs / "spec.json"
        _write_bytes(spec_copy, (self.root / spec["spec_file"]).read_bytes())
        staged["spec"] = {"source": spec["spec_file"], "staged": self.rel(spec_copy),
                          "sha256": spec["spec_sha256"]}
        manifest = {"batch_id": self.batch_id, "created_at": _now(),
                    "category": spec["category"], "question_id": spec["question"]["question_id"],
                    "materials": staged}
        _write_bytes(self.inputs / "materials.json", _json(manifest).encode("utf-8"))
        return staged

    def load_plan(self) -> dict | None:
        if not self.plan_path.is_file():
            return None
        return _read_json(self.plan_path, "评测批次计划")

    def ensure_plan(self, spec: dict, staged: dict) -> dict:
        plan = self.load_plan()
        if plan is not None:
            if plan.get("spec_sha256") != spec["spec_sha256"]:
                raise EvalSpecError(
                    "评测批次已存在且规格文件已被修改；同一 batch_id 不能混用不同规格。"
                    "请使用新的 batch.batch_id 重新准备。"
                )
            return plan
        plan = {"schema_version": 1, "batch_id": self.batch_id, "created_at": _now(), "updated_at": _now(),
                "spec_file": spec["spec_file"], "spec_sha256": spec["spec_sha256"],
                "category": spec["category"], "question_id": spec["question"]["question_id"],
                "inputs": staged, "candidates": {}}
        _write_bytes(self.plan_path, _json(plan).encode("utf-8"))
        return plan

    def update_plan(self, mutate) -> dict:
        with _Lock(self.base / ".plan.lock"):
            plan = self.load_plan()
            if plan is None:
                raise EvalSpecError("评测批次计划不存在；请先准备批次")
            mutate(plan)
            plan["updated_at"] = _now()
            _write_bytes(self.plan_path, _json(plan).encode("utf-8"))
            return plan

    def candidate_identity(self, spec: dict, candidate: dict, *, run_id: str | None = None,
                           dry_run: bool = False) -> dict:
        """读取已保存身份（dry-run 时只预览），不写入计划。"""
        plan = None if dry_run else self.load_plan()
        return self._identity_from_plan(plan, candidate, run_id=run_id, create_preview=dry_run)

    @staticmethod
    def _identity_from_plan(plan, candidate: dict, *, run_id: str | None = None,
                            create_preview: bool = False) -> dict:
        identity = (plan or {}).get("candidates", {}).get(candidate["candidate_id"])
        if identity is None:
            from .spec import new_run_id
            identity = {"ai_id": candidate["ai_id"], "run_id": run_id or new_run_id(),
                        "created_at": _now()}
        if identity.get("ai_id") != candidate["ai_id"]:
            raise EvalSpecError(
                f"候选 {candidate['candidate_id']} 的 ai_id 与批次计划不一致；"
                "同一评测批次不能更换模型或工作流身份"
            )
        if run_id is not None and identity.get("run_id") != run_id:
            raise EvalSpecError(
                f"候选 {candidate['candidate_id']} 已准备的 run_id={identity.get('run_id')} "
                f"与指定 run_id={run_id} 不一致；续跑必须沿用原 run_id"
            )
        return identity

    def register_candidate(self, spec: dict, candidate: dict, *, run_id: str | None = None) -> dict:
        """在批次计划中登记候选的稳定身份；独立运行首次登记时生成 UTC 随机 run_id。"""
        def mutate(plan):
            entry = plan["candidates"].get(candidate["candidate_id"])
            if entry is None:
                from .spec import new_run_id
                plan["candidates"][candidate["candidate_id"]] = {
                    "ai_id": candidate["ai_id"], "run_id": run_id or new_run_id(),
                    "created_at": _now()}
            elif entry.get("ai_id") != candidate["ai_id"]:
                raise EvalSpecError(
                    f"候选 {candidate['candidate_id']} 的 ai_id 与批次计划不一致；"
                    "同一评测批次不能更换模型或工作流身份"
                )
            elif run_id is not None and entry.get("run_id") != run_id:
                raise EvalSpecError(
                    f"候选 {candidate['candidate_id']} 已保存 run_id={entry.get('run_id')}；"
                    f"与指定 run_id={run_id} 不一致，续跑必须沿用原 run_id"
                )
        plan = self.update_plan(mutate)
        return dict(plan["candidates"][candidate["candidate_id"]])

    # ---- 候选任务包 -------------------------------------------------------
    def prepare_candidate(self, spec: dict, candidate: dict, identity: dict, staged: dict) -> dict:
        directory = self.candidate_dir(candidate["candidate_id"])
        directory.mkdir(parents=True, exist_ok=True)
        prompt_staged = _resolve_staged_prompt(self.inputs)
        target_prompt = directory / ("prompt" + prompt_staged.suffix)
        _write_bytes(target_prompt, prompt_staged.read_bytes())
        metadata_template = self._metadata_template(spec, candidate)
        template_path = directory / "import-template.json"
        _write_bytes(template_path, _json(metadata_template).encode("utf-8"))
        readme = self._readme(spec, candidate, identity, prompt_staged)
        readme_path = directory / "README.md"
        _write_bytes(readme_path, readme.encode("utf-8"))
        return {"candidate_id": candidate["candidate_id"], "ai_id": candidate["ai_id"],
                "run_id": identity["run_id"], "directory": self.rel(directory),
                "prompt": self.rel(target_prompt), "import_template": self.rel(template_path),
                "readme": self.rel(readme_path)}

    def _metadata_template(self, spec: dict, candidate: dict) -> dict:
        default_iteration = {"teaching_comparison": "teaching-initial",
                             "solving_comparison": "solving-initial",
                             "pipeline_comparison": "pipeline-initial"}[spec["category"]]
        return {
            "_instructions": [
                "这是导入元数据模板，不是模型回答；把实际观察值填入后另存为 metadata.json。",
                "无法取得的字段写“未记录”或 null，不要估算成实测值。",
                "actual_model 只填写响应或提供方实际给出的模型名；无法确认时写“未记录”，不要把 DSH/Codex/Rethlas 当作模型名。",
                "status 取值：completed/failed/timeout/interrupted/unverified。",
                "round.type 取值：initial/verification_revision/prompt_iteration。",
                "完整回答另存为 response.md 或 response.txt，与 metadata.json 一起导入。",
            ],
            "status": "completed",
            "actual_model": "未记录",
            "provider": "未记录",
            "framework": candidate["framework"],
            "dsh_version": "未记录",
            "tool_permissions": "未记录",
            "generation": {},
            "timing": {"started_at": "未记录", "duration_seconds": None, "timed_out": False},
            "usage": {"input_tokens": None, "output_tokens": None, "cost": "未记录"},
            "evaluation": merge_design(spec.get("evaluation", {}), candidate.get("evaluation", {})),
            "external_reviews": [],
            "tool_events": [],
            "error": None,
            "notes": "",
            "round": {"iteration_id": default_iteration, "type": "initial",
                      "change_reason": "独立初稿；未参考其他模型回答"},
        }

    def _readme(self, spec: dict, candidate: dict, identity: dict, prompt_staged: Path) -> str:
        return "\n".join([
            f"# 评测任务包：{candidate['candidate_id']}",
            "",
            f"- 评测类别：{spec['category_label']}",
            f"- question_id：{spec['question']['question_id']}",
            f"- ai_id：{candidate['ai_id']}",
            f"- run_id：{identity['run_id']}",
            f"- 实际发送输入：`{self.rel(prompt_staged)}`（完整文本，须原样发送）",
            f"- 导入模板：`{self.rel(self.candidate_dir(candidate['candidate_id']) / 'import-template.json')}`",
            "",
            "## 手动步骤",
            "",
            "1. 把 prompt 文件的完整内容原样发送给指定模型或工作台；不要加入其他候选的回答或评价。",
            "2. 保存原始回答为 UTF-8 文本（不要整理、不要改写）。工具事件和原始响应如可取得，另存为 JSON。",
            "3. 复制 import-template.json，按实际观察填写 metadata.json；无法取得的写“未记录”。",
            "4. 运行导入命令（参数见 docs/模型与工作流评测接入.md）。",
            "5. 独立核验后另行导入核验结果；核验者身份不得与受测模型相同。",
            "",
            "本轮接入不自动调用模型；此任务包准备过程未调用任何模型。",
            "",
        ])

    # ---- 导入文件暂存 -----------------------------------------------------
    def stage_external_file(self, candidate_id: str, role: str, source, *, extension_hint=None) -> dict:
        source = Path(source)
        if not source.is_file():
            raise EvalSpecError(f"{role} 文件不存在：{source}")
        suffix = (extension_hint or source.suffix or ".txt").lower()
        if suffix not in (".md", ".txt", ".json", ".jsonl", ".tex", ".log", ".csv", ".pdf", ".png", ".jpg", ".jpeg", ".svg", ".html"):
            raise EvalSpecError(f"{role} 文件类型不受支持：{source.suffix}")
        raw = source.read_bytes()
        digest = _digest(raw)
        imports = self.candidate_dir(candidate_id) / "imports"
        imports.mkdir(parents=True, exist_ok=True)
        target = imports / f"{role}_{digest[:12]}{suffix}"
        _write_bytes(target, raw)
        index = imports / "index.jsonl"
        with index.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps({"time": _now(), "role": role, "origin": str(source),
                                     "sha256": digest, "staged": self.rel(target)},
                                    ensure_ascii=False) + "\n")
        return {"origin": str(source), "sha256": digest, "staged": self.rel(target),
                "absolute": target}

    def stage_text(self, candidate_id: str, role: str, text: str, *, suffix=".md") -> dict:
        directory = self.candidate_dir(candidate_id)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / (role + suffix)
        _write_bytes(target, text.encode("utf-8"))
        return {"sha256": _digest(target.read_bytes()), "staged": self.rel(target), "absolute": target}

    def stage_bytes(self, candidate_id: str, role: str, data: bytes, *, suffix=".jsonl") -> dict:
        """按原始字节保存可观察日志（不重新编码，便于核对哈希）。"""
        directory = self.candidate_dir(candidate_id) / "imports"
        directory.mkdir(parents=True, exist_ok=True)
        digest = _digest(data)
        target = directory / f"{role}_{digest[:12]}{suffix}"
        _write_bytes(target, data)
        return {"sha256": digest, "staged": self.rel(target), "absolute": target}

    def append_import_record(self, candidate_id: str, record: dict) -> dict:
        directory = self.candidate_dir(candidate_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "imports" / "import-records.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return {"path": self.rel(path)}


def _resolve_staged_prompt(inputs: Path) -> Path:
    matches = sorted(inputs.glob("prompt.*"))
    if not matches:
        raise EvalSpecError("输入目录缺少 prompt 文件；请重新准备评测批次")
    if len(matches) > 1:
        raise EvalSpecError(f"输入目录存在多个 prompt 文件：{[path.name for path in matches]}")
    return matches[0]
