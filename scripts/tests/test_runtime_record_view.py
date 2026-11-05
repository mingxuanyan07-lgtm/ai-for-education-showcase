"""Readable model/time/token evidence without changing prompts or raw records."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmark_evaluation import record_summary, detail_markdown, _role_reading
from record_reading import render_reading


class RuntimeRecordViewTests(unittest.TestCase):
    def record(self):
        return {
            "question_id": "Q-runtime", "ai_id": "rethlas-dsh-fixture", "run_id": "run-fixture",
            "model": "deepseek-flash", "status": "completed", "iterations": [],
            "evaluation": {
                "roles": [
                    {"role_id": "solve-1", "role": "solve-1", "configured_model": "deepseek-flash",
                     "actual_model": "deepseek-flash", "provider": "deepseek-account", "reasoning_effort": "max",
                     "duration_seconds": 17.775, "input_tokens": 8000, "output_tokens": 10000,
                     "cached_input_tokens": 963, "total_tokens": 18963, "calls": 1},
                    {"role_id": "current-final", "role": "final_review", "configured_model": "未记录",
                     "actual_model": "未记录", "duration_seconds": None, "input_tokens": None,
                     "output_tokens": None, "cached_input_tokens": None, "total_tokens": None, "calls": None},
                ],
                "usage": {
                    "calls": None, "input_tokens": None, "output_tokens": None,
                    "total_tokens": None, "wall_seconds": None, "cost": None,
                    "observed_deepseek_roles": {"host_role_invocations": 4, "input_tokens_dsh_field": 8000,
                        "output_tokens": 10000, "cached_input_tokens": 963, "total_tokens": 18963},
                    "observed_host_staged_wall_seconds": 99.6400022,
                    "host_timing_scope": "staged flow only; excludes current-chat final and common review",
                },
            },
        }

    def test_record_summary_puts_known_metrics_before_review_and_no_json_dump(self):
        record = self.record()
        before = copy.deepcopy(record)
        body = "\n".join(record_summary(record))
        self.assertIn("deepseek-flash", body)
        self.assertIn("99.64", body)
        self.assertIn("18,963", body)
        self.assertLess(body.index("18,963"), body.index("统一评测摘要"))
        self.assertNotIn('"observed_deepseek_roles"', body)
        self.assertNotIn('"total_tokens": null', body)
        self.assertIn("费用未记录", body)
        self.assertEqual(record, before)

    def test_detail_tables_keep_measured_roles_when_final_review_is_missing(self):
        record = self.record()
        before = copy.deepcopy(record)
        body = detail_markdown(record)
        readable = body.split("## 原始结构化详情")[0]
        self.assertIn("## 各角色模型", readable)
        self.assertIn("## 各角色耗时与 Token", readable)
        self.assertIn("实际响应模型", readable)
        self.assertIn(">17.77<", render_reading(readable, "runtime fixture"))
        self.assertIn("8,000", readable)
        self.assertIn("10,000", readable)
        self.assertIn("963", readable)
        self.assertIn("18,963", readable)
        self.assertIn("final\\_review", readable)
        self.assertIn("缺失值不是零", readable)
        self.assertIn('"observed_deepseek_roles"', body)
        self.assertEqual(record, before)

    def test_deeptutor_configuration_is_separate_from_unavailable_actual_model(self):
        record = self.record()
        record["ai_id"] = "deeptutor-api-fixture"
        record["model"] = "未记录"
        record["evaluation"] = {
            "roles": [{"role_id": "native", "role": "native", "configured_model": "deepseek-flash",
                       "actual_model": "未记录", "provider": "deepseek", "duration_seconds": 15.95,
                       "input_tokens": 38539, "output_tokens": 2198, "cached_input_tokens": 36992,
                       "total_tokens": 40737, "native_ledger_attempted_calls": 5}],
            "usage": {"wall_seconds": 19.562575, "input_tokens": 38539, "output_tokens": 2198,
                      "cached_input_tokens": 36992, "total_tokens": 40737, "estimated": False},
        }
        body = detail_markdown(record).split("## 原始结构化详情")[0]
        self.assertIn("配置", body)
        self.assertIn("deepseek-flash", body)
        self.assertIn("未取得实际响应型号", body)
        self.assertIn("19.56", body)
        self.assertIn("40,737", body)
        self.assertIn("5 条原生尝试账本", body)

    def test_role_metadata_is_literal_and_does_not_create_html_or_table_columns(self):
        record = self.record()
        record["evaluation"]["roles"][0]["configured_model"] = '<script>bad()</script>|[X](https://example.invalid)'
        html = render_reading(detail_markdown(record), "fixture")
        self.assertNotIn("<script>", html)
        self.assertNotIn('href="https://example.invalid"', html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("katex-error", html)

    def test_record_summary_shows_known_zero_fee_without_treating_it_as_missing(self):
        record = self.record()
        record["evaluation"]["usage"].update({"cost": 0, "currency": "CNY"})
        body = "\n".join(record_summary(record))
        self.assertIn("费用 0 CNY", body)
        self.assertNotIn("费用未记录", body)

    def test_no_metadata_explains_missing_values_without_fabricating_zero(self):
        record = {"question_id": "Q", "ai_id": "direct", "run_id": "R", "status": "failed",
                  "model": "未记录", "iterations": [], "evaluation": {}}
        body = detail_markdown(record)
        self.assertIn("未记录角色明细", body)
        self.assertIn("不能从回答长度推算Token", body)
        self.assertNotIn("| 0 |", body)

    def test_estimated_role_tokens_are_labeled_and_not_presented_as_api_actuals(self):
        record = self.record()
        record["model"] = "未记录"
        record["evaluation"] = {
            "roles": [{"role_id": "native", "configured_model": "deepseek-flash", "actual_model": "未记录",
                       "estimated": True, "input_tokens": None, "total_tokens": None,
                       "estimated_usage": {"prompt_tokens": 3514, "completion_tokens": 0, "total_tokens": 3514}}],
            "usage": {"estimated": True, "total_tokens": None,
                      "estimated_usage": {"prompt_tokens": 3514, "completion_tokens": 0, "total_tokens": 3514}},
        }
        body = detail_markdown(record).split("## 原始结构化详情")[0]
        self.assertIn("估计 3,514", body)
        self.assertIn("实际未记录", body)

    def test_legacy_iteration_dsh_usage_and_actual_timing_are_readable(self):
        record = self.record()
        record["model"] = "legacy-model"
        record["evaluation"] = {}
        record["iterations"] = [{"iteration_id": "old-solve", "settings": {
            "actual_model": "legacy-model", "configured_model": "legacy-model", "stage": "solve",
            "timing": {"duration_seconds": 2.25},
            "usage": {"total": {"inputTokens": 120, "outputTokens": 30, "cacheReadTokens": 50, "totalTokens": 200}},
        }}]
        body = detail_markdown(record).split("## 原始结构化详情")[0]
        html = render_reading(body, "legacy fixture")
        self.assertIn("legacy-model", html)
        self.assertIn(">2.25<", html)
        self.assertIn(">120<", html)
        self.assertIn(">30<", html)
        self.assertIn(">50<", html)
        self.assertIn(">200<", html)

    def legacy_recovered(self):
        return {
            "question_id": "Q3", "ai_id": "rethlas-legacy", "run_id": "R-legacy", "model": "gpt-6.1-sol",
            "status": "completed", "evaluation": {},
            "iterations": [
                {"iteration_id": "rethlas-0", "settings": {"model": "gpt-6.1-sol", "iteration": 0,
                    "session_id_after": "generation-thread", "timing": {"duration_seconds": 571.751085}}},
                {"iteration_id": "teaching-1", "settings": {"model": "未记录", "stage": "核验后的外层教学整理"}},
                {"iteration_id": "teaching-2", "settings": {"model": "未记录", "stage": "post_verification_teaching"}},
            ],
            "runtime_observations": {
                "usage": {"input_tokens": 1092100, "output_tokens": 7145, "cached_input_tokens": 1031936, "total_tokens": 1099245},
                "seconds": 571.751085, "actual_model": "gpt-6.1-sol", "calls": None, "recorded_turns": 1,
                "scope": "已校验首轮生成归档；不含教学和后续轮。", "seconds_scope": "原始生成run起止差值。",
                "deduplication": {"thread_id": "generation-thread"}, "evidence_files": ["archived-generation.jsonl"],
            },
        }

    def test_each_legacy_role_uses_own_model_and_never_inherits_record_model(self):
        record = self.legacy_recovered()
        record.pop("runtime_observations")
        rows = _role_reading(record)
        self.assertEqual(rows[0]["actual_model"], "gpt-6.1-sol")
        self.assertEqual([row["actual_model"] for row in rows[1:]], ["未记录", "未记录"])
        self.assertEqual([row["tokens"] for row in rows[1:]], [["未记录"] * 4, ["未记录"] * 4])

    def test_explicit_unknown_actual_model_does_not_fall_back_to_other_model_fields(self):
        record = self.legacy_recovered()
        record.pop("runtime_observations")
        record["iterations"][1]["settings"].update({"actual_model": "未记录", "model": "configuration-or-legacy-name"})
        rows = _role_reading(record)
        self.assertEqual(rows[1]["actual_model"], "未记录")

    def test_recovered_archive_only_fills_uniquely_matching_first_generation(self):
        record = self.legacy_recovered()
        before = copy.deepcopy(record)
        rows = _role_reading(record)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["tokens"], ["1,092,100", "7,145", "1,031,936", "1,099,245"])
        self.assertEqual(rows[0]["seconds"], "571.75")
        self.assertIn("归档 1 个turn", rows[0]["calls"])
        self.assertIn("唯一匹配", rows[0]["model_note"])
        for row in rows[1:]:
            self.assertEqual(row["actual_model"], "未记录")
            self.assertEqual(row["tokens"], ["未记录"] * 4)
            self.assertEqual(row["seconds"], "未记录")
        self.assertEqual(record, before)

    def test_ambiguous_later_or_teaching_session_match_gets_separate_recovery_row(self):
        for case in ("ambiguous", "later", "teaching", "no_thread"):
            with self.subTest(case=case):
                record = self.legacy_recovered()
                if case == "ambiguous":
                    record["iterations"].append({"iteration_id": "rethlas-1", "settings": {
                        "model": "未记录", "iteration": 1, "session_id_after": "generation-thread"}})
                elif case == "later":
                    record["iterations"][0]["settings"]["session_id_after"] = "first-unrelated"
                    record["iterations"].append({"iteration_id": "rethlas-1", "settings": {
                        "model": "未记录", "iteration": 1, "session_id_after": "generation-thread"}})
                elif case == "teaching":
                    record["iterations"][0]["settings"]["session_id_after"] = "first-unrelated"
                    record["iterations"][1]["settings"]["session_id_after"] = "generation-thread"
                else:
                    record["runtime_observations"].pop("deduplication")
                before = copy.deepcopy(record)
                rows = _role_reading(record)
                self.assertEqual(len(rows), len(record["iterations"]) + 1)
                self.assertIn("归档恢复的首轮生成", rows[-1]["role"])
                self.assertEqual(rows[-1]["tokens"][-1], "1,099,245")
                self.assertIn("不与其他行相加", rows[-1]["model_note"])
                for row in rows[:-1]:
                    self.assertEqual(row["tokens"], ["未记录"] * 4)
                self.assertEqual(record, before)

    def test_conflicting_recovery_preserves_existing_role_meter_and_stays_separate(self):
        record = self.legacy_recovered()
        record["iterations"][0]["settings"]["usage"] = {"input_tokens": 8, "output_tokens": 2, "total_tokens": 10}
        rows = _role_reading(record)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["tokens"], ["8", "2", "未记录", "10"])
        self.assertEqual(rows[-1]["tokens"][-1], "1,099,245")
        self.assertIn("计量不一致", rows[-1]["model_note"])


if __name__ == "__main__":
    unittest.main()
