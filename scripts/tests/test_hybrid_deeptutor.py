"""Offline contract checks; no installed credentials or model network needed."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hybrid_tutor import deeptutor_runtime as runtime
from hybrid_tutor import deeptutor_worker as worker


class CaptureTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[2] / "work/hybrid-integration-20261030/tests"
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.directory = Path(self.temp.name)
        self.capture = worker.Capture(self.directory, test_double=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_config_cannot_change_provider_or_add_secrets(self):
        worker.validate_config(runtime.EXPECTED)
        for value in ({**runtime.EXPECTED, "provider": "other"},
                      {**runtime.EXPECTED, "api_key": "fake"}):
            with self.assertRaises(worker.RoleBoundaryError):
                worker.validate_config(value)

    def test_sdk_second_attempt_is_blocked_before_delegate(self):
        calls = []
        async def create(**kwargs):
            calls.append(kwargs)
            return worker.FixtureStream({"answer": "教学稿"})
        client = worker.CapturedClient(SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create))), self.capture)
        async def run():
            await client.create(model=worker.MODEL, messages=[{"role": "user", "content": "prompt"}],
                                stream=True, temperature=.3, max_tokens=8192)
            with self.assertRaises(worker.RoleBoundaryError):
                await client.create(model=worker.MODEL, messages=[], stream=True)
        asyncio.run(run())
        self.assertEqual(len(calls), 1)
        self.assertTrue(self.capture.violations)

    def test_optional_teacher_configuration_is_explicit_and_legacy_compatible(self):
        from deepseek_rethlas.pipeline import validate_teaching_config
        for value in (runtime.EXPECTED, {**runtime.EXPECTED, "max_output_tokens": 8192,
                "thinking_mode": "disabled", "output_profile": "concise-v1"}):
            self.assertEqual(worker.validate_config(value), value)
            self.assertEqual(validate_teaching_config(value), value)
        for field, value in (("max_output_tokens", True), ("max_output_tokens", 32769),
                ("max_output_tokens", 0), ("thinking_mode", "enabled"), ("thinking_mode", {}),
                ("output_profile", "unknown")):
            bad = {**runtime.EXPECTED, field: value}
            with self.subTest(field=field, value=value):
                with self.assertRaises(worker.RoleBoundaryError): worker.validate_config(bad)
                with self.assertRaises(ValueError): validate_teaching_config(bad)

    def test_disabled_thinking_is_sent_to_actual_sdk_and_budget_is_not_raised(self):
        calls = []
        async def create(**kwargs):
            calls.append(kwargs)
            return worker.FixtureStream({"answer": "教学稿"})
        capture = worker.Capture(self.directory, test_double=True, config={**runtime.EXPECTED,
            "thinking_mode": "disabled", "max_output_tokens": 8192})
        client = worker.CapturedClient(SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create))), capture)
        asyncio.run(client.create(model=worker.MODEL, messages=[], stream=True, temperature=.3, max_tokens=8192))
        self.assertEqual(calls[0]["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertEqual(calls[0]["max_tokens"], 8192)
        saved = json.loads((self.directory / "sdk-request.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, calls[0])

    def test_wrong_output_budget_rejected_before_delegate(self):
        calls = []
        async def create(**kwargs): calls.append(kwargs)
        client = worker.CapturedClient(SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create))), self.capture)
        with self.assertRaises(worker.RoleBoundaryError):
            asyncio.run(client.create(model=worker.MODEL, messages=[], stream=True, temperature=.3, max_tokens=32768))
        self.assertEqual(calls, [])

    def test_wire_must_retain_disabled_thinking_and_pinned_budget(self):
        capture = worker.Capture(self.directory, test_double=True, config={**runtime.EXPECTED,
            "thinking_mode": "disabled", "max_output_tokens": 8192})
        async def body():
            return json.dumps({"model": worker.MODEL, "messages": [], "stream": True,
                "temperature": .3, "max_tokens": 8192, "thinking": {"type": "enabled"}}).encode()
        request = SimpleNamespace(url=SimpleNamespace(scheme="https", host="api.deepseek.com",
            path="/chat/completions", query=b""), method="POST", aread=body)
        with self.assertRaises(worker.RoleBoundaryError):
            asyncio.run(capture.http_request(request))
        self.assertEqual(capture.http_attempts, 0)
        self.assertFalse((self.directory / "http-request.body.json").exists())

    def test_length_drains_usage_then_stops_native_continuation(self):
        stream = worker.CapturedStream(worker.FixtureStream({"scenario": "length_separate_usage",
            "answer": "截断的原文"}), self.capture)
        async def consume():
            chunks = []
            with self.assertRaises(worker.TeachingTerminalFailure) as caught:
                async for chunk in stream: chunks.append(chunk)
            self.assertEqual(caught.exception.finish_reason, "length")
            self.assertEqual(len(chunks), 2)
        asyncio.run(consume())
        state = self.capture.state()
        self.assertEqual(state["raw_answer"], "截断的原文")
        self.assertEqual(state["usage"]["completion_tokens"], 8192)
        self.assertEqual(state["finish_reasons"], ["length"])
        self.assertFalse(state["violations"])

    def test_tools_are_never_sent(self):
        with self.assertRaises(worker.RoleBoundaryError):
            self.capture.admit_sdk({"model": worker.MODEL, "messages": [], "tools": [{"name": "exec"}]})
        self.assertEqual(self.capture.http_attempts, 0)

    def test_actual_model_and_usage_from_chunks_not_config(self):
        self.capture.chunk({"id": "r1", "model": "actual-other", "choices": [{
            "delta": {"content": "原始回答"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}})
        state = self.capture.state()
        self.assertEqual(state["actual_model"], "actual-other")
        self.assertEqual(state["usage"]["total_tokens"], 8)
        self.assertEqual(state["raw_answer"], "原始回答")

    def test_missing_usage_and_model_remain_unknown(self):
        self.capture.chunk({"choices": [{"delta": {"content": "回答"}}]})
        self.assertEqual(self.capture.state()["actual_model"], "未记录")
        self.assertIsNone(self.capture.state()["usage"])

    def test_tool_output_is_preserved_then_rejected(self):
        raw = {"model": worker.MODEL, "choices": [{"delta": {"tool_calls": [{"id": "bad"}]}}]}
        with self.assertRaises(worker.RoleBoundaryError):
            self.capture.chunk(raw)
        saved = json.loads((self.directory / "provider-response.chunks.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(saved, raw)
        self.assertTrue(self.capture.tool_events)

    def test_multiple_model_names_are_not_silently_fixed(self):
        self.capture.chunk({"model": "a", "choices": []})
        self.capture.chunk({"model": "b", "choices": []})
        self.assertEqual(self.capture.state()["actual_model"], "未记录")

    def test_atomic_snapshot_failure_keeps_previous_json(self):
        target = self.directory / "capture-state.json"
        worker.dump(target, {"old": "complete"})
        with patch.object(worker.os, "replace", side_effect=OSError("test interruption")):
            with self.assertRaises(OSError):
                worker.dump(target, {"new": "not committed"})
        self.assertEqual(json.loads(target.read_text()), {"old": "complete"})
        pending = list(self.directory.glob(".snapshot-*"))
        self.assertEqual(len(pending), 1)
        self.assertEqual(json.loads(pending[0].read_text(encoding="utf-8")), {"new": "not committed"})

    def test_dispatch_is_not_proof_provider_received_request(self):
        capture = worker.Capture(self.directory, test_double=False)
        capture.http_attempts = 1
        self.assertIsNone(capture.state()["called_model"])
        capture.response_observed = True
        self.assertTrue(capture.state()["called_model"])


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[2]
        parent = self.root / "work/hybrid-integration-20261030/runtime-tests"
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.directory = Path(self.temp.name) / "call"

    def tearDown(self):
        self.temp.cleanup()

    def execute(self, *, timed_out=False, response=True, actual_model=worker.MODEL, exit_code=0, worker_status="completed"):
        def fake(argv, cwd, environment, timeout):
            result = {"status": worker_status, "answer": "原始教学稿", "raw_answer": "原始教学稿",
                "actual_model": actual_model, "actual_provider": "deepseek-api",
                "session_id": "observed-session", "provider_request_attempts": 1,
                "provider_request_observed": True, "response_observed": response,
                "evidence_kind": "actual_provider", "tool_events": [], "violations": [],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
            target = cwd / ("capture-state.json" if timed_out else "worker-result.json")
            target.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            return {"exit_code": -1 if timed_out else exit_code, "timed_out": timed_out,
                    "elapsed_seconds": 1.2, "pid": 123, "stdout": b"safe-status", "stderr": b"secret-token-not-archived"}
        with patch.object(runtime, "_execute", side_effect=fake):
            return runtime.run_call(self.root, self.directory, "完整提示词", runtime.EXPECTED, 5)

    def test_runtime_preserves_actual_input_and_evidence(self):
        result = self.execute()
        self.assertEqual(result["status"], "completed")
        self.assertEqual((self.directory / "prompt.txt").read_text(encoding="utf-8"), "完整提示词")
        self.assertEqual(result["usage"]["total_tokens"], 15)
        self.assertNotIn("secret-token-not-archived", (self.directory / "process.json").read_text())
        self.assertTrue(result["evidence_files"])

    def test_timeout_keeps_partial_original(self):
        result = self.execute(timed_out=True)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["answer"], "原始教学稿")
        self.assertEqual(result["provider_request_attempts"], 1)

    def test_missing_actual_model_fails_not_backfilled(self):
        result = self.execute(actual_model="未记录")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["actual_model"], "未记录")

    def test_no_response_not_reported_completed(self):
        self.assertEqual(self.execute(response=False)["status"], "failed")

    def test_nonzero_exit_downgrades_completed_worker_and_retains_answer(self):
        result = self.execute(exit_code=1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["original_worker_status"], "completed")
        self.assertEqual(result["answer"], "原始教学稿")
        self.assertEqual(result["failure_kind"], "runtime_failure")
        self.assertTrue(result["error"])

    def test_worker_timeout_nonzero_exit_keeps_timeout_category(self):
        result = self.execute(exit_code=1, worker_status="timeout")
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["answer"], "原始教学稿")

    def test_existing_call_directory_not_overwritten(self):
        self.directory.mkdir()
        with self.assertRaises(FileExistsError):
            runtime.run_call(self.root, self.directory, "prompt", runtime.EXPECTED, 5)

    def test_launch_failure_retains_saved_input_and_no_fake_call(self):
        with patch.object(runtime, "_execute", side_effect=FileNotFoundError("private detail")):
            result = runtime.run_call(self.root, self.directory, "prompt", runtime.EXPECTED, 5)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["invocation_started"])
        self.assertFalse(result["response_observed"])
        self.assertEqual(result["provider_request_attempts"], 0)
        self.assertIn(str(self.directory / "prompt.txt"), result["evidence_files"])
        self.assertNotIn("private detail", (self.directory / "launch-error.json").read_text())

    def test_corrupt_json_recovers_only_complete_chunk_prefix(self):
        self.directory.mkdir()
        (self.directory / "worker-result.json").write_text('{"status":', encoding="utf-8")
        (self.directory / "capture-state.json").write_text('{"raw_answer":', encoding="utf-8")
        chunk = {"model": worker.MODEL, "choices": [{"delta": {"content": "可恢复原文"}}]}
        (self.directory / "provider-response.chunks.jsonl").write_text(
            json.dumps(chunk, ensure_ascii=False) + '\n{"cut":', encoding="utf-8")
        recovered = runtime.recover_result(self.directory)
        self.assertEqual(recovered["raw_answer"], "可恢复原文")
        self.assertEqual(recovered["status"], "failed")
        self.assertEqual(recovered["recovery"]["complete_chunk_count"], 1)
        self.assertTrue(recovered["recovery"]["truncated"])
        self.assertTrue(recovered["recovery"]["incomplete"])
        self.assertEqual(len(recovered["evidence_read_errors"]), 2)

    def test_terminal_corruption_uses_atomic_snapshot(self):
        self.directory.mkdir()
        (self.directory / "worker-result.json").write_text("{", encoding="utf-8")
        (self.directory / "capture-state.json").write_text(json.dumps(
            {"raw_answer": "已保存原文", "session_id": "s-real", "response_observed": True}), encoding="utf-8")
        recovered = runtime.recover_result(self.directory)
        self.assertEqual(recovered["raw_answer"], "已保存原文")
        self.assertEqual(recovered["session_id"], "s-real")
        self.assertEqual(recovered["status"], "failed")
        self.assertTrue(recovered["recovery"]["incomplete"])


if __name__ == "__main__":
    unittest.main()
