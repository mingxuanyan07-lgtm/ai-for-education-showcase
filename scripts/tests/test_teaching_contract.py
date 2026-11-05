"""Offline teaching-contract boundaries, bounded repairs and final-review binding.

All model replies are synthetic fixtures. These tests do not measure pedagogy,
call a provider, or modify the project's real experiment records.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from deepseek_rethlas import prompts, teaching_contract as pedagogy
from deepseek_rethlas.final_review import finalize_review
from deepseek_rethlas.pipeline import PipelineError, run_pipeline, validate_config, validate_report
import test_deepseek_pipeline as base

FakeCalls = base.FakeCalls
report = base.report


def teaching_review(text, *, revise=False):
    checks = [{"criterion": key, "status": "met", "evidence": [text],
               "reason": "合成检查证据，仅用于离线合同测试：" + key} for key in pedagogy.CRITERIA]
    if revise:
        checks[3].update(status="needs_revision", evidence=[], reason="缺少连贯的解题主线。")
    return {"contract_version": pedagogy.VERSION,
            "verdict": "revise" if revise else "pass", "checks": checks}


def teaching_report(text, *, revise=False, math_correct=True):
    payload = json.loads(report(math_correct))
    payload["teaching_review"] = teaching_review(text, revise=revise)
    return json.dumps(payload, ensure_ascii=False)


class TeachingContractTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[2]
        self.schema = json.loads((root / "Rethlas/agents/verification/schemas/verification_output.schema.json").read_text(encoding="utf-8"))
        self.text = "合成教学稿：学生背景未知；学习准备与解题路线见正文。"

    def test_schema_extension_preserves_original_math_contract(self):
        before = deepcopy(self.schema)
        result = validate_report(teaching_report(self.text, revise=True), self.schema,
                                 teaching=self.text, contract=pedagogy.VERSION)
        self.assertEqual(result["verdict"], "correct")
        self.assertEqual(result["teaching_review"]["verdict"], "revise")
        self.assertEqual(self.schema, before)
        with self.assertRaises(PipelineError):
            validate_report(teaching_report(self.text), self.schema)

    def test_missing_duplicate_or_unsupported_evidence_cannot_pass(self):
        base = json.loads(teaching_report(self.text))
        variants = []
        missing = deepcopy(base)
        del missing["teaching_review"]
        variants.append(missing)
        duplicate = deepcopy(base)
        duplicate["teaching_review"]["checks"][-1] = duplicate["teaching_review"]["checks"][0]
        variants.append(duplicate)
        fake_quote = deepcopy(base)
        fake_quote["teaching_review"]["checks"][0]["evidence"] = ["稿件不存在的知识说明"]
        variants.append(fake_quote)
        no_quote = deepcopy(base)
        no_quote["teaching_review"]["checks"][0]["evidence"] = []
        variants.append(no_quote)
        empty_reason = deepcopy(base)
        empty_reason["teaching_review"]["checks"][0]["reason"] = "  "
        variants.append(empty_reason)
        inconsistent = deepcopy(base)
        inconsistent["teaching_review"]["checks"][0]["status"] = "needs_revision"
        variants.append(inconsistent)
        for payload in variants:
            with self.subTest(payload=payload), self.assertRaises(PipelineError):
                validate_report(json.dumps(payload), self.schema,
                                teaching=self.text, contract=pedagogy.VERSION)

    def test_quotes_can_normalize_crlf_but_not_invent_whitespace(self):
        value = teaching_review("原文\r\n第二行")
        pedagogy.validate_review(value, "原文\n第二行")
        with self.assertRaises(ValueError):
            pedagogy.validate_review(value, "原 文\n第二行")

    def test_rendered_or_paraphrased_quotes_still_fail_literal_contract(self):
        source = """对于任意一个已经排好的四人骨架，沿圆周看，相邻的两人之间都有一个空隙：
