"""Text-only DSH calls with per-call model selection and observable evidence.

No credentials are read by this adapter.  DSH resolves its own managed login.
The boot-free config dump is inspected before mounting a model-facing agent;
unknown plugins are disabled, and known core plugin identities are checked.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

import yaml

from model_eval import dsh


class RuntimeConfigError(ValueError):
    pass


# Installed 0.2.1-rc.2 dsh-llm-deepseek-account/lib/index.js emits this
# informational catalog message via its logger to stdout, even in --json mode.
# Accept only that exact shape and official URL; all other non-JSON lines fail.
_CATALOG_DIAGNOSTIC = re.compile(
    r"\[llm-deepseek-account\] model listing from https://api\.deepseek\.com/models "
    r"loaded [0-9]+ model\(s\), skipped [0-9]+ row\(s\)\Z")


def _parse_runtime_stream(stdout_text):
    protocol, diagnostics, errors = [], [], []
    for number, line in enumerate(stdout_text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            if _CATALOG_DIAGNOSTIC.fullmatch(line):
                diagnostics.append({"line": number, "text": line, "kind": "model_catalog_info"})
            else:
                errors.append(f"Unknown non-JSON stdout at line {number}")
            continue
        if not isinstance(event, dict):
            errors.append(f"Non-object protocol value at line {number}")
            continue
        protocol.append(line)
    parsed = dsh.parse_headless_events("\n".join(protocol))
    observed_sessions = [event for event in parsed["events"] if event.get("type") == "session"]
    if len(observed_sessions) != 1:
        errors.append("Expected exactly one observed session event")
        # No guessing a session identity from a conflicting or absent event.
        parsed["session_id"] = None
    if parsed.get("session_id") and not dsh.SAFE_SESSION_ID.fullmatch(parsed["session_id"]):
        errors.append("Unsafe observed session id")
        parsed["session_id"] = None
    return parsed, diagnostics, errors


def _classify_session_inputs(session):
    """Keep full harness input while identifying the submitted human task.

    The shipped headless driver appends source.kind=user.  Core working-dir,
    sandbox, and approval components append a source.kind=runtime-context
    snapshot in the same turn; that snapshot is real model input, not a
    second question or a second turn.
    """
    logical = session.get("session_jsonl")
    if not isinstance(logical, bytes):
        return session
    submitted, contexts, unknown, messages = [], [], [], []
    allowed_sections = {"working-directory:current", "sandbox:policy", "approval:policy"}
    snapshot_prefix = "Current runtime context. This snapshot supersedes earlier runtime-context snapshots.\n\n"
    for line in logical.decode("utf-8-sig").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("type") == "system/message":
            data = event.get("data") or {}
            messages.append({"role": "system", "message": data.get("message"),
                             "turn": data.get("turn"), "step": data.get("step")})
            continue
        if event.get("type") != "user/message":
            continue
        data = event.get("data") or {}
        source = data.get("source") or {}
        content = data.get("content")
        text = dsh._content_text(content)
        messages.append({"source": source, "role": data.get("role"), "content": content})
        if source.get("kind") == "user":
            submitted.append(text)
        elif source.get("kind") == "runtime-context" and source.get("form") == "snapshot":
            sections = source.get("sections")
            valid_sections = (isinstance(sections, list) and bool(sections)
                              and all(isinstance(s, dict) and s.get("name") in allowed_sections
                                      and isinstance(s.get("text"), str) for s in sections)
                              and len({s["name"] for s in sections}) == len(sections))
            if valid_sections and text == snapshot_prefix + "\n\n".join(s["text"] for s in sections):
                contexts.append({"source": source, "text": text})
            else:
                unknown.append(source)
        else:
            unknown.append(source)
    session["submitted_user_messages"] = submitted
    session["runtime_context_messages"] = contexts
    session["unknown_input_sources"] = unknown
    session["actual_input_messages"] = messages
    return session


# Each enabled plugin is a known non-tool component from the installed base.
# All other rows (including user additions) are disabled in this call only.
_CORE = {
    "timer": "cordis-plugin-timer", "llm": "dsh-llm",
    "deepseek-llm-api-extensions": "dsh-deepseek-llm-api-extensions",
    "session": "dsh-session", "session-log-deepseek": "dsh-session-log-deepseek",
    "typert": "dsh-typert-registry", "typert-loader": "dsh-typert-loader",
    "typert-gateway": "dsh-api-gateway", "remote-access": "dsh-api-remote-access",
    "agent": "dsh-agent", "agent-default-model": "dsh-agent-default-model",
    "authorization": "dsh-authorization", "credentials": "dsh-credentials-local",
    "deepseek-account": "dsh-deepseek-account-platform",
    "session-persistence-jsonl": "dsh-session-persistence-jsonl",
    "attachment-local": "dsh-attachment-local",
    "session-query-sqlite": "dsh-session-query-sqlite",
    "working-directory": "dsh-working-directory", "session-projection": "dsh-session-projection",
    "storage": "dsh-storage", "storage-json": "dsh-storage-json",
    "storage-domain": "dsh-storage-domain",
    "session-projection-cache": "dsh-session-projection-cache",
    "subprocess": "dsh-subprocess-local", "sandbox": "dsh-sandbox-local",
    "sandbox-policy": "dsh-sandbox-policy", "pwsh-sandbox": "dsh-pwsh-sandbox",
    "bash-sandbox": "dsh-bash-sandbox", "approval": "dsh-user-approval",
    "permission": "dsh-permission-presets", "shell-env": "dsh-shell-env",
    "fs-observation-policy": "dsh-fs-observation-policy",
    "spill-local": "dsh-spill-local", "spill-policy": "dsh-spill-policy",
    "session-checkpoint-policy": "dsh-session-checkpoint-policy",
    "tools": "dsh-tools", "system-prompt": "dsh-system-prompt",
    "agent-loop": "dsh-agent-loop", "fs-sandbox": "dsh-fs-sandbox",
    "llm-deepseek-account": "dsh-llm-deepseek-account",
    "headless-startup": "dsh-headless/startup", "headless-runner": "dsh-headless",
}
_REQUIRED = {"agent", "agent-loop", "agent-default-model", "credentials", "authorization",
             "deepseek-account", "llm-deepseek-account", "tools", "system-prompt",
             "sandbox-policy", "approval", "headless-startup", "headless-runner"}


class _DumpLoader(yaml.SafeLoader):
    pass


def _opaque_js(loader, node):
    # --dump-config explicitly does not evaluate JavaScript tags. Nor do we.
    return {"unevaluated_js": loader.construct_scalar(node)}


_DumpLoader.add_constructor("tag:yaml.org,2002:js", _opaque_js)


def _settings(config):
    provider = config.get("provider", "deepseek-account")
    model = config.get("model", "deepseek-flash")
    effort = config.get("reasoning_effort", "max")
    if provider != "deepseek-account" or not isinstance(model, str) or not model.startswith("deepseek-"):
        raise RuntimeConfigError("This adapter requires the explicit deepseek-account route and a DeepSeek model.")
    if effort not in {"low", "medium", "high", "max"}:
        raise RuntimeConfigError("Unsupported reasoning_effort")
    return {"provider": provider, "model": model, "reasoningEffort": effort}


def preflight(project_root, config):
    """Read-only detection; does not authenticate, launch DSH, or call a model."""
    selection = _settings(config)
    detected = dsh.detect_install(probe=False)
    cli = Path(config["dsh_cli"]) if config.get("dsh_cli") else dsh.resolve_cli()[0]
    home = Path(config.get("dsh_home") or os.environ.get("DSH_HOME") or Path.home() / ".dsh")
    return {"ready": bool(cli and cli.is_file()), "cli": str(cli) if cli else None,
            "selection": selection, "managed_credentials_present": (home / ".credentials.yaml").is_file(),
            "dsh_home": str(home), "headless_profile_present": (home / "profiles/headless").is_dir(),
            "runtime": detected, "authentication": "not_tested", "model_called": False,
            "notice": "Only file presence was checked; credentials were not read and login validity is untested."}


def _rows(text):
    rows = yaml.load(text, Loader=_DumpLoader)
    if not isinstance(rows, list) or not rows:
        raise RuntimeConfigError("DSH composed config is not a nonempty row list")
    ids = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise RuntimeConfigError("DSH composed config contains an unidentified row")
        if row.get("children") or row.get("insert"):
            raise RuntimeConfigError("Nested plugin composition is unsupported")
        ids.append(row["id"])
    if len(ids) != len(set(ids)):
        raise RuntimeConfigError("Duplicate DSH plugin ids")
    if not _REQUIRED.issubset(ids):
        raise RuntimeConfigError("DSH profile lacks required core components")
    return rows


def build_patch(rows, selection, cwd, stream_idle_timeout_ms=None):
    """Allow known core identities; disable every other composed plugin row."""
    patch = []
    for row in rows:
        identifier = row["id"]
        enabled = identifier in _CORE
        if identifier == "bash-sandbox" and os.name == "nt":
            enabled = False
        if identifier == "pwsh-sandbox" and os.name != "nt":
            enabled = False
        if enabled and row.get("name") != "@deepseek-ai/" + _CORE[identifier]:
            raise RuntimeConfigError(f"Unexpected implementation for core component {identifier}")
        patch.append({"id": identifier, "disabled": not enabled})
    overrides = {
        "agent-default-model": selection,
        "tools": {"mode": "native"},
        "system-prompt": {"personaPrefix": "You are a mathematical proof assistant. Answer the supplied task using reasoning only."},
        "sandbox-policy": {"mode": "workspace-write", "workspaceRoot": str(cwd)},
        "approval": {"policy": "ask"},
        "agent-loop": {"agents": []},
        "session-query-sqlite": {"path": ":memory:", "openAt": "never"},
    }
    if stream_idle_timeout_ms is not None:
        if type(stream_idle_timeout_ms) is not int or not 0 < stream_idle_timeout_ms <= 900000:
            raise RuntimeConfigError("Per-call stream idle timeout must be within (0, 900000] ms")
        # Installed dsh-llm-deepseek-account inherits this volatile field from
        # dsh-llm-deepseek. Apply only in this --patch overlay; never change the
        # managed profile, OAuth timeout, credentials, or installed plugin.
        overrides["llm-deepseek-account"] = {"streamIdleTimeoutMs": stream_idle_timeout_ms}
    for item in patch:
        if item["id"] in overrides:
            item["config"] = overrides[item["id"]]
    return patch


def _provider_timeouts(stream, session):
    """Classify structured runtime failures; partial text is never completion."""
    reasons = [event.get("reason") for event in stream.get("events", [])
               if event.get("type") == "status" and event.get("phase") == "turn_end"]
    reasons.append(((session or {}).get("last_turn_end") or {}).get("reason"))
    errors = []
    for reason in reasons:
        error = reason.get("error") if isinstance(reason, dict) and reason.get("kind") == "error" else None
        if isinstance(error, dict) and error.get("code") == "TIMEOUT" and error not in errors:
            errors.append(error)
    return errors


def _kill_owned_tree(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                           capture_output=True, check=False, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            # The exact owned child remains killable even if taskkill failed.
            pass
        if process.poll() is None:
            process.kill()
    else:
        process.kill()


def _stop_and_collect(process, stdout=b"", stderr=b""):
    """Bounded cleanup of this Popen only, retaining the latest available bytes."""
    cleanup_errors = []
    try:
        _kill_owned_tree(process)
    except (OSError, subprocess.SubprocessError) as error:
        cleanup_errors.append(type(error).__name__)
    try:
        final_stdout, final_stderr = process.communicate(timeout=10)
        stdout = final_stdout if final_stdout is not None else stdout
        stderr = final_stderr if final_stderr is not None else stderr
    except subprocess.TimeoutExpired as error:
        # communicate's TimeoutExpired.output contains its full buffered prefix,
        # so do not concatenate it with the earlier prefix and duplicate bytes.
        stdout = error.output if error.output is not None else stdout
        stderr = error.stderr if error.stderr is not None else stderr
        cleanup_errors.append("TimeoutExpired")
        try:
            _kill_owned_tree(process)
        except (OSError, subprocess.SubprocessError) as stop_error:
            cleanup_errors.append(type(stop_error).__name__)
    except (OSError, subprocess.SubprocessError) as error:
        cleanup_errors.append(type(error).__name__)
    return stdout or b"", stderr or b"", cleanup_errors


def _execute(argv, cwd, timeout, stdin=None):
    environment = dict(os.environ)
    # These pin safe process-local defaults; the call overlay pins them again.
    environment.update({"DSH_PERMISSION_MODE": "workspace-write", "DSH_TOOLS_MODE": "native"})
    started = time.monotonic()
    process = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=environment,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    timed_out, cleanup_errors = False, []
    try:
        stdout, stderr = process.communicate(stdin.encode("utf-8") if stdin is not None else None,
                                             timeout=timeout)
    except subprocess.TimeoutExpired as error:
        timed_out = True
        stdout, stderr, cleanup_errors = _stop_and_collect(
            process, error.output or b"", error.stderr or b"")
    except BaseException as error:
        # KeyboardInterrupt/SystemExit must not leave a generating child alive.
        # Preserve the original exception and attach only this process's output
        # for run_call's interrupted-artifact handler before re-raising it.
        stdout, stderr, cleanup_errors = _stop_and_collect(process)
        error.dsh_execution = {"stdout": stdout, "stderr": stderr, "pid": process.pid,
                               "exit_code": process.returncode, "interrupted": True,
                               "cleanup_errors": cleanup_errors}
        raise
    return {"stdout": stdout or b"", "stderr": stderr or b"", "exit_code": process.returncode,
            "timed_out": timed_out, "pid": process.pid,
            "cleanup_errors": cleanup_errors,
            "elapsed_seconds": round(time.monotonic() - started, 3)}


def _validate_session(session, stream, prompt, selection, cwd):
    errors = []
    if not session or session.get("session_id") != stream.get("session_id"):
        return ["Missing exact persisted session evidence"]
    if session.get("submitted_user_messages", session.get("user_messages")) != [prompt] or session.get("turn_count") != 1:
        errors.append("Session is not one fresh turn containing the exact saved input")
    if session.get("unknown_input_sources"):
        errors.append("Unexpected source supplied additional model input")
    if os.path.normcase(os.path.abspath(session.get("cwd", ""))) != os.path.normcase(os.path.abspath(cwd)):
        errors.append("Session working directory differs from the isolated call directory")
    if not session.get("request_headers"):
        errors.append("Missing actual request configuration")
    for header in session.get("request_headers", []):
        if (header.get("model"), header.get("provider"), header.get("reasoningEffort")) != (
                selection["model"], selection["provider"], selection["reasoningEffort"]):
            errors.append("Actual request model/provider/reasoning differs from the pinned selection")
        if header.get("tool_count") != 0:
            errors.append("Unexpected model-facing tools in the request")
    for message in session.get("assistant_messages", []):
        if (message.get("model"), message.get("provider")) != (selection["model"], selection["provider"]):
            errors.append("Actual response model/provider differs from the pinned selection")
    if not session.get("assistant_messages") or not session.get("final_answer", "").strip():
        errors.append("Missing assistant final answer")
    if stream.get("final_answer") != session.get("final_answer"):
        errors.append("Stream final answer differs from the durable answer")
    if session.get("tools") or session.get("tool_events") or stream.get("tool_events"):
        errors.append("Unexpected tools were registered or called")
    if session.get("sandbox_mode") != "workspace-write" or session.get("approval_policy") != "ask":
        errors.append("Safe sandbox/approval mode was not confirmed by session evidence")
    terminal = session.get("last_turn_end") or {}
    reason = terminal.get("reason")
    if not isinstance(reason, dict) or reason.get("kind") != "completed":
        errors.append("No completed durable terminal event")
    if stream.get("errors"):
        errors.append("Headless error events were emitted")
    return errors


def run_call(project_root, call_dir, prompt, config, timeout_seconds):
    """Persist inputs, perform one fresh DSH turn, and fail closed on evidence gaps."""
    # Resolve before mkdir so neither '..' nor an existing junction/symlink can
    # redirect artifact writes outside the project.  Permission errors propagate
    # to the caller for the normal approval path rather than weakening this check.
    if ".." in Path(call_dir).parts:
        raise RuntimeConfigError("Call directory cannot contain parent traversal")
    root, directory = Path(project_root).resolve(strict=True), Path(call_dir).resolve(strict=False)
    if root != directory and root not in directory.parents:
        raise RuntimeConfigError("Call directory must be inside the project")
    if not isinstance(prompt, str) or not prompt.strip():
        raise RuntimeConfigError("Prompt must be nonempty text")
    if isinstance(timeout_seconds, bool) or not 0 < timeout_seconds <= 900:
        raise RuntimeConfigError("Call timeout must be within (0, 900] seconds")
    selection = _settings(config)
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise RuntimeConfigError("A call directory cannot be reused")
    def save(name, value):
        path = directory / name
        if isinstance(value, bytes):
            path.write_bytes(value)
        else:
            path.write_text(value, encoding="utf-8", newline="")
        return path
    def save_json(name, value):
        return save(name, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    save("prompt.txt", prompt)
    save_json("selection.json", {"selection": selection, "timeout_seconds": timeout_seconds,
                                  "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()})
    started = time.monotonic()
    result = {"status": "failed", "answer": "", "actual_model": "未记录", "actual_provider": "未记录",
              "session_id": None, "usage": {}, "tool_events": [], "error": None,
              "invocation_started": False, "provider_request_observed": False,
              "response_observed": False, "failure_kind": None}
    session = None
    model_started = False
    def remaining():
        value = timeout_seconds - (time.monotonic() - started)
        if value <= 0:
            raise TimeoutError("Call budget exhausted")
        return value
    try:
        info = preflight(root, config)
        save_json("preflight.json", info)
        if not info["ready"]:
            raise RuntimeConfigError("DSH CLI not installed")
        cli = info["cli"]
        # This command composes rows without mounting plugins or evaluating !!js.
        # Full dump is kept in memory only: user plugin configs may contain secrets.
        dump = _execute([cli, "--profile", "headless", "--dump-config"], directory, remaining())
        if dump["timed_out"]:
            raise TimeoutError("DSH config composition timed out")
        if dump["exit_code"] != 0:
            save("config-error.stderr.log", dump["stderr"])
            raise RuntimeConfigError("DSH config composition failed; see saved stderr")
        rows = _rows(dump["stdout"].decode("utf-8-sig"))
        provider_remaining = remaining()
        idle_timeout_ms = max(1, int(provider_remaining * 1000))
        patch = build_patch(rows, selection, directory, idle_timeout_ms)
        patch_file = save_json("call.patch.json", patch)
        save_json("timeout-policy.json", {
            "policy_version": "remaining-role-idle-v1", "role_timeout_seconds": timeout_seconds,
            "remaining_seconds_before_model": provider_remaining,
            "provider_stream_idle_timeout_ms": idle_timeout_ms,
            "config_scope": "per-call llm-deepseek-account overlay",
            "global_configuration_modified": False, "adapter_automatic_retries": 0,
            "outer_role_deadline_unchanged": True,
            "completion_requirement": "exact durable completed terminal plus matching answer"})
        # Record identities/enablement only, never arbitrary configuration values.
        enabled_by_id = {row["id"]: not row["disabled"] for row in patch}
        save_json("plugin-audit.json", [{"id": row["id"], "name": row.get("name"),
                                        "enabled_for_call": enabled_by_id[row["id"]]} for row in rows])
        argv = [cli, "--profile", "headless", "--patch", str(patch_file), "--json"]
        save_json("invocation.json", {"argv": argv, "cwd": str(directory), "stdin_file": "prompt.txt"})
        model_started = True
        execution = _execute(argv, directory, remaining(), prompt)
        result["invocation_started"] = True
        save("stdout.jsonl", execution.pop("stdout"))
        save("stderr.log", execution.pop("stderr"))
        save_json("process.json", execution)
        if execution["timed_out"]:
            result["status"] = "timeout"
        stream, diagnostics, protocol_errors = _parse_runtime_stream(
            (directory / "stdout.jsonl").read_text(encoding="utf-8-sig"))
        if diagnostics:
            save_json("nonprotocol-diagnostics.json", diagnostics)
        if protocol_errors:
            save_json("protocol-errors.json", protocol_errors)
        result["session_id"] = stream.get("session_id")
        result["tool_events"] = stream.get("tool_events") or []
        if result["session_id"]:
            session = dsh.read_session_by_id(result["session_id"], dsh_home=info["dsh_home"])
        if session:
            result["provider_request_observed"] = bool(session.get("request_headers"))
            result["response_observed"] = bool(session.get("assistant_messages"))
            session = _classify_session_inputs(session)
            if "actual_input_messages" in session:
                save_json("actual-input-messages.json", session["actual_input_messages"])
                save_json("harness-runtime-context.json", session["runtime_context_messages"])
            logical = session.pop("session_jsonl", None)
            if logical is not None:
                save("session.jsonl", logical)
            save_json("session-summary.json", session)
            result.update({key: session.get(source, default) for key, source, default in (
                ("answer", "final_answer", ""), ("actual_model", "actual_model", "未记录"),
                ("actual_provider", "actual_provider", "未记录"), ("usage", "usage", {}))})
        failures = _validate_session(session, stream, prompt, selection, directory)
        failures = protocol_errors + failures
        provider_timeouts = _provider_timeouts(stream, session)
        if provider_timeouts:
            result["provider_errors"] = provider_timeouts
            result["timeout_subtype"] = ("provider_stream_idle" if any(
                error.get("message") == "DeepSeek Messages stream idle timeout"
                for error in provider_timeouts) else "provider_timeout")
            failures.insert(0, "DSH provider TIMEOUT; see preserved structured error and raw stream")
        if execution["timed_out"]:
            failures.insert(0, "DSH call timed out")
            result["timeout_subtype"] = "outer_role_deadline"
        elif execution["exit_code"] != 0:
            failures.insert(0, f"DSH exited with code {execution['exit_code']}")
        result["error"] = "; ".join(failures) or None
        if not failures:
            result["status"] = "completed"
        else:
            timed_out = execution["timed_out"] or bool(provider_timeouts)
            result["failure_kind"] = "timeout" if timed_out else "transport_or_protocol_failure"
            if timed_out:
                result["status"] = "timeout"
    except (OSError, ValueError, yaml.YAMLError, RuntimeError, TimeoutError) as error:
        if isinstance(error, TimeoutError):
            result["status"] = "timeout"
        result["error"] = f"{type(error).__name__}: {error}"
        result["failure_kind"] = ("timeout" if isinstance(error, TimeoutError) else
                                  "configuration_failure" if not model_started else "transport_or_protocol_failure")
    except BaseException as error:
        # Keep the same interruption signal for the pipeline; archive whatever
        # the exact owned model process had emitted before it was terminated.
        execution = getattr(error, "dsh_execution", None)
        if isinstance(execution, dict):
            execution = dict(execution)
            stdout = execution.pop("stdout", b"")
            stderr = execution.pop("stderr", b"")
            if model_started:
                result["invocation_started"] = bool(execution.get("pid"))
                save("stdout.jsonl", stdout)
                save("stderr.log", stderr)
            # Config dump stdout is deliberately not archived: it can contain
            # arbitrary user plugin configuration including secret values.
            save_json("interrupted-process.json", execution)
        result.update({"status": "interrupted", "error": type(error).__name__,
                       "failure_kind": "interrupted",
                       "elapsed_seconds": round(time.monotonic() - started, 3)})
        result["evidence_files"] = [p.relative_to(root).as_posix() for p in sorted(directory.iterdir()) if p.is_file()]
        save_json("result.json", result)
        raise
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    result["evidence_files"] = [p.relative_to(root).as_posix() for p in sorted(directory.iterdir()) if p.is_file()]
    result_file = save_json("result.json", result)
    result["evidence_files"].append(result_file.relative_to(root).as_posix())
    return result
