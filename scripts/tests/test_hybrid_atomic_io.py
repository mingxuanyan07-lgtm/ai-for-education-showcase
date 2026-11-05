"""Bounded Windows snapshot publication; synthetic files only, never model calls."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from hybrid_tutor import bridge, deeptutor_worker
from hybrid_tutor_cli import _write_result


def windows_error(code=5):
    error = PermissionError(13, "synthetic Windows sharing failure")
    error.winerror = code
    return error


class AtomicPublicationTests(unittest.TestCase):
    def setUp(self):
        self.base = ROOT / "work/hybrid-integration-20261030/atomic-tests"
        self.base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=self.base)
        self.root = Path(self.temp.name).resolve()
        self.target = self.root / "state.json"
        self.target.write_bytes(b'{"old": true}')

    def tearDown(self):
        assert self.root.is_relative_to(self.base.resolve())
        self.temp.cleanup()

    def writers(self):
        return ((bridge, bridge.atomic_json), (deeptutor_worker, deeptutor_worker.dump))

    def test_retry_policies_match_without_importing_host_into_worker(self):
        self.assertEqual(bridge.WINDOWS_COMMIT_DELAYS, deeptutor_worker.WINDOWS_COMMIT_DELAYS)
        self.assertEqual(len(bridge.WINDOWS_COMMIT_DELAYS) + 1, 6)
        self.assertAlmostEqual(sum(bridge.WINDOWS_COMMIT_DELAYS), .5)

    @unittest.skipUnless(os.name == "nt", "Windows-specific retry policy")
    def test_transient_locks_retry_same_complete_temp_and_commit(self):
        original_replace = os.replace
        for module, writer in self.writers():
            for code in (5, 32, 33):
                with self.subTest(writer=writer.__module__, code=code):
                    paths, payloads = [], []
                    def replacement(source, target):
                        paths.append(Path(source))
                        payloads.append(Path(source).read_bytes())
                        if len(paths) < 3:
                            raise windows_error(code)
                        return original_replace(source, target)
                    with patch.object(module.os, "replace", side_effect=replacement), patch.object(module.time, "sleep") as sleep:
                        writer(self.target, {"new": "完整中文"})
                    self.assertEqual(len(paths), 3)
                    self.assertEqual(len(set(paths)), 1)
                    self.assertTrue(all(raw == payloads[0] for raw in payloads))
                    self.assertEqual(json.loads(self.target.read_bytes()), {"new": "完整中文"})
                    self.assertEqual([c.args[0] for c in sleep.call_args_list], [.02, .04])
                    self.assertFalse(paths[0].exists())

    @unittest.skipUnless(os.name == "nt", "Windows-specific retry policy")
    def test_exhaustion_is_bounded_and_preserves_original_and_failed_snapshot(self):
        for module, writer in self.writers():
            with self.subTest(writer=writer.__module__):
                self.target.write_bytes(b'{"old": true}')
                seen = []
                failure = windows_error(5)
                def fail(source, target):
                    seen.append(Path(source))
                    raise failure
                with patch.object(module.os, "replace", side_effect=fail), patch.object(module.time, "sleep") as sleep:
                    with self.assertRaises(PermissionError) as caught:
                        writer(self.target, {"new": True})
                self.assertIs(caught.exception, failure)
                self.assertEqual(len(seen), 6)
                self.assertEqual(len(set(seen)), 1)
                self.assertEqual(sleep.call_count, 5)
                self.assertEqual(json.loads(self.target.read_bytes()), {"old": True})
                self.assertEqual(json.loads(seen[0].read_bytes()), {"new": True})

    def test_unrelated_errors_are_not_retried_or_reclassified(self):
        for module, writer in self.writers():
            with self.subTest(writer=writer.__module__):
                failure = OSError(28, "synthetic disk full")
                with patch.object(module.os, "replace", side_effect=failure) as replace, patch.object(module.time, "sleep") as sleep:
                    with self.assertRaises(OSError) as caught:
                        writer(self.target, {"new": True})
                self.assertIs(caught.exception, failure)
                self.assertEqual(replace.call_count, 1)
                sleep.assert_not_called()
                self.assertEqual(json.loads(self.target.read_bytes()), {"old": True})

    def test_serialization_errors_do_not_start_commit_retry(self):
        for module, writer in self.writers():
            with self.subTest(writer=writer.__module__):
                with patch.object(module.os, "replace") as replace, patch.object(module.time, "sleep") as sleep:
                    with self.assertRaises(TypeError):
                        writer(self.target, {"bad": object()})
                replace.assert_not_called()
                sleep.assert_not_called()
                self.assertEqual(json.loads(self.target.read_bytes()), {"old": True})

    def cli_target(self, name="outcome.json"):
        target = self.root / "work/hybrid-tutor/tasks/synthetic_task" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def test_cli_result_and_receipt_are_create_only(self):
        for name in ("outcome.json", "run-receipt.json"):
            target = self.cli_target(name)
            _write_result(self.root, target, {"original": True}, filename=name)
            before = target.read_bytes()
            with self.assertRaisesRegex(ValueError, "already exists"):
                _write_result(self.root, target, {"replacement": True}, filename=name)
            self.assertEqual(target.read_bytes(), before)

    @unittest.skipUnless(os.name == "nt", "Windows-specific no-clobber rename")
    def test_cli_retries_only_same_publish_after_sharing_error(self):
        target = self.cli_target()
        original_rename = os.rename
        sources = []
        def once(source, destination):
            sources.append(Path(source))
            if len(sources) == 1:
                raise windows_error(5)
            return original_rename(source, destination)
        with patch.object(bridge.os, "rename", side_effect=once), patch.object(bridge.time, "sleep") as sleep:
            _write_result(self.root, target, {"called_model": False, "status": "dry_run"})
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[0], sources[1])
        self.assertEqual(json.loads(target.read_bytes())["status"], "dry_run")
        sleep.assert_called_once_with(.02)

    @unittest.skipUnless(os.name == "nt", "Windows-specific no-clobber rename")
    def test_cli_collision_after_initial_check_cannot_overwrite(self):
        target = self.cli_target()
        original_rename = os.rename
        def collision(source, destination):
            target.write_bytes(b'{"another_publisher": true}')
            return original_rename(source, destination)
        with patch.object(bridge.os, "rename", side_effect=collision), patch.object(bridge.time, "sleep") as sleep:
            with self.assertRaises(FileExistsError):
                _write_result(self.root, target, {"replacement": True})
        self.assertEqual(json.loads(target.read_bytes()), {"another_publisher": True})
        sleep.assert_not_called()
        pending = list(target.parent.glob("*.tmp"))
        self.assertEqual(len(pending), 1)
        self.assertEqual(json.loads(pending[0].read_bytes()), {"replacement": True})


if __name__ == "__main__":
    unittest.main()
