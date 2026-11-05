"""Common external review isolation, immutable evidence and five-dimensional reporting."""
import copy
import json
from pathlib import Path
import shutil
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmark_evaluation import assessment_view, digest, validate_evaluation, validate_reviews
from experiment_records import record_manifest, refresh_records
from index_dashboard import sample, accuracy_text


class BenchmarkEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(__file__).resolve().parents[2] / "work/test-benchmark-evaluation"
        self.root = self.base / uuid.uuid4().hex
        self.root.mkdir(parents=True)

    def tearDown(self):
        self.assertTrue(self.root.resolve().is_relative_to(self.base.resolve()))
        shutil.rmtree(self.root)

    def manifest(self):
        return {"question_id": "Q1", "ai_id": "system-a", "run_id": "R1", "model": "actual",
                "framework": "fixture", "status": "completed", "question": "仅测试记录接口",
                "evaluation": {"system_id": "A", "roles": [{"role_id": "internal-verifier", "actual_model": "actual"}],
                    "budget": {"condition": "fixed_budget", "max_calls": 3},
                    "external_review_plan": {"protocol_id": "common-v1", "rubric_id": "rubric-v1"}},
                "iterations": [{"iteration_id": "final", "answer": "冻结的原始解答", "verification": {"verdict": "correct"}}]}

    def review(self, **extra):
        return {"review_id": "review-1", "scope": "final_output", "independent": True,
                "protocol_id": "common-v1", "rubric_id": "rubric-v1",
                "target": {"iteration_id": "final", "answer_sha256": digest("冻结的原始解答")},
                "reviewer": {"reviewer_id": "common-judge", "kind": "model", "model": "actual-judge"},
                "verdict": "wrong", "report": "真实报告夹具：结论不成立", **extra}

    def update(self, **extra):
        return {"question_id": "Q1", "ai_id": "system-a", "run_id": "R1", **extra}

    def saved(self, result):
        return json.loads((Path(result["record_dir"]) / "record.json").read_text(encoding="utf-8"))

    def test_internal_correct_and_legacy_grade_never_become_external_score(self):
        r = self.manifest()
        r["iterations"][0]["grading"] = {"verdict": "correct", "method": "exact_match", "reference": "ref", "evaluator": "old"}
        result = record_manifest(r, self.root)
        view = sample(self.saved(result), {}, {})
        self.assertEqual(view["final_verdict"], "correct")
        self.assertEqual(view["grade_verdict"], "correct")
        self.assertEqual(view["external_verdict"], "not_reviewed")
        self.assertEqual(accuracy_text([view], "external_verdict"), "待判分（已判 0/1）")
        body = Path(result["record_path"]).read_text(encoding="utf-8")
        self.assertIn("数学正确性（共同外审）", body)
        self.assertIn("教学质量", body)
        self.assertIn("互动能力", body)
        self.assertIn("运行可靠性", body)
        self.assertIn("成本与效率", body)

    def test_external_wrong_is_independent_of_internal_correct_and_review_id_is_immutable(self):
        r = self.manifest()
        r["external_reviews"] = [self.review()]
        result = record_manifest(r, self.root)
        old = self.saved(result)
        self.assertEqual(assessment_view(old)["external_verdict"], "wrong")
        self.assertEqual(old["iterations"][0]["verification"]["verdict"], "correct")
        before = (Path(result["record_dir"]) / "record.json").read_bytes()
        with self.assertRaises(ValueError):
            record_manifest(self.update(external_reviews=[self.review(verdict="correct")]), self.root)
        self.assertEqual((Path(result["record_dir"]) / "record.json").read_bytes(), before)
        record_manifest(self.update(external_reviews=[self.review(review_id="review-2", verdict="correct")]), self.root)
        saved = self.saved(result)
        self.assertEqual([r["verdict"] for r in saved["external_reviews"]], ["wrong", "correct"])

    def test_changed_output_invalidates_old_review_but_preserves_it(self):
        r = self.manifest()
        r["external_reviews"] = [self.review()]
        result = record_manifest(r, self.root)
        record_manifest(self.update(iterations=[{"iteration_id": "revised", "answer": "修订后需重新审阅"}]), self.root)
        saved = self.saved(result)
        self.assertEqual(assessment_view(saved)["external_verdict"], "not_reviewed")
        self.assertEqual(len(saved["external_reviews"]), 1)

    def test_process_final_review_report_is_not_the_student_output_or_common_review(self):
        r = self.manifest()
        r["external_reviews"] = [self.review()]
        r["iterations"].append({"iteration_id": "pipeline-final-review", "answer": "内部终审报告",
                                "settings": {"stage": "final_review"}, "verification": {"verdict": "correct"}})
        self.assertEqual(assessment_view(r)["external_verdict"], "wrong")
        r["external_reviews"] = []
        self.assertEqual(assessment_view(r)["external_verdict"], "not_reviewed")

    def test_mismatched_hash_or_internal_reviewer_rejected_before_record_write(self):
        for review in (self.review(target={"iteration_id": "final", "answer_sha256": "0" * 64}),
                       self.review(reviewer={"reviewer_id": "internal-verifier", "kind": "model"}),
                       self.review(independent=False)):
            with self.assertRaises(ValueError):
                record_manifest({**self.manifest(), "external_reviews": [review]}, self.root)
        self.assertFalse(list(self.root.glob("docs/experiments/*/*/*/record.json")))

    def test_human_audit_conflict_requires_adjudication(self):
        r = self.manifest()
        r["external_reviews"] = [self.review(human_audit={"status": "completed", "sampling_method": "random-seed-1",
            "reviewer_id": "real-human-fixture", "verdict": "correct", "report": "真实人工抽查原文夹具"})]
        self.assertEqual(assessment_view(r)["external_verdict"], "disputed")

    def test_five_dimensions_keep_scores_reasons_and_evidence_and_snapshot_materials(self):
        r = self.manifest()
        (self.root / "textbook.txt").write_text("教材固定版本", encoding="utf-8")
        r["evaluation"]["task_materials"] = {"textbooks": [{"file": "textbook.txt", "version": "v1"}], "independent_sessions": True}
        dimension = {"status": "evaluated", "items": [{"criterion": "关键思路", "score": 1, "max_score": 2,
                     "reason": "有解释但缺少适用条件", "evidence": "回答第2段"}]}
        r["external_reviews"] = [self.review(dimensions={"teaching_quality": dimension, "interaction": {"status": "not_evaluated"}})]
        result = record_manifest(r, self.root)
        saved = self.saved(result)
        self.assertTrue(any(x["source"] == "textbook.txt" for x in saved["artifacts"]))
        self.assertEqual(saved["external_reviews"][0]["dimensions"]["teaching_quality"], dimension)
        body = Path(result["record_path"]).read_text(encoding="utf-8")
        self.assertIn("关键思路 1/2", body)
        self.assertIn("未评测", body)
        self.assertTrue((Path(result["record_dir"]) / "评测详情.html").exists())
        record_manifest(self.update(evaluation={"usage": {"calls": 3, "total_tokens": 100, "wall_seconds": 5}}), self.root)
        saved = self.saved(result)
        self.assertEqual(saved["evaluation"]["roles"][0]["actual_model"], "actual")
        self.assertEqual(sample(saved, {}, {})["system_tokens"], 100)
        before = (Path(result["record_dir"]) / "record.json").read_bytes()
        refresh_records(self.root)
        self.assertEqual((Path(result["record_dir"]) / "record.json").read_bytes(), before)

    def test_protocol_mismatch_does_not_score_and_mixed_protocols_do_not_aggregate(self):
        r = self.manifest()
        r["external_reviews"] = [self.review(protocol_id="other-v1")]
        self.assertEqual(assessment_view(r)["external_verdict"], "not_reviewed")
        rows = [{"external_verdict": "correct", "external_protocol": p, "external_rubric": "r"} for p in ("p1", "p2")]
        self.assertIn("混合", accuracy_text(rows, "external_verdict"))

    def test_external_unanswered_is_zero_correct_not_pending(self):
        r = self.manifest()
        r["iterations"] = []
        r["status"] = "timeout"
        r["external_reviews"] = [self.review(verdict="unanswered", target={"iteration_id": "no-output", "answer_sha256": digest("")})]
        view = assessment_view(r)
        self.assertEqual(view["external_verdict"], "unanswered")
        self.assertEqual(accuracy_text([view], "external_verdict"), "0.0% (0/1)；已判 1/1，待判 0")

    def test_internal_draft_is_preserved_but_no_output_review_scores_and_unmarked_output_still_scores(self):
        r = self.manifest()
        r["status"] = "failed"
        r["iterations"][0]["settings"] = {"stage": "solve", "output_kind": "internal_draft"}
        r["evaluation"]["final_output_iteration_id"] = "no-output"
        r["external_reviews"] = [self.review(
            verdict="unanswered", target={"iteration_id": "no-output", "answer_sha256": digest("")})]
        result = record_manifest(r, self.root)
        saved = self.saved(result)
        self.assertEqual(saved["iterations"][0]["answer"], "冻结的原始解答")
        self.assertEqual(saved["iterations"][0]["settings"]["stage"], "solve")
        self.assertEqual(saved["iterations"][0]["settings"]["output_kind"], "internal_draft")
        view = assessment_view(saved)
        self.assertEqual(view["external_verdict"], "unanswered")
        self.assertEqual(accuracy_text([view], "external_verdict"), "0.0% (0/1)；已判 1/1，待判 0")

        ordinary = self.manifest()
        ordinary["iterations"][0]["settings"] = {"stage": "solve"}
        ordinary["external_reviews"] = [self.review()]
        self.assertEqual(assessment_view(ordinary)["external_verdict"], "wrong")

    def test_invalid_dimensions_and_budget_fail_validation(self):
        for evaluation in ({"budget": {"max_calls": -1}}, {"roles": [{"role_id": "a", "calls": True}]},
                           {"usage": {"total_tokens": float("nan")}}):
            with self.assertRaises(ValueError):
                validate_evaluation(evaluation)
        bad = {"status": "evaluated", "items": [{"criterion": "c", "score": 3, "max_score": 2, "reason": "r", "evidence": "e"}]}
        with self.assertRaises(ValueError):
            validate_reviews([self.review(dimensions={"teaching_quality": bad})])


if __name__ == "__main__":
    unittest.main()
