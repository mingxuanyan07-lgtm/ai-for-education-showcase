"""Chinese retrieval, version state, and native journal concurrency (no model)."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rethlas_core.memory import ClaimMemory, append_jsonl, read_jsonl, update_meta, tokenize
from rethlas_core.verification import make_binding
from test_claim_contract import claim_report
import test_deepseek_pipeline as base
from test_teaching_contract import teaching_report


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def test_chinese_and_mixed_tokens_are_searchable(self):
        tokens = tokenize("连续函数 f(x) 的极限")
        self.assertIn("连续", tokens)
        self.assertIn("极限", tokens)
        self.assertIn("x", tokens)

    def test_parallel_append_and_metadata_merge_lose_no_records(self):
        def write(i):
            append_jsonl(self.path / "events.jsonl", {"id": i, "text": "连续函数" * 500})
            update_meta(self.path / "meta.json", {"run_id": "fixture"}, {str(i): i})
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(40)))
        self.assertEqual({row["id"] for row in read_jsonl(self.path / "events.jsonl")}, set(range(40)))
        meta = json.loads((self.path / "meta.json").read_text(encoding="utf-8"))
        self.assertTrue(all(meta[str(i)] == i for i in range(40)))

    def test_rejected_latest_claim_never_appears_as_verified_and_history_remains(self):
        memory = ClaimMemory(self.path / "memory.db", {"question_id": "fixture", "run_id": "one"})
        proof = "# theorem\n## statement\nSynthetic.\n## proof\n连续函数推导。"
        report = claim_report(proof)
        report["claim_checks"]["claims"][0]["statement"] = "连续函数的极限"
        memory.record("Synthetic.", proof, report, make_binding("Synthetic.", proof, report, "first"))
        self.assertEqual(len(memory.search("连续函数", "verified")), 1)
        bad = deepcopy(report)
        bad["claim_checks"]["claims"][0]["status"] = "gap"
        bad["verification_report"]["gaps"] = [{"location": "C1", "issue": "Synthetic missing premise"}]
        bad.update(verdict="wrong", repair_hints="Add premise")
        memory.record("Synthetic.", proof, bad, make_binding("Synthetic.", proof, bad, "second"))
        self.assertEqual(memory.search("连续函数", "verified"), [])
        self.assertEqual(len(memory.search("极限", "rejected")), 1)
        self.assertEqual(len(memory.snapshot()["history"]), 2)

    def test_identity_and_binding_prevent_cross_run_or_forged_state(self):
        memory = ClaimMemory(self.path / "memory.db", {"run_id": "one"})
        with self.assertRaises(ValueError): ClaimMemory(self.path / "memory.db", {"run_id": "two"})
        proof = "Synthetic."
        report = claim_report(proof)
        binding = make_binding(proof, proof, report, "first")
        report["claim_checks"]["claims"][0]["status"] = "unchecked"
        with self.assertRaises(ValueError): memory.record(proof, proof, report, binding)


class MemoryPipelineTests(unittest.TestCase):
    setUp = base.PipelineTests.setUp
    tearDown = base.PipelineTests.tearDown
    run_flow = base.PipelineTests.run_flow
    record = base.PipelineTests.record

    def test_bound_review_memory_is_in_final_bundle(self):
        result, calls = self.run_flow([self.proof, json.dumps(claim_report(self.proof)),
            self.teaching, teaching_report(self.teaching)], config={"architecture_profile": "verified-v2"})
        self.assertEqual(result["status"], "pending_final_review")
        request = json.loads(Path(result["final_review_request_file"]).read_text(encoding="utf-8"))
        self.assertIn("claim_memory", request["artifacts"])
        self.assertTrue(any("claim-memory-1.json" in row["source"] for row in self.record(result)["artifacts"]))


if __name__ == "__main__": unittest.main()
