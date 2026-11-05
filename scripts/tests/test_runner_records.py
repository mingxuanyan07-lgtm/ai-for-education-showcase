"""Runner outcome collection checks; no Codex process or network is used."""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runner
from rethlas_core.verification import make_binding, text_sha, write_once


class RunnerRecordTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temporary = self.stack.enter_context(tempfile.TemporaryDirectory(
            prefix="runner-records-", dir=Path(__file__).resolve().parent,
        ))
        self.root = Path(temporary)
        self.generation = self.root / "Rethlas" / "agents" / "generation"
        (self.generation / "data").mkdir(parents=True)
        (self.generation / "AGENTS.md").write_text("Fixture instructions.\n", encoding="utf-8")
        (self.generation / "data" / "example.md").write_text("Prove 1 + 1 = 2.\n", encoding="utf-8")
        self.proof = self.generation / "results" / "example" / "blueprint_verified.md"
        settings_module = types.ModuleType("codex_runtime")
        settings_module.load_settings = Mock(return_value={"model": "fixture-model", "reasoning_effort": "medium"})
        record_module = types.ModuleType("experiment_records")
        self.collect = Mock(return_value={
            "record_path": str(self.root / "record.md"),
            "record_dir": str(self.root),
            "experiment_id": "fixture-run",
        })
        record_module.record_rethlas_run = self.collect
        self.stack.enter_context(patch.dict(sys.modules, {
            "codex_runtime": settings_module,
            "experiment_records": record_module,
        }))
        self.stack.enter_context(patch.dict(os.environ, {
            "DRY_RUN": "0", "MODEL": "", "REASONING_EFFORT": "", "SESSION_ID": "",
            "LOG_DIR": "", "PAUSE_FILE": "", "MAX_ITERATIONS": "1",
        }))
        response = Mock()
        response.__enter__ = Mock(return_value=io.StringIO('{"status": "ok"}'))
        response.__exit__ = Mock(return_value=False)
        self.opener = Mock()
        self.opener.open.return_value = response
        self.health = self.stack.enter_context(patch("urllib.request.build_opener", return_value=self.opener))
        self.iteration = self.stack.enter_context(patch.object(runner, "run_iteration", return_value="fixture-session"))
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.stdout))
        self.stack.enter_context(redirect_stderr(self.stderr))

    def invoke(self, *extra):
        return runner.main([
            "--project-root", str(self.root), "--problem-file", "data/example.md",
            "--max-iterations", "1", *extra,
        ])

    def make_proof(self, *unused):
        self.proof.parent.mkdir(parents=True, exist_ok=True)
        self.proof.write_text("Fixture verified result.\n", encoding="utf-8")
        statement = (self.generation / "data/example.md").read_text(encoding="utf-8")
        text = self.proof.read_text(encoding="utf-8")
        report = {"verification_report": {"summary": "Synthetic fixture.", "critical_errors": [], "gaps": []},
                  "verdict": "correct", "repair_hints": ""}
        binding = make_binding(statement, text, report, "fixture-verification")
        trusted = self.generation.parent / "verification/results/fixture-verification"
        write_once(trusted / "verification.json", report)
        write_once(trusted / "verification.binding.json", binding)
        write_once(self.generation / "verification_receipts" / (text_sha(text) + "_fixture-verification.json"), binding)
        return "fixture-session"

    def assert_collected(self, status, error=None):
        self.collect.assert_called_once_with(self.root.resolve(), "data/example.md", status, error=error)
        self.assertFalse((self.generation / "logs" / "example" / "iter" / ".runner.lock").exists())

    def test_success_collects_once_and_returns_original_success(self):
        self.iteration.side_effect = self.make_proof
        self.assertEqual(self.invoke(), 0)
        self.assert_collected("completed")
        self.assertIn("Experiment ID: fixture-run", self.stdout.getvalue())
        self.assertIn(f"Experiment folder: {self.root}", self.stdout.getvalue())
        self.assertNotIn("Experiment ZIP", self.stdout.getvalue())

    def test_unverified_iteration_limit_keeps_exit_code_one(self):
        self.assertEqual(self.invoke(), 1)
        self.assert_collected("unverified")

    def test_user_interrupt_is_recorded_and_rethrown(self):
        self.iteration.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.invoke()
        self.assert_collected("interrupted", "Interrupted by user.")

    def test_runner_error_is_recorded_and_rethrown(self):
        original = runner.RunnerError("Fixture execution failed")
        self.iteration.side_effect = original
        with self.assertRaises(runner.RunnerError) as caught:
            self.invoke()
        self.assertIs(caught.exception, original)
        self.assert_collected("failed", str(original))

    def test_os_error_is_recorded_and_rethrown(self):
        original = OSError("Fixture disk failure")
        self.iteration.side_effect = original
        with self.assertRaises(OSError) as caught:
            self.invoke()
        self.assertIs(caught.exception, original)
        self.assert_collected("failed", str(original))

    def test_unavailable_service_is_a_recorded_failed_attempt(self):
        self.opener.open.side_effect = OSError("Fixture unavailable service")
        with self.assertRaises(runner.RunnerError) as caught:
            self.invoke()
        self.assert_collected("failed", str(caught.exception))
        self.iteration.assert_not_called()

    def test_existing_proof_is_reused_without_model_or_health_request(self):
        self.make_proof()
        self.assertEqual(self.invoke(), 0)
        self.assert_collected("reused")
        self.iteration.assert_not_called()
        self.health.assert_not_called()

    def test_pause_after_iteration_is_recorded(self):
        def pause(problem, *unused):
            problem.result_dir.mkdir(parents=True, exist_ok=True)
            (problem.result_dir / "PAUSE_AFTER_ITERATION").write_text("Fixture pause", encoding="utf-8")
            return "fixture-session"

        self.iteration.side_effect = pause
        self.assertEqual(self.invoke(), 0)
        self.assert_collected("paused")

    def test_dry_run_does_not_create_any_files_or_records(self):
        before = sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("*"))
        self.assertEqual(self.invoke("--dry-run"), 0)
        after = sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("*"))
        self.assertEqual(before, after)
        self.collect.assert_not_called()
        self.iteration.assert_not_called()
        self.health.assert_not_called()

    def test_dry_run_existing_proof_does_not_collect(self):
        self.make_proof()
        self.assertEqual(self.invoke("--dry-run"), 0)
        self.collect.assert_not_called()

    def test_no_records_disables_collection_only(self):
        self.assertEqual(self.invoke("--no-records"), 1)
        self.iteration.assert_called_once()
        self.collect.assert_not_called()

    def test_invalid_problem_paths_do_not_create_records(self):
        for value in ("../escape.md", "other/example.md", "data/missing.md", str(self.root / "absolute.md")):
            with self.subTest(value=value):
                with self.assertRaises((runner.RunnerError, OSError)):
                    self.invoke("--problem-file", value)
        self.collect.assert_not_called()
        self.iteration.assert_not_called()

    def test_unsafe_log_directory_does_not_create_records(self):
        with self.assertRaises(runner.RunnerError):
            self.invoke("--log-dir", str(self.root.parent / "outside"))
        self.collect.assert_not_called()

    def test_invalid_session_is_rejected_even_if_proof_exists(self):
        self.make_proof()
        with self.assertRaises(runner.RunnerError):
            self.invoke("--session-id", "invalid session id")
        self.collect.assert_not_called()

    def test_invalid_integer_argument_does_not_create_records(self):
        with self.assertRaises(SystemExit) as caught:
            self.invoke("--max-iterations", "0")
        self.assertEqual(caught.exception.code, 2)
        self.collect.assert_not_called()

    def test_collector_failure_does_not_replace_success(self):
        self.iteration.side_effect = self.make_proof
        self.collect.side_effect = RuntimeError("Fixture archive failure")
        self.assertEqual(self.invoke(), 0)
        self.collect.assert_called_once()
        self.assertIn("AUTOMATIC EXPERIMENT RECORD FAILED", self.stderr.getvalue())

    def test_collector_failure_does_not_replace_unverified_result(self):
        self.collect.side_effect = RuntimeError("Fixture archive failure")
        self.assertEqual(self.invoke(), 1)
        self.assertIn("Fixture archive failure", self.stderr.getvalue())

    def test_collector_failure_does_not_replace_execution_error(self):
        original = runner.RunnerError("Original execution error")
        self.iteration.side_effect = original
        self.collect.side_effect = RuntimeError("Fixture archive failure")
        with self.assertRaises(runner.RunnerError) as caught:
            self.invoke()
        self.assertIs(caught.exception, original)
        self.assertIn("AUTOMATIC EXPERIMENT RECORD FAILED", self.stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
