# Locally modified Rethlas source snapshot (2026-11): verification/memory contracts and host integration. Upstream Apache-2.0 retained; see THIRD_PARTY_NOTICES.md.
from __future__ import annotations

import sys
from pathlib import Path as _CorePath
sys.path.insert(0, str(_CorePath(__file__).resolve().parents[4] / "scripts"))
from rethlas_core.verification import load_json, validate_math_report
from rethlas_core.memory import append_jsonl, read_jsonl, update_meta

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests

try:
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover - dependency managed by requirements
    Draft202012Validator = None  # type: ignore[assignment]

try:
    from fastmcp import FastMCP
except ImportError:  # pragma: no cover - dependency managed by requirements
    FastMCP = None  # type: ignore[assignment]


REPO_ROOT = Path(__file__).resolve().parents[1]
MEMORY_ROOT = REPO_ROOT / "memory"
RESULTS_ROOT = REPO_ROOT / "results"
SCHEMA_PATH = REPO_ROOT / "schemas" / "verification_output.schema.json"

THEOREM_SEARCH_URL = "https://leansearch.net/thm/search"
THEOREM_SEARCH_TASK = (
    "Given a math statement, retrieve useful references, such as theorems, "
    "lemmas, and definitions, that are useful for solving the given problem."
)

CHANNEL_FILES: Dict[str, str] = {
    "statement_checks": "statement_checks.jsonl",
    "reference_checks": "reference_checks.jsonl",
    "verification_reports": "verification_reports.jsonl",
    "failed_checks": "failed_checks.jsonl",
    "events": "events.jsonl",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_run_id(raw: str) -> str:
    cleaned = re.sub(r"\s+", "_", str(raw).strip())
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("._")
    return cleaned or "run"


def _run_dir(run_id: str) -> Path:
    return MEMORY_ROOT / sanitize_run_id(run_id)


def _channel_path(run_id: str, channel: str) -> Path:
    if channel not in CHANNEL_FILES:
        allowed = ", ".join(sorted(CHANNEL_FILES))
        raise ValueError(f"Unknown channel '{channel}'. Allowed channels: {allowed}")
    return _run_dir(run_id) / CHANNEL_FILES[channel]


def _append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    append_jsonl(path, payload)


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    yield from read_jsonl(path)


def search_arxiv_theorems(
    query: str,
    num_results: int = 10,
    endpoint: str = THEOREM_SEARCH_URL,
    timeout_seconds: int = 30,
) -> Dict[str, Any]:
    if not query.strip():
        raise ValueError("query must be non-empty")
    if num_results <= 0:
        raise ValueError("num_results must be > 0")

    payload = {
        "query": query,
        "task": THEOREM_SEARCH_TASK,
        "num_results": num_results,
    }

    response = requests.post(endpoint, json=payload, timeout=timeout_seconds)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise ValueError("The theorem endpoint must return a JSON list")

    normalized: List[Dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "title": str(item.get("title", "")),
                "theorem": str(item.get("theorem", "")),
                "arxiv_id": str(item.get("arxiv_id", "")),
                "theorem_id": str(item.get("theorem_id", "")),
            }
        )

    return {
        "query": query,
        "count": len(normalized),
        "results": normalized,
        "endpoint": endpoint,
    }


