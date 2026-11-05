"""Windows entry point preserving Rethlas' upstream API and verifier prompts."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException
from codex_runtime import build_command, child_environment, load_settings, stop_process_tree
from rethlas_core.verification import load_json, make_binding, validate_math_report, write_once
from rethlas_core.claims import review_brief
import secrets

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VERIFY_ROOT = PROJECT_ROOT / "Rethlas" / "agents" / "verification"
spec = importlib.util.spec_from_file_location("rethlas_upstream_api", VERIFY_ROOT / "api" / "server.py")
upstream = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upstream)
settings = load_settings(PROJECT_ROOT)


def run_codex_verification(run_id: str, statement: str, proof: str) -> dict:
    result_dir = upstream._results_dir(run_id)
    result_dir.mkdir(parents=True, exist_ok=True)
    log_path = result_dir / "log.md"
    events_path = result_dir / "events.jsonl"
    model = os.getenv("CODEX_MODEL", settings["model"])
    effort = os.getenv("CODEX_REASONING_EFFORT", settings["reasoning_effort"])
    timeout = int(os.getenv("CODEX_TIMEOUT_SECONDS", "900"))
    if timeout <= 0:
        raise HTTPException(status_code=500, detail="CODEX_TIMEOUT_SECONDS must be positive")
    cmd = build_command(VERIFY_ROOT, model, effort, web_mode="disabled")
    prompt = upstream.build_prompt(run_id, statement, proof) + review_brief(proof)
    write_once(result_dir / "verification.input.json", {"statement": statement, "proof": proof})
    # Stdin avoids Windows command-length limits for long mathematical proofs.
    with log_path.open("x", encoding="utf-8") as log, events_path.open("x", encoding="utf-8") as events:
        log.write(f"started_at_utc: {datetime.now(timezone.utc).isoformat()}\n")
        log.write(f"model: {model}\nreasoning_effort: {effort}\nsandbox: workspace-write\n")
        log.write(f"prompt:\n{prompt}\n\nevents: {events_path}\n")
        log.flush()
        proc = subprocess.Popen(
            cmd + ["-"], cwd=VERIFY_ROOT, stdin=subprocess.PIPE,
            stdout=events, stderr=log, text=True, encoding="utf-8",
            env=child_environment(),
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        try:
            proc.communicate(prompt, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            stop_process_tree(proc)
            raise HTTPException(status_code=504, detail=f"Verifier timeout; see {log_path}") from exc
        except BaseException:
            stop_process_tree(proc)
            raise
        log.write(f"\nexit_code: {proc.returncode}\n")
    if proc.returncode:
        raise HTTPException(status_code=500, detail=f"Codex failed ({proc.returncode}); see {log_path}")
    verification_path = upstream._verification_path(run_id)
    if verification_path is None:
        raise HTTPException(status_code=500, detail=f"No verification.json; see {events_path}")
    try:
        payload = load_json(verification_path)
        validate_math_report(payload, proof=proof, require_claims=True)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Invalid verification output: {exc}") from exc
    binding = make_binding(statement, proof, payload, run_id)
    write_once(result_dir / "verification.binding.json", binding)
    return {**payload, "verification_id": run_id, "verification_binding": binding}


upstream.run_codex_verification = run_codex_verification
upstream._allocate_run_id = lambda statement: datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + secrets.token_hex(8)
app = upstream.app
