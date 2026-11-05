"""Claim coverage/dependencies and opt-in integration; synthetic replies only."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rethlas_core.claims import proof_blocks, review_brief
from rethlas_core.verification import validate_math_report
import test_deepseek_pipeline as base
from test_teaching_contract import teaching_report


def claim_report(proof):
    result = json.loads(base.report())
    result["claim_checks"] = {"contract_version": "claims-v1", "claims": [
        {"id": f"C{i}", "statement": "Synthetic checked deduction " + str(i),
         "assumptions": ["Synthetic input conditions"], "depends_on": [] if i == 1 else [f"C{i-1}"],
         "sources": [{"block_id": block["block_id"], "quote": block["text"]}],
         "status": "supported", "reason": "Synthetic check, not a real mathematical review."}
        for i, block in enumerate(proof_blocks(proof), 1)]}
    return result


class ClaimContractTests(unittest.TestCase):
    proof = "# lemma L\n## statement\nA\n## proof\nFirst deduction.\n\nSecond deduction.\n# 定理 T\n## statement\nB\n## proof\nThird deduction."

    def test_blocks_follow_all_lemma_and_target_proofs(self):
        self.assertEqual([b["text"] for b in proof_blocks(self.proof)],
                         ["First deduction.", "Second deduction.", "Third deduction."])
        validate_math_report(claim_report(self.proof), proof=self.proof, require_claims=True)

    def test_coverage_quotes_and_dependencies_are_not_optional(self):
        original = claim_report(self.proof)
        variants = []
        missing = deepcopy(original)
        missing["claim_checks"]["claims"].pop()
        variants.append(missing)
        cyclic = deepcopy(original)
        cyclic["claim_checks"]["claims"][0]["depends_on"] = ["C3"]
        variants.append(cyclic)
        duplicate = deepcopy(original)
        duplicate["claim_checks"]["claims"][1]["id"] = "C1"
        variants.append(duplicate)
        unreviewed = deepcopy(original)
        unreviewed["claim_checks"]["claims"][0]["status"] = "unchecked"
        variants.append(unreviewed)
        fake_quote = deepcopy(original)
        fake_quote["claim_checks"]["claims"][0]["sources"][0]["quote"] = "Nonexistent deduction."
        variants.append(fake_quote)
        for version in ("claims-v1", "claims-v2"):
            for value in variants:
                value = deepcopy(value)
                value["claim_checks"]["contract_version"] = version
                with self.subTest(version=version, value=value), self.assertRaises(Exception):
                    validate_math_report(value, proof=self.proof, require_claims=True)

    def test_one_claim_may_cite_multiple_distinct_excerpts_from_one_block(self):
        proof = "# lemma L\n## statement\nA\n## proof\nFirst deduction. Second deduction."
        value = json.loads(base.report())
        value["claim_checks"] = {"contract_version": "claims-v2", "claims": [{
            "id": "C1", "statement": "Both deductions are checked", "assumptions": [],
            "depends_on": [], "sources": [
                {"block_id": "P001", "quote": "First deduction."},
                {"block_id": "P001", "quote": "Second deduction."}],
            "status": "supported", "reason": "Both excerpts were checked."}]}
        validate_math_report(value, proof=proof, require_claims=True)
        legacy = deepcopy(value)
        legacy["claim_checks"]["contract_version"] = "claims-v1"
        with self.assertRaises(Exception):
            validate_math_report(legacy, proof=proof, require_claims=True)

    def test_one_block_cannot_be_owned_by_two_claims_or_repeat_quote(self):
        proof = "# lemma L\n## statement\nA\n## proof\nFirst deduction. Second deduction."
        value = json.loads(base.report())
        value["claim_checks"] = {"contract_version": "claims-v2", "claims": [
            {"id": "C1", "statement": "First", "assumptions": [], "depends_on": [],
             "sources": [{"block_id": "P001", "quote": "First deduction."},
                         {"block_id": "P001", "quote": "First deduction."}],
             "status": "supported", "reason": "duplicate"}]}
        with self.assertRaises(Exception):
            validate_math_report(value, proof=proof, require_claims=True)
        value["claim_checks"]["claims"] = [
            {"id": "C1", "statement": "First", "assumptions": [], "depends_on": [],
             "sources": [{"block_id": "P001", "quote": "First deduction."}],
             "status": "supported", "reason": "first"},
            {"id": "C2", "statement": "Second", "assumptions": [], "depends_on": ["C1"],
             "sources": [{"block_id": "P001", "quote": "Second deduction."}],
             "status": "supported", "reason": "second"}]
        with self.assertRaises(Exception):
            validate_math_report(value, proof=proof, require_claims=True)

    def test_v2_repeated_text_in_different_blocks_is_not_duplicate_evidence(self):
        proof = "# theorem T\n## proof\nRepeated deduction.\n\nRepeated deduction."
        value = json.loads(base.report())
        value["claim_checks"] = {"contract_version": "claims-v2", "claims": [{
            "id": "C1", "statement": "Both blocks", "assumptions": [], "depends_on": [],
            "sources": [{"block_id": "P001", "quote": "Repeated deduction."},
                        {"block_id": "P002", "quote": "Repeated deduction."}],
            "status": "supported", "reason": "Both distinct proof blocks were checked."}]}
        validate_math_report(value, proof=proof, require_claims=True)

    def test_versions_are_explicit_and_unknown_versions_fail(self):
        self.assertIn("claims-v2", review_brief(self.proof))
        self.assertIn("claims-v1", review_brief(self.proof, "claims-v1"))
        bad = claim_report(self.proof)
        bad["claim_checks"]["contract_version"] = "claims-v999"
        with self.assertRaises(Exception):
            validate_math_report(bad, proof=self.proof, require_claims=True)

    def test_missing_contract_does_not_become_correct(self):
        with self.assertRaises(Exception):
            validate_math_report(json.loads(base.report()), proof=self.proof, require_claims=True)

    def test_failed_claim_can_be_reported_with_a_mathematical_gap(self):
        value = claim_report(self.proof)
        value["claim_checks"]["claims"][0]["status"] = "unchecked"
        value["verification_report"]["gaps"] = [{"location": "C1", "issue": "Not checked."}]
        value.update(verdict="wrong", repair_hints="Complete the C1 deduction.")
        validate_math_report(value, proof=self.proof, require_claims=True)


class ImprovedPipelineTests(unittest.TestCase):
    setUp = base.PipelineTests.setUp
    tearDown = base.PipelineTests.tearDown
    run_flow = base.PipelineTests.run_flow
    record = base.PipelineTests.record

    def test_profile_activates_claim_review_and_pedagogy_with_separate_identity(self):
        result, calls = self.run_flow([self.proof, json.dumps(claim_report(self.proof)),
            self.teaching, teaching_report(self.teaching)], config={"architecture_profile": "verified-v2"})
        self.assertEqual(result["status"], "pending_final_review")
        self.assertTrue(result["ai_id"].endswith("-v2"))
        self.assertIn("claims-v2", calls.calls[1]["prompt"])
        self.assertIn("knowledge-route-v1", calls.calls[2]["prompt"])
        self.assertIn("claim_checks", self.record(result)["verification"][0])

    def test_profile_missing_claims_stops_with_raw_report_preserved(self):
        result, calls = self.run_flow([self.proof, base.report()], config={"architecture_profile": "verified-v2"})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(calls.calls), 2)
        self.assertEqual(self.record(result)["iterations"][1]["answer"], base.report())

    def test_new_version_preserves_multiquote_report_and_legacy_stays_rejected(self):
        report = claim_report(self.proof)
        report["claim_checks"]["contract_version"] = "claims-v2"
        report["claim_checks"]["claims"][0]["sources"] = [
            {"block_id": "P001", "quote": "实数"},
            {"block_id": "P001", "quote": "平方非负"}]
        raw = json.dumps(report, ensure_ascii=False)
        result, calls = self.run_flow([self.proof, raw, self.teaching, teaching_report(self.teaching)],
                                     config={"architecture_profile": "verified-v2"})
        self.assertEqual(result["status"], "pending_final_review")
        self.assertEqual(self.record(result)["iterations"][1]["answer"], raw)
        self.assertEqual(self.record(result)["verification"][0]["claim_checks"], report["claim_checks"])
        legacy = deepcopy(report)
        legacy["claim_checks"]["contract_version"] = "claims-v1"
        legacy_raw = json.dumps(legacy, ensure_ascii=False)
        result, calls = self.run_flow([self.proof, legacy_raw], config={"architecture_profile": "verified-v2"})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(calls.calls), 2)
        self.assertEqual(self.record(result)["iterations"][1]["answer"], legacy_raw)


if __name__ == "__main__":
    unittest.main()
