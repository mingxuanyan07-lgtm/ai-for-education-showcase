"""Hybrid Tutor host. Dry-run never calls a model or writes an experiment."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid


def _write_result(root, value, payload, *, filename="outcome.json"):
    from deepseek_rethlas.pipeline import _inside
    from hybrid_tutor.bridge import _commit_file_with_retry
    path = _inside(root, value, exists=False)
    if not path.is_relative_to(root / "work/hybrid-tutor/tasks") or path.name != filename:
        raise ValueError("Result and receipt must be in work/hybrid-tutor/tasks/<task>/ with the expected filename")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Results and receipts are create-only; original evidence is append-only.
    if path.exists():
        raise ValueError("An outcome already exists; refusing to overwrite it")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     suffix=".tmp", delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    _commit_file_with_retry(temporary, path, replace_existing=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--config", default="setup/hybrid-tutor.json")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    for field in ("problem", "requirements", "user-prompt"):
        run.add_argument("--" + field + "-file", required=True)
    run.add_argument("--question-id")
    run.add_argument("--tag", action="append", default=[])
    run.add_argument("--result-file")
    run.add_argument("--receipt-file")
    mode = run.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--allow-model-calls", action="store_true",
                      help="Only after the user has approved the explicit model and shared budget")
    args = parser.parse_args(argv)
    root = Path(args.project_root).resolve(strict=True)
    try:
        from hybrid_tutor.config import load_config
        from hybrid_tutor.deeptutor_runtime import run_call
        from deepseek_rethlas.pipeline import run_pipeline
        config, path, digest, math_file = load_config(root, args.config)
        if args.receipt_file:
            from deepseek_rethlas.pipeline import _inside
            receipt = _inside(root, args.receipt_file, exists=False)
            outcome = _inside(root, args.result_file, exists=False) if args.result_file else None
            if (outcome is None or receipt.parent != outcome.parent or receipt.name != "run-receipt.json"
                    or not receipt.is_relative_to(root / "work/hybrid-tutor/tasks") or receipt.exists()):
                raise ValueError("Receipt must be a new run-receipt.json beside the outcome")
        if not args.dry_run:
            # Freeze the exact checked math bytes. Later live setup edits cannot
            # replace the approved computation executable for this run.
            raw_math = math_file.read_bytes()
            if hashlib.sha256(raw_math).hexdigest() != config["math_runtime_sha256"]:
                raise ValueError("Math runtime changed during dispatch")
            dispatch = root / "work/hybrid-tutor/dispatch" / uuid.uuid4().hex
            dispatch.mkdir(parents=True, exist_ok=False)
            math_file = dispatch / "math-runtime.json"
            math_file.write_bytes(raw_math)
            (dispatch / "configuration-source.json").write_text(json.dumps({
                "config_file": str(path), "sha256": digest,
                "math_runtime_sha256": config["math_runtime_sha256"]}, indent=2), encoding="utf-8")
        def receipt_callback(identity):
            if args.receipt_file:
                _write_result(root, args.receipt_file, {**identity, "config_sha256": digest},
                              filename="run-receipt.json")
        result = run_pipeline(root, args.problem_file, args.requirements_file,
                              args.user_prompt_file, question_id=args.question_id,
                              tags=args.tag, config=config["proof"], dry_run=args.dry_run,
                              teaching_caller=run_call, teaching_config=config["teaching"],
                              math_runtime_file=math_file, on_run_created=receipt_callback)
        result["hybrid_configuration"] = {"file": path.relative_to(root).as_posix(), "sha256": digest}
        result["workflow"] = "hybrid-rethlas-deeptutor-v1"
    except (OSError, ValueError, KeyError, RuntimeError, ImportError) as error:
        result = {"status": "failed", "failure_kind": getattr(error, "kind", "configuration_or_artifact_failure"),
                  "error": str(error), "workflow": "hybrid-rethlas-deeptutor-v1"}
    if args.result_file:
        try:
            _write_result(root, args.result_file, result)
        except (OSError, ValueError) as error:
            print(json.dumps({"status": "failed", "failure_kind": "result_write_failure",
                              "error": str(error), "task_result": result}, ensure_ascii=False), file=sys.stderr)
            return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {"dry_run", "pending_final_review"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
