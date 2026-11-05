"""Offline contracts: synthetic records/processes only; no model or real service."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from hybrid_tutor.bridge import Bridge, BridgeError, atomic_json, sha, worker, PARTIAL_FILES
from hybrid_tutor.server import accepted_origin, public_config


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.test_base = ROOT / "work/hybrid-integration-20261030/bridge-tests"
        self.test_base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=self.test_base)
        self.root = Path(self.temp.name).resolve()
        (self.root / "setup").mkdir()
        atomic_json(self.root / "setup/hybrid-tutor.json", {"proof": {"model": "deepseek-flash", "max_total_seconds": 120}, "teaching": {"model": "deepseek-flash"}})
        self.launches = []
        self.bridge = Bridge(self.root, launcher=lambda task, folder: self.launches.append(task))
        self.payload = dict(problem="测试替身题面：x=x", requirements="测试替身：中文讲解", user_prompt="离线合同测试，不是模型回答", idempotency_key="test_1", question_id="q_test", tags=["测试"], mode="dry_run")
        (self.root / ".venv/Scripts").mkdir(parents=True)
        (self.root / ".venv/Scripts/python.exe").write_bytes(b"offline-placeholder")

    def tearDown(self):
        self.assertTrue(self.root.resolve().is_relative_to(self.test_base.resolve()))
        self.temp.cleanup()

    def task(self, **updates):
        return self.bridge.start(**{**self.payload, **updates})

    def authorize(self, **updates):
        auth = {"schema_version": 1, "enabled": True, "config_sha256": sha(self.bridge.config_path.read_bytes()),
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), "max_runs": 1,
                "allowed_question_ids": ["q_test"], "authorization_id": "explicit_test_double",
                "allowed_input_sha256": {"q_test": {name: sha(self.payload[name].encode()) for name in ("problem", "requirements", "user_prompt")}}}
        atomic_json(self.bridge.authorization_path, {**auth, **updates})

    def make_record(self, run="run_1", status="failed"):
        record_id = "q_test/hybrid-rethlas-deeptutor-deepseek-flash-v1/" + run
        path = self.root / "docs/experiments" / record_id / "record.json"
        path.parent.mkdir(parents=True)
        record = {"question_id":"q_test", "ai_id":"hybrid-rethlas-deeptutor-deepseek-flash-v1", "run_id":run,
                  "question":self.payload["problem"], "requirements":self.payload["requirements"], "user_prompt":self.payload["user_prompt"],
                  "status":status, "record_kind":"math_experiment", "iterations":[{"answer":"<script>alert('x')</script>", "prompt":"测试替身"}]}
        atomic_json(path, record)
        return record_id, path, record

    def test_start_default_is_no_model_and_preserves_utf8(self):
        task = self.task()
        self.assertEqual(task["status"], "queued")
        self.assertFalse(task["called_model"])
        result = self.bridge.results(task["task_id"])
        self.assertEqual(result["inputs"]["problem"], self.payload["problem"])
        self.assertEqual(result["input_sha256"]["problem"], sha(self.payload["problem"].encode()))
        self.assertEqual(len(self.launches), 1)

    def test_identical_submission_is_idempotent_after_restart(self):
        first = self.task()
        second = Bridge(self.root, launcher=lambda *_: self.fail("duplicated process")).start(**self.payload)
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertTrue(second["reused"])

    def test_duplicate_id_with_changed_content_or_config_conflicts(self):
        self.task()
        with self.assertRaisesRegex(BridgeError, "冲突"):
            self.task(problem="changed")
        self.bridge.config_path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(BridgeError, "冲突"):
            self.task()

    def test_real_run_requires_host_auth_and_argument_cannot_create_it(self):
        with self.assertRaisesRegex(BridgeError, "尚未授权"):
            self.task(mode="run")
        with self.assertRaises(TypeError):
            self.bridge.start(**self.payload, allow_model_calls=True)
        self.assertEqual(self.launches, [])

    def test_auth_binds_all_original_inputs(self):
        self.authorize()
        for field in ("problem", "requirements", "user_prompt"):
            with self.assertRaisesRegex(BridgeError, "授权材料不匹配"):
                self.task(mode="run", **{field:"another text"})
        self.assertEqual(self.launches, [])

    def test_auth_count_includes_failed_submissions(self):
        self.authorize()
        first = self.task(mode="run")
        folder, _ = self.bridge._load(first["task_id"])
        atomic_json(folder / "state.json", {"status":"failed", "called_model":False})
        with self.assertRaisesRegex(BridgeError, "耗尽"):
            self.task(mode="run", idempotency_key="test_2")

    def test_expired_or_changed_config_authorization_rejected(self):
        self.authorize(expires_at="2020-01-01T00:00:00Z")
        with self.assertRaisesRegex(BridgeError, "过期"):
            self.task(mode="run")
        self.authorize(config_sha256="0" * 64)
        with self.assertRaisesRegex(BridgeError, "配置已变化"):
            self.task(mode="run")

    def test_malformed_ids_and_oversize_text_rejected(self):
        for bad in ("../x", "C:/Windows", "con", "q/test", "a."):
            with self.assertRaises(BridgeError):
                self.task(question_id=bad)
        with self.assertRaises(BridgeError):
            self.bridge.status("../state")
        with self.assertRaises(BridgeError):
            self.task(problem="a" * (256 * 1024 + 1))

    def test_changed_material_is_never_silently_used(self):
        task = self.task()
        folder, _ = self.bridge._load(task["task_id"])
        (folder / "problem.txt").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(BridgeError, "已变化"):
            self.bridge.results(task["task_id"])

    def test_cross_project_identity_rejected(self):
        task = self.task()
        folder, data = self.bridge._load(task["task_id"])
        data["project_root"] = str(self.root.parent)
        atomic_json(folder / "task.json", data)
        with self.assertRaisesRegex(BridgeError, "项目不匹配"):
            self.bridge.status(task["task_id"])

    def test_cancel_only_sets_owned_task_flag(self):
        task = self.task()
        with patch("hybrid_tutor.bridge.subprocess.run") as run:
            result = self.bridge.cancel(task["task_id"])
            run.assert_not_called()
        self.assertEqual(result["status"], "cancelling")
        self.assertIsNone(result["cancelled"])
        folder, _ = self.bridge._load(task["task_id"])
        self.assertTrue((folder / "cancel.request.json").is_file())

    def test_dead_worker_and_never_started_worker_are_reported(self):
        task = self.task()
        folder, data = self.bridge._load(task["task_id"])
        atomic_json(folder / "state.json", {"status":"running", "worker":{"pid":123, "creation_id":"a"}})
        with patch("hybrid_tutor.bridge.process_identity", return_value="different"):
            self.assertEqual(self.bridge.status(task["task_id"])["status"], "host_lost")
        atomic_json(folder / "state.json", {"status":"queued"})
        data["created_at"] = "2020-01-01T00:00:00Z"
        atomic_json(folder / "task.json", data)
        self.assertEqual(self.bridge.status(task["task_id"])["failure_kind"], "worker_not_started")

    def test_replay_is_read_only_and_has_provenance(self):
        record_id, path, record = self.make_record()
        before = path.read_bytes()
        replay = self.bridge.replay(record_id)
        self.assertEqual(replay["provenance"], "historical_replay")
        self.assertFalse(replay["called_model"])
        self.assertEqual(replay["record"]["iterations"][0]["answer"], record["iterations"][0]["answer"])
        self.assertEqual(before, path.read_bytes())
        self.assertFalse(self.bridge.tasks.exists())

    def test_history_requires_matching_file_identity(self):
        record_id, path, record = self.make_record()
        record["question_id"] = "other"
        atomic_json(path, record)
        with self.assertRaisesRegex(BridgeError, "身份不一致"):
            self.bridge.replay(record_id)
        self.assertEqual(self.bridge.history(), [])

    def test_final_status_cannot_be_promoted_by_record_status_alone(self):
        task = self.task()
        folder, _ = self.bridge._load(task["task_id"])
        _, _, record = self.make_record(status="completed")
        atomic_json(folder / "outcome.json", {key:record[key] for key in ("question_id", "ai_id", "run_id")})
        atomic_json(folder / "state.json", {"status":"pending_final_review"})
        result = self.bridge.status(task["task_id"])
        self.assertEqual(result["status"], "artifact_failure")
        self.assertEqual(result["failure_kind"], "final_review_binding_failure")

    def test_cancelled_without_outcome_retains_registered_partial_record(self):
        task = self.task()
        folder, data = self.bridge._load(task["task_id"])
        record_id, _, record = self.make_record(status="unverified")
        receipt = {key:record[key] for key in ("question_id", "ai_id", "run_id")}
        receipt["input_sha256"] = data["input_sha256"]
        atomic_json(folder / "run-receipt.json", receipt)
        atomic_json(folder / "state.json", {"status":"interrupted", "failure_kind":"user_cancelled", "called_model":None})
        result = self.bridge.results(task["task_id"])
        self.assertTrue(result["partial_record"])
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["record_id"], record_id)
        self.assertEqual(result["record"]["iterations"][0]["answer"], record["iterations"][0]["answer"])
        self.assertFalse((folder / "outcome.json").exists())

    def test_registered_partial_record_rejects_another_input_hash(self):
        task = self.task()
        folder, data = self.bridge._load(task["task_id"])
        _, _, record = self.make_record()
        receipt = {key:record[key] for key in ("question_id", "ai_id", "run_id")}
        receipt["input_sha256"] = {**data["input_sha256"], "problem":"0" * 64}
        atomic_json(folder / "run-receipt.json", receipt)
        with self.assertRaisesRegex(BridgeError, "运行登记"):
            self.bridge.results(task["task_id"])

    def partial_fixture(self):
        task = self.task()
        folder, data = self.bridge._load(task["task_id"])
        _, _, record = self.make_record(status="unverified")
        receipt = {key:record[key] for key in ("question_id", "ai_id", "run_id")}
        receipt["input_sha256"] = data["input_sha256"]
        receipt["work_dir"] = str(self.root / "untrusted-other-run")
        atomic_json(folder / "run-receipt.json", receipt)
        atomic_json(folder / "state.json", {"status":"interrupted", "failure_kind":"user_cancelled", "called_model":None})
        run_root = self.root / "work/deepseek-rethlas" / record["run_id"]
        calls = run_root / "calls/03-teaching-1"
        (calls / "runtime").mkdir(parents=True)
        for name in ("problem", "requirements", "user_prompt"):
            (run_root / (name + ".txt")).write_text(self.payload[name], encoding="utf-8")
        return task, folder, receipt, run_root, calls

    def test_hard_cancel_shows_unarchived_deeptutor_capture_without_success(self):
        task, _, _, _, calls = self.partial_fixture()
        atomic_json(calls / "runtime/capture-state.json", {"evidence_kind":"test_double", "answer":"尚未完成的教学片段", "status":"streaming"})
        (calls / "host.prompt.txt").write_text("实际输入替身", encoding="utf-8")
        (calls / "runtime/provider-response.chunks.jsonl").write_text('{"test_double":true,"content":"部分原文"}\n', encoding="utf-8")
        (calls / "runtime/config.json").write_text('{"not_to_export":"test-only-private-setting"}', encoding="utf-8")
        result = self.bridge.results(task["task_id"])
        self.assertEqual(result["status"], "interrupted")
        self.assertFalse(result["final_review_validated"])
        raw = result["raw_partials"]
        self.assertEqual(raw["review_status"], "unreviewed")
        self.assertTrue(any("尚未完成的教学片段" in row["text"] for row in raw["files"]))
        self.assertNotIn("test-only-private-setting", json.dumps(raw, ensure_ascii=False))
        self.assertTrue(all(row["source_sha256"] == row["captured_sha256"] for row in raw["files"]))

    def test_partial_limits_bind_exact_prefix_and_omit_excess(self):
        task, _, _, _, calls = self.partial_fixture()
        raw = b"X" * (128 * 1024 + 7)
        for name in PARTIAL_FILES:
            (calls / name).write_bytes(raw)
        result = self.bridge.results(task["task_id"])["raw_partials"]
        self.assertEqual(result["captured_bytes"], 512 * 1024)
        self.assertEqual(len(result["files"]), 4)
        self.assertEqual(result["omitted_files"], len(PARTIAL_FILES) - 4)
        for entry in result["files"]:
            self.assertTrue(entry["truncated"])
            self.assertEqual(entry["captured_bytes"], 128 * 1024)
            self.assertEqual(entry["captured_sha256"], sha(raw[:128 * 1024]))
            self.assertIsNone(entry["source_sha256"])

    def test_partial_input_scope_and_run_traversal_are_rejected(self):
        task, folder, receipt, run_root, calls = self.partial_fixture()
        (calls / "host.prompt.txt").write_text("not trusted", encoding="utf-8")
        (run_root / "problem.txt").write_text("another question", encoding="utf-8")
        with self.assertRaisesRegex(BridgeError, "在途材料目录"):
            self.bridge.results(task["task_id"])
        receipt["run_id"] = "../other"
        atomic_json(folder / "run-receipt.json", receipt)
        with self.assertRaisesRegex(BridgeError, "安全标识"):
            self.bridge.results(task["task_id"])

    def test_archived_stages_not_duplicated_as_raw_partial(self):
        task, _, _, _, calls = self.partial_fixture()
        (calls / "host.prompt.txt").write_text("already archived", encoding="utf-8")
        record_path = self.bridge.record_path("q_test/hybrid-rethlas-deeptutor-deepseek-flash-v1/run_1")
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["iterations"] = [{"iteration_id":"teaching-1", "prompt":"already archived", "answer":"test double"}]
        atomic_json(record_path, record)
        self.assertEqual(self.bridge.results(task["task_id"])["raw_partials"]["files"], [])

    def test_final_status_uses_authoritative_dry_finalizer(self):
        task = self.task()
        folder, _ = self.bridge._load(task["task_id"])
        _, path, record = self.make_record(status="completed")
        report = self.root / "work/report.json"
        atomic_json(report, {"test_double":True})
        request = self.root / "work/review-request.json"
        atomic_json(request, {"reviewer":{"prompt":{"file":"work/prompt.txt"}}})
        (self.root / "work/prompt.txt").write_text("test_double", encoding="utf-8")
        record["verification"] = [{"stage":"final_review", "verdict":"correct", "verifier":{"source":"current_codex_chat"}, "report_file":"work/report.json"}]
        atomic_json(path, record)
        outcome = {key:record[key] for key in ("question_id", "ai_id", "run_id")}
        outcome["final_review_request_file"] = "work/review-request.json"
        atomic_json(folder / "outcome.json", outcome)
        atomic_json(folder / "state.json", {"status":"pending_final_review"})
        with patch("deepseek_rethlas.final_review.finalize_review", return_value={"verdict":"correct", "expected_status":"completed"}) as finalizer:
            result = self.bridge.status(task["task_id"])
            self.assertEqual(result["status"], "completed")
            self.assertTrue(finalizer.call_args.kwargs["dry_run"])
            self.assertTrue(result["final_review_validated"])
        with patch("deepseek_rethlas.final_review.finalize_review", return_value={"verdict":"correct", "expected_status":"unverified"}):
            self.assertEqual(self.bridge.status(task["task_id"])["status"], "artifact_failure")
        with patch("deepseek_rethlas.final_review.finalize_review", side_effect=ValueError("hash mismatch")):
            self.assertEqual(self.bridge.status(task["task_id"])["status"], "artifact_failure")

    def test_local_origin_and_host_are_both_required(self):
        self.assertTrue(accepted_origin("127.0.0.1:8108", "http://127.0.0.1:8108", 8108))
        self.assertFalse(accepted_origin("evil.example:8108", None, 8108))
        self.assertFalse(accepted_origin("127.0.0.1:8108", "https://evil.example", 8108))
        self.assertFalse(accepted_origin("127.0.0.1:8108", "null", 8108))

    def test_public_configuration_excludes_arbitrary_secret_fields(self):
        atomic_json(self.bridge.config_path, {"api_key":"not-a-real-secret", "proof":{"model":"deepseek-flash", "api_key":"test-only"}, "teaching":{"headers":{"Authorization":"synthetic"}}})
        rendered = json.dumps(public_config(self.bridge))
        self.assertNotIn("test-only", rendered)
        self.assertNotIn("not-a-real-secret", rendered)
        self.assertNotIn("Authorization", rendered)

    def test_browser_uses_text_nodes_and_preserves_drafts(self):
        source = (ROOT / "scripts/hybrid_tutor/static/app.js").read_text(encoding="utf-8")
        self.assertNotIn(".innerHTML", source)
        self.assertIn(".textContent", source)
        self.assertIn('completed:"运行完成（审阅见详情）"', source)
        self.assertIn('localStorage.setItem("hybrid-tutor-draft-v1"', source)
        html = (ROOT / "scripts/hybrid_tutor/static/index.html").read_text(encoding="utf-8")
        self.assertIn("http://127.0.0.1:3783/chat", html)

    def test_exit_zero_without_outcome_is_not_success(self):
        task = self.task()
        class FakeProcess:
            returncode = 0
            def poll(self): return 0
        with patch("hybrid_tutor.bridge.subprocess.Popen", return_value=FakeProcess()):
            worker(self.root, task["task_id"])
        result = self.bridge.status(task["task_id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_kind"], "missing_outcome")

    def test_worker_accepts_only_no_model_dry_run_contract(self):
        task = self.task()
        folder, _ = self.bridge._load(task["task_id"])
        class FakeProcess:
            returncode = 0
            def poll(self): return 0
        atomic_json(folder / "outcome.json", {"status":"dry_run", "called_model":False})
        with patch("hybrid_tutor.bridge.subprocess.Popen", return_value=FakeProcess()):
            worker(self.root, task["task_id"])
        self.assertEqual(self.bridge.status(task["task_id"])["status"], "dry_run")
        atomic_json(folder / "outcome.json", {"status":"dry_run", "called_model":True})
        with patch("hybrid_tutor.bridge.subprocess.Popen", return_value=FakeProcess()):
            worker(self.root, task["task_id"])
        self.assertEqual(self.bridge.status(task["task_id"])["status"], "failed")


if __name__ == "__main__":
    unittest.main()
