"""Configuration/result and math-runtime transport checks; no model calls."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from hybrid_tutor.config import load_config
from hybrid_tutor_cli import _write_result
from rethlas_core.computation import execute_requests


class HybridConfigTests(unittest.TestCase):
    def test_real_config_has_shared_limits(self):
        config, path, digest, math_file = load_config(ROOT)
        self.assertEqual(config["proof"]["max_model_calls"], 8)
        self.assertEqual(config["teaching"]["provider"], "deepseek-api")
        self.assertEqual(len(digest), 64)
        self.assertEqual(math_file.name, "hybrid-math-runtime.json")

    def test_config_rejects_unknown_fields_and_wrong_model(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "work") as tmp:
            root = Path(tmp)
            config = json.loads((ROOT / "setup/hybrid-tutor.json").read_text(encoding="utf-8"))
            path = root / "c.json"
            config["credential"] = "not-a-real-key"
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaises(ValueError): load_config(root, path)
            del config["credential"]
            config["teaching"]["model"] = "different"
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaises(ValueError): load_config(root, path)

    def test_result_write_is_confined_and_does_not_overwrite(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "work") as tmp:
            root = Path(tmp)
            path = root / "work/hybrid-tutor/tasks/example/outcome.json"
            _write_result(root, path, {"status": "failed", "error": "真实失败"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "failed")
            with self.assertRaises(ValueError): _write_result(root, path, {"status": "completed"})
            with self.assertRaises(ValueError): _write_result(root, root / "wrong.json", {})

    def test_math_runtime_is_explicit_worker_argument(self):
        request = {"id": "K1", "operation": "symbolic_calculate", "arguments": {
            "operation": "differentiate", "expression": "2*x", "variable": "x"},
            "assumptions": {"x": {"real": True}}, "domain": "real x", "purpose": "probe", "required": True}
        with tempfile.TemporaryDirectory(dir=ROOT / "work") as tmp:
            with patch("rethlas_core.computation.subprocess.Popen") as popen:
                proc = popen.return_value
                proc.communicate.return_value = (b'{"ok": true, "result": "2"}', b'')
                proc.returncode = 0
                import time
                result = execute_requests(ROOT, [request], tmp, time.monotonic()+30,
                    math_runtime_file=ROOT / "setup/hybrid-math-runtime.json")
                self.assertEqual(result[0]["status"], "completed")
                self.assertEqual(Path(popen.call_args.args[0][-1]).name, "hybrid-math-runtime.json")


if __name__ == "__main__": unittest.main()
