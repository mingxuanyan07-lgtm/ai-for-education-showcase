"""Bounded staged orchestration with synthetic replies only, never a real model."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from deepseek_rethlas.pipeline import (PipelineError, run_pipeline, validate_config,
                                      validate_proof, validate_report)


def report(correct=True):
    return json.dumps({"verification_report": {
        "summary": "已按顺序核对所有推导及边界。",
        "critical_errors": [] if correct else [{"location": "主证明", "issue": "缺少关键依据"}],
        "gaps": [], "checked_items": ["实数平方非负及原题条件"],
        "external_reference_checks": ["无外部论文引用"]},
        "verdict": "correct" if correct else "wrong",
        "repair_hints": "" if correct else "补足关键依据"}, ensure_ascii=False)


class FakeCalls:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, *, project_root, call_dir, prompt, config, timeout_seconds):
        # The real runtime rejects nonempty call directories. Test that boundary.
        if call_dir.exists() and any(call_dir.iterdir()):
            raise AssertionError("Runtime directory must not contain host artifacts")
        call_dir.mkdir(parents=True, exist_ok=True)
        parent_prompt = call_dir.parent / "host.prompt.txt"
        assert parent_prompt.read_text(encoding="utf-8") == prompt
        self.calls.append({"prompt": prompt, "config": config,
                           "timeout": timeout_seconds, "call_dir": call_dir})
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            (call_dir / "partial.log").write_text("已收到中断前事件", encoding="utf-8")
            raise reply
        result = {"status": "completed", "answer": reply if isinstance(reply, str) else "",
                  "actual_model": config["model"], "actual_provider": config["provider"],
                  "session_id": "independent-session-" + str(len(self.calls)),
                  "usage": {"tokens": 1}, "elapsed_seconds": 0.01,
                  "error": None, "tool_events": [], "evidence_files": []}
        if isinstance(reply, dict):
            result.update(reply)
        log = call_dir / "events.jsonl"
        log.write_text(json.dumps({"actual_model": result["actual_model"]}) + "\n", encoding="utf-8")
        result["evidence_files"] = [log.relative_to(project_root).as_posix()]
        return result


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.staging = ROOT / "work/test-deepseek-pipeline"
        self.staging.mkdir(parents=True, exist_ok=True)
        self.root = self.staging / ("fixture-" + uuid.uuid4().hex)
        self.root.mkdir()
        schema = self.root / "Rethlas/agents/verification/schemas/verification_output.schema.json"
        schema.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / "Rethlas/agents/verification/schemas/verification_output.schema.json", schema)
        self.schema = json.loads(schema.read_text(encoding="utf-8"))
        self.problem = "证明：对每个实数 x，x²≥0。"
        self.requirements = "面向基础未知的学生，先解释关键思路，再给完整证明。"
        self.user = "请使用全 DeepSeek 流程解题，最后交由当前 Codex 复核。\n" + self.problem
        for name, content in (("problem", self.problem), ("requirements", self.requirements), ("user", self.user)):
            (self.root / (name + ".txt")).write_text(content, encoding="utf-8", newline="\n")
        self.proof = "# theorem thm:target\n\n## statement\n" + self.problem + "\n\n## proof\n实数的平方非负。\n"
        self.teaching = "关键思路：使用实数平方的非负性。\n\n对任意实数 x，x²≥0。"

    def tearDown(self):
        self.assertTrue(self.root.resolve().is_relative_to(self.staging.resolve()))
        shutil.rmtree(self.root)

    def run_flow(self, replies=None, **options):
        caller = FakeCalls(replies or [self.proof, report(), self.teaching, report()])
        result = run_pipeline(self.root, "problem.txt", "requirements.txt", "user.txt",
                              question_id="Q1", tags=["测试夹具"], caller=caller, **options)
        return result, caller

    def record(self, result):
        return json.loads(Path(result["record_dir"]).joinpath("record.json").read_text(encoding="utf-8"))

    def test_success_is_pending_final_review_and_binds_all_inputs_and_prompt(self):
        result, caller = self.run_flow()
        self.assertEqual(result["status"], "pending_final_review")
        self.assertEqual(len(caller.calls), 4)
        record = self.record(result)
        self.assertEqual(record["model"], "deepseek-flash")
        self.assertEqual(record["user_prompt"], self.user)
        self.assertEqual(len(record["iterations"]), 4)
        self.assertEqual(record["verification"][-1]["verdict"], "not_run")
        self.assertEqual(record["verification"][-1]["stage"], "final_review")
        self.assertNotIn(self.requirements, caller.calls[0]["prompt"])
        self.assertNotIn(self.user, caller.calls[0]["prompt"])
        self.assertIn(self.requirements, caller.calls[2]["prompt"])
        self.assertEqual(record["iterations"][2]["settings"]["stage"], "post_verification_teaching")
        self.assertEqual(record["iterations"][3]["settings"]["stage"], "review-teaching")
        request = json.loads(Path(result["final_review_request_file"]).read_text(encoding="utf-8"))
        for group in ("inputs", "artifacts"):
            for bound in request[group].values():
                self.assertEqual(hashlib.sha256((self.root / bound["file"]).read_bytes()).hexdigest(), bound["sha256"])
        prompt = request["reviewer"]["prompt"]
        self.assertEqual(hashlib.sha256((self.root / prompt["file"]).read_bytes()).hexdigest(), prompt["sha256"])
        self.assertEqual(request["identity"]["run_id"], result["run_id"])
        self.assertFalse((self.root / "exports").exists())

    def test_real_host_sources_are_archived_as_byte_exact_text_snapshots(self):
        sources = ("scripts/deepseek_rethlas/pipeline.py", "scripts/deepseek_rethlas/prompts.py",
                   "scripts/deepseek_rethlas/statement_contract.py")
        expected = {}
        for relative in sources:
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            # Exercise the real installed source-file presence boundary and
            # Windows newline preservation; these fixture sources are data.
            raw = b"# CRLF snapshot fixture\r\n" + (ROOT / relative).read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            target.write_bytes(raw)
            expected[relative] = raw
        result, caller = self.run_flow(config={"record_kind": "installation_test"})
        self.assertEqual(result["status"], "pending_final_review")
        self.assertEqual(len(caller.calls), 4)
        record = self.record(result)
        versions = json.loads(Path(result["work_dir"]).joinpath("memory.init.json").read_text(encoding="utf-8"))["source_versions"]
        self.assertFalse(any(a["source"].endswith(".py") for a in record["artifacts"]))
        for relative, raw in expected.items():
            version = next(item for item in versions if item["file"] == relative)
            self.assertTrue(version["snapshot"].endswith(relative + ".txt"))
            self.assertEqual(version["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(version["snapshot_sha256"], version["sha256"])
            self.assertEqual((self.root / version["snapshot"]).read_bytes(), raw)
            archived = next(a for a in record["artifacts"] if a["source"] == version["snapshot"])
            self.assertEqual((Path(result["record_dir"]) / archived["snapshot"]).read_bytes(), raw)
            self.assertEqual(archived["sha256"], version["sha256"])
        self.assertNotIn(result["run_id"], (self.root / "docs/实验索引.md").read_text(encoding="utf-8"))

    def test_wrong_proof_is_repaired_and_prior_report_and_answers_are_preserved(self):
        first = self.proof.replace("实数的平方非负。", "这一关键依据尚未补齐。")
        result, caller = self.run_flow([first, report(False), self.proof, report(), self.teaching, report()])
        self.assertEqual(result["status"], "pending_final_review")
        self.assertEqual(len(caller.calls), 6)
        self.assertIn("缺少关键依据", caller.calls[2]["prompt"])
        record = self.record(result)
        self.assertEqual(record["iterations"][0]["answer"], first)
        self.assertEqual(record["iterations"][0]["verification"]["verdict"], "wrong")
        self.assertEqual(record["iterations"][2]["answer"], self.proof)
        self.assertEqual([x["verdict"] for x in record["verification"]], ["wrong", "correct", "correct", "not_run"])

    def test_three_wrong_rounds_stop_without_teaching(self):
        result, caller = self.run_flow([self.proof, report(False)] * 3)
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(len(caller.calls), 6)
        self.assertTrue(all("teaching" not in x["iteration_id"] for x in self.record(result)["iterations"]))
        self.assertNotIn("final_review_request_file", result)

    def test_invalid_review_json_is_saved_and_never_counted_as_correct(self):
        result, caller = self.run_flow([self.proof, "```json\n{}\n```"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(caller.calls), 2)
        self.assertEqual(self.record(result)["iterations"][1]["answer"], "```json\n{}\n```")
        self.assertEqual(self.record(result)["verification"], [])

    def test_corrupt_or_contradictory_report_is_rejected(self):
        invalid = json.loads(report(False))
        invalid["verdict"] = "correct"
        for text in (json.dumps(invalid), report().replace('"repair_hints": ""', '"repair_hints": "仍需修复"'),
                     '{"verdict":"wrong","verdict":"correct"}'):
            with self.subTest(text=text), self.assertRaises(PipelineError):
                validate_report(text, self.schema)

    def test_changed_main_statement_stops_before_verifier(self):
        result, caller = self.run_flow([self.proof.replace(self.problem, "只证明 x=0")])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(caller.calls), 1)
        self.assertIn("完整原题", result["error"])

    def test_proof_contract_accepts_original_markdown_title_inside_statement(self):
        problem = "# 题目\n\n证明实数的平方非负。"
        proof = "# theorem thm:target\n## statement\n" + problem + "\n## proof\n平方非负。"
        validate_proof(proof, problem)
        with self.assertRaises(PipelineError):
            validate_proof(proof + "\n# lemma late\n", problem)

    def test_first_pilot_statement_excerpt_accepts_only_delimiter_presentation(self):
        # Captured verbatim statement from the first MATH algebra output. The
        # actual full immutable output is also replayed in the repair audit.
        problem = r"If $f(x) = \frac{3x-2}{x-2}$, what is the value of $f(-2) +f(-1)+f(0)$? Express your answer as a common fraction."
        statement = r"If \(f(x) = \frac{3x-2}{x-2}\), what is the value of \(f(-2) +f(-1)+f(0)\)? Express your answer as a common fraction."
        proof = "# theorem thm:target\n## statement\n" + statement + "\n## proof\nThe sum is 14/3."
        audit = validate_proof(proof, problem)
        self.assertEqual(audit["statement_match"], "cosmetic_equivalent")
        self.assertFalse(audit["raw_exact"])
        self.assertEqual(len(audit["delimiter_changes"]), 2)
        self.assertFalse(audit["proof_text_rewritten"])
        self.assertEqual(audit["proof_sha256"], hashlib.sha256(proof.encode()).hexdigest())
        self.assertEqual(audit["mathematical_verdict"], "not_run")

    def test_delimiter_compatibility_rejects_content_and_mode_changes(self):
        problem = r"Let $x=1$ and $y=2$."
        for statement in (r"Let \(x=3\) and \(y=2\).", r"Let \(y=2\) and \(x=1\).",
                          r"Let \(x=1\) or \(y=2\).", r"Let \(x=1\) and \(y=2\), with x>0.",
                          r"Let \(x =1\) and \(y=2\).", r"Let \[x=1\] and \(y=2\)."):
            proof = "# theorem thm:target\n## statement\n" + statement + "\n## proof\nProof."
            with self.subTest(statement=statement), self.assertRaises(PipelineError):
                validate_proof(proof, problem)
        exact = "# theorem thm:target\n## statement\n" + problem + "\n## proof\nProof."
        self.assertEqual(validate_proof(exact, problem)["statement_match"], "raw_exact")

    def test_display_delimiter_compatibility_preserves_body_whitespace(self):
        problem = "Solve $$\nx=1\n$$."
        statement = "Solve \\[\nx=1\n\\]."
        proof = "# theorem thm:target\n## statement\n" + statement + "\n## proof\nProof."
        audit = validate_proof(proof, problem)
        self.assertEqual(audit["statement_match"], "cosmetic_equivalent")
        self.assertEqual(audit["delimiter_changes"][0]["kind"], "display")
        with self.assertRaises(PipelineError):
            validate_proof(proof.replace("x=1\n\\]", " x=1\n\\]"), problem)

    def test_currency_escaped_dollars_and_code_are_never_reinterpreted(self):
        bad_pairs = [
            (r"Pay $5 and $10.", r"Pay \(5 and \)10."),
            (r"Pay $5-$10.", r"Pay \(5-\)10."),
            (r"Pay $5.00$10.00.", r"Pay \(5.00\)10.00."),
            (r"Pay \$5; let $x=1$.", r"Pay \(5\); let \(x=1\)."),
            (r"Literal `$x$`; let $y=2$.", r"Literal `\(x\)`; let \(y=2\)."),
            ("```\n$x$\n```\nLet $y=2$.", "```\n\\(x\\)\n```\nLet \\(y=2\\)."),
        ]
        for problem, statement in bad_pairs:
            proof = "# theorem thm:target\n## statement\n" + statement + "\n## proof\nProof."
            with self.subTest(problem=problem), self.assertRaises(PipelineError):
                validate_proof(proof, problem)
        for problem, statement in (
            (r"Pay \$5; let $x=1$.", r"Pay \$5; let \(x=1\)."),
            (r"Literal `$x$`; let $y=2$.", r"Literal `$x$`; let \(y=2\)."),
        ):
            proof = "# theorem thm:target\n## statement\n" + statement + "\n## proof\nProof."
            self.assertEqual(validate_proof(proof, problem)["statement_match"], "cosmetic_equivalent")

    def test_unpaired_mixed_and_multiple_dollar_delimiters_fail_closed(self):
        problem = r"Let $x=1$ and $y=2$."
        for statement in (r"Let \(x=1 and \(y=2\).", r"Let \(x=1$ and \(y=2\).",
                          r"Let $$$x=1$$$ and \(y=2\).", r"Let \(x=1\] and \(y=2\)."):
            proof = "# theorem thm:target\n## statement\n" + statement + "\n## proof\nProof."
            with self.subTest(statement=statement), self.assertRaises(PipelineError):
                validate_proof(proof, problem)

    def test_structure_audit_preserves_raw_candidate_and_does_not_skip_math_review(self):
        self.problem = r"Find $x+x$ when $x=1$."
        (self.root / "problem.txt").write_text(self.problem, encoding="utf-8")
        statement = r"Find \(x+x\) when \(x=1\)."
        proof = "# theorem thm:target\n## statement\n" + statement + "\n## proof\nx+x=1+1=2."
        result, caller = self.run_flow([proof, report(), self.teaching, report()])
        self.assertEqual(result["status"], "pending_final_review")
        self.assertEqual(len(caller.calls), 4)
        self.assertIn(proof, caller.calls[1]["prompt"])
        record = self.record(result)
        self.assertEqual(record["iterations"][0]["answer"], proof)
        self.assertEqual(Path(result["work_dir"]).joinpath("blueprint_verified.md").read_text(encoding="utf-8"), proof)
        saved = record["iterations"][0]["settings"]["structural_validation"]
        self.assertEqual(saved["statement_match"], "cosmetic_equivalent")
        self.assertTrue(any(a["source"].endswith("host.structure-validation.json") for a in record["artifacts"]))

    def test_rejected_structure_has_an_audit_and_stops_before_math_verifier(self):
        result, caller = self.run_flow([self.proof.replace(self.problem, self.problem + "另加x=0。")])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(caller.calls), 1)
        saved = self.record(result)["iterations"][0]["settings"]["structural_validation"]
        self.assertFalse(saved["accepted"])
        self.assertFalse(saved["proof_text_rewritten"])
        self.assertEqual(saved["mathematical_verdict"], "not_run")
        self.assertIn("完整原题", saved["error"])

    def test_actual_model_mismatch_or_missing_identity_fail_closed(self):
        for overrides in ({"actual_model": "other-model"}, {"actual_model": "未记录"},
                          {"actual_provider": "other-provider"}, {"session_id": None},
                          {"tool_events": [{"name": "pwsh"}]}, {"answer": ""}):
            with self.subTest(overrides=overrides):
                result, caller = self.run_flow([{**overrides, "answer": overrides.get("answer", self.proof)}])
                self.assertEqual(result["status"], "failed")
                self.assertEqual(len(caller.calls), 1)
                expected = overrides.get("actual_model", "deepseek-flash")
                self.assertEqual(self.record(result)["model"], expected)

    def test_reused_session_is_not_an_independent_verification(self):
        result, caller = self.run_flow([self.proof, {"answer": report(), "session_id": "independent-session-1"}])
        self.assertEqual(result["status"], "failed")
        self.assertIn("独立会话", result["error"])
        self.assertEqual(len(caller.calls), 2)

    def test_timeout_and_keyboard_interrupt_are_recorded_without_more_calls(self):
        for reply, expected in (({"status": "timeout", "error": "超时"}, "timeout"),
                                (KeyboardInterrupt(), "interrupted")):
            with self.subTest(expected=expected):
                result, caller = self.run_flow([reply])
                self.assertEqual(result["status"], expected)
                self.assertEqual(len(caller.calls), 1)
                self.assertEqual(self.record(result)["status"], expected)
                if expected == "interrupted":
                    self.assertEqual(self.record(result)["model"], "未记录")
                    self.assertTrue(any(x["source"].endswith("partial.log") for x in self.record(result)["artifacts"]))

    def test_solve_and_review_share_one_monotonic_budget(self):
        clock = [10.0]
        fake = FakeCalls([self.proof])
        def caller(**kwargs):
            result = fake(**kwargs)
            clock[0] += 900
            return result
        with patch("deepseek_rethlas.pipeline.time.monotonic", side_effect=lambda: clock[0]):
            result = run_pipeline(self.root, "problem.txt", "requirements.txt", "user.txt", caller=caller)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(len(fake.calls), 1)

    def test_teaching_wrong_does_not_enter_final_review(self):
        result, caller = self.run_flow([self.proof, report(), self.teaching, report(False)])
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(len(caller.calls), 4)
        self.assertNotIn("final_review_request_file", result)
        self.assertEqual(self.record(result)["iterations"][2]["verification"]["verdict"], "wrong")

    def test_external_paper_without_retrieval_cannot_be_accepted(self):
        proof = self.proof + "\n依据 https://example.org/paper 中的结果。"
        result, caller = self.run_flow([proof, report()])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(caller.calls), 2)
        self.assertIn("没有检索证据", result["error"])

    def test_installation_fixture_is_not_in_daily_index(self):
        result, _ = self.run_flow(config={"record_kind": "installation_test"})
        record = self.record(result)
        self.assertEqual(record["record_kind"], "installation_test")
        self.assertNotIn(result["run_id"], (self.root / "docs/实验索引.md").read_text(encoding="utf-8"))

    def test_dry_run_is_read_only_and_root_config_is_accepted(self):
        config = json.loads((ROOT / "setup/deepseek-rethlas.json").read_text(encoding="utf-8"))
        validate_config(config)
        caller = FakeCalls([])
        before = sorted(x.relative_to(self.root) for x in self.root.rglob("*") if x.is_file())
        result = run_pipeline(self.root, "problem.txt", "requirements.txt", "user.txt", config=config,
                              dry_run=True, caller=caller)
        self.assertEqual(result["status"], "dry_run")
        self.assertFalse(result["called_model"])
        self.assertEqual(caller.calls, [])
        self.assertEqual(before, sorted(x.relative_to(self.root) for x in self.root.rglob("*") if x.is_file()))

    def test_unsafe_input_and_unbounded_config_rejected_before_call(self):
        for config in ({"max_rounds": 4}, {"timeout_seconds": 901}, {"provider": "other"},
                       {"allow_provider_fallback": True}, {"api_key": "do-not-store"}):
            with self.subTest(config=config), self.assertRaises(PipelineError):
                self.run_flow(config=config)
        with self.assertRaises(PipelineError):
            run_pipeline(self.root, "../outside.txt", "requirements.txt", "user.txt", dry_run=True)
        self.assertFalse((self.root / "docs/experiments").exists())


if __name__ == "__main__":
    unittest.main()
