from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deepseek_rethlas import runtime


class DeepSeekRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.test_root = Path(__file__).absolute().parents[2] / "work/tests-deepseek-runtime"
        self.test_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=self.test_root)
        self.root = Path(self.temp.name)
        self.directory = self.root / "calls/solve"
        self.selection = {"provider": "deepseek-account", "model": "deepseek-flash", "reasoningEffort": "max"}
        self.config = {"provider": "deepseek-account", "model": "deepseek-flash", "reasoning_effort": "max"}
        self.rows = [{"id": i, "name": "@deepseek-ai/" + name} for i, name in runtime._CORE.items()]
        self.rows += [{"id": "custom-tool", "name": "@example/tool"},
                      {"id": "tool-web", "name": "@deepseek-ai/dsh-tool-web"},
                      {"id": "agent-instructions", "name": "@deepseek-ai/dsh-agent-instructions"},
                      {"id": "session-title-llm", "name": "@deepseek-ai/dsh-session-title-first-prompt-llm"}]
        self.stdout = b'{"type":"session","sessionId":"session-test"}\n{"type":"final","text":"Answer\\n"}\n'
        self.session = {"session_id": "session-test", "cwd": str(self.directory),
                        "user_messages": ["Input"], "turn_count": 1,
                        "request_headers": [{"provider": "deepseek-account", "model": "deepseek-flash",
                                             "reasoningEffort": "max", "tool_count": 0}],
                        "assistant_messages": [{"provider": "deepseek-account", "model": "deepseek-flash"}],
                        "final_answer": "Answer\n", "actual_model": "deepseek-flash",
                        "actual_provider": "deepseek-account", "tools": [], "tool_events": [],
                        "sandbox_mode": "workspace-write", "approval_policy": "ask",
                        "last_turn_end": {"reason": {"kind": "completed"}},
                        "session_jsonl": (json.dumps({"type": "user/message", "data": {
                            "content": [{"type": "text", "text": "Input"}],
                            "source": {"kind": "user"}, "role": "user"}}) + "\n").encode()}

    def tearDown(self):
        self.assertEqual(Path(self.temp.name).absolute().parent, self.test_root.absolute())
        self.temp.cleanup()

    def invoke(self, *, session=None, stdout=None, exit_code=0):
        def execute(argv, cwd, timeout, stdin=None):
            self.assertGreater(timeout, 0)
            if "--dump-config" in argv:
                self.assertIsNone(stdin)
                return {"stdout": json.dumps(self.rows).encode(), "stderr": b"", "exit_code": 0,
                        "timed_out": False, "pid": 101, "elapsed_seconds": .01}
            self.assertNotIn("--session-id", argv)
            self.assertEqual(stdin, "Input")
            self.assertEqual((cwd / "prompt.txt").read_text(encoding="utf-8"), "Input")
            overlay = json.loads((cwd / "call.patch.json").read_text(encoding="utf-8"))
            self.assertTrue(next(r for r in overlay if r["id"] == "tool-web")["disabled"])
            return {"stdout": self.stdout if stdout is None else stdout, "stderr": b"",
                    "exit_code": exit_code, "timed_out": False, "pid": 102, "elapsed_seconds": .02}
        with patch.object(runtime, "preflight", return_value={"ready": True, "cli": "dsh.cmd", "dsh_home": "ignored"}), \
             patch.object(runtime, "_execute", side_effect=execute), \
             patch.object(runtime.dsh, "read_session_by_id", return_value=copy.deepcopy(self.session if session is None else session)):
            return runtime.run_call(self.root, self.directory, "Input", self.config, 5)

    def test_completed_requires_real_response_metadata_and_preserves_raw(self):
        result = self.invoke()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["answer"], "Answer\n")
        self.assertEqual((self.directory / "stdout.jsonl").read_bytes(), self.stdout)
        self.assertEqual(result["actual_model"], "deepseek-flash")
        self.assertTrue((self.directory / "session.jsonl").is_file())

    def test_unexpected_tool_plugin_is_disabled_before_agent(self):
        overlay = runtime.build_patch(self.rows, self.selection, self.directory)
        by_id = {r["id"]: r for r in overlay}
        for identifier in ("custom-tool", "tool-web", "agent-instructions", "session-title-llm"):
            self.assertTrue(by_id[identifier]["disabled"])
        self.assertEqual(by_id["agent-default-model"]["config"], self.selection)
        self.assertEqual(by_id["approval"]["config"], {"policy": "ask"})

    def test_provider_idle_override_is_call_local_and_bounded(self):
        original = copy.deepcopy(self.rows)
        patch_rows = runtime.build_patch(self.rows, self.selection, self.directory, 899000)
        plugin = next(row for row in patch_rows if row["id"] == "llm-deepseek-account")
        self.assertEqual(plugin["config"], {"streamIdleTimeoutMs": 899000})
        self.assertEqual(self.rows, original)
        for invalid in (True, 0, -1, 900001, 1.5):
            with self.subTest(invalid=invalid), self.assertRaises(runtime.RuntimeConfigError):
                runtime.build_patch(self.rows, self.selection, self.directory, invalid)

    def test_call_records_idle_policy_within_unchanged_role_limit(self):
        result = self.invoke()
        policy = json.loads((self.directory / "timeout-policy.json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(policy["role_timeout_seconds"], 5)
        self.assertLessEqual(policy["provider_stream_idle_timeout_ms"], 5000)
        self.assertGreater(policy["provider_stream_idle_timeout_ms"], 0)
        self.assertFalse(policy["global_configuration_modified"])
        self.assertEqual(policy["adapter_automatic_retries"], 0)

    def test_structured_provider_idle_timeout_preserves_partial_usage_and_stays_failed(self):
        failure = {"kind": "error", "error": {"message": "DeepSeek Messages stream idle timeout", "code": "TIMEOUT"}}
        self.session["last_turn_end"] = {"reason": failure}
        self.session["usage"] = {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13}
        stream = self.stdout + (json.dumps({"type": "status", "phase": "turn_end", "reason": failure}) + "\n").encode()
        result = self.invoke(stdout=stream, exit_code=1)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["failure_kind"], "timeout")
        self.assertEqual(result["timeout_subtype"], "provider_stream_idle")
        self.assertEqual(result["usage"]["total_tokens"], 13)
        self.assertEqual(result["answer"], "Answer\n")
        self.assertIn("No completed durable terminal", result["error"])
        self.assertEqual(result["provider_errors"], [failure["error"]])
        self.assertEqual((self.directory / "stdout.jsonl").read_bytes(), stream)

    def test_timeout_word_in_answer_does_not_classify_provider_failure(self):
        text = "TIMEOUT: DeepSeek Messages stream idle timeout"
        self.session["final_answer"] = text
        result = self.invoke(stdout=(json.dumps({"type": "session", "sessionId": "session-test"})
            + "\n" + json.dumps({"type": "final", "text": text}) + "\n").encode())
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("timeout_subtype", result)

    def test_replaced_core_implementation_rejected_before_model(self):
        self.rows[0]["name"] = "@example/evil"
        with self.assertRaises(runtime.RuntimeConfigError):
            runtime.build_patch(self.rows, self.selection, self.directory)

    def test_only_platform_sandbox_plugin_is_enabled(self):
        with patch.object(runtime.os, "name", "nt"):
            overlay = {r["id"]: r for r in runtime.build_patch(self.rows, self.selection, self.directory)}
        self.assertTrue(overlay["bash-sandbox"]["disabled"])
        self.assertFalse(overlay["pwsh-sandbox"]["disabled"])

    def test_missing_or_changed_model_is_not_inferred_from_config(self):
        self.session["actual_model"] = "未记录"
        self.session["assistant_messages"][0]["model"] = "未记录"
        result = self.invoke()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["actual_model"], "未记录")

    def test_tool_registration_and_tool_calls_fail(self):
        self.session["request_headers"][0]["tool_count"] = 1
        self.session["tools"] = ["pwsh"]
        result = self.invoke()
        self.assertEqual(result["status"], "failed")
        self.assertIn("tools", result["error"])

    def test_unverified_permissions_fail(self):
        self.session["sandbox_mode"] = "未记录"
        result = self.invoke()
        self.assertEqual(result["status"], "failed")
        self.assertIn("sandbox", result["error"])

    def test_reused_session_and_changed_input_fail(self):
        self.session["user_messages"] = ["Earlier", "Input"]
        self.session["turn_count"] = 2
        result = self.invoke()
        self.assertEqual(result["status"], "failed")
        self.assertIn("fresh turn", result["error"])

    def test_error_exit_is_not_completed_despite_answer(self):
        result = self.invoke(exit_code=1)
        self.assertEqual(result["status"], "failed")
        self.assertIn("code 1", result["error"])

    def test_bad_json_is_archived_and_failed(self):
        result = self.invoke(stdout=b"broken\n")
        self.assertEqual(result["status"], "failed")
        self.assertEqual((self.directory / "stdout.jsonl").read_bytes(), b"broken\n")

    def test_installed_catalog_diagnostic_is_preserved_and_whitelisted(self):
        lines = self.stdout.splitlines(keepends=True)
        diagnostic = b"[llm-deepseek-account] model listing from https://api.deepseek.com/models loaded 2 model(s), skipped 0 row(s)\n"
        mixed = lines[0] + diagnostic + b"".join(lines[1:])
        result = self.invoke(stdout=mixed)
        self.assertEqual(result["status"], "completed")
        self.assertEqual((self.directory / "stdout.jsonl").read_bytes(), mixed)
        saved = json.loads((self.directory / "nonprotocol-diagnostics.json").read_text(encoding="utf-8"))
        self.assertEqual(saved[0]["text"], diagnostic.decode().rstrip("\n"))
        self.assertEqual(result["actual_model"], "deepseek-flash")

    def test_unknown_diagnostic_still_fails_but_observed_session_is_archived(self):
        lines = self.stdout.splitlines(keepends=True)
        mixed = lines[0] + b"unknown plugin diagnostic\n" + b"".join(lines[1:])
        result = self.invoke(stdout=mixed)
        self.assertEqual(result["status"], "failed")
        self.assertIn("Unknown non-JSON", result["error"])
        self.assertEqual(result["actual_model"], "deepseek-flash")
        self.assertTrue((self.directory / "session.jsonl").is_file())
        self.assertEqual((self.directory / "stdout.jsonl").read_bytes(), mixed)

    def test_catalog_diagnostic_whitelist_rejects_alternate_url(self):
        lines = self.stdout.splitlines(keepends=True)
        mixed = lines[0] + b"[llm-deepseek-account] model listing from https://example.com/models loaded 2 model(s), skipped 0 row(s)\n" + b"".join(lines[1:])
        result = self.invoke(stdout=mixed)
        self.assertEqual(result["status"], "failed")
        self.assertIn("Unknown non-JSON", result["error"])

    def test_saved_prompt_matches_stdin_bytes_with_mixed_newlines(self):
        prompt = "First\r\nSecond\nThird\rFourth\n"
        session = copy.deepcopy(self.session)
        session["user_messages"] = [prompt]
        session["session_jsonl"] = (json.dumps({"type": "user/message", "data": {
            "content": [{"type": "text", "text": prompt}], "source": {"kind": "user"}}}) + "\n").encode()
        def execute(argv, cwd, timeout, stdin=None):
            if "--dump-config" in argv:
                return {"stdout": json.dumps(self.rows).encode(), "stderr": b"", "exit_code": 0,
                        "timed_out": False, "pid": 100}
            self.assertEqual(stdin, prompt)
            self.assertEqual((cwd / "prompt.txt").read_bytes(), prompt.encode("utf-8"))
            return {"stdout": self.stdout, "stderr": b"", "exit_code": 0, "timed_out": False, "pid": 101}
        with patch.object(runtime, "preflight", return_value={"ready": True, "cli": "dsh.cmd", "dsh_home": "ignored"}), \
             patch.object(runtime, "_execute", side_effect=execute), \
             patch.object(runtime.dsh, "read_session_by_id", return_value=session):
            result = runtime.run_call(self.root, self.directory, prompt, self.config, 10)
        self.assertEqual(result["status"], "completed")

    def test_harness_context_is_preserved_separately_from_exact_submitted_prompt(self):
        context_text = "Current runtime context. This snapshot supersedes earlier runtime-context snapshots.\n\nKnown runtime policy snapshot"
        context = {"type": "user/message", "data": {
            "content": [{"type": "text", "text": context_text}],
            "source": {"kind": "runtime-context", "form": "snapshot", "sections": [
                {"name": "working-directory:current", "text": "Known runtime policy snapshot"}]}}}
        self.session["session_jsonl"] += (json.dumps(context) + "\n").encode()
        self.session["user_messages"].append(context_text)
        result = self.invoke()
        self.assertEqual(result["status"], "completed")
        saved = json.loads((self.directory / "actual-input-messages.json").read_text(encoding="utf-8"))
        self.assertEqual(len(saved), 2)
        self.assertEqual(saved[1]["source"]["kind"], "runtime-context")
        self.assertTrue((self.directory / "harness-runtime-context.json").is_file())

    def test_unknown_injected_input_source_is_not_ignored(self):
        unknown = {"type": "user/message", "data": {"content": [{"type": "text", "text": "Other task"}],
                                                    "source": {"kind": "unknown-injection"}}}
        self.session["session_jsonl"] += (json.dumps(unknown) + "\n").encode()
        result = self.invoke()
        self.assertEqual(result["status"], "failed")
        self.assertIn("Unexpected source", result["error"])

    def test_system_prompt_is_part_of_actual_model_input_evidence(self):
        system = {"type": "system/message", "data": {"turn": 1, "step": 1, "message": {
            "role": "system", "content": [{"type": "text", "text": "Host system prompt"}],
            "source": {"kind": "system-prompt"}}}}
        self.session["session_jsonl"] = (json.dumps(system) + "\n").encode() + self.session["session_jsonl"]
        result = self.invoke()
        self.assertEqual(result["status"], "completed")
        saved = json.loads((self.directory / "actual-input-messages.json").read_text(encoding="utf-8"))
        self.assertEqual(saved[0]["message"]["content"][0]["text"], "Host system prompt")

    def test_call_directory_reuse_is_rejected(self):
        self.invoke()
        with self.assertRaises(runtime.RuntimeConfigError):
            runtime.run_call(self.root, self.directory, "Input", self.config, 5)

    def test_dump_does_not_evaluate_javascript(self):
        text = "\n".join([f"- id: {r['id']}\n  name: '{r['name']}'" for r in self.rows])
        text += "\n  config:\n    value: !!js process.env.SECRET\n"
        parsed = runtime._rows(text)
        self.assertEqual(parsed[-1]["config"]["value"], {"unevaluated_js": "process.env.SECRET"})

    def test_parent_traversal_is_rejected_before_writes(self):
        with self.assertRaises(runtime.RuntimeConfigError):
            runtime.run_call(self.root, self.root / "calls/../../escaped", "Input", self.config, 5)

    def test_external_directory_is_rejected_before_writes(self):
        with self.assertRaises(runtime.RuntimeConfigError):
            runtime.run_call(self.root, self.root.parent / "outside", "Input", self.config, 5)

    def test_keyboard_interrupt_stops_only_owned_process_and_reraises(self):
        process = Mock(pid=43210, returncode=-9)
        interruption = KeyboardInterrupt()
        process.communicate.side_effect = [interruption, (b"partial stdout", b"partial stderr")]
        with patch.object(runtime.subprocess, "Popen", return_value=process), \
             patch.object(runtime, "_kill_owned_tree") as stop:
            with self.assertRaises(KeyboardInterrupt) as caught:
                runtime._execute(["dsh.cmd"], self.root, 10, "Input")
        self.assertIs(caught.exception, interruption)
        stop.assert_called_once_with(process)
        self.assertEqual(interruption.dsh_execution["stdout"], b"partial stdout")
        self.assertEqual(interruption.dsh_execution["pid"], 43210)

    def test_timeout_retains_partial_output_if_drain_also_times_out(self):
        process = Mock(pid=43211, returncode=None)
        process.communicate.side_effect = [
            subprocess.TimeoutExpired("dsh", 1, output=b"part", stderr=b"err"),
            subprocess.TimeoutExpired("dsh", 10, output=b"partial full", stderr=b"error full")]
        with patch.object(runtime.subprocess, "Popen", return_value=process), \
             patch.object(runtime, "_kill_owned_tree") as stop:
            result = runtime._execute(["dsh.cmd"], self.root, 1)
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["stdout"], b"partial full")
        self.assertEqual(stop.call_count, 2)

    def test_run_call_archives_interrupted_model_output_then_reraises(self):
        interruption = KeyboardInterrupt()
        interruption.dsh_execution = {"stdout": b"raw partial", "stderr": b"raw error",
                                      "pid": 43212, "interrupted": True, "exit_code": -9}
        responses = [{"stdout": json.dumps(self.rows).encode(), "stderr": b"", "exit_code": 0,
                      "timed_out": False, "pid": 100}, interruption]
        with patch.object(runtime, "preflight", return_value={"ready": True, "cli": "dsh.cmd", "dsh_home": "ignored"}), \
             patch.object(runtime, "_execute", side_effect=responses):
            with self.assertRaises(KeyboardInterrupt):
                runtime.run_call(self.root, self.directory, "Input", self.config, 10)
        self.assertEqual((self.directory / "stdout.jsonl").read_bytes(), b"raw partial")
        saved = json.loads((self.directory / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()
