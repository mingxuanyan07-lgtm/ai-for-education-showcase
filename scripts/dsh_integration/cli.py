"""Command-line access to the same operations as the DSH MCP server.

Useful for local acceptance, debugging, and as a fallback if the MCP server is
not mounted. Every command prints JSON; it never calls a model unless
``task-start ... --allow-model-calls`` is explicitly given for mode=run.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dsh_integration import core  # noqa: E402


def _print(data) -> int:
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0 if data.get("ok", True) else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health")

    service = sub.add_parser("service")
    service.add_argument("service", choices=["deeptutor", "rethlas_verification"])
    service.add_argument("action", choices=["status", "start", "stop"], default="status", nargs="?")
    service.add_argument("--confirm", action="store_true")
    service.add_argument("--wait-seconds", type=int, default=0)

    start = sub.add_parser("task-start")
    start.add_argument("system", choices=["deepseek", "rethlas_codex", "deeptutor"])
    start.add_argument("--mode", choices=["run", "dry_run", "check"], default="run")
    start.add_argument("--allow-model-calls", action="store_true")
    start.add_argument("--options", help="JSON object with task options")
    start.add_argument("--options-file", help="project-relative JSON file with task options")
    start.add_argument("--identity", help="JSON object with Rethlas sidecar identity")

    status = sub.add_parser("status")
    status.add_argument("task_id")

    results = sub.add_parser("results")
    results.add_argument("task_id")

    logs = sub.add_parser("logs")
    logs.add_argument("task_id")
    logs.add_argument("--source", choices=["stdout", "stderr"], default="stdout")
    logs.add_argument("--lines", type=int, default=80)

    cancel = sub.add_parser("cancel")
    cancel.add_argument("task_id")
    cancel.add_argument("--confirm", action="store_true")

    review = sub.add_parser("final-review")
    review.add_argument("task_id")

    archive = sub.add_parser("archive")
    archive.add_argument("--manifest", required=True)
    archive.add_argument("--no-refresh", action="store_true")

    evaluate = sub.add_parser("eval-prepare")
    evaluate.add_argument("--spec", required=True)
    evaluate.add_argument("--write", action="store_true", help="run prepare without --dry-run")

    sub.add_parser("eval-refresh")

    args = parser.parse_args(argv)
    try:
        if args.command == "health":
            return _print(core.project_health())
        if args.command == "service":
            return _print(core.service_control(args.service, args.action,
                                               confirm=args.confirm,
                                               wait_seconds=args.wait_seconds))
        if args.command == "task-start":
            if args.options_file:
                options_path = Path(args.options_file)
                if not options_path.is_absolute():
                    options_path = Path(core.PROJECT_ROOT) / options_path
                options = json.loads(options_path.read_text(encoding="utf-8-sig"))
            else:
                options = json.loads(args.options) if args.options else {}
            identity = json.loads(args.identity) if args.identity else None
            return _print(core.task_start(system=args.system, mode=args.mode,
                                          allow_model_calls=args.allow_model_calls,
                                          options=options, identity=identity))
        if args.command == "status":
            return _print(core.task_status(args.task_id))
        if args.command == "results":
            return _print(core.task_results(args.task_id))
        if args.command == "logs":
            return _print(core.task_logs(args.task_id, source=args.source, lines=args.lines))
        if args.command == "cancel":
            return _print(core.task_cancel(args.task_id, confirm=args.confirm))
        if args.command == "final-review":
            return _print(core.prepare_final_review(args.task_id))
        if args.command == "archive":
            return _print(core.archive_record(manifest=args.manifest, refresh=not args.no_refresh))
        if args.command == "eval-prepare":
            return _print(core.evaluation_prepare(spec=args.spec, dry_run=not args.write))
        if args.command == "eval-refresh":
            return _print(core.evaluation_refresh())
        raise SystemExit("unknown command")
    except core.WorkflowError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