\\[
A\\text{ 与 }B,\\quad B\\text{ 与 }C,
\\]
之间，共四个空隙。"""
        excerpt = "对于任意一个已经排好的四人骨架，沿圆周看，相邻的两人之间都有一个空隙：A 与 B, B 与 C,"
        value = teaching_review(excerpt)
        with self.assertRaisesRegex(ValueError, "引文不在"):
            pedagogy.validate_review(value, source)
        comparison = pedagogy.quote_comparison_report(value, source)
        self.assertTrue(comparison["comparison_only_not_a_verdict"])
        self.assertTrue(all(x["comparison_status"] == "not_found" for x in comparison["comparisons"]))
        self.assertEqual(value["checks"][0]["evidence"], [excerpt])

    def test_quote_comparison_retains_math_symbols_and_only_canonicalizes_crlf(self):
        value = teaching_review("原文\r\n第二行")
        audit = pedagogy.quote_comparison_report(value, "原文\n第二行")
        self.assertEqual(audit["quote_policy"], "literal-substring-crlf-v1")
        self.assertTrue(all(x["comparison_status"] == "crlf_only" for x in audit["comparisons"]))
        for quote in ("x=2", "x^2=2", "x₂=2", "原 文"):
            with self.subTest(quote=quote):
                report = teaching_review(quote)
                comparison = pedagogy.quote_comparison_report(report, "x^2=3；原文")
                self.assertFalse(any(x["canonical_crlf_match"] for x in comparison["comparisons"]))
                with self.assertRaises(ValueError):
                    pedagogy.validate_review(report, "x^2=3；原文")

    def test_quote_bank_is_bounded_deterministic_and_source_exact(self):
        source = "\n".join(f"第{n}段：原始公式 \\(x_{n}^2=3\\) 与标点必须保留。" + "内容" * 120 for n in range(100))
        bank = pedagogy.quote_candidates(source)
        self.assertEqual(bank, pedagogy.quote_candidates(source))
        self.assertEqual(len(bank), pedagogy.QUOTE_BANK_MAX_ENTRIES)
        for item in bank:
            self.assertLessEqual(len(item["quote"]), pedagogy.QUOTE_BANK_MAX_CHARS)
            self.assertEqual(source[item["start_char"]:item["end_char"]], item["quote"])
            self.assertEqual(hashlib.sha256(item["quote"].encode()).hexdigest(), item["quote_sha256"])
        self.assertGreater(bank[-1]["start_char"], len(source) // 2)

    def test_generation_and_independent_review_cover_the_actual_request(self):
        text = prompts.teaching_prompt("原题", "原证明", "用户明确要求的格式", contract=pedagogy.VERSION)
        for fragment in ("知识点—需要会什么—在本题哪一步使用", "学生已学／未学范围未知",
                         "不能要求学生预先掌握", "用户明确指定的章节顺序", "各步怎样接起来"):
            self.assertIn(fragment, text)
        review = prompts.verification_prompt("原题", self.text, teaching=True, contract=pedagogy.VERSION)
        for key in pedagogy.CRITERIA:
            self.assertIn(key, review)
        self.assertIn("数学 verdict 只反映数学正确性", review)
        self.assertIn("不能仅凭标题或关键词存在", review)
        self.assertIn(pedagogy.PROMPT_REVISION, review)
        self.assertIn("逐字可复制引文候选", review)
        self.assertIn(hashlib.sha256(self.text.encode()).hexdigest(), review)
        self.assertIn("不能将candidate_id本身作为evidence", review)
        self.assertNotIn("teaching_review", prompts.verification_prompt("原题", "原证明"))

    def test_unknown_version_is_not_silently_downgraded(self):
        with self.assertRaises(PipelineError):
            validate_config({"teaching_contract": "unknown-version"})


class KnowledgeRoutePipelineTests(unittest.TestCase):
    setUp = base.PipelineTests.setUp
    tearDown = base.PipelineTests.tearDown
    run_flow = base.PipelineTests.run_flow
    record = base.PipelineTests.record
    config = {"teaching_contract": pedagogy.VERSION, "record_kind": "installation_test"}

    def run_new(self, replies):
        return self.run_flow(replies, config=self.config)

    def test_optional_version_has_separate_identity_and_no_extra_success_calls(self):
        result, caller = self.run_new([self.proof, report(), self.teaching, teaching_report(self.teaching)])
        self.assertEqual(result["status"], "pending_final_review")
        self.assertEqual(len(caller.calls), 4)
        self.assertTrue(result["ai_id"].endswith("-kr1"))
        self.assertNotIn("knowledge-route-v1", caller.calls[0]["prompt"])
        self.assertIn("knowledge-route-v1", caller.calls[2]["prompt"])
        record = self.record(result)
        self.assertIn("knowledge_inventory", record["task_review"])
        self.assertIn("需人工确认", record["task_review"])
        request = json.loads(Path(result["final_review_request_file"]).read_text(encoding="utf-8"))
        self.assertEqual(request["teaching_contract"], pedagogy.VERSION)
        self.assertTrue(any(a["source"].endswith("teaching-1.pedagogy.json") for a in record["artifacts"]))
        quote_audit = Path(result["work_dir"]) / "teaching-1.quote-comparison.json"
        comparison = json.loads(quote_audit.read_text(encoding="utf-8"))
        self.assertTrue(comparison["comparison_only_not_a_verdict"])
        self.assertTrue(all(x["literal_match"] for x in comparison["comparisons"]))

    def test_quote_audit_is_preserved_when_raw_review_fails(self):
        bad = json.loads(teaching_report(self.teaching))
        bad["teaching_review"]["checks"][0]["evidence"] = ["不存在的逐字引用"]
        raw = json.dumps(bad, ensure_ascii=False)
        result, caller = self.run_new([self.proof, report(), self.teaching, raw])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(caller.calls), 4)
        record = self.record(result)
        self.assertEqual(record["iterations"][3]["answer"], raw)
        comparison = json.loads((Path(result["work_dir"]) / "teaching-1.quote-comparison.json").read_text(encoding="utf-8"))
        self.assertEqual(comparison["comparisons"][0]["comparison_status"], "not_found")
        self.assertEqual(record["iterations"][3]["settings"]["teaching_quote_policy"], pedagogy.QUOTE_POLICY)
        self.assertTrue(any(a["source"].endswith("teaching-1.quote-comparison.json") for a in record["artifacts"]))

    def test_mathematically_correct_draft_with_no_mainline_is_repaired_and_preserved(self):
        second = self.teaching + "\n修订后的合成路线说明。"
        result, caller = self.run_new([self.proof, report(), self.teaching,
            teaching_report(self.teaching, revise=True), second, teaching_report(second)])
        self.assertEqual(result["status"], "pending_final_review")
        self.assertEqual(len(caller.calls), 6)
        self.assertIn("缺少连贯的解题主线", caller.calls[4]["prompt"])
        self.assertIn(self.teaching, caller.calls[4]["prompt"])
        record = self.record(result)
        first, last = record["iterations"][2], record["iterations"][4]
        self.assertEqual(first["answer"], self.teaching)
        self.assertEqual(first["verification"]["verdict"], "correct")
        self.assertEqual(first["verification"]["teaching_review"]["verdict"], "revise")
        self.assertEqual(last["answer"], second)
        self.assertEqual(last["settings"]["stage"], "post_verification_teaching")
        self.assertNotIn("prompt_iteration", last["settings"])
        request = json.loads(Path(result["final_review_request_file"]).read_text(encoding="utf-8"))
        target = request["artifacts"]["teaching"]
        self.assertEqual((self.root / target["file"]).read_text(encoding="utf-8"), second)
        self.assertEqual(hashlib.sha256((self.root / target["file"]).read_bytes()).hexdigest(), target["sha256"])

    def test_two_failed_teaching_checks_stop_without_final_handoff(self):
        result, caller = self.run_new([self.proof, report()] +
            [self.teaching, teaching_report(self.teaching, revise=True)] * 2)
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(len(caller.calls), 6)
        self.assertNotIn("final_review_request_file", result)
        self.assertEqual(self.record(result)["verification"][-1]["verdict"], "correct")
        self.assertEqual(self.record(result)["verification"][-1]["teaching_review"]["verdict"], "revise")

    def test_math_failure_cannot_be_overruled_by_pedagogy_pass(self):
        result, caller = self.run_new([self.proof, report()] +
            [self.teaching, teaching_report(self.teaching, math_correct=False)] * 2)
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(len(caller.calls), 6)
        self.assertNotIn("final_review_request_file", result)

    def test_missing_teaching_review_fails_closed_without_unbounded_repair(self):
        result, caller = self.run_new([self.proof, report(), self.teaching, report()])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(caller.calls), 4)
        self.assertEqual(self.record(result)["iterations"][3]["answer"], report())

    def test_repair_does_not_reset_shared_teaching_deadline(self):
        clock = [10.0]
        fake = FakeCalls([self.proof, report(), self.teaching, teaching_report(self.teaching, revise=True)])
        def caller(**kwargs):
            response = fake(**kwargs)
            if len(fake.calls) == 4:
                clock[0] += 900
            return response
        with patch("deepseek_rethlas.pipeline.time.monotonic", side_effect=lambda: clock[0]):
            result = run_pipeline(self.root, "problem.txt", "requirements.txt", "user.txt",
                                  config=self.config, caller=caller)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(len(fake.calls), 4)

    def test_new_version_dry_run_is_read_only_and_declares_teaching_budget(self):
        before = sorted(p.relative_to(self.root) for p in self.root.rglob("*") if p.is_file())
        result = run_pipeline(self.root, "problem.txt", "requirements.txt", "user.txt",
                              config=self.config, dry_run=True)
        self.assertEqual(result["teaching_plan"]["max_rounds"], 2)
        self.assertEqual(result["teaching_plan"]["shared_timeout_seconds"], 900)
        self.assertFalse(result["called_model"])
        self.assertEqual(before, sorted(p.relative_to(self.root) for p in self.root.rglob("*") if p.is_file()))

    def make_final(self, *, revise=False, repaired=False):
        if repaired:
            second = self.teaching + "\n修订后的合成路线。"
            replies = [self.proof, report(), self.teaching, teaching_report(self.teaching, revise=True),
                       second, teaching_report(second)]
        else:
            second = self.teaching
            replies = [self.proof, report(), second, teaching_report(second)]
        result, _ = self.run_new(replies)
        request_path = Path(result["final_review_request_file"])
        request = json.loads(request_path.read_text(encoding="utf-8"))
        final = {"review_request_sha256": hashlib.sha256(request_path.read_bytes()).hexdigest(),
                 "verdict": "correct", "critical_errors": [], "gaps": [],
                 "verifier": {"verifier_id": "codex-current-chat", "source": "current_codex_chat", "model": "未记录"},
                 "summary": "合成终审，仅用于离线绑定测试。", "task_review": "自动分析，需人工确认。",
                 "checks": {key: "合成的覆盖说明" for key in (
                     "problem_conditions", "proof", "deepseek_reports", "teaching_math", "requirements",
                     "knowledge_requirements", "solution_route")},
                 "teaching_review": teaching_review(second, revise=revise)}
        final_path = self.root / "synthetic-final-review.json"
        final_path.write_text(json.dumps(final, ensure_ascii=False), encoding="utf-8")
        return result, request_path, self.root / request["reviewer"]["prompt"]["file"], final_path, final

    def test_final_review_checks_last_repaired_draft_and_preserves_prior_answers(self):
        result, request, prompt, final_path, _ = self.make_final(repaired=True)
        finalized = finalize_review(self.root, request, prompt, final_path)
        self.assertEqual(finalized["status"], "completed")
        record = self.record(result)
        self.assertEqual(record["iterations"][2]["answer"], self.teaching)
        self.assertEqual(record["iterations"][4]["answer"], self.teaching + "\n修订后的合成路线。")
        self.assertEqual(record["verification"][-1]["teaching_review"]["verdict"], "pass")
        self.assertEqual(record["verification"][-1]["verdict"], "correct")

    def test_final_pedagogy_revision_keeps_math_correct_but_bundle_unverified(self):
        result, request, prompt, final_path, _ = self.make_final(revise=True)
        finalized = finalize_review(self.root, request, prompt, final_path)
        self.assertEqual(finalized["status"], "unverified")
        self.assertEqual(finalized["final_verdict"], "correct")
        self.assertEqual(finalized["final_teaching_verdict"], "revise")
        self.assertEqual(self.record(result)["verification"][-1]["verification_report"]["gaps"], [])

    def test_final_review_cannot_omit_knowledge_and_route_coverage(self):
        _, request, prompt, final_path, final = self.make_final()
        del final["checks"]["knowledge_requirements"]
        final_path.write_text(json.dumps(final, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "coverage missing"):
            finalize_review(self.root, request, prompt, final_path)


if __name__ == "__main__":
    unittest.main()
