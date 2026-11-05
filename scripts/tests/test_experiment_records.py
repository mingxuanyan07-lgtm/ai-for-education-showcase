"""Record-integrity tests with synthetic fixtures only; no models or HTTP."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import sys
import shutil
import unittest
from urllib.parse import unquote
import uuid
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiment_records import record_manifest, record_rethlas_run, refresh_records, package_records, main
from record_reading import render_reading


class ReadingHTML(HTMLParser):
    """Inspect rendered content without mistaking hidden TeX annotations for text."""

    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.visible = []
        self.hidden = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.elements.append((tag, attrs))
        if tag in {"head", "style", "script", "annotation", "annotation-xml"}:
            self.hidden.append(tag)

    def handle_endtag(self, tag):
        if self.hidden and self.hidden[-1] == tag:
            self.hidden.pop()

    def handle_data(self, data):
        if not self.hidden:
            self.visible.append(data)

    @property
    def text(self):
        return "".join(self.visible)


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.staging = Path(__file__).resolve().parents[2] / "work/test-records"
        self.staging.mkdir(parents=True, exist_ok=True)
        self.root = self.staging / ("education-records-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.root = self.root.resolve(strict=True)
        (self.root / "docs/templates").mkdir(parents=True)
        (self.root / "docs/数学教学AI测试记录.md").write_text("组长要求：提炼关键思路。", encoding="utf-8")
        (self.root / "docs/templates/数学教学AI测试记录_原始模板.md").write_text("原始模板", encoding="utf-8")

    def tearDown(self):
        self.assertTrue(self.root.is_relative_to(self.staging.resolve()))
        shutil.rmtree(self.root)

    def manifest(self, q="Q01", ai="codex-direct", run="R01", **extra):
        return {"question_id": q, "ai_id": ai, "run_id": run, "question": "证明 x²≥0。", "model": "测试夹具",
                "framework": "直接回答", "status": "completed", "requirements": "用中文解释", "user_prompt": "请解释这道题。",
                "iterations": [{"iteration_id": "initial", "prompt": "真实夹具输入", "answer": "实数的平方非负。"}], **extra}

    def invoke(self, command):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main([command, "--project-root", str(self.root)]), 0)
        return json.loads(output.getvalue())

    def test_two_questions_and_two_ais_have_independent_docs_and_utf8(self):
        results = [record_manifest(self.manifest(q=q, ai=ai), self.root)
                   for q in ("Q01", "Q02") for ai in ("codex-direct", "deepseek-direct")]
        self.assertEqual(len({item["record_path"] for item in results}), 4)
        for item in results:
            self.assertEqual(Path(item["record_dir"]), Path(item["record_path"]).parent)
            self.assertNotIn("zip_path", item)
            self.assertNotIn("total_zip_path", item)
            text = Path(item["record_path"]).read_text(encoding="utf-8")
            self.assertIn("请解释这道题。", text)
            self.assertIn("真实夹具输入", text)
            self.assertIn("实数的平方非负。", text)
            saved = json.loads(Path(item["record_dir"]).joinpath("record.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["question"], "证明 x²≥0。")
            self.assertNotIn(".zip", text)
        self.assertFalse((self.root / "exports").exists())
        index = (self.root / "docs/实验索引.md").read_text(encoding="utf-8")
        self.assertEqual(index.count("[打开]"), 4)

    def test_append_iterations_and_preserve_human_review_and_original_prompt(self):
        result = record_manifest(self.manifest(human_review="第一轮比较简洁"), self.root)
        review = Path(result["record_path"]).with_name("人工评价.md")
        review.write_text("我的手写感受：没有解释平方为什么非负。", encoding="utf-8")
        next_manifest = self.manifest(iterations=[{"iteration_id": "revision-1", "prompt": "请增加通用方法。",
                                                  "answer": "利用平方非负可证明二次不等式。", "change_reason": "增加迁移总结"}],
                                      human_review="第二轮感受")
        record_manifest(next_manifest, self.root)
        self.assertEqual(review.read_text(encoding="utf-8"), "我的手写感受：没有解释平方为什么非负。")
        text = Path(result["record_path"]).read_text(encoding="utf-8")
        self.assertIn("真实夹具输入", text)
        self.assertIn("请增加通用方法。", text)
        self.assertNotIn("增加迁移总结", text)
        self.assertIn("Prompt 迭代（第 2 轮）", text)
        record_manifest(self.manifest(iterations=[{"iteration_id": "initial", "verification": {"verdict": "wrong"}}],
                                      task_review="原始回答没有解释适用条件。"), self.root)
        text = Path(result["record_path"]).read_text(encoding="utf-8")
        self.assertIn("真实夹具输入", text)
        self.assertNotIn("原始回答没有解释适用条件。", text)
        self.assertIn("record.json", text)
        record = json.loads(Path(result["record_dir"]).joinpath("record.json").read_text(encoding="utf-8"))
        self.assertEqual(record["task_review"], "原始回答没有解释适用条件。")
        self.assertIn("人工评价补充_", text)
        with self.assertRaises(ValueError):
            record_manifest(self.manifest(iterations=[{"iteration_id": "initial", "prompt": "改写旧输入", "answer": "假回答"}]), self.root)

    def test_tags_survive_supplement_and_refresh_and_can_be_changed(self):
        result = record_manifest(self.manifest(tags=[" 概率论 ", "概率论", "联合分布", ""]), self.root)
        path = Path(result["record_dir"]) / "record.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(record["tags"], ["概率论", "联合分布"])
        originals = {key: record[key] for key in ("iterations", "user_prompt", "final_answer", "verification", "status", "tool_checks")}
        review = path.with_name("人工评价.md")
        review.write_text("用户的真实阅读感受。", encoding="utf-8")
        record_manifest({"question_id": "Q01", "ai_id": "codex-direct", "run_id": "R01"}, self.root)
        refresh_records(self.root)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["tags"], ["概率论", "联合分布"])
        self.assertIn("| 题目 | 标签 |", (self.root / "docs/实验索引.md").read_text(encoding="utf-8"))
        self.assertIn("概率论、联合分布", (self.root / "docs/实验索引.md").read_text(encoding="utf-8"))
        self.assertIn("标签：概率论、联合分布", path.with_name("测试记录.md").read_text(encoding="utf-8"))
        record_manifest({"question_id": "Q01", "ai_id": "codex-direct", "run_id": "R01", "tags": ["数学分析"]}, self.root)
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(record["tags"], ["数学分析"])
        for key, value in originals.items():
            self.assertEqual(record[key], value)
        self.assertEqual(review.read_text(encoding="utf-8"), "用户的真实阅读感受。")
        record_manifest({"question_id": "Q01", "ai_id": "codex-direct", "run_id": "R01", "tags": []}, self.root)
        self.assertIn("标签：未分类", path.with_name("测试记录.md").read_text(encoding="utf-8"))

    def test_untagged_legacy_record_stays_unchanged_and_table_escapes_tag_pipes(self):
        legacy = record_manifest(self.manifest(run="legacy-untagged"), self.root)
        path = Path(legacy["record_dir"]) / "record.json"
        before = path.read_bytes()
        record_manifest(self.manifest(run="tagged", tags=["概率|论"]), self.root)
        refresh_records(self.root)
        index = (self.root / "docs/实验索引.md").read_text(encoding="utf-8")
        self.assertIn("| Q01 | 未分类 |", index)
        self.assertIn("概率\\|论", index)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.root / "exports").exists())

    def test_invalid_tags_are_rejected_without_changing_records(self):
        invalid = ("概率论", None, {}, [1], ["概率\n论"])
        for tags in invalid:
            with self.subTest(tags=tags), self.assertRaises(ValueError):
                record_manifest(self.manifest(tags=tags), self.root)
        self.assertFalse((self.root / "docs/experiments").exists())
        result = record_manifest(self.manifest(tags=["代数"]), self.root)
        path = Path(result["record_dir"]) / "record.json"
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            record_manifest(self.manifest(tags="概率论"), self.root)
        self.assertEqual(path.read_bytes(), before)

    def test_wrong_timeout_never_become_correct(self):
        for run, status, verification in (("wrong", "failed", {"verdict": "wrong"}), ("timeout", "failed", {"timed_out": True})):
            result = record_manifest(self.manifest(run=run, status=status, verification=verification, error="真实失败夹具"), self.root)
            record = json.loads(Path(result["record_path"]).with_name("record.json").read_text(encoding="utf-8"))
            self.assertEqual(record["status"], "failed")
            self.assertNotEqual(record["verification"].get("verdict"), "correct")
            self.assertEqual(record["error"], "真实失败夹具")
            self.assertIn("运行状态：failed", Path(result["record_path"]).read_text(encoding="utf-8"))

    def test_explicit_package_has_ascii_names_expected_files_and_no_credentials(self):
        (self.root / ".venv").mkdir()
        (self.root / ".venv/auth.json").write_text("private", encoding="utf-8")
        (self.root / ".env").write_text("API_KEY=private", encoding="utf-8")
        record_manifest(self.manifest(), self.root)
        self.assertFalse((self.root / "exports").exists())
        result = self.invoke("package")
        self.assertEqual(len(result["zip_paths"]), 1)
        self.assertEqual(Path(result["total_zip_path"]).name, "ai_math_records.zip")
        for zip_path in [*result["zip_paths"], result["total_zip_path"]]:
            self.assertTrue(Path(zip_path).name.isascii())
            with zipfile.ZipFile(zip_path) as archive:
                names = archive.namelist()
                self.assertTrue(any(name.endswith("测试记录.md") for name in names))
                self.assertTrue(any(name.endswith("人工评价.md") for name in names))
                self.assertIn("docs/数学教学AI测试记录.md", names)
                self.assertIn("docs/templates/数学教学AI测试记录_原始模板.md", names)
                self.assertFalse(any(name.startswith("/") or ".." in Path(name).parts or "auth.json" in name or ".venv" in name for name in names))
        for path in (".env", ".venv/auth.json"):
            with self.assertRaises(ValueError):
                record_manifest(self.manifest(run="denied", evidence_files=[path]), self.root)

    def test_concurrent_index_keeps_every_run(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda i: record_manifest(self.manifest(run=f"R{i:02}"), self.root), range(8)))
        index = (self.root / "docs/实验索引.md").read_text(encoding="utf-8")
        self.assertEqual(index.count("[打开]"), 8)
        self.assertFalse((self.root / "exports").exists())
        package = package_records(self.root)
        with zipfile.ZipFile(package["total_zip_path"]) as archive:
            self.assertEqual(sum(name.endswith("测试记录.md") and "experiments/" in name for name in archive.namelist()), 8)

    def test_path_escape_and_unsafe_ids_are_rejected(self):
        for key, value in (("question_id", "../escape"), ("ai_id", "CON"), ("run_id", "LPT1.txt")):
            with self.assertRaises(ValueError):
                record_manifest(self.manifest(**{key: value}), self.root)
        outside = self.root.parent / "outside-source.md"
        outside.write_text("outside", encoding="utf-8")
        try:
            for value in (str(outside), "../outside-source.md"):
                with self.assertRaises(ValueError):
                    record_manifest(self.manifest(question_file=value), self.root)
            link = self.root / "linked.md"
            try:
                os.symlink(outside, link)
            except (OSError, NotImplementedError):
                return  # Windows without symbolic-link privilege still tested traversal.
            with self.assertRaises(ValueError):
                record_manifest(self.manifest(question_file="linked.md"), self.root)
        finally:
            outside.unlink(missing_ok=True)

    def test_file_input_snapshot_keeps_original_versions_and_package_refreshes_review(self):
        input_file = self.root / "original.md"
        input_file.write_text("原始回答", encoding="utf-8")
        manifest = self.manifest(evidence_files=["original.md"])
        result = record_manifest(manifest, self.root)
        input_file.write_text("后来修改", encoding="utf-8")
        record_manifest(manifest, self.root)
        record = json.loads(Path(result["record_path"]).with_name("record.json").read_text(encoding="utf-8"))
        self.assertEqual(len(record["artifacts"]), 2)
        snapshots = [Path(result["record_path"]).parent / item["snapshot"] for item in record["artifacts"]]
        self.assertEqual({path.read_text(encoding="utf-8") for path in snapshots}, {"原始回答", "后来修改"})
        review = Path(result["record_path"]).with_name("人工评价.md")
        review.write_text("更新后的人工感受", encoding="utf-8")
        package = self.invoke("package")
        with zipfile.ZipFile(package["zip_paths"][0]) as archive:
            name = next(name for name in archive.namelist() if name.endswith("人工评价.md"))
            self.assertEqual(archive.read(name).decode("utf-8"), "更新后的人工感受")

    def test_refresh_rebuilds_readable_records_without_changing_original_data_or_reviews(self):
        source = self.root / "original.md"
        source.write_text("原始数据", encoding="utf-8")
        result = record_manifest(self.manifest(evidence_files=["original.md"]), self.root)
        run_dir = Path(result["record_dir"])
        review = run_dir / "人工评价.md"
        review.write_text("本人的评价保持原样", encoding="utf-8")
        original_files = {path: path.read_bytes() for path in run_dir.rglob("*") if path.is_file() and path.name != "测试记录.md"}
        Path(result["record_path"]).write_text("旧页面：exports/旧包.zip", encoding="utf-8")
        (self.root / "docs/实验索引.md").unlink()
        refreshed = self.invoke("refresh")
        self.assertEqual(refreshed["record_count"], 1)
        self.assertEqual(Path(refreshed["index_path"]), self.root / "docs/实验索引.md")
        self.assertNotIn(".zip", Path(result["record_path"]).read_text(encoding="utf-8"))
        for path, content in original_files.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertFalse((self.root / "exports").exists())

    def test_default_record_and_refresh_leave_existing_manual_packages_unchanged(self):
        record_manifest(self.manifest(), self.root)
        package = package_records(self.root)
        paths = [Path(value) for value in [*package["zip_paths"], package["total_zip_path"]]]
        originals = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
        record_manifest(self.manifest(run="R02"), self.root)
        refresh_records(self.root)
        self.assertEqual(sorted((self.root / "exports").glob("*.zip")), sorted(paths))
        for path, (content, modified) in originals.items():
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(path.stat().st_mtime_ns, modified)

    def test_refresh_empty_project_creates_empty_index_without_zip_or_new_record(self):
        result = self.invoke("refresh")
        self.assertEqual(result["record_count"], 0)
        self.assertEqual(Path(result["index_path"]).read_text(encoding="utf-8").count("[打开]"), 0)
        self.assertFalse((self.root / "docs/experiments").exists())
        self.assertFalse((self.root / "exports").exists())

    def test_refresh_and_package_reject_record_identity_directory_mismatch(self):
        result = record_manifest(self.manifest(), self.root)
        path = Path(result["record_dir"]) / "record.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["run_id"] = "different-run"
        path.write_text(json.dumps(record), encoding="utf-8")
        for action in (refresh_records, package_records):
            with self.assertRaises(ValueError):
                action(self.root)
        self.assertFalse((self.root / "exports").exists())

    def test_rethlas_sidecar_resume_failure_and_proof_collection(self):
        gen = self.root / "Rethlas/agents/generation"
        data, logs = gen / "data", gen / "logs/example/iter"
        data.mkdir(parents=True)
        logs.mkdir(parents=True)
        (data / "example.md").write_text("证明 x²≥0。", encoding="utf-8")
        (data / "example.experiment.json").write_text(json.dumps({"question_id": "Q99", "ai_id": "rethlas-test", "run_id": "R99", "requirements": "初等方法", "record_kind": "installation_test", "tags": ["代数"], "test_set_id": "suite-v1"}), encoding="utf-8")
        (logs / "example_iter_0.run.json").write_text(json.dumps({"problem_file": "data/example.md", "iteration": 0, "model": "fixture", "timed_out": True}), encoding="utf-8")
        (logs / "example_iter_0.prompt.txt").write_text("入口指令引用data/example.md", encoding="utf-8")
        (logs / "example_iter_0.jsonl").write_text(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "尚未完成"}}), encoding="utf-8")
        memory = gen / "memory/example"
        memory.mkdir(parents=True)
        (memory / "verification_reports.jsonl").write_text(json.dumps({"record": {"statement": "证明 x²≥0。", "verdict": "wrong", "verification_report": {"summary": "夹具错误"}}}), encoding="utf-8")
        (self.root / "Rethlas/agents/verification/results").mkdir(parents=True)
        result = record_rethlas_run(self.root, "data/example.md", "failed", "timeout")
        self.assertNotIn("zip_path", result)
        self.assertFalse((self.root / "exports").exists())
        record = json.loads(Path(result["record_path"]).with_name("record.json").read_text(encoding="utf-8"))
        self.assertEqual((record["question_id"], record["ai_id"], record["run_id"]), ("Q99", "rethlas-test", "R99"))
        self.assertEqual(record["record_kind"], "installation_test")
        self.assertEqual(record["tags"], ["代数"])
        self.assertEqual(record["test_set_id"], "suite-v1")
        self.assertNotIn("| Q99 |", (self.root / "docs/实验索引.md").read_text(encoding="utf-8"))
        self.assertEqual(record["verification"][0]["verdict"], "wrong")
        proof = gen / "results/example/blueprint_verified.md"
        proof.parent.mkdir(parents=True)
        proof.write_text("最终完整证明文本", encoding="utf-8")
        updated = record_rethlas_run(self.root, "data/example.md", "completed")
        self.assertEqual(result["record_path"], updated["record_path"])
        saved = json.loads(Path(updated["record_path"]).with_name("record.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["final_answer"], "最终完整证明文本")
        text = Path(updated["record_path"]).read_text(encoding="utf-8")
        proof_snapshot = next(item["snapshot"] for item in saved["artifacts"]
                              if item["source"].endswith("/blueprint_verified.md"))
        self.assertTrue("最终完整证明文本" in text or proof_snapshot in text)

    def test_rethlas_display_uses_teaching_stage_and_keeps_wrapper_only_in_evidence(self):
        inputs = self.root / "inputs"
        inputs.mkdir()
        user_prompt = "请解答本题，使用 Rethlas 求解与独立核验。\n\n题目：证明 x²≥0。\n\n1. 用中文给出完整证明。\n2. 请将我的提问时的提示词也记录在测试记录里面。"
        wrapper = "Use AGENTS.md exactly to solve the math problem in data/example.md.\nIteration: 0. Do not use search tools."
        progress = "I’ll read the problem and any supported local references."
        (inputs / "example.prompt.txt").write_text(wrapper, encoding="utf-8")
        (inputs / "teaching.answer.txt").write_text("严谨教学证明：平方非负。", encoding="utf-8")
        (inputs / "teaching.prompt.txt").write_text("核验后中文教学整理要求。", encoding="utf-8")
        result = record_manifest(self.manifest(ai="rethlas-fixture", framework="Rethlas", name="未记录", user_prompt=user_prompt,
            final_answer="正式核验证明：x²≥0。", task_review="技术自动分析，不嵌入正文。",
            evidence_files=["inputs/example.prompt.txt", "inputs/teaching.answer.txt", "inputs/teaching.prompt.txt"],
            verification=[{"verdict": "correct", "verification_report": {"summary": "证明通过检查。", "critical_errors": [], "gaps": []}}],
            tool_checks=[{"status": "failed"}] * 3 + [{"status": "passed"}] * 2,
            iterations=[{"iteration_id": "rethlas_0_fixture", "prompt": wrapper, "answer": progress},
                        {"iteration_id": "teaching-1", "prompt": "教学实际要求", "answer": "严谨教学证明：平方非负。",
                         "settings": {"stage": "核验后的外层教学整理", "ai": "协作智能体", "model": "未记录"},
                         "verification": {"status": "passed", "summary": "教学数学复核通过。", "critical_errors": [], "gaps": []}}]), self.root)
        text = Path(result["record_path"]).read_text(encoding="utf-8")
        self.assertIn("#### 提示词（用户原文）\n\n" + user_prompt + "\n", text)
        self.assertEqual(text.count(user_prompt), 1)
        self.assertLess(text.index("#### 提示词（用户原文）"), text.index("#### 解答"))
        self.assertNotIn(wrapper, text)
        self.assertNotIn(progress, text)
        self.assertNotIn("技术自动分析，不嵌入正文。", text)
        self.assertNotIn("Prompt 迭代", text)
        self.assertNotIn("姓名：未记录", text)
        self.assertIn("核验后的中文教学整理", text)
        self.assertIn("严谨教学证明：平方非负。", text)
        self.assertIn("数学复核：correct", text)
        self.assertIn("数学复核：passed", text)
        for omitted in ("证明通过检查。", "教学数学复核通过。", "failed 3 次", "passed 2 次",
                        "求解器实际运行指令", "运行与归档详情", "## 原始材料", "### 回答要求",
                        "### 题面", "运行状态：completed", "教学实际要求", "正式核验证明：x²≥0。"):
            self.assertNotIn(omitted, text)
        self.assertIn("人工评价.md", text)
        self.assertIn("record.json", text)
        self.assertLess(len(text.encode("utf-8")), 8000)
        run_dir = Path(result["record_dir"])
        original = {path: path.read_bytes() for path in run_dir.rglob("*") if path.is_file() and path.name != "测试记录.md"}
        refresh_records(self.root)
        for path, data in original.items():
            self.assertEqual(path.read_bytes(), data)
        record = json.loads((run_dir / "record.json").read_text(encoding="utf-8"))
        self.assertEqual(record["iterations"][0]["prompt"], wrapper)
        self.assertEqual(record["iterations"][0]["answer"], progress)
        snapshot = next(item["snapshot"] for item in record["artifacts"] if item["source"] == "inputs/example.prompt.txt")
        self.assertEqual((run_dir / snapshot).read_text(encoding="utf-8"), wrapper)

    def test_missing_or_empty_user_prompt_is_not_inferred_from_question_or_iteration(self):
        for run, user_prompt in (("missing", "未记录"), ("empty", "")):
            manifest = self.manifest(ai="rethlas-fixture", framework="Rethlas", run=run, user_prompt=user_prompt,
                iterations=[{"iteration_id": "teaching-1", "prompt": "内部教学指令不是用户原文",
                             "answer": "真实教学稿。", "settings": {"stage": "post_verification_teaching"}}])
            if run == "missing":
                del manifest["user_prompt"]
            result = record_manifest(manifest, self.root)
            text = Path(result["record_path"]).read_text(encoding="utf-8")
            self.assertIn("#### 提示词（用户原文）\n\n未记录", text)
            self.assertNotIn("内部教学指令不是用户原文", text)
            self.assertIn("真实教学稿。", text)

    def test_rethlas_without_formal_answer_does_not_promote_progress_messages(self):
        result = record_manifest(self.manifest(ai="rethlas-fixture", framework="Rethlas", status="interrupted",
            iterations=[{"iteration_id": "rethlas_0_fixture", "prompt": "Use AGENTS.md exactly", "answer": "Working on the proof."}]), self.root)
        text = Path(result["record_path"]).read_text(encoding="utf-8")
        self.assertIn("运行状态：interrupted", text)
        self.assertIn("尚未保存正式解答", text)
        self.assertNotIn("Working on the proof.", text)
        self.assertNotIn("Use AGENTS.md exactly", text)
        self.assertIn("数学复核：未记录", text)

    def test_explicit_teaching_prompt_revision_appends_in_same_run_for_rethlas_and_direct_ai(self):
        inputs = self.root / "inputs"
        inputs.mkdir()
        first_user = "请证明 x²≥0，并用中文讲解。"
        revised_user = "延续刚才实验，按以下顺序组织回答：\n\n1. **关键思路。**\n2. **完整证明。**\n3. 按需概念补充。\n4. 只给出 1 项有依据的推广。\n"
        technical_prompt = "OUTER_TEACHING_API_INSTRUCTION: read saved verified proof and write the response."
        (inputs / "revision.user.txt").write_text(revised_user, encoding="utf-8")
        (inputs / "revision.prompt.txt").write_text(technical_prompt, encoding="utf-8")
        (inputs / "revision.answer.txt").write_text("新教学原文：关键思路、完整证明、概念及推广。", encoding="utf-8")
        for ai, framework in (("rethlas-fixture", "Rethlas"), ("codex-direct", "直接回答")):
            with self.subTest(ai=ai):
                first = record_manifest(self.manifest(ai=ai, framework=framework, user_prompt=first_user,
                    final_answer="正式证明保持原样。", iterations=[
                        {"iteration_id": "initial", "prompt": "原求解输入", "answer": "原求解回答"},
                        {"iteration_id": "teaching-1", "prompt": "第一稿内部教学指令", "answer": "第一稿教学原文。",
                         "settings": {"stage": "核验后的外层教学整理"},
                         "verification": {"verdict": "correct", "summary": "第一稿通过。"}}]), self.root)
                record_dir = Path(first["record_dir"])
                review = record_dir / "人工评价.md"
                review.write_text("本人填写的第一稿评价。", encoding="utf-8")
                revised = record_manifest(self.manifest(ai=ai, framework=framework, user_prompt=first_user,
                    iterations=[{"iteration_id": "teaching-2", "user_prompt_file": "inputs/revision.user.txt",
                        "prompt_file": "inputs/revision.prompt.txt", "answer_file": "inputs/revision.answer.txt",
                        "change_reason": "让读者先理解各步目的，精简重复并增加一项紧贴原题的推广。",
                        "settings": {"stage": "核验后的外层教学整理", "prompt_iteration": 2},
                        "verification": {"verdict": "correct", "summary": "第二稿与推广通过。"}}]), self.root)
                self.assertEqual(first["record_path"], revised["record_path"])
                text = Path(revised["record_path"]).read_text(encoding="utf-8")
                self.assertIn("#### 提示词（用户原文）\n\n" + first_user, text)
                if framework == "Rethlas":
                    self.assertNotIn("原求解输入", text)
                    self.assertNotIn("原求解回答", text)
                else:
                    self.assertIn("原求解输入", text)
                    self.assertIn("原求解回答", text)
                self.assertIn("第一稿教学原文。", text)
                self.assertIn("### Prompt 迭代（第 2 轮）", text)
                self.assertNotIn("修改目的：", text)
                self.assertNotIn(revised_user, text)
                self.assertIn("#### 提示词（实际输入）", text)
                self.assertIn(technical_prompt, text)
                self.assertIn("#### 解答", text)
                self.assertIn("新教学原文：关键思路、完整证明、概念及推广。", text)
                self.assertNotIn("第一稿内部教学指令", text)
                self.assertIn("数学复核：correct", text)
                self.assertNotIn("第一稿通过。", text)
                self.assertNotIn("第二稿与推广通过。", text)
                record = json.loads((record_dir / "record.json").read_text(encoding="utf-8"))
                self.assertEqual(record["user_prompt"], first_user)
                self.assertEqual(len(record["iterations"]), 3)
                iteration = record["iterations"][-1]
                self.assertEqual(iteration["user_prompt"], revised_user)
                self.assertEqual(iteration["prompt"], technical_prompt)
                snapshot = next(item["snapshot"] for item in record["artifacts"] if item["source"] == "inputs/revision.user.txt")
                self.assertEqual((record_dir / snapshot).read_text(encoding="utf-8"), revised_user)
                self.assertEqual(review.read_text(encoding="utf-8"), "本人填写的第一稿评价。")
                self.assertFalse((self.root / "exports").exists())
        index = (self.root / "docs/实验索引.md").read_text(encoding="utf-8")
        self.assertEqual(index.count("[打开]"), 2)

    def test_requested_actual_teaching_prompt_is_visible_and_distinct_from_user_request(self):
        actual_prompt = "请根据已核验证明生成教学稿。\n\n完整题面和旧证明作为资料。\n保持两个方向的依据。"
        user_request = "请把提示词和智能体的新回答写进相应测试记录。"
        result = record_manifest(self.manifest(ai="rethlas-fixture", framework="Rethlas", user_prompt="首轮原始提问。",
            iterations=[{"iteration_id": "teaching-2", "user_prompt": user_request, "prompt": actual_prompt,
                         "answer": "本轮真实教学回答。", "settings": {"stage": "post_verification_teaching",
                         "prompt_iteration": 2, "show_actual_prompt": True}}]), self.root)
        text = Path(result["record_path"]).read_text(encoding="utf-8")
        self.assertNotIn(user_request, text)
        self.assertIn("#### 提示词（实际输入）", text)
        self.assertIn(actual_prompt, text)
        self.assertIn("本轮真实教学回答。", text)
        self.assertLess(text.index(actual_prompt), text.index("#### 解答"))
        record = json.loads(Path(result["record_path"]).with_name("record.json").read_text(encoding="utf-8"))
        self.assertEqual(record["iterations"][0]["prompt"], actual_prompt)
        self.assertEqual(record["iterations"][0]["user_prompt"], user_request)

    def test_first_teaching_input_links_to_saved_snapshot_without_repeating_source_text(self):
        inputs = self.root / "inputs"
        inputs.mkdir()
        for source_key in ("referenced_task_prompt_file", "prompt_file"):
            with self.subTest(source_key=source_key):
                source = inputs / (source_key + ".txt")
                original_prompt = "真实首轮教学输入：依据核验证明解释每一步，不增加题设。"
                source.write_text(original_prompt, encoding="utf-8")
                item = {"iteration_id": "teaching-1", "answer": "首轮真实解答全文。",
                        "settings": {"stage": "post_verification_teaching"}}
                if source_key == "referenced_task_prompt_file":
                    item["prompt"] = "只负责读取已保存任务的调度语。"
                    item[source_key] = str(source)
                else:
                    item[source_key] = source.relative_to(self.root).as_posix()
                result = record_manifest(self.manifest(ai="rethlas-fixture", framework="Rethlas", run=source_key,
                    user_prompt="用户首轮完整请求。", iterations=[item],
                    evidence_files=[str(source)]), self.root)
                run_dir = Path(result["record_dir"])
                record = json.loads((run_dir / "record.json").read_text(encoding="utf-8"))
                snapshot = next(artifact["snapshot"] for artifact in record["artifacts"]
                                if artifact["source"] == source.relative_to(self.root).as_posix())
                text = Path(result["record_path"]).read_text(encoding="utf-8")
                self.assertIn("#### 提示词（用户原文）\n\n用户首轮完整请求。", text)
                self.assertIn(snapshot, text)
                self.assertNotIn(original_prompt, text)
                self.assertNotIn("只负责读取已保存任务的调度语。", text)
                self.assertIn("#### 解答\n\n首轮真实解答全文。", text)
                review = run_dir / "人工评价.md"
                review.write_text("用户的评价不能被精简流程改写。", encoding="utf-8")
                original_files = {path: path.read_bytes() for path in run_dir.rglob("*")
                                  if path.is_file() and path.name != "测试记录.md"}
                source.write_text("可变源文件的新内容，不是历史实际输入。", encoding="utf-8")
                refresh_records(self.root)
                refreshed = Path(result["record_path"]).read_text(encoding="utf-8")
                self.assertIn(snapshot, refreshed)
                self.assertNotIn("可变源文件的新内容", refreshed)
                self.assertEqual((run_dir / snapshot).read_text(encoding="utf-8"), original_prompt)
                for path, content in original_files.items():
                    self.assertEqual(path.read_bytes(), content)

    def test_later_teaching_prompt_extracts_explicit_requirements_and_preserves_complete_input(self):
        variants = (
            ("marked", "【本轮教学要求开始】\n", "\n【本轮教学要求结束】"),
            ("prose", "请按以下要求完成一份连贯、可供初学者核对的中文教学正文：\n\n", ""),
            ("four-parts", "请按以下四部分输出；其余程序信息和评测状态不要夹入教学正文。\n\n", ""),
        )
        for run, start, end in variants:
            with self.subTest(run=run):
                requirements = "1. 先解释关键思路，再给出完整证明。\n2. 保留必要条件，不以旧稿的判定代替本轮复核。"
                actual_prompt = ("本轮助手调度信息不属于教学要求。\n"
                    "<证明开始>\n重复原证明全文，不应出现在精简正文。\n<证明结束>\n"
                    "<上一轮教学稿开始>\n重复旧教学稿，不应出现在精简正文。\n<上一轮教学稿结束>\n"
                    + start + requirements + end + "\n\n执行与保存要求（仅在未来确实执行本提示词时适用）："
                    "内部归档流程与保存路径，不应出现在教学要求摘录。")
                source = self.root / (run + ".prompt.txt")
                source.write_text(actual_prompt, encoding="utf-8")
                result = record_manifest(self.manifest(ai="rethlas-fixture", framework="Rethlas", run=run,
                    iterations=[{"iteration_id": "teaching-2", "prompt_file": source.name,
                        "user_prompt": "请实际执行已设计提示词并更新记录。", "answer": "对应的完整新教学回答。",
                        "change_reason": "无需在正文重复显示的修改目的。",
                        "settings": {"stage": "post_verification_teaching", "prompt_iteration": 2},
                        "verification": {"verdict": "correct", "verification_report": {"summary": "完整核验的长摘要。"}}}]), self.root)
                text = Path(result["record_path"]).read_text(encoding="utf-8")
                self.assertIn("### Prompt 迭代（第 2 轮）", text)
                self.assertIn("#### 提示词（教学要求摘录）", text)
                self.assertIn(requirements, text)
                self.assertIn("#### 解答\n\n对应的完整新教学回答。", text)
                for detail in ("本轮助手调度信息", "重复原证明全文", "重复旧教学稿", "内部归档流程与保存路径",
                               "请实际执行已设计提示词并更新记录。", "无需在正文重复显示的修改目的。", "完整核验的长摘要。"):
                    self.assertNotIn(detail, text)
                record_path = Path(result["record_dir"]) / "record.json"
                saved_bytes = record_path.read_bytes()
                saved = json.loads(saved_bytes)
                self.assertEqual(saved["iterations"][0]["prompt"], actual_prompt)
                self.assertEqual(saved["iterations"][0]["user_prompt"], "请实际执行已设计提示词并更新记录。")
                snapshot = next(item["snapshot"] for item in saved["artifacts"] if item["source"] == source.name)
                self.assertIn(snapshot, text)
                self.assertEqual((record_path.parent / snapshot).read_text(encoding="utf-8"), actual_prompt)
                refresh_records(self.root)
                self.assertEqual(record_path.read_bytes(), saved_bytes)

    def test_direct_ai_keeps_real_prompts_answers_and_distinct_original_request(self):
        original_request = "用户要求：解释这道不等式。"
        actual_prompt = "实际直接模型输入：请证明实数平方非负。"
        answer = "直接模型原始回答全文：若 x 为实数，则 x²≥0。"
        result = record_manifest(self.manifest(user_prompt=original_request,
            iterations=[{"iteration_id": "initial", "prompt": actual_prompt, "answer": answer}]), self.root)
        text = Path(result["record_path"]).read_text(encoding="utf-8")
        self.assertIn("### 用户原始提示词（完整提问）\n\n" + original_request, text)
        self.assertIn(actual_prompt, text)
        self.assertIn(answer, text)
        self.assertLess(text.index(actual_prompt), text.index(answer))
        self.assertNotIn("### 回答要求", text)
        self.assertNotIn("运行与归档详情", text)

    def test_teaching_id_alone_and_invalid_prompt_iteration_do_not_claim_user_revision(self):
        for number, value in enumerate((None, True, "2", 1, 0, -1), 1):
            settings = {"stage": "核验后的外层教学整理"}
            if value is not None:
                settings["prompt_iteration"] = value
            result = record_manifest(self.manifest(run=f"unflagged-{number}", iterations=[{
                "iteration_id": "teaching-2", "prompt": "内部教学指令", "answer": "核验后教学稿。",
                "user_prompt": "这一文本不自动触发用户修订轮次。", "settings": settings}]), self.root)
            text = Path(result["record_path"]).read_text(encoding="utf-8")
            self.assertNotIn("Prompt 迭代", text)
            self.assertIn("核验后的中文教学整理", text)
            self.assertNotIn("内部教学指令", text)

    def test_revision_user_prompt_is_append_only_and_original_top_level_request_cannot_change(self):
        original = "延续测试，先解释关键思路。"
        source = self.root / "revision.user.txt"
        source.write_text(original, encoding="utf-8")
        manifest = self.manifest(iterations=[{"iteration_id": "teaching-2", "user_prompt_file": "revision.user.txt",
            "prompt": "实际教学 API 输入", "answer": "真实教学输出",
            "settings": {"stage": "核验后的外层教学整理", "prompt_iteration": 2}}])
        result = record_manifest(manifest, self.root)
        path = Path(result["record_dir"]) / "record.json"
        saved = path.read_bytes()
        source.write_text("悄悄改写该轮原始用户提示词。", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "original user_prompt"):
            record_manifest(manifest, self.root)
        self.assertEqual(path.read_bytes(), saved)
        with self.assertRaisesRegex(ValueError, "original user_prompt"):
            record_manifest(self.manifest(user_prompt="替换首轮用户提问", iterations=[]), self.root)
        self.assertEqual(path.read_bytes(), saved)
        source.write_text(original, encoding="utf-8")
        record_manifest(manifest, self.root)  # Reimporting the identical text is allowed.
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(record["iterations"][0]["user_prompt"], original)

    def test_iteration_user_prompt_must_be_text_and_missing_original_is_not_inferred(self):
        for invalid in ([], {}, 2, None):
            with self.assertRaisesRegex(ValueError, "iteration user_prompt must be text"):
                record_manifest(self.manifest(iterations=[{"iteration_id": "teaching-2", "user_prompt": invalid}]), self.root)
        self.assertFalse((self.root / "docs/experiments").exists())
        result = record_manifest(self.manifest(iterations=[{"iteration_id": "teaching-2",
            "prompt": "实际 API 技术指令不得冒充本轮用户提示词", "answer": "新教学稿。",
            "settings": {"stage": "核验后的外层教学整理", "prompt_iteration": 2}}]), self.root)
        text = Path(result["record_path"]).read_text(encoding="utf-8")
        self.assertNotIn("#### 本轮用户提示词（完整提问）", text)
        self.assertIn("#### 提示词（实际输入）", text)
        self.assertIn("实际 API 技术指令不得冒充本轮用户提示词", text)
        saved = json.loads(Path(result["record_path"]).with_name("record.json").read_text(encoding="utf-8"))
        self.assertNotIn("user_prompt", saved["iterations"][0])

    def test_verification_preserves_wrong_then_correct_and_missing_defect_fields(self):
        result = record_manifest(self.manifest(status="completed",
            verification=[{"verdict": "wrong", "verification_report": {"summary": "第一轮存在错误。", "critical_errors": ["关键等式错误"], "gaps": ["缺少极限条件"]}},
                          {"verdict": "correct", "verification_report": {"summary": "后轮已修复。", "critical_errors": [], "gaps": []}}]), self.root)
        text = Path(result["record_path"]).read_text(encoding="utf-8")
        self.assertIn("数学复核：wrong", text)
        self.assertIn("关键错误 1 项", text)
        self.assertIn("缺口 1 项", text)
        self.assertIn("correct", text)
        for detail in ("关键等式错误", "缺少极限条件", "第一轮存在错误。", "后轮已修复。"):
            self.assertNotIn(detail, text)
        saved = json.loads(Path(result["record_path"]).with_name("record.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["verification"][0]["verification_report"]["critical_errors"], ["关键等式错误"])
        result = record_manifest(self.manifest(run="missing", verification={"verdict": "correct"}), self.root)
        text = Path(result["record_path"]).read_text(encoding="utf-8")
        self.assertIn("数学复核：correct", text)
        self.assertNotIn("关键错误 0 项", text)
        self.assertNotIn("缺口 0 项", text)
        result = record_manifest(self.manifest(run="raw", verification="未完成：核验服务断开。"), self.root)
        text = Path(result["record_path"]).read_text(encoding="utf-8")
        self.assertIn("数学复核：", text)
        self.assertIn("未完成", text)
        self.assertNotIn("未完成：核验服务断开。", text)
        saved = json.loads(Path(result["record_path"]).with_name("record.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["verification"], "未完成：核验服务断开。")
        result = record_manifest(self.manifest(run="raw-inner", verification={"verdict": "wrong", "verification_report": "文本核验：缺少证明。"}), self.root)
        text = Path(result["record_path"]).read_text(encoding="utf-8")
        self.assertIn("数学复核：wrong", text)
        self.assertNotIn("文本核验：缺少证明。", text)
        result = record_manifest(self.manifest(run="inconsistent", verification={"verdict": "correct", "critical_errors": ["仍有错误"]}), self.root)
        text = Path(result["record_path"]).read_text(encoding="utf-8")
        self.assertIn("数学复核：correct", text)
        self.assertIn("关键错误 1 项", text)
        self.assertNotIn("仍有错误", text)

    def test_failed_inner_iteration_remains_visible_after_later_completed_run(self):
        result = record_manifest(self.manifest(ai="rethlas-fixture", framework="Rethlas", status="completed",
            iterations=[{"iteration_id": "rethlas_0_fixture", "prompt": "wrapper", "answer": "progress",
                         "settings": {"iteration": 0, "timed_out": True, "exit_code": 1, "error": "第一轮超时"}},
                        {"iteration_id": "rethlas_1_fixture", "prompt": "wrapper2", "answer": "progress2",
                         "settings": {"iteration": 1, "timed_out": False, "exit_code": 0}}]), self.root)
        text = Path(result["record_path"]).read_text(encoding="utf-8")
        self.assertNotIn("运行状态：completed", text)
        self.assertIn("求解轮次 0：第一轮超时", text)

    def test_index_excludes_only_explicit_installation_and_exact_legacy_smoke_source(self):
        record_manifest(self.manifest(run="explicit", record_kind="installation_test"), self.root)
        legacy = record_manifest(self.manifest(run="legacy", framework="Rethlas",
            iterations=[{"iteration_id": "rethlas_0", "settings": {"problem_file": "data/windows_smoke.md"}}]), self.root)
        gen = self.root / "Rethlas/agents/generation/data"
        gen.mkdir(parents=True)
        (gen / "windows_smoke.md").write_text("安装检查夹具", encoding="utf-8")
        record_manifest(self.manifest(run="source", evidence_files=[str(gen / "windows_smoke.md")]), self.root)
        for run, status in (("ordinary-reused", "reused"), ("ordinary-imported", "imported")):
            record_manifest(self.manifest(run=run, status=status), self.root)
        record_manifest(self.manifest(run="near-name", iterations=[{"iteration_id": "rethlas_0", "settings": {"problem_file": "data/my_windows_smoke.md"}}]), self.root)
        record_manifest(self.manifest(run="explicit-math", record_kind="math_experiment",
            iterations=[{"iteration_id": "rethlas_0", "settings": {"problem_file": "data/windows_smoke.md"}}]), self.root)
        path = Path(legacy["record_dir"]) / "record.json"
        original = path.read_bytes()
        result = refresh_records(self.root)
        self.assertEqual(result["record_count"], 7)
        index = Path(result["index_path"]).read_text(encoding="utf-8")
        self.assertEqual(index.count("[打开]"), 4)
        for excluded in ("explicit |", "legacy |", "source |"):
            self.assertNotIn(excluded, index)
        for included in ("ordinary-reused", "ordinary-imported", "near-name", "explicit-math"):
            self.assertIn(included, index)
        self.assertEqual(path.read_bytes(), original)
        self.assertNotIn("record_kind", json.loads(original))
        self.assertTrue(Path(legacy["record_path"]).exists())

    def test_explicit_index_removal_targets_one_run_and_survives_refresh(self):
        old = record_manifest(self.manifest(run="old", record_kind="math_experiment"), self.root)
        newer = record_manifest(self.manifest(run="newer"), self.root)
        other_ai = record_manifest(self.manifest(ai="other-ai", run="old"), self.root)
        other_question = record_manifest(self.manifest(q="Q02", run="old"), self.root)
        path = Path(old["record_dir"]) / "record.json"
        before = json.loads(path.read_text(encoding="utf-8"))
        review = path.with_name("人工评价.md").read_bytes()
        selection = {"question_id": "Q01", "ai_id": "codex-direct", "run_id": "old", "iterations": []}
        record_manifest({**selection, "index_visible": False,
                         "index_visibility_reason": "用户只保留同题较新日常入口。"}, self.root)
        # Later evidence-only updates must not bring the entry back.
        record_manifest(selection, self.root)
        refresh_records(self.root)
        index = (self.root / "docs/实验索引.md").read_text(encoding="utf-8")
        old_link = Path(old["record_path"]).relative_to(self.root / "docs").as_posix()
        self.assertNotIn(old_link, index)
        self.assertEqual(index.count("[打开]"), 3)
        for item in (newer, other_ai, other_question):
            self.assertIn(Path(item["record_path"]).relative_to(self.root / "docs").as_posix(), index)
        after = json.loads(path.read_text(encoding="utf-8"))
        for key in ("question", "user_prompt", "iterations", "final_answer", "verification", "artifacts", "status", "record_kind"):
            self.assertEqual(before[key], after[key])
        self.assertEqual(path.with_name("人工评价.md").read_bytes(), review)
        self.assertIn("已从日常索引移除", Path(old["record_path"]).read_text(encoding="utf-8"))
        self.assertFalse((self.root / "exports").exists())
        record_manifest({**selection, "index_visible": True}, self.root)
        self.assertIn(old_link, (self.root / "docs/实验索引.md").read_text(encoding="utf-8"))

    def test_invalid_index_visibility_is_rejected_before_mutation(self):
        for value in (None, "false", 0, 1, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                record_manifest(self.manifest(index_visible=value), self.root)
        self.assertFalse((self.root / "docs/experiments").exists())
        result = record_manifest(self.manifest(), self.root)
        path = Path(result["record_dir"]) / "record.json"
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            record_manifest(self.manifest(index_visibility_reason=123), self.root)
        self.assertEqual(path.read_bytes(), before)

    def test_invalid_record_kind_is_rejected_before_record_creation(self):
        for kind in ("unknown", "", [], None):
            with self.assertRaises(ValueError):
                record_manifest(self.manifest(record_kind=kind), self.root)
        self.assertFalse((self.root / "docs/experiments").exists())
        result = record_manifest(self.manifest(), self.root)
        path = Path(result["record_dir"]) / "record.json"
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            record_manifest(self.manifest(record_kind="unknown"), self.root)
        self.assertEqual(path.read_bytes(), before)

    def test_reading_page_typesets_markdown_and_math_without_visible_source_markup(self):
        source = r"""# 排版测试

