"""DSH（DeepSeek Harness）适配：版本探测、会话导出解析与 headless 受控运行。

本机已核对（2026-10-20，只读检查与 `--version`/`--help`，未调用模型）：
- 安装根下 `app/version` 是 Electron 外壳版本（44.7.0，**不是 DSH 版本**）；
- `dsh --version` 报 DSH 运行时版本 0.2.1-rc.2；
- CLI 可通过 `DSH_CLI` 或 `DSH_INSTALL_ROOT` 显式配置；
- 本轮实际运行过 `dsh --profile headless --help`，确认存在 `--json`、`--session-id`，任务可由 stdin 提供；
- 会话日志位于 `%DSH_HOME%\\sessions\\<编码 cwd>\\<session-id>\\session.vN.jsonl[.zstd]`，
  v4 事件包含 user/message、assistant/message（model/provider/usage）、request/header
  （模型配置与工具表）、tool/call、tool/result、sandbox/mode、permission/preset；
- 官方导出为 Web 会话菜单 `/export` 或“下载 Session 日志”得到的 ZIP（浏览器下载）。

本模块不调用模型。`run_headless()` 只有调用方显式要求执行时才会启动 DSH 进程；
本轮离线测试只验证命令构造、事件解析与失败映射，不进行真实模型调用。
"""
from __future__ import annotations

from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import time
import zipfile

from .spec import EvalSpecError

DSH_DEFAULT_INSTALL = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share")) / "Programs" / "DeepSeek Harness"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
SESSION_MEMBER = re.compile(r"(?:^|/)session(?:\.v[0-9]+)?\.jsonl$")
SESSION_FILE_NAME = re.compile(r"session(?:\.v([0-9]+))?\.jsonl(?:\.zstd)?$")
SAFE_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
USAGE_KEYS = ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens",
              "totalTokens", "reasoningTokens")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _env(env=None) -> dict:
    return dict(os.environ if env is None else env)


def install_root(env=None) -> tuple[Path | None, str]:
    """定位 DSH 安装根；只读取版本文件，不启动任何程序。"""
    environment = _env(env)
    candidates = []
    if environment.get("DSH_INSTALL_ROOT"):
        candidates.append((Path(environment["DSH_INSTALL_ROOT"]), "env:DSH_INSTALL_ROOT"))
    candidates.append((DSH_DEFAULT_INSTALL, "本机默认路径"))
    for path, source in candidates:
        try:
            if (path / "app" / "version").is_file():
                return path, source
        except OSError:
            continue
    return None, "未记录"


def _read_version_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig").strip() or "未记录"
    except OSError:
        return "未记录"


def resolve_cli(env=None) -> tuple[Path | None, str]:
    environment = _env(env)
    if environment.get("DSH_CLI"):
        path = Path(environment["DSH_CLI"])
        return (path, "env:DSH_CLI") if path.is_file() else (None, "env:DSH_CLI 指向的文件不存在")
    root, _source = install_root(environment)
    if root is not None:
        path = root / "app" / "resources" / "runtime" / "cli" / "bin" / "dsh.cmd"
        if path.is_file():
            return path, "安装目录"
    found = shutil.which("dsh")
    if found:
        return Path(found), "PATH"
    return None, "未记录"


def probe_runtime_version(cli: Path | None = None, env=None, timeout: int = 60) -> dict:
    """运行 `dsh --version`（不调用模型）；失败时返回未记录而不是猜测。"""
    cli = cli or resolve_cli(env)[0]
    if cli is None:
        return {"runtime_version": "未记录", "source": "未找到 dsh 命令行入口", "command": None}
    command = [str(cli), "--version"]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=timeout, check=False, env=_env(env))
    except (OSError, subprocess.SubprocessError) as error:
        return {"runtime_version": "未记录", "source": f"探测失败：{error}", "command": command}
    output = (completed.stdout or "").strip().splitlines()
    version = output[-1].strip() if output else "未记录"
    if completed.returncode != 0 or not version:
        return {"runtime_version": "未记录", "source": f"退出码 {completed.returncode}", "command": command}
    return {"runtime_version": version, "source": f"执行 {cli.name} --version", "command": command}


