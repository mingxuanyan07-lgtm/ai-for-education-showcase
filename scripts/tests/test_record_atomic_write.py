"""Bounded Windows replacement retries preserve the old record on failure."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import experiment_records as records


class RecordAtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.path = self.root / "record.md"
        self.path.write_text("old", encoding="utf-8")

    def failure(self, winerror=None):
        error = PermissionError("synthetic sharing failure")
        if winerror is not None:
            error.winerror = winerror
        return error

    def test_transient_denial_retains_old_file_until_commit(self):
        replace = records.os.replace
        count = 0
        def transient(source, destination):
            nonlocal count
            count += 1
            self.assertEqual(self.path.read_text(encoding="utf-8"), "old")
            if count < 3:
                raise self.failure(5)
            return replace(source, destination)
        with patch.object(records.os, "replace", side_effect=transient), \
             patch.object(records.time, "sleep") as sleep:
            records._write(self.root, self.path, "new")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "new")
        self.assertEqual(count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_exhaustion_is_fatal_and_preserves_original(self):
        with patch.object(records.os, "replace", side_effect=self.failure(32)) as replace, \
             patch.object(records.time, "sleep") as sleep:
            with self.assertRaises(PermissionError):
                records._write(self.root, self.path, "new")
        self.assertEqual(replace.call_count, 6)
        self.assertEqual(sleep.call_count, 5)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "old")
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_generic_permission_error_does_not_retry(self):
        with patch.object(records.os, "replace", side_effect=self.failure()) as replace, \
             patch.object(records.time, "sleep") as sleep:
            with self.assertRaises(PermissionError):
                records._write(self.root, self.path, "new")
        self.assertEqual(replace.call_count, 1)
        sleep.assert_not_called()

    def test_non_transient_windows_error_does_not_retry(self):
        with patch.object(records.os, "replace", side_effect=self.failure(87)) as replace, \
             patch.object(records.time, "sleep") as sleep:
            with self.assertRaises(PermissionError):
                records._write(self.root, self.path, "new")
        self.assertEqual(replace.call_count, 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
