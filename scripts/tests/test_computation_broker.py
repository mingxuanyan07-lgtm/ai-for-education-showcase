"""Whitelisted computation data, exact finite checks and failure evidence."""
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rethlas_core import computation as comp
import test_deepseek_pipeline as base
from test_claim_contract import claim_report
from test_teaching_contract import teaching_report


def request(operation="exact_arithmetic", arguments=None):
    return {"id": "K1", "operation": operation, "arguments": arguments or {"expression": "1/3+1/6", "expected": "1/2"},
            "assumptions": {}, "domain": "Synthetic exact rational input", "purpose": "Synthetic host tool check", "required": True}


def prefix(row):
    return "<!-- rethlas-compute-v1\n" + json.dumps({"contract_version": comp.VERSION, "requests": [row]}) + "\n-->\n"


class ComputationTests(unittest.TestCase):
    def test_mcp_sdk_one_and_two_field_names_are_supported_without_rewriting_results(self):
        schema = {"type": "object"}
        payload = {"ok": True, "result": "2", "trace_id": "synthetic"}
        for fields in ({"input_schema": schema}, {"inputSchema": schema}):
            self.assertIs(comp._mcp_tool_schema(SimpleNamespace(**fields)), schema)
        for fields in ({"is_error": False, "structured_content": payload},
                       {"isError": False, "structuredContent": payload}):
            self.assertIs(comp._mcp_result_payload(SimpleNamespace(**fields)), payload)
        text = SimpleNamespace(type="text", text=json.dumps(payload))
        self.assertEqual(comp._mcp_result_payload(SimpleNamespace(is_error=False,
            structured_content=None, content=[text])), payload)

    def test_mcp_error_and_unknown_envelopes_cannot_become_success(self):
        for fields in ({"is_error": True}, {"isError": True}, {}, {"is_error": "false"}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                comp._mcp_result_payload(SimpleNamespace(**fields))
        with self.assertRaises(ValueError):
            comp._mcp_tool_schema(SimpleNamespace(input_schema=None))

    def test_exact_arithmetic_never_evaluates_code_or_float_constants(self):
        self.assertEqual(str(comp.exact("1/3+1/6")), "1/2")
        for value in ("__import__('os')", "open('x')", "1.5", "x+1", "(1).__class__", "2**10000000"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                comp.exact(value)

    def test_unsupported_tools_and_duplicate_ids_are_rejected(self):
        row = request()
        row["operation"] = "run_python"
        with self.assertRaises(Exception): comp.extract_requests(prefix(row))
        text = prefix(request()).replace('"requests": [', '"requests": [' + json.dumps(request()) + ',')
        with self.assertRaises(ValueError): comp.extract_requests(text)

    def test_complete_divisor_and_circular_enumeration_scope(self):
        divisors = comp.builtin(request("integer_divisors", {"number": 36}))
        self.assertEqual(divisors["divisors"], [1, 2, 3, 4, 6, 9, 12, 18, 36])
        circular = comp.builtin(request("count_circular_permutations", {
            "items": ["A", "B", "C", "D"], "forbidden_pairs": [["A", "B"]], "rotations_identical": True}))
        self.assertEqual(circular["examined"], 6)
        self.assertEqual(circular["count"], 2)
        self.assertFalse(circular["reflections_identical"])

    def test_backend_failure_is_evidence_and_not_a_pass(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            def failed(*args): raise OSError("Synthetic DLL failure")
            result = comp.execute_requests(Path(folder), [request("verify_equivalence", {"left": "x", "right": "x"})],
                Path(folder) / "calls", time.monotonic() + 10, mcp_caller=failed)
        self.assertEqual(result[0]["status"], "failed")
        self.assertIn("Synthetic DLL failure", result[0]["result"]["error"])


class BrokerPipelineTests(unittest.TestCase):
    setUp = base.PipelineTests.setUp
    tearDown = base.PipelineTests.tearDown
    run_flow = base.PipelineTests.run_flow
    record = base.PipelineTests.record

    def test_actual_host_result_reaches_reviewer_and_final_binding(self):
        proof = prefix(request()) + self.proof
        result, calls = self.run_flow([proof, json.dumps(claim_report(proof)), self.teaching,
            teaching_report(self.teaching)], config={"architecture_profile": "verified-v2"})
        self.assertEqual(result["status"], "pending_final_review")
        self.assertIn('"exact_result": "1/2"', calls.calls[1]["prompt"])
        self.assertTrue(self.record(result)["tool_checks"][0]["result"]["matches_expected"])
        target = json.loads(Path(result["final_review_request_file"]).read_text(encoding="utf-8"))
        self.assertIn("computation_evidence", target["artifacts"])

    def test_required_tool_failure_stops_before_model_review(self):
        proof = prefix(request("verify_equivalence", {"left": "x", "right": "x"})) + self.proof
        def unavailable(root, requests, directory, deadline):
            return [{"evidence_id": "K1", "request": requests[0], "status": "failed", "result": {"ok": False}}]
        with patch("deepseek_rethlas.pipeline.computation.execute_requests", side_effect=unavailable):
            result, calls = self.run_flow([proof], config={"architecture_profile": "verified-v2"})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(calls.calls), 1)
        self.assertIn("required_computation_failed", result["error"])

    def test_math_review_cannot_silently_accept_refuting_exact_result(self):
        row = request(arguments={"expression": "1/3+1/6", "expected": "1"})
        proof = prefix(row) + self.proof
        result, calls = self.run_flow([proof, json.dumps(claim_report(proof))], config={"architecture_profile": "verified-v2"})
        self.assertEqual(result["status"], "failed")
        self.assertIn("computation_conflict", result["error"])

    def test_optional_refuting_result_also_blocks_publication(self):
        row = request(arguments={"expression": "1/2", "expected": "1"})
        row["required"] = False
        proof = prefix(row) + self.proof
        result, calls = self.run_flow([proof, json.dumps(claim_report(proof))], config={"architecture_profile": "verified-v2"})
        self.assertEqual(result["failure_kind"], "computation_conflict")


if __name__ == "__main__": unittest.main()
