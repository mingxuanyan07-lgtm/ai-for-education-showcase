"""Reading-only hybrid stage classification; no model calls or real records."""
import copy
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from experiment_records import _markdown
from record_reading import render_reading

class HybridRecordViewTests(unittest.TestCase):
    def fixture(self):
        proof="证明草稿：\\[x=1\\]"
        return {"question_id":"fixture","ai_id":"hybrid-rethlas-deeptutor-fixture-v1", "run_id":"fixture",
            "framework":"hybrid-rethlas-deeptutor-v1 / knowledge-route-v1", "question":"安装展示替身。", "requirements":"中文。",
            "user_prompt":"用户原文。", "status":"failed", "record_kind":"installation_test", "final_answer":proof,
            "iterations":[{"iteration_id":"solve-1","prompt":"actual solver instruction", "answer":proof,"settings":{"stage":"solve-1"}},
                {"iteration_id":"proof-review-1","prompt":"actual review instruction with "+json.dumps({"block":"\\[\nx=1\n\\]"}),
                 "answer":json.dumps({"verdict":"correct","quote":"\\[\nx=1\n\\]"}),"settings":{"stage":"proof-review-1"}}]}

    def test_review_json_is_evidence_not_a_new_user_prompt_iteration(self):
        record=self.fixture();before=copy.deepcopy(record);body=_markdown(record)
        self.assertIn("用户原文。",body);self.assertIn("非最终教学稿",body)
        self.assertNotIn("### Prompt 迭代",body);self.assertNotIn("actual review instruction",body)
        self.assertIn("[原始记录](record.json)",body);self.assertEqual(record,before)
        page=render_reading(body,"fixture")
        self.assertIn("<math",page);self.assertNotIn('data-math-render-error="true"',page)

    def test_teaching_keeps_outer_stage_label_and_does_not_become_prompt_round_two(self):
        record=self.fixture();record["iterations"].append({"iteration_id":"teaching-1","prompt":"actual teaching instruction",
            "answer":"完整教学稿。","settings":{"stage":"teaching-1"}})
        before=copy.deepcopy(record);body=_markdown(record)
        self.assertIn("核验后的中文教学整理",body);self.assertIn("完整教学稿。",body)
        self.assertNotIn("### Prompt 迭代",body);self.assertEqual(record,before)

    def test_unrelated_direct_workflow_retains_its_existing_iteration_view(self):
        record=self.fixture();record.update(ai_id="codex-direct",framework="direct")
        body=_markdown(record)
        self.assertIn("### Prompt 迭代（第 2 轮）",body)
        self.assertIn("actual review instruction",body)

if __name__=="__main__":unittest.main()
