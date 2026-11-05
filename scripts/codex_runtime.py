"""Local Windows runtime settings; never copies Codex credentials."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_settings(project_root: Path | None = None) -> dict:
    root = project_root or PROJECT_ROOT
    return json.loads((root / "setup" / "runtime.json").read_text(encoding="utf-8"))


def _value(value) -> str:
    # JSON scalar/array representations are also valid TOML for these settings.
    return json.dumps(value, ensure_ascii=False)


def build_command(workdir, model, effort, session_id=None, web_mode="disabled"):
    workdir = Path(workdir).resolve()
    project_root = workdir.parents[2]
    settings = load_settings(project_root)
    binary = settings["codex_bin"]
    if not Path(binary).is_file():
        binary = shutil.which("codex")
    if not binary:
        raise RuntimeError("Codex CLI not found. Install/update Codex and refresh setup/runtime.json.")
    cmd = [binary, "exec"]
    if session_id:
        cmd += ["resume"]
    else:
        cmd += ["-C", str(workdir)]
    cmd += ["-m", model, "--json"]
    overrides = {
        "sandbox_mode": "workspace-write",
        "approval_policy": "on-request",
        "approvals_reviewer": "auto_review",
        "model_reasoning_effort": effort,
        "web_search": web_mode,
        # Same official authentication/backend, HTTP transport for this machine.
        "model_provider": "openai_https",
        "model_providers.openai_https.name": "OpenAI",
        "model_providers.openai_https.wire_api": "responses",
        "model_providers.openai_https.requires_openai_auth": True,
        "model_providers.openai_https.supports_websockets": False,
    }
    # Make this invocation work without changing global project trust/config.
    local = tomllib.loads((workdir / ".codex" / "config.toml").read_text(encoding="utf-8"))
    for name in settings.get("disabled_global_mcp", []):
        overrides[f"mcp_servers.{name}.enabled"] = False
    servers = dict(settings.get("shared_mcp", {}))
    servers.update(local.get("mcp_servers", {}))
    for name, server in servers.items():
        for key, value in server.items():
            overrides[f"mcp_servers.{name}.{key}"] = value
    if workdir.name == "generation":
        overrides["features.multi_agent"] = True
        for name in ("max_threads", "max_depth", "job_max_runtime_seconds"):
            if name in local.get("agents", {}):
                overrides[f"agents.{name}"] = local["agents"][name]
        role = local.get("agents", {}).get("subgoal-prover", {})
        for key in ("description", "config_file"):
            if key in role:
                overrides[f"agents.subgoal-prover.{key}"] = role[key]
    for key, value in overrides.items():
        cmd += ["--config", f"{key}={_value(value)}"]
    if session_id:
        cmd.append(session_id)
    return cmd


def child_environment() -> dict:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def stop_process_tree(proc: subprocess.Popen) -> None:
    """Stop only the process created by this run and its children."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
    else:
        proc.kill()
    proc.wait(timeout=15)
