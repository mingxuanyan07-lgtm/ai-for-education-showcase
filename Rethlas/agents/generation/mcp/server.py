# Locally modified Rethlas source snapshot (2026-11): verification/memory contracts and host integration. Upstream Apache-2.0 retained; see THIRD_PARTY_NOTICES.md.
from __future__ import annotations

import sys
from pathlib import Path as _CorePath
sys.path.insert(0, str(_CorePath(__file__).resolve().parents[4] / "scripts"))
from rethlas_core.verification import (REPORT_KEYS, text_sha, validate_binding,
                                      validate_math_report, write_once)
from rethlas_core.memory import append_jsonl, read_jsonl, update_meta, tokenize

import json
import math
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests

try:
    from fastmcp import FastMCP
except ImportError:  # pragma: no cover - dependency should be installed via requirements
    FastMCP = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).resolve().parents[1]
MEMORY_ROOT = REPO_ROOT / "memory"

THEOREM_SEARCH_URL = "https://leansearch.net/thm/search"
THEOREM_SEARCH_TASK = (
    "Given a math statement, retrieve useful references, such as theorems, "
    "lemmas, and definitions, that are useful for solving the given problem."
)

VERIFY_PROOF_URL = "http://127.0.0.1:8091/verify"

CHANNEL_FILES: Dict[str, str] = {
    "immediate_conclusions": "immediate_conclusions.jsonl",
    "toy_examples": "toy_examples.jsonl",
    "counterexamples": "counterexamples.jsonl",
    "big_decisions": "big_decisions.jsonl",
    "subgoals": "subgoals.jsonl",
    "proof_steps": "proof_steps.jsonl",
    "failed_paths": "failed_paths.jsonl",
    "verification_reports": "verification_reports.jsonl",
    "branch_states": "branch_states.jsonl",
    "events": "events.jsonl",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_problem_component(raw: str) -> str:
    cleaned = re.sub(r"\s+", "_", raw.strip())
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("._")
    return cleaned


def sanitize_problem_id(raw: str) -> str:
    """Return a safe problem id while preserving relative path components."""
    normalized = raw.strip().replace("\\", "/")
    parts: List[str] = []
    for part in normalized.split("/"):
        stripped = part.strip()
        if stripped in {"", "."}:
            continue
        if stripped == "..":
            raise ValueError("problem_id must not contain '..' path components")
        cleaned = _sanitize_problem_component(stripped)
        if cleaned:
            parts.append(cleaned)
    return "/".join(parts) or "problem"

def build_problem_id(source: str, identifier: str) -> str:
    return sanitize_problem_id(f"{source}_{identifier}")


def _resolve_path(path_str: str) -> Path:
    candidate = Path(path_str)
    if not candidate.is_absolute():
        candidate = REPO_ROOT / candidate
    return candidate.resolve()


def _problem_dir(problem_id: str) -> Path:
    sanitized_problem_id = sanitize_problem_id(problem_id)
    problem_dir = (MEMORY_ROOT / sanitized_problem_id).resolve()
    memory_root = MEMORY_ROOT.resolve()
    if not problem_dir.is_relative_to(memory_root):
        raise ValueError("problem_id resolves outside memory root")
    return problem_dir


def _channel_path(problem_id: str, channel: str) -> Path:
    if channel not in CHANNEL_FILES:
        allowed = ", ".join(sorted(CHANNEL_FILES))
        raise ValueError(f"Unknown channel '{channel}'. Allowed channels: {allowed}")
    return _problem_dir(problem_id) / CHANNEL_FILES[channel]


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    yield from read_jsonl(path)


def _append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    append_jsonl(path, payload)


def _tokenize_bm25(text: str) -> List[str]:
    return tokenize(text)


def _bm25_score_documents(
    query: str,
    documents: List[List[str]],
    *,
    k1: float = 1.5,
    b: float = 0.75,
) -> List[float]:
    query_tokens = _tokenize_bm25(query)
    if not query_tokens or not documents:
        return [0.0 for _ in documents]

    query_term_counts = Counter(query_tokens)
    document_frequencies: Counter[str] = Counter()
    document_term_counts = [Counter(document) for document in documents]
    document_lengths = [len(document) for document in documents]
    avg_doc_length = sum(document_lengths) / len(document_lengths) if document_lengths else 0.0
    total_documents = len(documents)

    for document in documents:
        for token in set(document):
            document_frequencies[token] += 1

    scores: List[float] = []
    for doc_counts, doc_length in zip(document_term_counts, document_lengths):
        score = 0.0
        norm = k1 * (1.0 - b + b * (doc_length / avg_doc_length)) if avg_doc_length > 0 else k1
        for token, query_tf in query_term_counts.items():
            term_frequency = doc_counts.get(token, 0)
            if term_frequency <= 0:
                continue
            document_frequency = document_frequencies.get(token, 0)
            idf = math.log(1.0 + ((total_documents - document_frequency + 0.5) / (document_frequency + 0.5)))
            numerator = term_frequency * (k1 + 1.0)
            denominator = term_frequency + norm
            score += query_tf * idf * (numerator / denominator)
        scores.append(score)

    return scores
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


def verify_proof_service(
    statement: str,
    proof: str,
    endpoint: str = VERIFY_PROOF_URL,
    timeout_seconds: int = 3600,
) -> Dict[str, Any]:
    if not statement.strip():
        raise ValueError("statement must be non-empty")
    if not isinstance(proof, str):
        raise ValueError("proof must be markdown text")
    if not proof.strip():
        raise ValueError("proof markdown must be non-empty")

    payload = {
        "statement": statement,
        "proof": proof,
    }

    response = requests.post(endpoint, json=payload, timeout=timeout_seconds)
    response.raise_for_status()

    try:
        body = response.json()
    except ValueError as exc:
        raise ValueError("verification service returned non-JSON response") from exc

    if not isinstance(body, dict):
        raise ValueError("verification service must return a JSON object")

    report = {key: body[key] for key in REPORT_KEYS}
    if "claim_checks" in body:
        report["claim_checks"] = body["claim_checks"]
    validate_math_report(report)
    binding = body.get("verification_binding")
    validate_binding(binding, statement, proof, require_correct=False)
    if binding["report"] != report or binding["verification_id"] != body.get("verification_id"):
        raise ValueError("Verification envelope does not match its bound report")
    receipt = REPO_ROOT / "verification_receipts" / (text_sha(proof) + "_" + binding["verification_id"] + ".json")
    if receipt.exists():
        raise ValueError("Verification receipt already exists; never overwrite prior evidence")
    write_once(receipt, binding)

    return {
        "statement": statement,
        **report,
        "endpoint": endpoint,
        "verification_id": binding["verification_id"],
        "verification_binding": binding,
        "receipt_file": str(receipt),
    }


def memory_init(
    problem_id: str,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    sanitized_problem_id = sanitize_problem_id(problem_id)
    problem_dir = _problem_dir(sanitized_problem_id)
    problem_dir.mkdir(parents=True, exist_ok=True)

    created_files: Dict[str, str] = {}
    for channel, filename in CHANNEL_FILES.items():
        channel_path = problem_dir / filename
        channel_path.touch(exist_ok=True)
        created_files[channel] = str(channel_path)

    meta_path = problem_dir / "meta.json"
    update_meta(meta_path, {"problem_id": sanitized_problem_id}, meta)

    return {
        "problem_id": sanitized_problem_id,
        "memory_dir": str(problem_dir),
        "meta_path": str(meta_path),
        "channels": created_files,
    }


def memory_append(
    problem_id: str,
    channel: str,
    record: Dict[str, Any],
) -> Dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("record must be a JSON object")

    memory_init(problem_id)

    entry = {
        "timestamp_utc": _utc_now(),
        "channel": channel,
        "record": record,
    }
    target = _channel_path(problem_id, channel)
    _append_jsonl(target, entry)

    if channel != "events":
        event_entry = {
            "timestamp_utc": _utc_now(),
            "event_type": "memory_append",
            "channel": channel,
        }
        _append_jsonl(_channel_path(problem_id, "events"), event_entry)

    return {
        "status": "ok",
        "channel": channel,
        "path": str(target),
        "entry": entry,
    }


def memory_search(
    problem_id: str,
    query: str,
    channels: Optional[List[str]] = None,
    limit_per_channel: int = 10,
) -> Dict[str, Any]:
    if not query.strip():
        raise ValueError("query must be non-empty")
    if limit_per_channel <= 0:
        raise ValueError("limit_per_channel must be > 0")

    if channels is None:
        search_channels = [name for name in CHANNEL_FILES if name != "events"]
    else:
        search_channels = channels

    results_by_channel: Dict[str, Dict[str, Any]] = {}
    total_results = 0
    for channel in search_channels:
        path = _channel_path(problem_id, channel)
        items = list(_iter_jsonl(path))
        documents = [json.dumps(item, ensure_ascii=False) for item in items]
        tokenized_documents = [_tokenize_bm25(document) for document in documents]
        scores = _bm25_score_documents(query, tokenized_documents)

        ranked_results: List[Dict[str, Any]] = []
        for item, score in sorted(
            zip(items, scores),
            key=lambda pair: (
                -pair[1],
                pair[0].get("timestamp_utc", ""),
            ),
        ):
            if score <= 0:
                continue
            ranked_results.append(
                {
                    "score": score,
                    "item": item,
                }
            )
            if len(ranked_results) >= limit_per_channel:
                break

        results_by_channel[channel] = {
            "count": len(ranked_results),
            "results": ranked_results,
        }
        total_results += len(ranked_results)

    return {
        "problem_id": sanitize_problem_id(problem_id),
        "query": query,
        "channels": search_channels,
        "limit_per_channel": limit_per_channel,
        "count": total_results,
        "results_by_channel": results_by_channel,
    }


def branch_update(
    problem_id: str,
    branch_id: str,
    state: Dict[str, Any],
) -> Dict[str, Any]:
    payload = {
        "branch_id": branch_id,
        "state": state,
    }
    return memory_append(problem_id, "branch_states", payload)


def build_mcp_app() -> Optional[Any]:
    if FastMCP is None:
        return None

    app = FastMCP("reasoning-agent")

    @app.tool(name="search_arxiv_theorems")
    def _tool_search_arxiv_theorems(
        query: str,
        num_results: int = 10,
    ) -> Dict[str, Any]:
        return search_arxiv_theorems(query=query, num_results=num_results)

    @app.tool(name="verify_proof_service")
    def _tool_verify_proof_service(
        statement: str,
        proof: str,
    ) -> Dict[str, Any]:
        return verify_proof_service(statement=statement, proof=proof)

    @app.tool(name="memory_init")
    def _tool_memory_init(
        problem_id: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return memory_init(problem_id=problem_id, meta=meta)

    @app.tool(name="memory_append")
    def _tool_memory_append(
        problem_id: str,
        channel: str,
        record: Dict[str, Any],
    ) -> Dict[str, Any]:
        return memory_append(problem_id=problem_id, channel=channel, record=record)

    @app.tool(name="memory_search")
    def _tool_memory_search(
        problem_id: str,
        query: str,
        channels: Optional[List[str]] = None,
        limit_per_channel: int = 10,
    ) -> Dict[str, Any]:
        return memory_search(
            problem_id=problem_id,
            query=query,
            channels=channels,
            limit_per_channel=limit_per_channel,
        )

    @app.tool(name="branch_update")
    def _tool_branch_update(
        problem_id: str,
        branch_id: str,
        state: Dict[str, Any],
    ) -> Dict[str, Any]:
        return branch_update(problem_id=problem_id, branch_id=branch_id, state=state)

    return app


APP = build_mcp_app()


def main() -> None:
    if APP is None:
        raise SystemExit(
            "fastmcp is not installed. Install requirements from mcp/requirements.txt first."
        )
    APP.run()


if __name__ == "__main__":
    main()