def memory_init(run_id: str, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    sanitized_run_id = sanitize_run_id(run_id)
    run_dir = _run_dir(sanitized_run_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    created_files: Dict[str, str] = {}
    for channel, filename in CHANNEL_FILES.items():
        channel_path = run_dir / filename
        channel_path.touch(exist_ok=True)
        created_files[channel] = str(channel_path)

    meta_path = run_dir / "meta.json"
    update_meta(meta_path, {"run_id": sanitized_run_id}, meta)

    return {
        "run_id": sanitized_run_id,
        "memory_dir": str(run_dir),
        "meta_path": str(meta_path),
        "channels": created_files,
    }


def memory_append(run_id: str, channel: str, record: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("record must be a JSON object")

    resolved_run_id = sanitize_run_id(run_id)
    memory_init(resolved_run_id)

    entry = {
        "timestamp_utc": _utc_now(),
        "channel": channel,
        "record": record,
    }
    target = _channel_path(resolved_run_id, channel)
    _append_jsonl(target, entry)

    if channel != "events":
        _append_jsonl(
            _channel_path(resolved_run_id, "events"),
            {
                "timestamp_utc": _utc_now(),
                "event_type": "memory_append",
                "channel": channel,
            },
        )

    return {
        "status": "ok",
        "run_id": resolved_run_id,
        "channel": channel,
        "path": str(target),
        "entry": entry,
    }


def memory_query(
    run_id: str,
    channel: str,
    filters: Optional[Dict[str, Any]] = None,
    contains: Optional[str] = None,
    limit: int = 100,
    reverse: bool = True,
) -> Dict[str, Any]:
    if limit <= 0:
        raise ValueError("limit must be > 0")

    resolved_run_id = sanitize_run_id(run_id)
    path = _channel_path(resolved_run_id, channel)
    items = list(_iter_jsonl(path))

    if filters:
        filtered: List[Dict[str, Any]] = []
        for item in items:
            if all(item.get(key) == value for key, value in filters.items()):
                filtered.append(item)
        items = filtered

    if contains:
        needle = contains.lower()
        items = [item for item in items if needle in json.dumps(item, ensure_ascii=False).lower()]

    if reverse:
        items = list(reversed(items))

    items = items[:limit]
    return {
        "run_id": resolved_run_id,
        "channel": channel,
        "count": len(items),
        "items": items,
    }


def _load_schema() -> Dict[str, Any]:
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(f"Schema not found: {SCHEMA_PATH}")
    with SCHEMA_PATH.open("r", encoding="utf-8") as handle:
        schema = json.load(handle)
    if not isinstance(schema, dict):
        raise ValueError("schema must be a JSON object")
    return schema


def validate_verification_output(payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        validate_math_report(payload, _load_schema())
    except Exception as exc:
        return {"valid": False, "errors": [str(exc)]}
    return {"valid": True, "errors": []}


def write_verification_output(run_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    validation = validate_verification_output(payload)
    if not validation["valid"]:
        raise ValueError("verification output validation failed: " + "; ".join(validation["errors"]))

    resolved_run_id = sanitize_run_id(run_id)
    output_dir = RESULTS_ROOT / resolved_run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "verification.json"
    input_path = output_dir / "verification.input.json"
    if input_path.is_file():
        validate_math_report(payload, _load_schema(), proof=load_json(input_path)["proof"], require_claims=True)

    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)

    memory_init(resolved_run_id)
    memory_append(
        resolved_run_id,
        "verification_reports",
        {
            "event_type": "final_report_written",
            "output_path": str(output_path),
            "verdict": payload.get("verdict"),
        },
    )

    return {
        "status": "ok",
        "run_id": resolved_run_id,
        "output_path": str(output_path),
    }


def build_mcp_app() -> Optional[Any]:
    if FastMCP is None:
        return None

    app = FastMCP("verification_agent")

    @app.tool(name="search_arxiv_theorems")
    def _tool_search_arxiv_theorems(query: str, num_results: int = 10) -> Dict[str, Any]:
        return search_arxiv_theorems(query=query, num_results=num_results)

    @app.tool(name="memory_init")
    def _tool_memory_init(run_id: str, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return memory_init(run_id=run_id, meta=meta)

    @app.tool(name="memory_append")
    def _tool_memory_append(run_id: str, channel: str, record: Dict[str, Any]) -> Dict[str, Any]:
        return memory_append(run_id=run_id, channel=channel, record=record)

    @app.tool(name="memory_query")
    def _tool_memory_query(
        run_id: str,
        channel: str,
        filters: Optional[Dict[str, Any]] = None,
        contains: Optional[str] = None,
        limit: int = 100,
        reverse: bool = True,
    ) -> Dict[str, Any]:
        return memory_query(
            run_id=run_id,
            channel=channel,
            filters=filters,
            contains=contains,
            limit=limit,
            reverse=reverse,
        )

    @app.tool(name="validate_verification_output")
    def _tool_validate_verification_output(payload: Dict[str, Any]) -> Dict[str, Any]:
        return validate_verification_output(payload=payload)

    @app.tool(name="write_verification_output")
    def _tool_write_verification_output(run_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return write_verification_output(run_id=run_id, payload=payload)

    return app


APP = build_mcp_app()


def main() -> None:
    if APP is None:
        raise SystemExit(
            "fastmcp is not installed. Install dependencies from mcp/requirements.txt first."
        )
    APP.run()


if __name__ == "__main__":
    main()
