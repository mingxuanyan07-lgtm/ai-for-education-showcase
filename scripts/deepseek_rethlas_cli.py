"""Project CLI: DeepSeek stages and separately archived current-chat final review."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--config", default="setup/deepseek-rethlas.json")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight", help="Read-only installation and configuration check")
    run = commands.add_parser("run", help="Run DeepSeek stages; stop pending current-chat review")
    run.add_argument("--problem-file", required=True)
    run.add_argument("--requirements-file", required=True)
    run.add_argument("--user-prompt-file", required=True)
    run.add_argument("--question-id")
    run.add_argument("--tag", action="append", default=[])
    run.add_argument("--max-rounds", type=int, choices=range(1, 4))
    run.add_argument("--timeout-seconds", type=int)
    run.add_argument("--record-kind", choices=("math_experiment", "installation_test"))
    run.add_argument("--teaching-contract", choices=("legacy", "knowledge-route-v1"))
    run.add_argument("--architecture-profile", choices=("legacy", "verified-v2"))
    run.add_argument("--max-model-calls", type=int)
    run.add_argument("--max-tool-calls", type=int)
    run.add_argument("--max-total-seconds", type=int)
    run.add_argument("--max-observed-tokens", type=int)
    run.add_argument("--dry-run", action="store_true")
    final = commands.add_parser("finalize", help="Import the actual current Codex chat's saved final review")
    final.add_argument("--request", required=True)
    final.add_argument("--prompt", required=True)
    final.add_argument("--report", required=True)
    final.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        root = Path(args.project_root).resolve(strict=True)
        config_path = (root / args.config).resolve(strict=True)
        if not config_path.is_relative_to(root):
            raise ValueError("Configuration must be inside the project")
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        if args.command == "preflight":
            from deepseek_rethlas.runtime import preflight
            result = preflight(root, config)
        elif args.command == "run":
            from deepseek_rethlas.pipeline import run_pipeline
            for key in ("max_rounds", "timeout_seconds", "record_kind", "teaching_contract", "architecture_profile",
                        "max_model_calls", "max_tool_calls", "max_total_seconds", "max_observed_tokens"):
                value = getattr(args, key)
                if value is not None:
                    config[key] = value
            result = run_pipeline(root, args.problem_file, args.requirements_file,
                                  args.user_prompt_file, question_id=args.question_id,
                                  tags=args.tag, config=config, dry_run=args.dry_run)
        else:
            from deepseek_rethlas.final_review import finalize_review
            result = finalize_review(root, args.request, args.prompt, args.report, dry_run=args.dry_run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") not in ("failed", "timeout", "interrupted", "unverified") else 1
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        print(json.dumps({"status": "failed", "failure_kind": getattr(error, "kind", "configuration_or_artifact_failure"),
                          "error": str(error)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