def detect_install(env=None, probe: bool = False) -> dict:
    """只读探测 DSH 安装；probe=True 时才运行不产生模型调用的 --version。"""
    environment = _env(env)
    root, root_source = install_root(environment)
    cli, cli_source = resolve_cli(environment)
    home_text = environment.get("DSH_HOME")
    home = Path(home_text) if home_text else Path.home() / ".dsh"
    runtime = {}
    if root is not None:
        versions_file = root / "app" / "resources" / "runtime" / "versions.json"
        if versions_file.is_file():
            try:
                runtime = json.loads(versions_file.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                runtime = {}
    detected = {
        "detected_at": _now(),
        "install_root": str(root) if root else "未记录",
        "install_root_source": root_source,
        "desktop_app_version": _read_version_file(root / "app" / "version") if root else "未记录",
        "electron_version": _read_version_file(root / "app" / "version") if root else "未记录",
        "version_note": "desktop_app_version/electron_version 是 Electron 外壳版本，不是 DSH 版本；DSH 版本看 runtime_version。",
        "cli": str(cli) if cli else "未记录",
        "cli_source": cli_source,
        "runtime_version": "未记录",
        "runtime_version_source": "未探测（probe=False）" if not probe else "未记录",
        "node_runtime_version": str(runtime.get("node") or "未记录"),
        "pnpm_runtime_version": str(runtime.get("pnpm") or "未记录"),
        "dsh_home": str(home),
        "dsh_home_source": "env:DSH_HOME" if home_text else "默认 ~/.dsh",
        "headless_profile_initialized": (home / "profiles" / "headless").is_dir(),
        "sessions_root_exists": (home / "sessions").is_dir(),
        "session_log_reader": "Node.js Zstandard 解压" if _node_available() else "不可用（缺少 Node.js）",
        "capabilities": {
            "headless_one_task": "已安装版本提供 dsh --profile headless",
            "json_event_stream": "已安装版本提供 --json",
            "session_resume": "已安装版本提供 --session-id",
            "official_session_export_zip": "Web 会话 /export 或“下载 Session 日志”（浏览器下载）",
            "direct_session_log_read": "只读解析 session.vN.jsonl[.zstd]（本地文件，不是官方 SDK）",
            "sdk_mcp": "SDK/ACP 是 profile 而非独立命令；本适配器不管理 MCP",
        },
        "constraints": [
            "DSH 是 Agent 工作台/运行框架，不是模型名；实际模型必须来自响应或会话日志。",
            "在 DSH 中调用 Run-Rethlas.ps1 仍会启动 Codex，不能记为 DeepSeek 模型的解答。",
            "headless 只在通过 dsh 启动器启动时工作；凭据由 headless profile 自身解析，适配器不读取或保存密钥。",
            "工作区工具权限与沙箱能力由 DSH profile 决定，与本项目 Rethlas 的 Codex 配置不等价。",
        ],
    }
    if probe:
        version = probe_runtime_version(cli, environment)
        detected["runtime_version"] = version["runtime_version"]
        detected["runtime_version_source"] = version["source"]
        detected["runtime_version_command"] = version["command"]
    return detected


# ---- 会话日志与导出解析 -------------------------------------------------

def _node_binary() -> str:
    node = shutil.which("node")
    if node:
        return node
    bundled = Path(os.environ.get("USERPROFILE", str(Path.home()))) / (
        ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe")
    if bundled.is_file():
        return str(bundled)
    raise EvalSpecError("读取 DSH 会话日志需要 Node.js（用于解压 Zstandard）；未找到 node。")


def _node_available() -> bool:
    try:
        _node_binary()
        return True
    except EvalSpecError:
        return False


def _decompress_bytes(data: bytes) -> bytes:
    helper = Path(__file__).with_name("zstd_read.cjs")
    with tempfile.NamedTemporaryFile(suffix=".zstd", delete=False) as handle:
        handle.write(data)
        temporary = Path(handle.name)
    try:
        completed = subprocess.run([_node_binary(), str(helper), str(temporary)],
                                   capture_output=True, timeout=120, check=False)
    finally:
        temporary.unlink(missing_ok=True)
    if completed.returncode != 0:
        raise EvalSpecError("DSH 会话日志解压失败："
                            + (completed.stderr.decode("utf-8", "replace")[:400] or "未知错误"))
    return completed.stdout


def _records_with_logical(raw: bytes, label: str) -> tuple[list[dict], bytes]:
    """解压（如需要）一次并返回事件记录与逻辑 JSONL 字节。"""
    if raw[:4] == ZSTD_MAGIC:
        logical = _decompress_bytes(raw)
    else:
        logical = raw
    try:
        text = logical.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise EvalSpecError(f"{label} 不是 UTF-8 JSONL：{error}") from error
    records = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError as error:
            raise EvalSpecError(f"{label} 第 {number} 行不是 JSON：{error}") from error
        if not isinstance(record, dict):
            raise EvalSpecError(f"{label} 第 {number} 行不是 JSON 对象")
        records.append(record)
    if not records:
        raise EvalSpecError(f"{label} 为空，没有可解析的会话事件")
    return records, logical


def _records_from_bytes(raw: bytes, label: str) -> list[dict]:
    return _records_with_logical(raw, label)[0]


def read_session_file(path) -> dict:
    path = Path(path)
    raw = path.read_bytes()
    records, logical = _records_with_logical(raw, f"会话日志 {path.name}")
    parsed = parse_session_records(records)
    parsed["provenance"] = {"kind": "session_log_file", "path": str(path),
                            "sha256": _digest(raw), "logical_sha256": _digest(logical),
                            "bytes": len(raw)}
    parsed["session_jsonl"] = logical
    return parsed


def _message_text(message, *, allowed_types=("text",)) -> str:
    if isinstance(message, str):
        return message
    if not isinstance(message, dict):
        return ""
    return _content_text(message.get("content"), allowed_types=allowed_types)


def _content_text(content, *, allowed_types=("text",)) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if allowed_types is not None and block.get("type") not in allowed_types:
                    continue
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def _as_usage(value) -> dict:
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in USAGE_KEYS if isinstance(value.get(key), (int, float))}


def parse_session_records(records: list[dict]) -> dict:
    """把 DSH v4 会话事件折叠为可导入的完整输入输出与元数据。"""
    header = {}
    user_messages, assistant_messages, request_headers = [], [], []
    tool_calls: dict = {}
    tool_events, sandbox_modes, permission_presets, approval_policies = [], [], [], []
    turn_starts, turn_ends, timestamps = [], [], []
    for record in records:
        record_type = record.get("type")
        data = record.get("data") if isinstance(record.get("data"), dict) else {}
        timestamp = record.get("time")
        if isinstance(timestamp, (int, float)):
            timestamps.append(timestamp)
        if record_type == "session" and not header:
            header = {key: record.get(key) for key in
                      ("id", "cwd", "createdAt", "agentPreset", "origin", "parentSession",
                       "isSeeded", "delegationDepth", "version")}
        elif record_type == "user/message":
            text = _content_text(data.get("content"))
            if text.strip():
                user_messages.append(text)
        elif record_type == "assistant/message":
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            source = message.get("source") if isinstance(message.get("source"), dict) else {}
            assistant_messages.append({
                "text": _message_text(message),
                "model": source.get("model") or data.get("model") or "未记录",
                "provider": source.get("provider") or data.get("provider") or "未记录",
                "usage": _as_usage(data.get("usage")),
                "step": data.get("step"), "turn": data.get("turn"), "time": timestamp,
            })
        elif record_type == "request/header":
            body = data.get("header") if isinstance(data.get("header"), dict) else {}
            config = body.get("config") if isinstance(body.get("config"), dict) else {}
            # 已安装 v4 会话把工具表放在 header.tools（不是 header.config.tools）。
            raw_tools = body.get("tools") or config.get("tools") or []
            tools = [tool.get("name") for tool in raw_tools
                     if isinstance(tool, dict) and isinstance(tool.get("name"), str)]
            request_headers.append({
                "model": config.get("model") or "未记录",
                "provider": config.get("provider") or "未记录",
                "reasoningEffort": config.get("reasoningEffort") or "未记录",
                "maxTokens": config.get("maxTokens"),
                "tool_count": len(tools), "tools": tools,
                "reason": data.get("reason") or "未记录",
            })
        elif record_type == "tool/call":
            call_id = data.get("callId")
            if isinstance(call_id, str):
                tool_calls[call_id] = {"name": data.get("name") or "未记录",
                                       "arguments": data.get("arguments"),
                                       "step": data.get("step"), "turn": data.get("turn")}
        elif record_type == "tool/result":
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            source = message.get("source") if isinstance(message.get("source"), dict) else {}
            call_id = message.get("toolCallId") or source.get("callId")
            entry = dict(tool_calls.pop(call_id, {})) if call_id else {}
            entry.setdefault("name", source.get("tool") or "未记录")
            result_text = _message_text(message)
            entry.update({"is_error": bool(message.get("isError")),
                          "result_text": result_text[:4000],
                          "result_truncated": len(result_text) > 4000,
                          "status": "error" if message.get("isError") else "ok"})
            tool_events.append(entry)
        elif record_type == "sandbox/mode":
            sandbox_modes.append(data.get("mode") or "未记录")
        elif record_type == "permission/preset":
            permission_presets.append(data.get("preset") or "未记录")
        elif record_type == "approval/policy":
            approval_policies.append(data.get("policy") or "未记录")
        elif record_type == "turn/start":
            turn_starts.append(data.get("turn"))
        elif record_type == "turn/end":
            turn_ends.append({"turn": data.get("turn"), "reason": data.get("reason") or "未记录"})
    for call_id, entry in tool_calls.items():
        entry = dict(entry)
        entry.update({"status": "no_result", "is_error": False})
        tool_events.append(entry)
    final_message = next((message for message in reversed(assistant_messages)
                          if message["text"].strip()), None)
    usage_total: dict = {}
    usage_seen = False
    for message in assistant_messages:
        if message["usage"]:
            usage_seen = True
            for key, value in message["usage"].items():
                usage_total[key] = usage_total.get(key, 0) + value
    tools = sorted({name for header_item in request_headers for name in header_item["tools"]}
                   | {event["name"] for event in tool_events if event.get("name")})
    created = header.get("createdAt") if isinstance(header.get("createdAt"), (int, float)) else None
    last = max(timestamps) if timestamps else None
    timing = {"event_count": len(records)}
    if created and last:
        try:
            timing["started_at"] = datetime.fromtimestamp(created / 1000, tz=timezone.utc).isoformat()
            timing["duration_seconds"] = round((last - created) / 1000, 3)
        except (OverflowError, OSError, ValueError):
            pass
    return {
        "session_id": header.get("id") or "未记录",
        "cwd": header.get("cwd") or "未记录",
        "created_at_ms": created,
        "agent_preset": header.get("agentPreset") or "未记录",
        "session_origin": header.get("origin") or "未记录",
        "parent_session": header.get("parentSession") or "未记录",
        "format_version": header.get("version") or "未记录",
        "user_messages": user_messages,
        "assistant_messages": assistant_messages,
        "final_answer": final_message["text"] if final_message else "",
        "actual_model": final_message["model"] if final_message else "未记录",
        "actual_provider": final_message["provider"] if final_message else "未记录",
        "request_headers": request_headers,
        "tools": tools,
        "tool_events": tool_events,
        "sandbox_mode": sandbox_modes[-1] if sandbox_modes else "未记录",
        "permission_preset": permission_presets[-1] if permission_presets else "未记录",
        "approval_policy": approval_policies[-1] if approval_policies else "未记录",
        "turn_count": len(turn_starts),
        "last_turn_end": turn_ends[-1] if turn_ends else None,
        "usage": ({"total": usage_total,
                   "assistant_message_count": len(assistant_messages)} if usage_seen
                  else {"total": {key: None for key in USAGE_KEYS},
                        "assistant_message_count": len(assistant_messages),
                        "note": "会话日志未提供提供方用量"}),
        "timing": timing,
    }


def _newest_generation(archive: zipfile.ZipFile, candidates: list[str]) -> list[str]:
    """同一 session id 有多个 generation 时选文件名/头部版本最高的一个。"""
    def version(name: str) -> int:
        match = SESSION_FILE_NAME.search(PurePosixPath(name).name)
        filename_version = int(match.group(1)) if match and match.group(1) else 0
        header_version = _session_header(archive, name).get("version")
        return max(filename_version, header_version if isinstance(header_version, int) else 0)
    ordered = sorted(candidates, key=version, reverse=True)
    if len(ordered) >= 2 and version(ordered[0]) == version(ordered[1]):
        return ordered
    return ordered[:1]


def _session_header(archive: zipfile.ZipFile, name: str) -> dict:
    try:
        first_line = archive.read(name).split(b"\n", 1)[0].decode("utf-8-sig").strip()
        header = json.loads(first_line) if first_line else {}
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    return header if isinstance(header, dict) else {}


def _root_sessions(archive: zipfile.ZipFile, candidates: list[str]) -> list[str]:
    """用每个候选日志的 session header 判断根会话：根会话没有 parentSession。

    官方导出包含根会话与子会话；当 ZIP 外层有水印目录时，根与子可能同深度。
    """
    roots = []
    for name in candidates:
        header = _session_header(archive, name)
        if header.get("type") != "session":
            continue
        if not header.get("parentSession"):
            roots.append(name)
    return roots


def parse_session_export_zip(path, session_id: str | None = None) -> dict:
    """解析 DSH Web 导出的会话 ZIP；只选根会话（子会话不混入）。

    无法从目录深度或 session header 唯一确定根会话时，必须显式给 session_id；
    不会把子会话或另一个独立会话混入本次记录。
    """
    path = Path(path)
    if not path.is_file():
        raise EvalSpecError(f"DSH 会话导出 ZIP 不存在：{path}")
    if session_id is not None and not SAFE_SESSION_ID.fullmatch(session_id):
        raise EvalSpecError(f"session_id 不是安全标识：{session_id!r}")
    raw_zip = path.read_bytes()
    try:
        with zipfile.ZipFile(io.BytesIO(raw_zip)) as archive:
            members = [name for name in archive.namelist()
                       if not name.endswith("/") and SESSION_MEMBER.search(name)]
            if not members:
                raise EvalSpecError("ZIP 中没有 session*.jsonl；请确认这是 DSH 的“下载 Session 日志”导出")
            if session_id is not None:
                candidates = [name for name in members
                              if _session_header(archive, name).get("id") == session_id]
                if len(candidates) > 1:
                    candidates = _newest_generation(archive, candidates)
                if len(candidates) != 1:
                    raise EvalSpecError(
                        f"导出 ZIP 中没有唯一的 session id={session_id!r} 根日志；"
                        f"候选 {len(candidates)} 个")
            else:
                roots = [name for name in members if len(PurePosixPath(name).parts) == 1]
                if roots:
                    candidates = roots
                else:
                    depths = {len(PurePosixPath(name).parts) for name in members}
                    candidates = [name for name in members
                                  if len(PurePosixPath(name).parts) == min(depths)]
                if len(candidates) > 1:
                    candidates = _root_sessions(archive, candidates)
            if len(candidates) != 1:
                raise EvalSpecError(
                    "ZIP 含多个根会话日志，无法唯一确定本次独立任务："
                    f"{candidates[:5]}；请用 --dsh-session-id 指定本次会话 id")
            member = candidates[0]
            session_raw = archive.read(member)
    except zipfile.BadZipFile as error:
        raise EvalSpecError(f"不是有效的 ZIP 文件：{path}（{error}）") from error
    records, logical = _records_with_logical(session_raw, f"会话导出 {member}")
    parsed = parse_session_records(records)
    if not parsed["assistant_messages"]:
        raise EvalSpecError("会话导出中没有 assistant 回答；不能作为完成的评测回答导入")
    parsed["provenance"] = {"kind": "session_export_zip", "zip": str(path),
                            "member": member, "zip_sha256": _digest(raw_zip),
                            "session_sha256": _digest(session_raw),
                            "logical_sha256": _digest(logical),
                            "session_bytes": len(session_raw)}
    parsed["session_jsonl"] = logical
    return parsed


def find_session_log(session_id: str, dsh_home=None, env=None) -> Path | None:
    """按精确 session id 查找本地会话日志；不扫描或读取其他会话。"""
    if not isinstance(session_id, str) or not SAFE_SESSION_ID.fullmatch(session_id):
        return None
    environment = _env(env)
    home = Path(dsh_home) if dsh_home else Path(
        environment.get("DSH_HOME") or Path.home() / ".dsh")
    root = home / "sessions"
    if not root.is_dir():
        return None
    matches = []
    for project_dir in root.iterdir():
        if not project_dir.is_dir():
            continue
        session_dir = project_dir / session_id
        if not session_dir.is_dir():
            continue
        files = [item for item in session_dir.glob("session*.jsonl*")
                 if item.is_file() and SESSION_FILE_NAME.search(item.name)]
        if files:
            matches.append(max(files, key=_session_version))
    return matches[0] if matches else None


def _session_version(path: Path) -> int:
    match = SESSION_FILE_NAME.search(path.name)
    if match and match.group(1):
        return int(match.group(1))
    return 0


def read_session_by_id(session_id: str, dsh_home=None, env=None) -> dict | None:
    path = find_session_log(session_id, dsh_home, env)
    return read_session_file(path) if path else None


# ---- headless 运行（默认只预览） ---------------------------------------

def build_headless_command(prompt_text: str, *, cli, profile: str = "headless",
                           session_id: str | None = None) -> dict:
    """构造一次 headless 调用；完整 prompt 通过 stdin 传入，不出现在命令行里。"""
    if not isinstance(prompt_text, str) or not prompt_text.strip():
        raise EvalSpecError("headless 任务文本不能为空")
    if cli is None:
        raise EvalSpecError("未找到 dsh 命令行入口；请用 dsh-info 检查安装，或设置 DSH_CLI")
    argv = [str(cli), "--profile", profile, "--json"]
    if session_id is not None:
        if not SAFE_SESSION_ID.fullmatch(session_id):
            raise EvalSpecError(f"session_id 不是安全标识：{session_id!r}")
        argv.extend(["--session-id", session_id])
    display = subprocess.list2cmdline(argv) + "  ← 完整任务通过 stdin 传入"
    return {"argv": argv, "stdin": prompt_text, "display": display,
            "cwd_note": "运行目录由调用方指定；默认使用该候选的独立工作目录"}


def _kill_tree(process) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                       capture_output=True, check=False)
    else:
        process.kill()