**关键想法**：任取 \(q\in\mathbb Q\)，比较 \(X<q<Y\)。

\[
\{X<Y\}=\bigcup_{q\in\mathbb Q}\{X\le q<Y\}.
\]

\[
\begin{aligned}
P(A\cap B)&=P(B),\\
P(A\setminus B)&=0.
\end{aligned}
\]

普通比较：a < b，c > d。
"""
        page = render_reading(source, "测试阅读版")
        parsed = ReadingHTML(page)
        tags = [tag for tag, _ in parsed.elements]
        self.assertIn("h1", tags)
        self.assertIn("strong", tags)
        self.assertGreaterEqual(tags.count("math"), 4)
        self.assertIn("mtable", tags)
        self.assertIn("关键想法", parsed.text)
        self.assertIn("普通比较：a < b，c > d。", parsed.text)
        for symbol in ("∈", "⋃", "≤", "∩"):
            self.assertIn(symbol, parsed.text)
        for raw_markup in ("# 排版测试", "**关键想法**", r"\(", r"\[",
                           r"\mathbb", r"\bigcup", r"\begin{aligned}"):
            self.assertNotIn(raw_markup, parsed.text)
        self.assertFalse(any(tag == "script" for tag, _ in parsed.elements))
        self.assertFalse(any(tag == "link" and attrs.get("href", "").startswith(("http:", "https:"))
                             for tag, attrs in parsed.elements))

    def test_reading_page_keeps_raw_html_and_unsafe_links_inert(self):
        source = """# 不可信原文

