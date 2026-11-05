"""Scope-safe meters and hash-bound legacy recovery; no model calls."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import unittest
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from runtime_metrics import runtime_summary
from runtime_observations import load_observation


class RuntimeMetersTests(unittest.TestCase):
    def test_dsh_failed_stage_retains_attempts_without_inventing_tokens(self):
        r=runtime_summary({"evaluation":{"usage":{"observed_deepseek_roles":{"host_role_invocations":1,"total_tokens":None,"unmetered_roles":["solve-1"]},"observed_host_staged_wall_seconds":2600.377,"host_timing_scope":"Actual host wait, not model inference"}}})
        self.assertIsNone(r["total_tokens"])
        self.assertEqual(r["calls"],1)
        self.assertEqual(r["seconds"],2600.377)
        self.assertEqual(r["seconds_scope"],"Actual host wait, not model inference")

    def test_dsh_subtotal_survives_unknown_reviewer_and_preserves_cache_convention(self):
        record={"model":"flash","iterations":[{"settings":{"stage":"final_review"}}],"evaluation":{"usage":{
            "total_tokens":None,"observed_deepseek_roles":{"input_tokens_dsh_field":5015,"output_tokens":12924,"cached_input_tokens":1024,"total_tokens":18963,"host_role_invocations":4},
            "observed_host_staged_wall_seconds":99.6400022}}}
        original=deepcopy(record); r=runtime_summary(record)
        self.assertEqual((r["total_tokens"],r["seconds"],r["calls"]),(18963,99.6400022,4))
        self.assertEqual(r["input_tokens"]+r["output_tokens"]+r["cached_input_tokens"],r["total_tokens"])
        self.assertEqual(r["coverage"],"partial"); self.assertEqual(record,original)

    def test_failed_native_estimate_is_not_actual_or_zero_but_attempt_count_remains(self):
        r=runtime_summary({"model":"未记录","evaluation":{"roles":[{"configured_model":"flash","actual_model":"未记录"}],"usage":{
            "estimated":True,"estimated_usage":{"total_tokens":3514},"native_ledger_attempted_calls":1,"wall_seconds":50.1398}}})
        self.assertIsNone(r["total_tokens"]); self.assertEqual(r["estimated_total_tokens"],3514)
        self.assertEqual(r["calls"],1);self.assertEqual(r["seconds"],50.1398)
        self.assertIn("配置 flash",r["model"]);self.assertEqual(r["actual_model"],"未记录")

    def test_api_cache_is_input_subset_not_added_to_total(self):
        r=runtime_summary({"evaluation":{"usage":{"input_tokens":38539,"output_tokens":2198,"cached_input_tokens":36992,"total_tokens":40737,"native_ledger_attempted_calls":5}}})
        self.assertEqual(r["total_tokens"],40737)
        self.assertEqual(r["input_tokens"]+r["output_tokens"],r["total_tokens"])

    def test_no_bookkeeping_date_runtime_or_missing_as_zero(self):
        r=runtime_summary({"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-02T00:00:00Z"})
        self.assertIsNone(r["seconds"]);self.assertIsNone(r["total_tokens"])
        zero=runtime_summary({"iterations":[{"settings":{"timing":{"duration_seconds":0},"usage":{"total_tokens":0}}}]})
        self.assertEqual((zero["seconds"],zero["total_tokens"]),(0,0))

    def test_actual_run_dates_and_dsh_camelcase_are_supported(self):
        r=runtime_summary({"iterations":[{"settings":{"started_at":"2026-01-01T00:00:00Z","finished_at":"2026-01-01T00:00:08Z", "usage":{"total":{"inputTokens":3,"outputTokens":2,"cacheReadTokens":4,"totalTokens":9}}}}]})
        self.assertEqual((r["seconds"],r["total_tokens"],r["cached_input_tokens"]),(8,9,4))


class LegacyRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.base=Path(__file__).resolve().parents[2]/"work/test-runtime-observations"
        self.root=self.base/uuid.uuid4().hex
        self.folder=self.root/"docs/experiments/Q/A/R"
        self.source=self.folder/"evidence/original.jsonl";self.source.parent.mkdir(parents=True)
        self.source.write_text('{"type":"turn.completed","usage":{"input_tokens":8,"output_tokens":2}}\n',encoding="utf-8")
        digest=hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.record={"question_id":"Q","ai_id":"A","run_id":"R","model":"actual","artifacts":[{"snapshot":"evidence/original.jsonl","sha256":digest}]}
        self.path=self.folder/"record.json";self.path.write_text(json.dumps(self.record),encoding="utf-8")
        self.sidecar=self.root/"docs/runtime-observations/Q/A/R.json";self.sidecar.parent.mkdir(parents=True)
        self.data={"schema_version":1,"identity":{k:self.record[k] for k in ("question_id","ai_id","run_id")},
                   "record_sha256":hashlib.sha256(self.path.read_bytes()).hexdigest(),"sources":[{"project_relative_file":self.source.relative_to(self.root).as_posix(),"sha256":digest}],
                   "runtime_observations":{"usage":{"input_tokens":8,"output_tokens":2,"cached_input_tokens":5,"total_tokens":10},"seconds":7,"seconds_scope":"原始生成run","actual_model":"actual","scope":"归档生成，非全系统","calls":None,"recorded_turns":1}}
        self.save()
    def save(self):
        self.sidecar.write_text(json.dumps(self.data),encoding="utf-8")
    def tearDown(self):
        self.assertTrue(self.root.resolve().is_relative_to(self.base.resolve()));shutil.rmtree(self.root)
    def test_recovery_is_verified_and_render_only(self):
        original=self.path.read_bytes();obs,error=load_observation(self.root,self.path,self.record)
        self.assertIsNone(error);r=runtime_summary({**self.record,"runtime_observations":obs})
        self.assertEqual((r["total_tokens"],r["seconds"]),(10,7));self.assertEqual(original,self.path.read_bytes())
        self.assertIsNone(r["calls"]);self.assertIn("1 个turn",r["calls_label"])
    def test_stale_record_and_changed_source_fail_closed(self):
        self.source.write_text("changed",encoding="utf-8");obs,error=load_observation(self.root,self.path,self.record)
        self.assertIsNone(obs);self.assertIn("SHA256",error)
        self.path.write_text("{}",encoding="utf-8");obs,error=load_observation(self.root,self.path,self.record)
        self.assertIsNone(obs);self.assertIn("旧record版本",error)
    def test_unregistered_or_cross_record_source_is_never_used(self):
        self.data["sources"][0]["project_relative_file"]="docs/experiments/elsewhere.json";self.save()
        target=self.root/"docs/experiments/elsewhere.json";target.write_text("not in this record",encoding="utf-8")
        obs,error=load_observation(self.root,self.path,self.record)
        self.assertIsNone(obs);self.assertIn("超出",error)


if __name__=="__main__":unittest.main()