def run_headless(prompt_text: str, *, cli, cwd, timeout_seconds: int,
                 profile: str = "headless", session_id: str | None = None,
                 env=None) -> dict:
    """显式执行一次 headless 任务。超时后终止进程树，不重试。"""
    command = build_headless_command(prompt_text, cli=cli, profile=profile, session_id=session_id)
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            command["argv"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            cwd=str(cwd), env=_env(env))
    except (OSError, ValueError) as error:
        return {"status": "failed", "timed_out": False, "exit_code": None,
                "stdout": "", "stderr": f"无法启动 dsh：{error}",
                "duration_seconds": round(time.monotonic() - started, 3),
                "argv": command["argv"], "cwd": str(cwd), "timeout_seconds": timeout_seconds}
    timed_out = False
    try:
        stdout, stderr = process.communicate(prompt_text, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            _kill_tree(process)
            stdout, stderr = "", ""
    duration = round(time.monotonic() - started, 3)
    if timed_out:
        status = "timeout"
    elif process.returncode == 0:
        status = "completed"
    else:
        status = "failed"
    return {"status": status, "timed_out": timed_out, "exit_code": process.returncode,
            "stdout": stdout or "", "stderr": stderr or "", "duration_seconds": duration,
            "argv": command["argv"], "cwd": str(cwd), "timeout_seconds": timeout_seconds}


def parse_headless_events(stdout_text: str) -> dict:
    """解析 `dsh --json` 逐行事件流。

    字段依据已安装版本 0.2.1-rc.2 的 headless json-stream 投影源码：
    session{sessionId,cwd}、status{phase,turn,step,reason,usage}、text{text}、
    thinking{text}、tool_call{callId,tool,input}、tool_result{callId,status,result}、
    final{text}、error。真实 headless 运行本轮未执行，解析结果仍需首次实测核对。
    """
    events, session_id, final_answer, errors = [], None, None, []
    texts, thinking, tool_events, phases, turn_end = [], [], [], [], None
    usage_parts = []
    by_call: dict = {}
    for number, line in enumerate(stdout_text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError as error:
            raise EvalSpecError(f"DSH --json 第 {number} 行不是 JSON：{error}") from error
        if not isinstance(event, dict):
            raise EvalSpecError(f"DSH --json 第 {number} 行不是 JSON 对象")
        events.append(event)
        event_type = event.get("type") or ""
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if event_type == "session":
            session_id = (event.get("sessionId") or event.get("id") or data.get("sessionId")
                          or data.get("id") or event.get("session"))
        elif event_type == "final":
            final_answer = event.get("text")
        elif event_type == "text":
            if isinstance(event.get("text"), str):
                texts.append(event["text"])
        elif event_type == "thinking":
            if isinstance(event.get("text"), str):
                thinking.append(event["text"])
        elif event_type == "status":
            phase = event.get("phase") or "未记录"
            phases.append({"phase": phase, "turn": event.get("turn"), "step": event.get("step")})
            if phase == "turn_end":
                turn_end = {"turn": event.get("turn"),
                            "reason": event.get("reason") or "未记录"}
            if isinstance(event.get("usage"), dict):
                usage_parts.append(event["usage"])
        elif event_type == "tool_call":
            call_id = event.get("callId")
            entry = {"name": event.get("tool") or event.get("name") or "未记录",
                     "arguments": event.get("input", event.get("arguments")),
                     "call_id": call_id, "status": "call", "is_error": False}
            tool_events.append(entry)
            if isinstance(call_id, str):
                by_call[call_id] = entry
        elif event_type == "tool_result":
            call_id = event.get("callId")
            entry = by_call.pop(call_id, None) if isinstance(call_id, str) else None
            if entry is None:
                entry = {"name": "未记录", "call_id": call_id}
                tool_events.append(entry)
            result = event.get("result")
            entry.update({
                "status": event.get("status") or "未记录",
                "is_error": event.get("status") == "error",
                "result_text": result if isinstance(result, str) else None,
                "result_truncated": isinstance(result, str) and len(result) > 4000,
            })
            if isinstance(result, str) and len(result) > 4000:
                entry["result_text"] = result[:4000]
        elif event_type == "error":
            errors.append(event.get("message") or event.get("error") or "未记录")
    for entry in by_call.values():
        entry["status"] = "no_result"
    if final_answer is None and texts:
        final_answer = texts[-1]
    usage = None
    if usage_parts:
        totals: dict = {}
        for part in usage_parts:
            for key, value in part.items():
                if isinstance(value, (int, float)):
                    totals[key] = totals.get(key, 0) + value
        usage = {"total": totals, "step_samples": len(usage_parts),
                 "source": "dsh_json_step_end_usage"}
    return {"session_id": session_id, "event_count": len(events),
            "events": events, "final_answer": final_answer,
            "texts": texts, "thinking": thinking,
            "tool_events": tool_events, "phases": phases, "statuses": phases,
            "usage": usage, "turn_end": turn_end, "errors": errors}


# ---- 元数据与导出包 ------------------------------------------------------

def metadata_from_session(parsed: dict, *, dsh_version: str = "未记录",
                          desktop_app_version: str = "未记录",
                          source: str = "dsh_session") -> dict:
    """从会话事件构造导入元数据；实际模型不可得时为“未记录”，不推断。"""
    completed = bool(parsed.get("final_answer"))
    permission_bits = [
        f"沙箱模式={parsed.get('sandbox_mode', '未记录')}",
        f"权限预设={parsed.get('permission_preset', '未记录')}",
        f"审批策略={parsed.get('approval_policy', '未记录')}",
        f"工具数={len(parsed.get('tools') or [])}",
    ]
    generation = {
        "request_headers": parsed.get("request_headers") or [],
        "tools": parsed.get("tools") or [],
        "sandbox_mode": parsed.get("sandbox_mode") or "未记录",
        "permission_preset": parsed.get("permission_preset") or "未记录",
        "approval_policy": parsed.get("approval_policy") or "未记录",
    }
    return {
        "status": "completed" if completed else "failed",
        "actual_model": parsed.get("actual_model") or "未记录",
        "provider": parsed.get("actual_provider") or "未记录",
        "framework": "DSH（DeepSeek Harness）",
        "dsh_version": dsh_version,
        "tool_permissions": "；".join(permission_bits),
        "generation": generation,
        "timing": parsed.get("timing") or {},
        "usage": parsed.get("usage") or {},
        "tool_events": parsed.get("tool_events") or [],
        "error": None if completed else f"会话中没有最终 assistant 回答（来源：{source}）",
        "notes": (f"元数据来自 {source}；DSH 运行时版本 {dsh_version}；"
                  f"Electron 外壳版本 {desktop_app_version}（应用 version 文件不是 DSH 版本）；"
                  "会话日志是可观察事件，不是模型全部内部推理。"),
        "round": {"iteration_id": "initial", "type": "initial",
                  "change_reason": "DSH 独立初稿；未参考其他模型回答"},
    }


def build_bundle(parsed: dict, *, source: str, dsh_version: str = "未记录",
                 desktop_app_version: str = "未记录") -> dict:
    """导出/运行结果的可保存摘要；原始会话日志另行保存为证据。"""
    return {
        "schema_version": 1,
        "source": source,
        "captured_at": _now(),
        "dsh_version": dsh_version,
        "desktop_app_version": desktop_app_version,
        "session_id": parsed.get("session_id") or "未记录",
        "cwd": parsed.get("cwd") or "未记录",
        "agent_preset": parsed.get("agent_preset") or "未记录",
        "session_origin": parsed.get("session_origin") or "未记录",
        "actual_model": parsed.get("actual_model") or "未记录",
        "provider": parsed.get("actual_provider") or "未记录",
        "actual_input": (parsed.get("user_messages") or [""])[-1] if parsed.get("user_messages") else "未记录",
        "final_answer": parsed.get("final_answer") or "",
        "request_headers": parsed.get("request_headers") or [],
        "tools": parsed.get("tools") or [],
        "tool_events": parsed.get("tool_events") or [],
        "sandbox_mode": parsed.get("sandbox_mode") or "未记录",
        "permission_preset": parsed.get("permission_preset") or "未记录",
        "approval_policy": parsed.get("approval_policy") or "未记录",
        "usage": parsed.get("usage") or {},
        "timing": parsed.get("timing") or {},
        "notes": [
            "DSH 是 Agent 工作台；实际模型取自响应或会话事件。",
            "会话日志保存可观察事件，不等于模型全部内部推理。",
            "本 bundle 由本地文件解析生成，未调用模型。",
        ],
    }
