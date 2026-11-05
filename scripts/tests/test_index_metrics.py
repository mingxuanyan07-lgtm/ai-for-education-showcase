"""Benchmark denominator, provenance and offline dashboard integrity tests; no models."""
import base64
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiment_records import record_manifest, refresh_records, _identifier, _record_metrics
from index_dashboard import sample, validate_test_sets, render_dashboard, accuracy_text
from record_reading import render_reading


class IndexMetricsTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(__file__).resolve().parents[2] / "work/test-index-metrics"
        self.root = self.base / uuid.uuid4().hex
        self.root.mkdir(parents=True)

    def tearDown(self):
        self.assertTrue(self.root.resolve().is_relative_to(self.base.resolve()))
        shutil.rmtree(self.root)

    def manifest(self, **extra):
        return {"question_id": "Q01", "ai_id": "model-a", "run_id": "run1", "model": "actual-model",
                "framework": "direct", "status": "completed", "question": "测试夹具，不解题",
                "iterations": [{"iteration_id": "first", "answer": "原始回答", "verification": {"verdict": "wrong"}},
                               {"iteration_id": "last", "answer": "新回答", "verification": {"verdict": "correct"}}], **extra}

    def test_final_verdict_is_answer_scoped_not_historical_union(self):
        r = self.manifest(verification=[{"verdict": "correct"}])
        row = sample(r, {}, {})
        self.assertEqual((row["first_verdict"], row["final_verdict"]), ("wrong", "correct"))
        r["iterations"].append({"iteration_id": "teaching", "answer": "尚未核验的教学稿"})
        self.assertEqual(sample(r, {}, {})["final_verdict"], "未记录")
        r["iterations"][-1]["verification"] = {"status": "passed"}
        self.assertEqual(sample(r, {}, {})["final_verdict"], "未记录")
        r["iterations"][-1]["verification"] = [{"verdict": "correct"}, {"verdict": "not_run"}]
        self.assertEqual(sample(r, {}, {})["final_verdict"], "not_run")

    def test_no_answer_and_missing_usage_are_not_success_or_zero(self):
        row = sample(self.manifest(iterations=[], status="timeout"), {}, {})
        self.assertEqual(row["first_verdict"], "未记录")
        self.assertIsNone(row["generation_seconds"])
        self.assertIsNone(row["tokens"])

    def test_partial_metering_is_visible_without_relabeling_system_totals(self):
        r = self.manifest(model="deepseek-flash", evaluation={
            "roles": [{"role_id": "solve", "actual_model": "deepseek-flash", "configured_model": "deepseek-flash", "provider": "deepseek-account"},
                      {"role_id": "codex-review", "role": "final_review", "actual_model": "未记录"}],
            "usage": {"total_tokens": None, "wall_seconds": None,
                      "observed_deepseek_roles": {"total_tokens": 18963, "input_tokens_dsh_field": 1000, "output_tokens": 17963,
                                                  "host_role_invocations": 4},
                      "observed_host_staged_wall_seconds": 99.64}})
        row = sample(r, {}, {})
        self.assertEqual(row["observed_tokens"], 18963)
        self.assertEqual(row["observed_seconds"], 99.64)
        self.assertIsNone(row["system_tokens"])
        self.assertIsNone(row["wall_seconds"])
        self.assertIsNone(row["tokens"])
        self.assertIn("deepseek-flash", row["model"])
        self.assertEqual(row["observed_tokens_scope_id"], "deepseek_roles")

    def test_requested_model_is_shown_as_configuration_not_actual_response(self):
        r = self.manifest(model="未记录", evaluation={
            "roles": [{"role_id": "native", "actual_model": "未记录", "configured_model": "deepseek-flash", "provider": "deepseek"}],
            "usage": {"total_tokens": 40737, "wall_seconds": 19.56, "estimated": False}})
        row = sample(r, {}, {})
        self.assertIn("配置", row["model"])
        self.assertIn("deepseek-flash", row["model"])
        self.assertEqual(row["actual_model"], "未记录")
        self.assertEqual(row["configured_model"], "deepseek-flash")
        self.assertEqual(row["observed_tokens"], 40737)
        self.assertEqual(row["observed_seconds"], 19.56)

    def test_accuracy_uses_scored_denominator_and_reports_pending(self):
        rows = [{"grade_verdict": x} for x in ("correct", "wrong", "partial", "not_graded")]
        self.assertEqual(accuracy_text(rows, "grade_verdict"), "33.3% (1/3)；已判 3/4，待判 1")
        self.assertEqual(accuracy_text([{}], "grade_verdict"), "待判分（已判 0/1）")
        self.assertEqual(accuracy_text([], "grade_verdict"), "待判分（已判 0/0）")
        rows = [{"grade_verdict": "wrong"}] * 399 + [{"grade_verdict": "correct"}]
        self.assertTrue(accuracy_text(rows, "grade_verdict").startswith("0.3%"))

    def test_reference_grade_is_answer_scoped_and_never_inferred_from_verification(self):
        r = self.manifest()
        self.assertEqual(sample(r, {}, {})["grade_verdict"], "not_graded")
        grade = {"verdict": "wrong", "method": "human_rubric", "reference": "实际评分规则", "evaluator": "实际人工评判者"}
        r["iterations"][-1]["grading"] = grade
        row = sample(r, {}, {})
        self.assertEqual(row["final_verdict"], "correct")
        self.assertEqual(row["grade_verdict"], "wrong")
        result = record_manifest(r, self.root)
        path = Path(result["record_dir"]) / "record.json"
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["iterations"][-1]["grading"], grade)
        record_manifest({"question_id": "Q01", "ai_id": "model-a", "run_id": "run1",
                         "iterations": [{"iteration_id": "new-answer", "answer": "未判分的新稿"}]}, self.root)
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(sample(saved, {}, {})["grade_verdict"], "not_graded")
        self.assertEqual(saved["iterations"][1]["grading"], grade)

    def test_invalid_grades_are_rejected_without_new_records(self):
        for grade in ({"verdict": "correct"}, {"verdict": []}, {"verdict": "wrong", "method": "model"},
                      {"verdict": "partial", "method": "human_rubric", "reference": "rubric", "evaluator": ""}):
            with self.assertRaises(ValueError):
                record_manifest(self.manifest(iterations=[{"iteration_id": "first", "answer": "原回答", "grading": grade}]), self.root)
        self.assertFalse((self.root / "docs/experiments").exists())

    def test_prompt_only_iteration_is_not_counted_as_an_answer_or_teaching_draft(self):
        r = self.manifest(iterations=[{"iteration_id": "prompt-only", "answer": "未记录",
                                      "settings": {"stage": "teaching"}}])
        p = self.root / "docs/experiments/Q01/model-a/run1/record.json"
        metrics = _record_metrics(self.root.resolve(), p.resolve(), r)
        self.assertEqual((metrics["rounds"], metrics["answers"], metrics["teaching_rounds"]), (0, 0, 0))

    def test_metering_requires_all_iterations_and_preserves_zero(self):
        r = self.manifest()
        r["iterations"][0]["settings"] = {"timing": {"duration_seconds": 0}, "usage": {"total": {"input_tokens": 3, "output_tokens": 2}}}
        self.assertIsNone(sample(r, {}, {})["tokens"])
        r["iterations"][1]["settings"] = {"timing": {"duration_seconds": 8}, "usage": {"total_tokens": 4}}
        row = sample(r, {}, {})
        self.assertEqual(row["tokens"], 9)
        self.assertEqual(row["generation_seconds"], 8)
        for invalid in (True, -1, float("nan"), float("inf"), "8"):
            r["iterations"][1]["settings"]["timing"]["duration_seconds"] = invalid
            self.assertIsNone(sample(r, {}, {})["generation_seconds"])

    def test_set_membership_survives_append_and_refresh_preserves_raw_files(self):
        result = record_manifest(self.manifest(test_set_id="suite-v1"), self.root)
        path = Path(result["record_dir"]) / "record.json"
        record_manifest({"question_id": "Q01", "ai_id": "model-a", "run_id": "run1",
                         "human_review": "用户明确提供的补充评价"}, self.root)
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(record["test_set_id"], "suite-v1")
        self.assertEqual(_record_metrics(self.root.resolve(), path.resolve(), record)["human_review"], "已填写")
        originals = {p: p.read_bytes() for p in path.parent.rglob("*") if p.is_file() and p.name not in {"测试记录.md", "测试记录.html"}}
        refresh_records(self.root)
        self.assertTrue(all(p.read_bytes() == content for p, content in originals.items()))
        html = Path(result["index_reading_path"]).read_text(encoding="utf-8")
        self.assertIn("suite-v1", html)
        self.assertIn('id="metric-options"', html)
        self.assertIn("导出筛选样本 CSV", html)

    def test_invalid_ids_and_duplicate_plans_are_rejected_before_record_write(self):
        for key in ("../outside", "中文", None, ""):
            with self.assertRaises(ValueError):
                record_manifest(self.manifest(test_set_id=key), self.root)
        self.assertFalse((self.root / "docs/experiments").exists())
        plan = {"schema_version": 1, "test_sets": [{"test_set_id": "suite", "question_ids": ["Q1", "Q1"]}]}
        with self.assertRaises(ValueError):
            validate_test_sets(plan, _identifier)
        (self.root / "evaluations").mkdir()
        (self.root / "evaluations/index-settings.json").write_text(json.dumps(plan), encoding="utf-8")
        with self.assertRaises(ValueError):
            record_manifest(self.manifest(), self.root)
        self.assertFalse((self.root / "docs/experiments").exists())

    def test_scripts_are_hash_authorized_and_data_cannot_escape_script_tag(self):
        attack = '</script><img src=x onerror="alert(1)">'
        html, _ = render_dashboard(render_reading("# 静态索引", "索引"), [{"model": attack}], [])
        self.assertNotIn(attack, html)
        self.assertIn("\\u003c/script\\u003e", html)
        blocks = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
        self.assertEqual(len(blocks), 2)
        for block in blocks:
            digest = base64.b64encode(hashlib.sha256(block.encode("utf-8")).digest()).decode()
            self.assertIn("'sha256-" + digest + "'", html)
        self.assertNotIn("script-src 'unsafe-inline'", html)
        self.assertNotIn("https://", html)
        self.assertIn('id="runtime-results"', html)
        self.assertLess(html.index('id="runtime-results"'), html.index('<details><summary>内部核验'))


if __name__ == "__main__":
    unittest.main()
