"""Admission budgets, truthful call accounting, and bounded routing (offline)."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rethlas_core.budget import RunBudget, BudgetLimit
import test_deepseek_pipeline as base
from test_claim_contract import claim_report
from test_teaching_contract import teaching_report


class BudgetTests(unittest.TestCase):
    def test_time_call_tool_and_token_limits(self):
        clock = [0]
        budget = RunBudget(seconds=10, model_calls=1, tool_calls=1, observed_tokens=20, clock=lambda: clock[0])
        self.assertEqual(budget.admit_model(30), 10)
        with self.assertRaises(BudgetLimit): budget.admit_model(30)
        budget.admit_tools(1, 30)
        with self.assertRaises(BudgetLimit): budget.admit_tools(1, 30)
        clock[0] = 10
        with self.assertRaises(BudgetLimit): budget.remaining(30)
        token = RunBudget(observed_tokens=20)
        token.observe("solver", {"usage": {"totalTokens": 25}})
        with self.assertRaises(BudgetLimit): token.admit_model(token.deadline)

    def test_missing_usage_is_unknown_and_dsh_cache_not_silently_added(self):
        budget = RunBudget()
        budget.observe("one", {"usage": {"inputTokens": 2, "outputTokens": 3}})
        self.assertIsNone(budget.snapshot()["observed_total_tokens"])
        self.assertEqual(budget.snapshot()["unmetered_roles"], ["one"])


class BudgetPipelineTests(unittest.TestCase):
    setUp = base.PipelineTests.setUp
    tearDown = base.PipelineTests.tearDown
    run_flow = base.PipelineTests.run_flow
    record = base.PipelineTests.record

    def test_role_limit_stops_before_extra_call_and_keeps_raw_answer(self):
        result, calls = self.run_flow([self.proof], config={"max_model_calls": 1})
        self.assertEqual(result["failure_kind"], "budget_exhausted")
        self.assertEqual(len(calls.calls), 1)
        self.assertEqual(self.record(result)["iterations"][0]["answer"], self.proof)

    def test_preflight_failure_is_not_reported_as_model_call(self):
        result, calls = self.run_flow([{"status": "failed", "invocation_started": False,
            "failure_kind": "configuration_failure", "error": "Synthetic CLI unavailable"}])
        self.assertFalse(result["called_model"])
        self.assertEqual(result["role_attempts"], 1)
        self.assertEqual(result["failure_kind"], "configuration_failure")

    def test_structural_failure_uses_next_bounded_round_then_verifies(self):
        result, calls = self.run_flow(["bad format", self.proof, json.dumps(claim_report(self.proof)),
            self.teaching, teaching_report(self.teaching)], config={"architecture_profile": "verified-v2"})
        self.assertEqual(result["status"], "pending_final_review")
        self.assertEqual(len(calls.calls), 5)
        self.assertIn("invalid_format", calls.calls[1]["prompt"])


if __name__ == "__main__": unittest.main()
