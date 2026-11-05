"""Offline native DeepTutor acceptance: all providers are explicit test doubles."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from hybrid_tutor.deeptutor_runtime import DEEPTUTOR_ROOT, PYTHON, EXPECTED


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = (ROOT / args.output).resolve()
    if not output.is_relative_to(ROOT):
        raise ValueError("Probe output must stay in project")
    output.mkdir(parents=True, exist_ok=False)
    sources = []
    for name in ("deeptutor_worker.py", "deeptutor_runtime.py", "deeptutor_probe.py"):
        source = ROOT / "scripts/hybrid_tutor" / name
        raw = source.read_bytes()
        (output / (name + ".txt")).write_bytes(raw)
        sources.append({"file": str(source), "sha256": hashlib.sha256(raw).hexdigest()})
    cases = ["success", "missing_model", "missing_usage", "tool_call", "provider_failure", "empty_answer", "timeout", "close_failure",
             "length", "length_separate_usage", "content_filter", "thinking_disabled"]
    rows = []
    for case in cases:
        directory = output / case
        directory.mkdir()
        config = dict(EXPECTED)
        if case == "thinking_disabled":
            config.update(max_output_tokens=8192, thinking_mode="disabled", output_profile="concise-v1")
        (directory / "config.json").write_text(json.dumps(config), encoding="utf-8")
        (directory / "prompt.txt").write_text("这是无数学求解的工程测试。只解释给定文本，不能调用工具。", encoding="utf-8")
        fixture = directory / "fixture.json"
        fixture.write_text(json.dumps({"scenario": case,
            "answer": "" if case == "empty_answer" else "离线测试替身：教学内容。"}), encoding="utf-8")
        env = dict(os.environ)
        env.update(DEEPTUTOR_HOME=str(directory / "runtime"),
                   TIKTOKEN_CACHE_DIR=str(DEEPTUTOR_ROOT / "cache/tiktoken"), PYTHONUTF8="1")
        argv = [str(PYTHON), "-I", "-X", "utf8", str(ROOT / "scripts/hybrid_tutor/deeptutor_worker.py"),
                "--directory", str(directory), "--timeout", "5" if case == "timeout" else "20",
                "--offline-fixture", str(fixture)]
        started = time.monotonic()
        completed = subprocess.run(argv, capture_output=True, timeout=45, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        (directory / "process.stdout.txt").write_bytes(completed.stdout)
        (directory / "process.stderr.txt").write_bytes(completed.stderr)
        result = json.loads((directory / "worker-result.json").read_text(encoding="utf-8"))
        expected_status = "timeout" if case == "timeout" else "completed" if case in ("success", "missing_usage", "thinking_disabled") else "failed"
        wire = json.loads((directory / "http-request.body.json").read_text(encoding="utf-8"))
        checks = {"expected_status": result["status"] == expected_status,
                  "test_double": result["evidence_kind"] == "test_double" and result["called_model"] is False,
                  "one_simulated_dispatch": result["provider_request_attempts"] == 1,
                  "native_loop_evidence": (directory / "native-model-turn.json").is_file(),
                  "actual_input_captured": (directory / "http-request.body.json").is_file(),
                  "unchanged_8192_budget": wire.get("max_tokens") == 8192}
        if case == "missing_usage":
            checks["unknown_not_estimated"] = result["usage"] is None
        if case == "missing_model":
            checks["model_not_inferred"] = result["actual_model"] == "未记录"
        if case == "provider_failure":
            checks["no_second_provider_dispatch"] = result["provider_request_attempts"] == 1
        if case == "empty_answer":
            checks["second_call_blocked"] = bool(result["violations"])
        if case == "close_failure":
            checks["original_output_retained"] = result["answer"] == "离线测试替身：教学内容。"
            checks["cleanup_failure_reported"] = result["failure_kind"] == "cleanup_failure" and bool(result["cleanup_errors"])
        if case in ("length", "length_separate_usage", "content_filter"):
            checks["no_native_continuation_attempt"] = result["sdk_call_attempts"] == 1 and not result["violations"]
            checks["terminal_category"] = result["failure_kind"] == ("provider_content_filter" if case == "content_filter" else "output_truncated")
            checks["original_output_retained"] = result["answer"] == "离线测试替身：教学内容。"
        if case == "length_separate_usage":
            checks["final_usage_preserved"] = result["usage"]["completion_tokens"] == 8192
        if case == "thinking_disabled":
            checks["disabled_on_wire"] = wire.get("thinking") == {"type": "disabled"}
            checks["concise_persona_captured"] = any("保持简洁" in str(item) for item in wire.get("messages", []))
        rows.append({"case": case, "status": result["status"], "checks": checks,
                     "elapsed_seconds": time.monotonic() - started,
                     "result_sha256": hashlib.sha256((directory / "worker-result.json").read_bytes()).hexdigest()})
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    report = {"status": "passed" if all(all(row["checks"].values()) for row in rows) else "failed",
              "execution_kind": "native_deeptutor_with_test_double", "real_model_calls": 0,
              "sources": sources,
              "network": "socket.connect blocked inside native execution; no API credentials supplied",
              "cases": rows}
    (output / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
