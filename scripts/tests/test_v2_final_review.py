"""Complete synthetic v2 handoff: require binding, scope and reviewer coverage."""
import hashlib
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_deepseek_pipeline as base
from test_claim_contract import claim_report
from test_teaching_contract import teaching_report, teaching_review
from deepseek_rethlas.final_review import finalize_review


class V2FinalTests(unittest.TestCase):
    setUp = base.PipelineTests.setUp
    tearDown = base.PipelineTests.tearDown
    run_flow = base.PipelineTests.run_flow
    record = base.PipelineTests.record

    def bundle(self):
        result, _ = self.run_flow([self.proof, json.dumps(claim_report(self.proof)),
            self.teaching, teaching_report(self.teaching)], config={"architecture_profile": "verified-v2"})
        self.request_path = Path(result["final_review_request_file"])
        self.request = json.loads(self.request_path.read_text(encoding="utf-8"))
        self.prompt = self.root / self.request["reviewer"]["prompt"]["file"]
        self.final = {"verdict": "correct", "critical_errors": [], "gaps": [],
            "verifier": {"verifier_id": "codex-current-chat", "source": "current_codex_chat", "model": "未记录"},
            "summary": "Synthetic final evidence only.", "task_review": "Synthetic analysis; pending human review.",
            "checks": {key: "Synthetic complete coverage" for key in ("problem_conditions", "proof", "deepseek_reports",
                "teaching_math", "requirements", "knowledge_requirements", "solution_route", "claim_dependencies",
                "computation_evidence", "memory_scope")}, "teaching_review": teaching_review(self.teaching)}
        self.final_path = self.root / "synthetic-final.json"
        return result

    def invoke(self):
        self.request_path.write_text(json.dumps(self.request, ensure_ascii=False), encoding="utf-8")
        self.final["review_request_sha256"] = hashlib.sha256(self.request_path.read_bytes()).hexdigest()
        self.final_path.write_text(json.dumps(self.final, ensure_ascii=False), encoding="utf-8")
        return finalize_review(self.root, self.request_path, self.prompt, self.final_path)

    def test_complete_v2_can_finalize_only_with_independent_report(self):
        self.bundle()
        self.assertIn("claim_dependencies", self.prompt.read_text(encoding="utf-8"))
        self.assertEqual(self.invoke()["status"], "completed")

    def test_missing_proof_binding_rejected_even_if_request_hash_updated(self):
        self.bundle()
        del self.request["artifacts"]["proof_binding"]
        with self.assertRaisesRegex(ValueError, "requires"): self.invoke()

    def test_memory_from_other_run_rejected_even_if_file_hash_updated(self):
        self.bundle()
        entry = self.request["artifacts"]["claim_memory"]
        path = self.root / entry["file"]
        memory = json.loads(path.read_text(encoding="utf-8"))
        memory["identity"]["run_id"] = "other-run"
        path.write_text(json.dumps(memory), encoding="utf-8")
        entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, "another proof/report/run"): self.invoke()

    def test_missing_tool_scope_coverage_rejected(self):
        self.bundle()
        del self.final["checks"]["computation_evidence"]
        with self.assertRaisesRegex(ValueError, "coverage missing"): self.invoke()


if __name__ == "__main__": unittest.main()
