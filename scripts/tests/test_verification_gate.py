"""Publication failures must never become a completed mathematical experiment."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rethlas_core.publication import check_or_publish
from rethlas_core.verification import make_binding, text_sha, validate_binding, validate_math_report, write_once


class VerificationGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        gen = self.root / "Rethlas/agents/generation"
        (gen / "data").mkdir(parents=True)
        statement = gen / "data/q.md"
        statement.write_text("Synthetic statement.\n", encoding="utf-8")
        result = gen / "results/q"
        result.mkdir(parents=True)
        self.problem = types.SimpleNamespace(generation_root=gen, file=statement,
            result_dir=result, verified_path=result / "blueprint_verified.md")
        self.candidate = result / "blueprint.md"
        self.candidate.write_text("Synthetic proof.\n", encoding="utf-8")
        self.report = {"verification_report": {"summary": "Synthetic check.", "critical_errors": [], "gaps": []},
                       "verdict": "correct", "repair_hints": ""}

    def bind(self, report=None):
        value = make_binding(self.problem.file.read_text(), self.candidate.read_text(),
                             report or self.report, "test-verification")
        trusted = self.problem.generation_root.parent / "verification/results/test-verification"
        write_once(trusted / "verification.json", report or self.report)
        write_once(trusted / "verification.binding.json", value)
        receipt = self.problem.generation_root / "verification_receipts" / (text_sha(self.candidate.read_text()) + "_test-verification.json")
        write_once(receipt, value)
        return value, trusted, receipt

    def test_host_publishes_exact_candidate_and_idempotently_checks_receipt(self):
        self.bind()
        self.assertTrue(check_or_publish(self.problem))
        self.assertEqual(self.candidate.read_bytes(), self.problem.verified_path.read_bytes())
        self.assertTrue(check_or_publish(self.problem))
        self.assertTrue((self.problem.result_dir / "blueprint_verified.binding.json").is_file())

    def test_unbound_file_is_preserved_but_not_accepted(self):
        self.problem.verified_path.write_text("Legacy proof.", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "legacy_unbound"):
            check_or_publish(self.problem)
        self.assertEqual(self.problem.verified_path.read_text(), "Legacy proof.")

    def test_statement_or_proof_changes_invalidate_existing_result(self):
        self.bind()
        self.assertTrue(check_or_publish(self.problem))
        self.problem.file.write_text("Different statement.", encoding="utf-8")
        with self.assertRaises(ValueError):
            check_or_publish(self.problem)

    def test_model_written_receipt_cannot_replace_verifier_copy(self):
        value, trusted, receipt = self.bind()
        forged = deepcopy(value)
        forged["created_at_utc"] = "2099-01-01T00:00:00+00:00"
        receipt.write_text(json.dumps(forged), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "differs"):
            check_or_publish(self.problem)
        self.assertFalse(self.problem.verified_path.exists())

    def test_wrong_report_never_publishes(self):
        bad = deepcopy(self.report)
        bad["verification_report"]["gaps"] = [{"location": "proof", "issue": "Synthetic missing deduction."}]
        bad.update(verdict="wrong", repair_hints="Repair the missing deduction.")
        self.bind(bad)
        self.assertFalse(check_or_publish(self.problem))
        self.assertFalse(self.problem.verified_path.exists())

    def test_schema_valid_but_contradictory_report_is_rejected(self):
        bad = deepcopy(self.report)
        bad["verification_report"]["gaps"] = [{"location": "proof", "issue": "Gap."}]
        with self.assertRaises(ValueError):
            validate_math_report(bad)

    def test_mutated_bound_report_is_rejected(self):
        binding = make_binding("Q", "P", self.report, "V1")
        binding["report"]["verification_report"]["summary"] = "Changed"
        with self.assertRaisesRegex(ValueError, "changed"):
            validate_binding(binding, "Q", "P")

    def test_generation_mcp_preserves_claim_ledger_in_bound_api_response(self):
        from test_claim_contract import claim_report
        module_path = Path(__file__).resolve().parents[2] / "Rethlas/agents/generation/mcp/server.py"
        spec = importlib.util.spec_from_file_location("generation_gate_fixture", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        statement, proof = "Synthetic statement.", "Synthetic proof."
        report = claim_report(proof)
        binding = make_binding(statement, proof, report, "fixture-claims")
        response = types.SimpleNamespace(raise_for_status=lambda: None,
            json=lambda: {**report, "verification_id": "fixture-claims", "verification_binding": binding})
        with patch.object(module, "REPO_ROOT", self.problem.generation_root), \
             patch.object(module.requests, "post", return_value=response):
            result = module.verify_proof_service(statement, proof)
        self.assertEqual(result["claim_checks"], report["claim_checks"])
        self.assertTrue(Path(result["receipt_file"]).is_file())

    def test_windows_api_rejects_direct_file_write_bypassing_mcp_rules(self):
        import verification_api as api
        class FakeProcess:
            returncode = 0
            def communicate(proc, prompt, timeout):
                directory = self.root / "api/V1"
                directory.mkdir(parents=True, exist_ok=True)
                bad = deepcopy(self.report)
                bad["repair_hints"] = "There is still a mathematical gap."
                (directory / "verification.json").write_text(json.dumps(bad), encoding="utf-8")
        with patch.object(api.upstream, "RESULTS_ROOT", self.root / "api"), \
             patch.object(api, "build_command", return_value=["synthetic-not-executed"]), \
             patch.object(api.subprocess, "Popen", return_value=FakeProcess()):
            with self.assertRaises(api.HTTPException):
                api.run_codex_verification("V1", "Q", "P")


if __name__ == "__main__":
    unittest.main()
