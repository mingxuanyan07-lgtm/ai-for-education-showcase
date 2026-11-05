"""Hybrid pipeline boundary tests with synthetic responses only, no providers.

Fixtures live in temporary project trees and are marked installation_test.
They establish orchestration contracts, not mathematical or teaching quality.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_deepseek_pipeline as base
from test_claim_contract import claim_report
from test_teaching_contract import teaching_report, teaching_review
from deepseek_rethlas.pipeline import PipelineError, run_pipeline
from deepseek_rethlas.final_review import finalize_review


TEACHING_CONFIG = {"provider": "deepseek-api", "model": "deepseek-flash",
                   "workflow_variant": "deeptutor-chat-teaching-v1"}


class TeachingCalls(base.FakeCalls):
    def __call__(self, **kwargs):
        original = self.replies[0]
        result = super().__call__(**kwargs)
        result.update(session_id="synthetic-deeptutor-" + str(len(self.calls)),
                      provider_request_attempts=1, provider_request_observed=True,
                      response_observed=True, invocation_started=True,
                      usage={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5})
        if isinstance(original, dict):
            result.update(original)
        return result


class HybridPipelineTests(unittest.TestCase):
    setUp = base.PipelineTests.setUp
    tearDown = base.PipelineTests.tearDown
    record = base.PipelineTests.record

    def run_hybrid(self, *, dsh_replies=None, teaching_replies=None, config=None, **kwargs):
        dsh = base.FakeCalls(dsh_replies if dsh_replies is not None else [
            self.proof, json.dumps(claim_report(self.proof)), teaching_report(self.teaching)])
        tutor = TeachingCalls(teaching_replies if teaching_replies is not None else [self.teaching])
        result = run_pipeline(self.root, "problem.txt", "requirements.txt", "user.txt", question_id="Q1",
            tags=["测试替身"], config={"architecture_profile": "verified-v2", "record_kind": "installation_test", **(config or {})},
            caller=dsh, teaching_caller=tutor, teaching_config=TEACHING_CONFIG, **kwargs)
        return result, dsh, tutor

    def test_teaching_role_is_distinct_and_full_handoff_stays_pending(self):
        result, dsh, tutor = self.run_hybrid()
        self.assertEqual(result["status"], "pending_final_review")
        self.assertEqual(result["ai_id"], "hybrid-rethlas-deeptutor-deepseek-flash-v1")
        self.assertTrue(result["completed_hybrid_chain"])
        self.assertFalse(result["completed_deepseek_chain"])
        self.assertEqual(len(dsh.calls), 3)
        self.assertEqual(len(tutor.calls), 1)
        self.assertNotIn(self.requirements, dsh.calls[0]["prompt"])
        self.assertIn(self.proof, tutor.calls[0]["prompt"])
        self.assertEqual(tutor.calls[0]["config"], TEACHING_CONFIG)
        record = self.record(result)
        row = next(item for item in record["iterations"] if item["iteration_id"] == "teaching-1")
        self.assertEqual(row["settings"]["actual_provider"], "deepseek-api")
        self.assertEqual(row["settings"]["configured_provider"], "deepseek-api")
        self.assertIn("DeepTutor", row["settings"]["ai"])
        self.assertEqual(row["settings"]["role_workflow"], "deeptutor-chat-teaching-v1")
        self.assertEqual(result["budget"]["model_role_attempts"], 4)
        self.assertEqual(result["budget"]["observed_total_tokens"], 5)
        self.assertEqual(record["verification"][-1]["verdict"], "not_run")
        self.assertNotIn(result["run_id"], (self.root / "docs/实验索引.md").read_text(encoding="utf-8"))
        request = json.loads(Path(result["final_review_request_file"]).read_text(encoding="utf-8"))
        source = request["artifacts"]["teaching_source"]
        source_path = self.root / source["file"]
        self.assertEqual(source["sha256"], hashlib.sha256(source_path.read_bytes()).hexdigest())
        provenance = json.loads(source_path.read_text(encoding="utf-8"))
        self.assertEqual(provenance["proof"], request["artifacts"]["proof"])
        self.assertEqual(provenance["prompt_sha256"], hashlib.sha256(row["prompt"].encode()).hexdigest())

    def test_teaching_repair_does_not_restart_proof_and_preserves_first_draft(self):
        repaired = self.teaching + "\n修订后的完整说明。"
        result, dsh, tutor = self.run_hybrid(dsh_replies=[self.proof, json.dumps(claim_report(self.proof)),
            teaching_report(self.teaching, math_correct=False), teaching_report(repaired)],
            teaching_replies=[self.teaching, repaired])
        self.assertEqual(result["status"], "pending_final_review")
        self.assertEqual(len(dsh.calls), 4)
        self.assertEqual(len(tutor.calls), 2)
        rows = self.record(result)["iterations"]
        self.assertEqual([row["iteration_id"] for row in rows],
                         ["solve-1", "proof-review-1", "teaching-1", "review-teaching", "teaching-2", "review-teaching-2"])
        self.assertEqual(rows[2]["answer"], self.teaching)
        self.assertEqual(rows[4]["answer"], repaired)
        self.assertIn("缺少关键依据", tutor.calls[1]["prompt"])
        source = json.loads(Path(result["work_dir"]).joinpath("teaching-2.source.json").read_text(encoding="utf-8"))
        self.assertEqual(source["previous_teaching_sha256"], hashlib.sha256(self.teaching.encode()).hexdigest())
        self.assertEqual(source["teaching_version"], 2)

    def test_second_bad_teaching_stops_without_third_generation_or_final_handoff(self):
        result, dsh, tutor = self.run_hybrid(dsh_replies=[self.proof, json.dumps(claim_report(self.proof)),
            teaching_report(self.teaching, revise=True), teaching_report(self.teaching, revise=True)],
            teaching_replies=[self.teaching, self.teaching])
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["failure_kind"], "teaching_unverified")
        self.assertEqual(len(tutor.calls), 2)
        self.assertNotIn("final_review_request_file", result)

    def test_missing_model_evidence_preserves_actual_failed_teaching(self):
        result, dsh, tutor = self.run_hybrid(teaching_replies=[{"answer": "真实失败稿内容 <script>x</script>",
                                                            "actual_model": "未记录"}])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(dsh.calls), 2)
        self.assertEqual(self.record(result)["iterations"][-1]["answer"], "真实失败稿内容 <script>x</script>")
        self.assertNotIn("final_review_request_file", result)

    def test_reused_session_and_hidden_extra_calls_and_tools_fail_closed(self):
        for override in ({"session_id": "independent-session-1"}, {"provider_request_attempts": 2},
                         {"provider_request_attempts": True}, {"provider_request_observed": None},
                         {"response_observed": False}, {"tool_events": [{"name": "exec"}]}):
            with self.subTest(override=override):
                result, dsh, tutor = self.run_hybrid(teaching_replies=[{"answer": self.teaching, **override}])
                self.assertEqual(result["status"], "failed")
                self.assertEqual(len(dsh.calls), 2)
                self.assertEqual(len(tutor.calls), 1)

    def test_runtime_timeout_preserves_partial_answer_and_does_not_retry(self):
        result, dsh, tutor = self.run_hybrid(teaching_replies=[{"status": "timeout", "answer": "已收到的部分文本", "error": "synthetic deadline"}])
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(self.record(result)["iterations"][-1]["answer"], "已收到的部分文本")
        self.assertEqual(len(tutor.calls), 1)
        self.assertEqual(result["budget"]["model_role_attempts"], 3)

    def test_shared_call_limit_prevents_unbudgeted_teaching(self):
        result, dsh, tutor = self.run_hybrid(config={"max_model_calls": 2})
        self.assertEqual(result["failure_kind"], "budget_exhausted")
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(len(dsh.calls), 2)
        self.assertEqual(tutor.calls, [])

    def test_dry_run_validates_without_writing_or_dispatch(self):
        before = sorted(path.relative_to(self.root) for path in self.root.rglob("*"))
        result, dsh, tutor = self.run_hybrid(dry_run=True)
        self.assertEqual(result["status"], "dry_run")
        self.assertFalse(result["wrote"])
        self.assertEqual(dsh.calls, [])
        self.assertEqual(tutor.calls, [])
        self.assertEqual(before, sorted(path.relative_to(self.root) for path in self.root.rglob("*")))

    def test_run_created_callback_precedes_models_and_binds_copied_inputs(self):
        events, receipts = [], []
        dsh = base.FakeCalls([self.proof, json.dumps(claim_report(self.proof)),
                             teaching_report(self.teaching)])
        tutor = TeachingCalls([self.teaching])

        def created(receipt):
            self.assertEqual(events, [])
            self.assertEqual(dsh.calls, [])
            self.assertEqual(tutor.calls, [])
            work = Path(receipt["work_dir"])
            self.assertTrue(work.is_dir())
            self.assertFalse(list(work.glob("calls/*/host.prompt.txt")))
            self.assertEqual(set(receipt["input_sha256"]), {"problem", "requirements", "user_prompt"})
            for name, text in (("problem", self.problem), ("requirements", self.requirements),
                               ("user_prompt", self.user)):
                copied = work / (name + ".txt")
                self.assertEqual(copied.read_bytes().decode("utf-8"), text)
                self.assertEqual(receipt["input_sha256"][name],
                                 hashlib.sha256(copied.read_bytes()).hexdigest())
            self.assertEqual(receipt["question_id"], "Q1")
            self.assertEqual(receipt["ai_id"], "hybrid-rethlas-deeptutor-deepseek-flash-v1")
            self.assertEqual(work.name, receipt["run_id"])
            receipts.append(deepcopy(receipt))
            events.append("created")

        def checked_dsh(**kwargs):
            self.assertEqual(events[0], "created")
            self.assertEqual(len(receipts), 1)
            events.append("dsh")
            return dsh(**kwargs)

        def checked_tutor(**kwargs):
            self.assertEqual(events[0], "created")
            self.assertEqual(len(receipts), 1)
            events.append("deeptutor")
            return tutor(**kwargs)

        result = run_pipeline(self.root, "problem.txt", "requirements.txt", "user.txt",
            question_id="Q1", tags=["测试替身"],
            config={"architecture_profile": "verified-v2", "record_kind": "installation_test"},
            caller=checked_dsh, teaching_caller=checked_tutor, teaching_config=TEACHING_CONFIG,
            on_run_created=created)
        self.assertEqual(result["status"], "pending_final_review")
        self.assertEqual(events, ["created", "dsh", "dsh", "deeptutor", "dsh"])
        for key in ("question_id", "ai_id", "run_id", "work_dir"):
            self.assertEqual(receipts[0][key], result[key])

    def test_dry_run_never_emits_run_created_callback(self):
        receipts = []
        result, dsh, tutor = self.run_hybrid(dry_run=True, on_run_created=receipts.append)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(receipts, [])
        self.assertEqual(dsh.calls, [])
        self.assertEqual(tutor.calls, [])
        self.assertFalse(result["wrote"])
        self.assertFalse(result["called_model"])

    def test_unsafe_or_implicit_teaching_configuration_rejected_before_execution(self):
        for config in ({**TEACHING_CONFIG, "api_key": "must-not-save"},
                       {**TEACHING_CONFIG, "provider": "fallback"},
                       {**TEACHING_CONFIG, "workflow_variant": "native-rethlas"}, None):
            with self.subTest(config=config), self.assertRaises(PipelineError):
                run_pipeline(self.root, "problem.txt", "requirements.txt", "user.txt",
                    config={"architecture_profile": "verified-v2"}, teaching_caller=TeachingCalls([]), teaching_config=config)
        with self.assertRaises(PipelineError):
            run_pipeline(self.root, "problem.txt", "requirements.txt", "user.txt",
                teaching_caller=TeachingCalls([]), teaching_config=TEACHING_CONFIG)
        self.assertFalse((self.root / "work").exists())

    def test_math_runtime_is_frozen_and_bound_before_dispatch(self):
        runtime = self.root / "math-runtime.json"
        raw = b'{"shared_mcp":{"math-local":{"command":"fixture-python","args":["fixture-server.py"]}}}\r\n'
        runtime.write_bytes(raw)
        result, _, _ = self.run_hybrid(math_runtime_file=runtime)
        request = json.loads(Path(result["final_review_request_file"]).read_text(encoding="utf-8"))
        bound = request["artifacts"]["math_runtime"]
        self.assertEqual((self.root / bound["file"]).read_bytes(), raw)
        self.assertEqual(bound["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertNotEqual(self.root / bound["file"], runtime)

    def final_fixture(self, result):
        request_path = Path(result["final_review_request_file"])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        final = {"review_request_sha256": hashlib.sha256(request_path.read_bytes()).hexdigest(),
            "verdict": "correct", "critical_errors": [], "gaps": [],
            "verifier": {"verifier_id": "codex-current-chat", "source": "current_codex_chat", "model": "未记录"},
            "summary": "Synthetic final review fixture, not actual mathematics evaluation.",
            "task_review": "Synthetic only; human evaluation pending.",
            "checks": {key: "Synthetic coverage" for key in ("problem_conditions", "proof", "deepseek_reports",
                "teaching_math", "requirements", "knowledge_requirements", "solution_route", "claim_dependencies",
                "computation_evidence", "memory_scope")}, "teaching_review": teaching_review(self.teaching)}
        final_path = self.root / "synthetic-final.json"
        final_path.write_text(json.dumps(final), encoding="utf-8")
        return request_path, request, final_path

    def test_hybrid_finalizer_checks_provenance_and_uses_existing_bound_record(self):
        result, _, _ = self.run_hybrid()
        request_path, request, final_path = self.final_fixture(result)
        with patch("deepseek_rethlas.final_review.experiment_records.record_manifest", return_value={}):
            finalized = finalize_review(self.root, request_path, self.root / request["reviewer"]["prompt"]["file"], final_path)
        self.assertEqual(finalized["status"], "completed")
        self.assertEqual(self.record(result)["status"], "pending_final_review")
        # Even a newly rehashed request cannot substitute another proof/run.
        source_entry = request["artifacts"]["teaching_source"]
        source_path = self.root / source_entry["file"]
        source = json.loads(source_path.read_text(encoding="utf-8"))
        source["identity"]["run_id"] = "other-run"
        source_path.write_text(json.dumps(source), encoding="utf-8")
        source_entry["sha256"] = hashlib.sha256(source_path.read_bytes()).hexdigest()
        request_path.write_text(json.dumps(request), encoding="utf-8")
        final = json.loads(final_path.read_text(encoding="utf-8"))
        final["review_request_sha256"] = hashlib.sha256(request_path.read_bytes()).hexdigest()
        final_path.write_text(json.dumps(final), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "provenance belongs"):
            finalize_review(self.root, request_path, self.root / request["reviewer"]["prompt"]["file"], final_path)


if __name__ == "__main__":
    unittest.main()
