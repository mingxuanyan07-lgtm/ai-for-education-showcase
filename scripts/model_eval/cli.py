"""模型与工作流评测接入命令行。

默认只做本地文件操作：准备任务包、导入真实输入输出、补充核验与人工材料。
`run-dsh` 默认只打印将执行的命令；只有显式 `--execute` 才启动 DSH headless
（会真实调用模型）。离线 dry-run 不写实验记录、不调用模型。
"""
from __future__ import annotations

from datetime import datetime, timezone
import argparse
import json
from pathlib import Path
import sys

_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import experiment_records  # noqa: E402

from . import dsh  # noqa: E402
from .manifest import (  # noqa: E402
    annotate_record,
    assert_no_secrets,
    find_candidate,
    import_candidate,
    prepare_batch,
    verify_iteration,
)
from .spec import EvalSpecError, load_json_file, load_spec, safe_id  # noqa: E402
from .staging import BatchStore  # noqa: E402

DEFAULT_ROOT = _SCRIPTS.parent


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_text(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        raise EvalSpecError(f"{label} 不可读取：{path}（{error}）") from error


def _payload_from_parsed(parsed: dict, source: str, dsh_version: str) -> dict:
    """把会话解析结果变成一次导入所需的数据；独立性要求单次用户输入。"""
    user_messages = parsed.get("user_messages") or []
    if len(user_messages) != 1:
        raise EvalSpecError(
            f"{source} 含 {len(user_messages)} 条用户消息；独立评测必须是一次任务的新会话。"
            "请新建会话后重新导出，或改用 manual_import 明确提供本次完整输入。")
    response = (parsed.get("final_answer") or "").strip()
    if not response:
        raise EvalSpecError(f"{source} 没有最终 assistant 回答，不能作为 completed 导入")
    metadata = dsh.metadata_from_session(parsed, dsh_version=dsh_version,
                                        source=source)
    bundle = dsh.build_bundle(parsed, source=source, dsh_version=dsh_version)
    extra_settings = {"dsh_session": {
        "source": source,
        "session_id": parsed.get("session_id") or "未记录",
        "format_version": parsed.get("format_version"),
        "agent_preset": parsed.get("agent_preset"),
        "session_origin": parsed.get("session_origin"),
        "turn_count": parsed.get("turn_count"),
        "sandbox_mode": parsed.get("sandbox_mode"),
        "permission_preset": parsed.get("permission_preset"),
        "approval_policy": parsed.get("approval_policy"),
        "tools": parsed.get("tools") or [],
        "request_headers": parsed.get("request_headers") or [],
    }}
    return {"metadata": metadata, "response": response, "actual_input": user_messages[0],
            "bundle": bundle, "session_jsonl": parsed.get("session_jsonl"),
            "extra_settings": extra_settings, "source": source}


def _payload_from_events(text: str, dsh_version: str) -> dict:
    parsed = dsh.parse_headless_events(text)
    response = (parsed.get("final_answer") or "").strip()
    status = "completed" if response else "failed"
    metadata = {
        "status": status,
        "actual_model": "未记录",
        "provider": "未记录",
        "framework": "DSH（DeepSeek Harness）",
        "dsh_version": dsh_version,
        "tool_permissions": "未记录（事件流不包含权限字段）",
        "generation": {"json_events": {"event_count": parsed.get("event_count"),
                                       "phase_count": len(parsed.get("phases") or []),
                                       "turn_end": parsed.get("turn_end")}},
        "timing": {"event_count": parsed.get("event_count")},
        "usage": parsed.get("usage") or {"note": "DSH --json 未提供完整 usage"},
        "tool_events": parsed.get("tool_events") or [],
        "error": None if status == "completed" else "事件流中没有 final 事件或文本",
        "notes": ("从 DSH --json 事件流导入；实际模型未记录。"
                  "usage 仅在 step_end 状态事件给出完整样本时才有值，否则未记录。"),
        "round": {"iteration_id": "initial", "type": "initial",
                  "change_reason": "DSH 独立初稿；未参考其他模型回答"},
    }
    bundle = {"schema_version": 1, "source": "dsh_json_events", "captured_at": _now_iso(),
              "dsh_version": dsh_version, "session_id": parsed.get("session_id") or "未记录",
              "final_answer": response, "event_count": parsed.get("event_count"),
              "usage": parsed.get("usage"), "turn_end": parsed.get("turn_end"),
              "events": parsed.get("events") or []}
    return {"metadata": metadata, "response": response, "actual_input": None,
            "bundle": bundle, "session_jsonl": None,
            "extra_settings": {"dsh_events": {"session_id": parsed.get("session_id") or "未记录",
                                              "event_count": parsed.get("event_count"),
                                              "turn_end": parsed.get("turn_end"),
                                              "phase_count": len(parsed.get("phases") or []),
                                              "errors": parsed.get("errors") or []}},
            "source": "dsh_json_events"}


def _stage_payload(store: BatchStore, candidate_id: str, payload: dict) -> dict:
    """把会话解析结果暂存为可导入文件；返回路径与证据列表。"""
    staged_metadata = store.stage_text(candidate_id, "metadata",
                                       _json(payload["metadata"]), suffix=".json")
    staged_response = store.stage_text(candidate_id, "response-from-session",
                                       payload["response"], suffix=".md")
    staged_bundle = store.stage_bytes(candidate_id, "dsh-bundle",
                                      _json(payload["bundle"]).encode("utf-8"), suffix=".json")
    evidence = [staged_bundle["staged"]]
    if payload.get("session_jsonl"):
        staged_session = store.stage_bytes(candidate_id, "dsh-session-export",
                                           payload["session_jsonl"], suffix=".jsonl")
        evidence.append(staged_session["staged"])
    return {"metadata": staged_metadata, "response": staged_response, "evidence": evidence}


def cmd_prepare(args) -> dict:
    return prepare_batch(args.project_root, args.spec, dry_run=args.dry_run)


def cmd_import(args) -> dict:
    root = Path(args.project_root).resolve(strict=True)
    spec = load_spec(args.spec, root)
    candidate = find_candidate(spec, args.candidate)
    store = BatchStore(root, spec["batch_id"])
    sources = [bool(args.response or args.metadata), bool(args.bundle),
               bool(args.dsh_export), bool(args.session_file), bool(args.dsh_events)]
    if sum(sources) != 1:
        raise EvalSpecError(
            "import 需要且只能选择一种来源：手动 --response/--metadata、--bundle、"
            "--dsh-export、--session-file 或 --dsh-events")
    if args.dry_run and (args.dsh_export or args.session_file or args.dsh_events or args.bundle):
        return _import_from_session_source(args, root, spec, candidate, store, dry_run=True)
    if args.response or args.metadata:
        if not args.response or not args.metadata:
            raise EvalSpecError("手动导入必须同时提供 --response 与 --metadata")
        response_text = _read_text(Path(args.response), "回答文件")
        metadata = load_json_file(args.metadata, "导入元数据")
        assert_no_secrets(metadata, "导入元数据")
        from .spec import normalize_metadata, normalize_verification, not_run_verification
        normalized = normalize_metadata(metadata, spec, candidate)
        if normalized["status"] == "completed" and not response_text.strip():
            raise EvalSpecError("回答文件为空；空回答不能记为完成")
        verification = not_run_verification()
        if args.verification:
            raw_verification = load_json_file(args.verification, "核验结果")
            assert_no_secrets(raw_verification, "核验结果")
            verification = [normalize_verification(raw_verification, spec, candidate)]
        if args.dry_run:
            return {"dry_run": True, "wrote": False,
                    "candidate_id": candidate["candidate_id"], "ai_id": candidate["ai_id"],
                    "run_id": args.run_id or "预览（未保存）",
                    "status": normalized["status"],
                    "actual_model": normalized["actual_model"],
                    "verification_verdict": verification[0]["verdict"],
                    "response_sha256": __import__("hashlib").sha256(
                        response_text.encode("utf-8")).hexdigest(),
                    "notice": "dry-run 未写入暂存文件，未创建实验记录，未调用模型。"}
        return import_candidate(
            root, args.spec, args.candidate,
            response_path=args.response, metadata_path=args.metadata,
            verification_path=args.verification, report_path=args.report,
            run_id=args.run_id, dry_run=False)
    return _import_from_session_source(args, root, spec, candidate, store, dry_run=False)


def _import_from_session_source(args, root, spec, candidate, store, *, dry_run: bool) -> dict:
    if args.bundle:
        bundle = load_json_file(args.bundle, "导入包")
        assert_no_secrets(bundle, "导入包")
        response = bundle.get("response") or bundle.get("final_answer")
        if not isinstance(response, str) or not response.strip():
            raise EvalSpecError("导入包缺少非空 response/final_answer")
        metadata = bundle.get("metadata")
        if not isinstance(metadata, dict):
            raise EvalSpecError("导入包缺少 metadata 对象；应包含实际模型、状态与设置")
        actual_input = bundle.get("actual_input")
        if actual_input is not None and not isinstance(actual_input, str):
            raise EvalSpecError("导入包 actual_input 必须是字符串")
        payload = {"metadata": metadata, "response": response,
                   "actual_input": actual_input, "bundle": bundle,
                   "session_jsonl": None,
                   "extra_settings": {"dsh_bundle": {"source": bundle.get("source"),
                                                     "dsh_version": bundle.get("dsh_version"),
                                                     "desktop_app_version": bundle.get("desktop_app_version"),
                                                     "session_id": bundle.get("session_id")}},
                   "source": str(bundle.get("source") or "bundle")}
    elif args.dsh_export:
        parsed = dsh.parse_session_export_zip(args.dsh_export, session_id=args.dsh_session_id)
        payload = _payload_from_parsed(parsed, "dsh_session_export_zip",
                                       args.dsh_version or "未记录")
    elif args.session_file:
        parsed = dsh.read_session_file(args.session_file)
        payload = _payload_from_parsed(parsed, "dsh_session_log_file",
                                       args.dsh_version or "未记录")
    else:
        text = _read_text(Path(args.dsh_events), "DSH --json 事件流")
        payload = _payload_from_events(text, args.dsh_version or "未记录")
    if dry_run:
        import hashlib
        from .spec import normalize_metadata, normalize_verification
        normalized = normalize_metadata(payload["metadata"], spec, candidate)
        verdict = "not_run"
        if args.verification:
            raw_verification = load_json_file(args.verification, "核验结果")
            assert_no_secrets(raw_verification, "核验结果")
            verdict = normalize_verification(raw_verification, spec, candidate)["verdict"]
        return {"dry_run": True, "wrote": False, "batch_id": spec["batch_id"],
                "candidate_id": candidate["candidate_id"], "ai_id": candidate["ai_id"],
                "source": payload["source"], "status": normalized["status"],
                "actual_model": normalized["actual_model"],
                "verification_verdict": verdict,
                "response_sha256": hashlib.sha256(payload["response"].encode("utf-8")).hexdigest(),
                "actual_input_sha256": (hashlib.sha256(payload["actual_input"].encode("utf-8")).hexdigest()
                                        if payload.get("actual_input") else None),
                "notice": "dry-run 只解析会话内容，未写入暂存文件，未创建实验记录，未调用模型。"}
    staged = _stage_payload(store, candidate["candidate_id"], payload)
    result = import_candidate(
        root, args.spec, args.candidate,
        response_path=str(staged["response"]["absolute"]),
        metadata_path=str(staged["metadata"]["absolute"]),
        verification_path=args.verification, report_path=args.report,
        run_id=args.run_id, dry_run=False,
        extra_evidence=staged["evidence"], extra_settings=payload["extra_settings"],
        actual_input_text=payload.get("actual_input"))
    result["source"] = payload["source"]
    return result


def cmd_verify(args) -> dict:
    return verify_iteration(args.project_root, args.spec, args.candidate,
                            verification_path=args.verification, report_path=args.report,
                            run_id=args.run_id, dry_run=args.dry_run)


def cmd_annotate(args) -> dict:
    return annotate_record(args.project_root, args.spec, args.candidate,
                           task_review_path=args.task_review,
                           human_review_path=args.human_review,
                           run_id=args.run_id, dry_run=args.dry_run)


def cmd_refresh(args) -> dict:
    root = Path(args.project_root).resolve(strict=True)
    return experiment_records.refresh_records(root)


def cmd_dsh_info(args) -> dict:
    info = dsh.detect_install(probe=args.probe)
    info["note"] = ("只读探测，未调用模型。DSH 是 Agent 工作台，版本号不是模型名；"
                    "实际模型必须来自响应或会话日志。")
    return info


def cmd_run_dsh(args) -> dict:
    root = Path(args.project_root).resolve(strict=True)
    spec = load_spec(args.spec, root)
    candidate = find_candidate(spec, args.candidate)
    mode = (candidate.get("invocation") or {}).get("mode")
    if mode != "dsh_headless":
        raise EvalSpecError(
            f"候选 {candidate['candidate_id']} 的 invocation.mode 是 {mode!r}，不是 dsh_headless；"
            "其他模型请用 import 导入完整输入输出")
    install = dsh.detect_install(probe=True)
    cli = Path(install["cli"]) if install["cli"] != "未记录" else None
    if cli is None:
        raise EvalSpecError("未找到 dsh 命令行入口；请先运行 dsh-info 检查安装")
    prompt_text = spec["prompt"]["text"]
    command = dsh.build_headless_command(prompt_text, cli=cli,
                                         profile=candidate["invocation"].get("profile", "headless"))
    timeout = args.timeout or candidate["budget"].get("timeout_seconds") or 900
    preview = {
        "candidate_id": candidate["candidate_id"], "ai_id": candidate["ai_id"],
        "framework": candidate["framework"], "configured_model": candidate["configured_model"],
        "dsh": {"runtime_version": install["runtime_version"],
                "desktop_app_version": install["desktop_app_version"],
                "cli": install["cli"], "version_source": install["runtime_version_source"]},
        "command": command["display"], "argv": command["argv"],
        "stdin_sha256": __import__("hashlib").sha256(prompt_text.encode("utf-8")).hexdigest(),
        "timeout_seconds": timeout,
        "preflight": [
            "真实执行会调用 DSH headless 所配置的模型并产生费用；请先确认模型与调用预算。",
            "headless profile 默认禁用 GUI 托管凭据，需要启动环境或 $DSH_HOME/.env 中的 API Key；本机是否已配置见 dsh-info。",
            "运行时会话日志与 --json 事件流将作为原始证据保存；超时将终止进程树且不重试。",
            "不会通过 --patch 或命令参数关闭权限检查；以会话日志记录的 sandbox/权限预设为准。",
        ],
        "notice": ("预览：尚未执行 DSH。真实执行会调用已配置模型并产生费用；"
                   "需要 --execute 且由用户事先确认模型、凭据与预算。"
                   "本项目的 dry-run/离线测试不执行此命令。"),
    }
    if not args.execute:
        preview.update({"dry_run": True, "wrote": False})
        return preview
    store = BatchStore(root, spec["batch_id"])
    from .manifest import _prepare_for_import  # 复用同一批次准备逻辑
    _prepare_for_import(store, spec)
    plan = store.load_plan() or {}
    identity = plan["candidates"][candidate["candidate_id"]]
    workspace = store.candidate_dir(candidate["candidate_id"]) / "dsh-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    run = dsh.run_headless(prompt_text, cli=cli, cwd=workspace, timeout_seconds=timeout,
                           profile=candidate["invocation"].get("profile", "headless"))
    staged_stdout = store.stage_bytes(candidate["candidate_id"], "dsh-stdout",
                                      (run.get("stdout") or "").encode("utf-8"), suffix=".jsonl")
    staged_stderr = store.stage_bytes(candidate["candidate_id"], "dsh-stderr",
                                      (run.get("stderr") or "").encode("utf-8"), suffix=".log")
    events = {}
    parse_error = None
    try:
        events = dsh.parse_headless_events(run.get("stdout") or "")
    except EvalSpecError as error:
        parse_error = str(error)
    parsed_session = None
    for _attempt in range(3):
        if events.get("session_id"):
            parsed_session = dsh.read_session_by_id(events["session_id"])
        if parsed_session:
            break
        import time
        time.sleep(0.5)
    response_text = (events.get("final_answer") or "").strip()
    if parsed_session and parsed_session.get("final_answer"):
        response_text = response_text or parsed_session["final_answer"]
    status = run["status"]
    error = None
    if status == "timeout":
        error = f"headless 超时（{timeout}s），已终止进程树，未重试"
    elif status == "failed":
        stderr_tail = (run.get("stderr") or "").strip().splitlines()[-3:]
        error = "headless 失败：" + (" / ".join(stderr_tail) if stderr_tail else "未知原因")
    if status == "completed" and not response_text:
        status = "failed"
        error = "headless 退出码 0 但没有最终回答"
    metadata = (dsh.metadata_from_session(parsed_session, dsh_version=install["runtime_version"],
                                          desktop_app_version=install["desktop_app_version"],
                                          source="dsh_headless_run")
                if parsed_session else {
                    "status": status, "actual_model": "未记录", "provider": "未记录",
                    "framework": "DSH（DeepSeek Harness）",
                    "dsh_version": install["runtime_version"],
                    "desktop_app_version": install["desktop_app_version"],
                    "tool_permissions": "未记录（未找到本次会话日志）",
                    "generation": {"json_event_count": events.get("event_count", 0)},
                    "timing": {"duration_seconds": run["duration_seconds"]},
                    "usage": {"note": "未找到会话日志，token 用量未记录"},
                    "tool_events": events.get("tool_events") or [],
                    "notes": "从 DSH headless 事件流导入；实际模型与权限未记录。",
                    "round": {"iteration_id": "initial", "type": "initial",
                              "change_reason": "DSH headless 独立初稿；未参考其他模型回答"}})
    metadata["status"] = status
    metadata["error"] = error
    metadata["generation"] = dict(metadata.get("generation") or {})
    metadata["generation"]["headless_run"] = {
        "argv": run["argv"], "cwd": run["cwd"], "exit_code": run["exit_code"],
        "timed_out": run["timed_out"], "duration_seconds": run["duration_seconds"],
        "timeout_seconds": run["timeout_seconds"],
        "session_log_found": bool(parsed_session),
        "session_log_note": (None if parsed_session else
                             "未找到或无法解析会话日志；模型/用量按未记录保存"),
        "event_parse_error": parse_error,
    }
    metadata["timing"] = {**(metadata.get("timing") or {}),
                          "wall_clock_seconds": run["duration_seconds"],
                          "timed_out": run["timed_out"], "exit_code": run["exit_code"]}
    bundle = {
        "schema_version": 1, "source": "dsh_headless_run",
        "captured_at": _now_iso(),
        "dsh_version": install["runtime_version"],
        "desktop_app_version": install["desktop_app_version"],
        "command": run["argv"], "cwd": run["cwd"], "exit_code": run["exit_code"],
        "timed_out": run["timed_out"], "duration_seconds": run["duration_seconds"],
        "session_id": events.get("session_id") or (parsed_session or {}).get("session_id") or "未记录",
        "events": events.get("events") or [],
        "session": ({key: parsed_session[key] for key in
                     ("session_id", "cwd", "actual_model", "actual_provider", "usage",
                      "timing", "sandbox_mode", "permission_preset", "approval_policy",
                      "tools", "request_headers")}
                    if parsed_session else None),
        "notes": ["运行命令与原始事件已保存；未重试。",
                  "会话日志是可观察事件，不是模型全部内部推理。"],
    }
    staged_metadata = store.stage_text(candidate["candidate_id"], "metadata",
                                       _json(metadata), suffix=".json")
    staged_bundle = store.stage_bytes(candidate["candidate_id"], "dsh-bundle",
                                      _json(bundle).encode("utf-8"), suffix=".json")
    staged_response = None
    if response_text:
        staged_response = store.stage_text(candidate["candidate_id"], "response-from-session",
                                           response_text, suffix=".md")
    evidence = [staged_stdout["staged"], staged_stderr["staged"], staged_bundle["staged"]]
    if parsed_session and parsed_session.get("session_jsonl"):
        staged_session = store.stage_bytes(candidate["candidate_id"], "dsh-session-log",
                                           parsed_session["session_jsonl"], suffix=".jsonl")
        evidence.append(staged_session["staged"])
    if staged_response:
        evidence.append(staged_response["staged"])
    result = import_candidate(
        root, args.spec, args.candidate,
        response_path=str(staged_response["absolute"]) if staged_response else None,
        metadata_path=str(staged_metadata["absolute"]),
        verification_path=args.verification, report_path=args.report,
        run_id=identity["run_id"], dry_run=False,
        extra_evidence=evidence,
        extra_settings={"headless_run": metadata["generation"]["headless_run"]},
        actual_input_text=prompt_text)
    result.update({"run": {key: run[key] for key in
                           ("status", "exit_code", "timed_out", "duration_seconds")},
                   "session_log_found": bool(parsed_session)})
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="模型与工作流评测接入：复用 experiment_records，不调用模型（run-dsh --execute 除外）。")
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project-root", type=Path, default=DEFAULT_ROOT)

    prepare = sub.add_parser("prepare", parents=[common],
                             help="校验评测规格并生成候选任务包；不调用模型")
    prepare.add_argument("--spec", required=True, help="项目内评测规格 JSON")
    prepare.add_argument("--dry-run", action="store_true", help="只校验并显示身份，不写文件")
    prepare.set_defaults(func=cmd_prepare)

    imp = sub.add_parser("import", parents=[common],
                         help="导入真实输入输出并归档；不调用模型")
    imp.add_argument("--spec", required=True)
    imp.add_argument("--candidate", required=True)
    imp.add_argument("--response", help="完整原始回答文件（UTF-8）")
    imp.add_argument("--metadata", help="导入元数据 JSON")
    imp.add_argument("--bundle", help="完整输入输出导入包 JSON")
    imp.add_argument("--dsh-export", help="DSH Web 会话导出 ZIP")
    imp.add_argument("--dsh-session-id", help="导出 ZIP 含多个会话时指定本次会话 id")
    imp.add_argument("--session-file", help="DSH 会话日志 JSONL/JSONL.zstd")
    imp.add_argument("--dsh-events", help="保存下来的 DSH --json 事件流文件")
    imp.add_argument("--verification", help="独立核验结果 JSON")
    imp.add_argument("--report", help="核验报告附件")
    imp.add_argument("--dsh-version", help="实际 DSH 版本（无法确认时省略并记为未记录）")
    imp.add_argument("--run-id", help="沿用已保存的 run_id（仅续跑/迭代）")
    imp.add_argument("--dry-run", action="store_true")
    imp.set_defaults(func=cmd_import)

    verify = sub.add_parser("verify", parents=[common], help="补充某一轮的独立核验结果")
    verify.add_argument("--spec", required=True)
    verify.add_argument("--candidate", required=True)
    verify.add_argument("--verification", required=True)
    verify.add_argument("--report")
    verify.add_argument("--run-id")
    verify.add_argument("--dry-run", action="store_true")
    verify.set_defaults(func=cmd_verify)

    annotate = sub.add_parser("annotate", parents=[common], help="补充自动分析或用户提供的人工评价")
    annotate.add_argument("--spec", required=True)
    annotate.add_argument("--candidate", required=True)
    annotate.add_argument("--task-review", help="自动检查说明文件（需人工确认）")
    annotate.add_argument("--human-review", help="用户明确提供的评价文字文件")
    annotate.add_argument("--run-id")
    annotate.add_argument("--dry-run", action="store_true")
    annotate.set_defaults(func=cmd_annotate)

    refresh = sub.add_parser("refresh", parents=[common], help="刷新阅读页面与索引")
    refresh.set_defaults(func=cmd_refresh)

    info = sub.add_parser("dsh-info", parents=[common], help="只读显示 DSH 安装/版本/接口能力")
    info.add_argument("--probe", action="store_true", help="运行 dsh --version（不调用模型）")
    info.set_defaults(func=cmd_dsh_info)

    run = sub.add_parser("run-dsh", parents=[common],
                         help="预览或显式执行 DSH headless 并归档；默认不执行")
    run.add_argument("--spec", required=True)
    run.add_argument("--candidate", required=True)
    run.add_argument("--execute", action="store_true", help="真实启动 DSH（会调用模型并产生费用）")
    run.add_argument("--timeout", type=int, help="覆盖候选预算中的超时秒数")
    run.add_argument("--verification")
    run.add_argument("--report")
    run.set_defaults(func=cmd_run_dsh)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
    except (EvalSpecError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"评测接入失败：{error}", file=sys.stderr)
        return 2
    print(_json(result), end="")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
