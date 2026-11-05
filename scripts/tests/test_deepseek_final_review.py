"""Synthetic final-review binding tests; no models or real experiment writes."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deepseek_rethlas.final_review import finalize_review


class FinalReviewTests(unittest.TestCase):
    def setUp(self):
        staging = Path(__file__).resolve().parents[2] / "work/test-final-review"
        staging.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=staging)
        self.root = Path(self.temp.name).resolve()
        self.identity = {"question_id": "Q", "ai_id": "rethlas-dsh-test", "run_id": "R"}
        self.inputs, self.artifacts = {}, {}
        for group, names in ((self.inputs, ("problem", "requirements", "user_prompt")),
                             (self.artifacts, ("proof", "proof_report", "teaching", "teaching_report"))):
            for name in names:
                path = self.root / (name + ".txt")
                path.write_text(name, encoding="utf-8")
                group[name] = {"file": path.name, "sha256": self.sha(path)}
        self.request = {"schema_version": 1, "status": "pending_final_review", "identity": self.identity,
                        "inputs": self.inputs, "artifacts": self.artifacts,
                        "reviewer": {"kind": "codex_current_chat"}}
        self.request_path = self.write("request.json", self.request)
        self.prompt_path = self.root / "prompt.txt"
        self.prompt_path.write_text("Actual saved test review request", encoding="utf-8")
        self.request["reviewer"]["prompt"] = {"file": "prompt.txt", "sha256": self.sha(self.prompt_path)}
        self.write("request.json", self.request)
        self.report = {"review_request_sha256": self.sha(self.request_path), "verdict": "correct",
                       "verifier": {"verifier_id": "codex-current-chat", "source": "current_codex_chat", "model": "未记录"},
                       "critical_errors": [], "gaps": [], "summary": "Synthetic fixture passed.",
                       "task_review": "Synthetic automatic review; pending human evaluation.",
                       "checks": {key: "Synthetic checked evidence" for key in
                                  ("problem_conditions", "proof", "deepseek_reports", "teaching_math", "requirements")}}
        self.report_path = self.write("review.json", self.report)
        self.record = {**self.identity, "status": "pending_final_review",
                       "question": "problem", "requirements": "requirements", "user_prompt": "user_prompt", "final_answer": "proof",
                       "iterations": [{"iteration_id": "teaching-1", "prompt": "old input", "answer": "teaching",
                                       "settings": {"stage": "teaching"}, "verification": [{"verdict": "correct"}]}],
                       "verification": [{"verdict": "correct", "stage": "proof_verification"},
                                        {"stage": "final_review", "verdict": "not_run", "verifier": {"verifier_id": "codex-current-chat"}}]}
        self.record_path = self.write("docs/experiments/Q/rethlas-dsh-test/R/record.json", self.record)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    @staticmethod
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def invoke(self, **kwargs):
        return finalize_review(self.root, self.request_path, self.prompt_path, self.report_path, **kwargs)

    def test_appends_review_without_changing_original_text(self):
        with patch("deepseek_rethlas.final_review.experiment_records.record_manifest", return_value={}) as record:
            result = self.invoke()
        manifest = record.call_args.args[0]
        self.assertEqual(result["status"], "completed")
        self.assertEqual([item["verdict"] for item in manifest["verification"]], ["correct", "correct"])
        update = manifest["iterations"][1]
        self.assertEqual(update["iteration_id"], "teaching-1")
        self.assertNotIn("prompt", update)
        self.assertNotIn("answer", update)
        self.assertNotIn("user_prompt", manifest)
        self.assertNotIn("human_review", manifest)

    def test_modified_subject_rejected(self):
        (self.root / "proof.txt").write_text("Changed proof", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "material changed"):
            self.invoke()

    def test_wrong_review_prompt_rejected(self):
        self.prompt_path.write_text("Review another problem", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "review prompt differs"):
            self.invoke()

    def test_report_request_hash_and_complete_coverage_required(self):
        self.report["review_request_sha256"] = "0" * 64
        self.write("review.json", self.report)
        with self.assertRaisesRegex(ValueError, "not bound"):
            self.invoke()
        self.report["review_request_sha256"] = self.sha(self.request_path)
        del self.report["checks"]["teaching_math"]
        self.write("review.json", self.report)
        with self.assertRaisesRegex(ValueError, "coverage missing"):
            self.invoke()

    def test_inconsistent_verdict_and_failed_run_rejected(self):
        self.report["gaps"] = ["Unproved assertion"]
        self.write("review.json", self.report)
        with self.assertRaisesRegex(ValueError, "cannot contain"):
            self.invoke()
        self.report["gaps"] = []
        self.write("review.json", self.report)
        self.record["status"] = "timeout"
        self.write("docs/experiments/Q/rethlas-dsh-test/R/record.json", self.record)
        with self.assertRaisesRegex(ValueError, "pending final review"):
            self.invoke()

    def test_dryrun_does_not_record(self):
        with patch("deepseek_rethlas.final_review.experiment_records.record_manifest") as record:
            result = self.invoke(dry_run=True)
        self.assertFalse(result["wrote"])
        record.assert_not_called()

    def test_wrong_review_remains_unverified(self):
        self.report.update(verdict="wrong", critical_errors=["Synthetic counterexample"])
        self.write("review.json", self.report)
        with patch("deepseek_rethlas.final_review.experiment_records.record_manifest", return_value={}) as record:
            result = self.invoke()
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(record.call_args.args[0]["status"], "unverified")


if __name__ == "__main__":
    unittest.main()