<script>alert('raw')</script>
<img src=x onerror="alert('image')">

[危险链接](javascript:alert%281%29)

[正常链接](https://example.com/reference)
"""
        page = render_reading(source, '<script>alert("title")</script>')
        parsed = ReadingHTML(page)
        self.assertIn("不可信原文", parsed.text)
        self.assertTrue(any(tag == "a" and attrs.get("href") == "https://example.com/reference"
                            for tag, attrs in parsed.elements))
        self.assertFalse(any(tag == "script" for tag, _ in parsed.elements))
        self.assertFalse(any(name.lower().startswith("on") for _, attrs in parsed.elements for name in attrs))
        self.assertFalse(any(attrs.get("href", "").strip().lower().startswith("javascript:")
                             for _, attrs in parsed.elements))

    def test_reading_paths_refresh_and_package_preserve_original_prompts_answers_and_reviews(self):
        source = self.root / "original-answer.md"
        answer = r"""# 证明草稿

**关键想法**：固定 \(q\in\mathbb Q\)。

\[
P(X\le q<Y)=0.
\]
"""
        prompt = r"请保留原文符号：**要求**，\(X\le Y\)，并生成草稿。"
        source.write_text(answer, encoding="utf-8")
        result = record_manifest(self.manifest(
            verification={"status": "not_run"},
            iterations=[{"iteration_id": "initial", "prompt": prompt, "answer_file": "original-answer.md"}],
            evidence_files=["original-answer.md"]), self.root)
        run_dir = Path(result["record_dir"])
        reading = Path(result["reading_path"])
        index_reading = Path(result["index_reading_path"])
        self.assertEqual(reading, run_dir / "测试记录.html")
        self.assertEqual(index_reading, self.root / "docs/实验索引.html")
        self.assertTrue(reading.is_file())
        self.assertTrue(index_reading.is_file())
        parsed = ReadingHTML(reading.read_text(encoding="utf-8"))
        self.assertIn("尚未独立核验", parsed.text)
        self.assertNotIn("not_run", parsed.text)
        index = Path(result["index_path"]).read_text(encoding="utf-8")
        self.assertIn(reading.relative_to(self.root / "docs").as_posix(), index)
        self.assertIn(Path(result["record_path"]).relative_to(self.root / "docs").as_posix(), index)
        html_index = ReadingHTML(index_reading.read_text(encoding="utf-8"))
        self.assertTrue(any(tag == "a" and unquote(attrs.get("href", "")).endswith("测试记录.html")
                            for tag, attrs in html_index.elements))

        review = run_dir / "人工评价.md"
        review.write_text("用户真实评价：这里的公式容易阅读。", encoding="utf-8")
        originals = {path: path.read_bytes() for path in run_dir.rglob("*")
                     if path.is_file() and path.name != "测试记录.html"}
        record = json.loads((run_dir / "record.json").read_text(encoding="utf-8"))
        self.assertEqual(record["iterations"][0]["prompt"], prompt)
        self.assertEqual(record["iterations"][0]["answer"], answer)
        reading.write_text("损坏的阅读版", encoding="utf-8")
        index_reading.unlink()
        refreshed = refresh_records(self.root)
        self.assertEqual(Path(refreshed["index_reading_path"]), index_reading)
        self.assertTrue(index_reading.is_file())
        self.assertIn("math", [tag for tag, _ in ReadingHTML(reading.read_text(encoding="utf-8")).elements])
        for path, content in originals.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(source.read_text(encoding="utf-8"), answer)
        self.assertFalse((self.root / "exports").exists())

        package = package_records(self.root)
        for zip_path in [*package["zip_paths"], package["total_zip_path"]]:
            with zipfile.ZipFile(zip_path) as archive:
                names = archive.namelist()
                self.assertIn(reading.relative_to(self.root).as_posix(), names)
                self.assertEqual(archive.read(review.relative_to(self.root).as_posix()), review.read_bytes())
        with zipfile.ZipFile(package["total_zip_path"]) as archive:
            self.assertIn("docs/实验索引.html", archive.namelist())


if __name__ == "__main__":
    unittest.main()
