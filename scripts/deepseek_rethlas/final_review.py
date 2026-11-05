"""Archive an actual current-chat review, bound to the completed DeepSeek bundle.

This module does not call a model or derive a Codex verdict from DeepSeek reports.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

import experiment_records
from . import teaching_contract as pedagogy
from rethlas_core.verification import load_json, validate_binding, validate_math_report, text_sha


def _inside(root: Path, value: str) -> Path:
    path = Path(value)
    if ".." in path.parts:
        raise ValueError("Parent traversal is not allowed")
    path = (root / path).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Review materials must be files inside the project")
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def finalize_review(project_root, request_file, prompt_file, report_file, *, dry_run=False):
    """Validate saved review evidence and append it to exactly the bound record."""
    root = Path(project_root).resolve(strict=True)
    request_path = _inside(root, str(request_file))
    prompt_path = _inside(root, str(prompt_file))
    report_path = _inside(root, str(report_file))
    request = json.loads(request_path.read_text(encoding="utf-8-sig"))
    report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    if request.get("schema_version") != 1 or request.get("status") != "pending_final_review":
        raise ValueError("Expected a pending final-review request, schema version 1")
    if request.get("reviewer", {}).get("kind") != "codex_current_chat":
        raise ValueError("Review request is not addressed to the current Codex chat")
    contract = request.get("teaching_contract", pedagogy.LEGACY)
    if contract not in (pedagogy.LEGACY, pedagogy.VERSION):
        raise ValueError("Unknown teaching contract in final-review request")
    profile = request.get("architecture_profile", "legacy")
    hybrid = request.get("workflow_variant") == "hybrid-rethlas-deeptutor-v1"
    if str(request.get("identity", {}).get("ai_id", "")).startswith("hybrid-rethlas-deeptutor-") and not hybrid:
        raise ValueError("Hybrid review request is missing its workflow identity")
    if profile not in ("legacy", "verified-v2"):
        raise ValueError("Unknown architecture profile")
    if profile == "verified-v2" and (contract != pedagogy.VERSION
            or not {"proof_binding", "claim_memory"}.issubset(request.get("artifacts", {}))):
        raise ValueError("verified-v2 requires teaching contract, proof binding and claim memory")
    if hybrid and (profile != "verified-v2" or not {"teaching_source", "teaching_config"}.issubset(request.get("artifacts", {}))):
        raise ValueError("Hybrid final review requires verified-v2 and bound teaching provenance")
    expected_prompt = request["reviewer"]["prompt"]
    bound_prompt_path = _inside(root, expected_prompt["file"])
    if _sha(bound_prompt_path) != expected_prompt["sha256"] or _sha(prompt_path) != expected_prompt["sha256"]:
        raise ValueError("The saved actual review prompt differs from the bound review task")
    identity = request["identity"]
    for key in ("question_id", "ai_id", "run_id"):
        if not isinstance(identity.get(key), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", identity[key]):
            raise ValueError("Invalid review identity")
    bound = []
    required = {"inputs": ("problem", "requirements", "user_prompt"),
                "artifacts": ("proof", "proof_report", "teaching", "teaching_report")}
    if "proof_binding" in request.get("artifacts", {}):
        required["artifacts"] += ("proof_binding",)
    if "computation_evidence" in request.get("artifacts", {}):
        required["artifacts"] += ("computation_evidence",)
    if "claim_memory" in request.get("artifacts", {}):
        required["artifacts"] += ("claim_memory",)
    if hybrid:
        required["artifacts"] += ("teaching_source", "teaching_config")
    if "math_runtime" in request.get("artifacts", {}):
        required["artifacts"] += ("math_runtime",)
    for group, names in required.items():
        for name in names:
            entry = request[group][name]
            path = _inside(root, entry["file"])
            if _sha(path) != entry["sha256"]:
                raise ValueError(f"Review material changed: {group}.{name}")
            bound.append(path)
    if "proof_binding" in request["artifacts"]:
        binding = load_json(_inside(root, request["artifacts"]["proof_binding"]["file"]))
        validate_binding(binding,
            _inside(root, request["inputs"]["problem"]["file"]).read_text(encoding="utf-8-sig"),
            _inside(root, request["artifacts"]["proof"]["file"]).read_text(encoding="utf-8-sig"))
        if binding["report"] != load_json(_inside(root, request["artifacts"]["proof_report"]["file"])):
            raise ValueError("Bound proof report differs from the final-review bundle")
    if profile == "verified-v2":
        proof = _inside(root, request["artifacts"]["proof"]["file"]).read_text(encoding="utf-8-sig")
        validate_math_report(binding["report"], proof=proof, require_claims=True)
        memory = load_json(_inside(root, request["artifacts"]["claim_memory"]["file"]))
        current = memory.get("current") or {}
        if (memory.get("identity") != identity or current.get("proof_sha256") != text_sha(proof)
                or current.get("report_sha256") != binding["report_sha256"]):
            raise ValueError("Claim memory belongs to another proof/report/run")
    if report.get("review_request_sha256") != _sha(request_path):
        raise ValueError("Report is not bound to this final-review request")
    verifier = report.get("verifier", {})
    if (verifier.get("verifier_id") != "codex-current-chat"
            or verifier.get("source") != "current_codex_chat"
            or not isinstance(verifier.get("model"), str) or not verifier["model"].strip()):
        raise ValueError("Explicit current-chat reviewer metadata is required")
    verdict = report.get("verdict")
    errors, gaps = report.get("critical_errors"), report.get("gaps")
    if verdict not in ("correct", "wrong") or not isinstance(errors, list) or not isinstance(gaps, list):
        raise ValueError("Review needs correct/wrong verdict and error/gap lists")
    if verdict == "correct" and (errors or gaps):
        raise ValueError("A correct review cannot contain errors or gaps")
    if verdict == "wrong" and not (errors or gaps):
        raise ValueError("A wrong review needs a specific error or gap")
    for key in ("summary", "task_review"):
        if not isinstance(report.get(key), str) or not report[key].strip():
            raise ValueError(f"Review needs substantive {key}")
    checks = report.get("checks", {})
    if profile == "verified-v2":
        for key in ("claim_dependencies", "computation_evidence", "memory_scope"):
            if not isinstance(checks.get(key), str) or not checks[key].strip():
                raise ValueError("Final review coverage missing: " + key)
    for key in ("problem_conditions", "proof", "deepseek_reports", "teaching_math", "requirements"):
        if not isinstance(checks.get(key), str) or not checks[key].strip():
            raise ValueError(f"Review coverage missing: {key}")
    teaching_review = None
    if contract == pedagogy.VERSION:
        for key in ("knowledge_requirements", "solution_route"):
            if not isinstance(checks.get(key), str) or not checks[key].strip():
                raise ValueError(f"Review coverage missing: {key}")
        teaching_text = _inside(root, request["artifacts"]["teaching"]["file"]).read_text(encoding="utf-8-sig")
        internal = json.loads(_inside(root, request["artifacts"]["teaching_report"]["file"]).read_text(encoding="utf-8-sig"))
        internal_review = pedagogy.validate_review(internal.get("teaching_review"), teaching_text)
        if internal.get("verdict") != "correct" or internal_review["verdict"] != "pass":
            raise ValueError("Final review needs an internally passed mathematics and teaching bundle")
        teaching_review = pedagogy.validate_review(report.get("teaching_review"), teaching_text)
    if not prompt_path.read_text(encoding="utf-8-sig").strip():
        raise ValueError("The actual final-review prompt cannot be empty")
    record_path = _inside(root, str(Path("docs/experiments") / identity["question_id"] /
                                  identity["ai_id"] / identity["run_id"] / "record.json"))
    record = json.loads(record_path.read_text(encoding="utf-8-sig"))
    if any(record.get(key) != identity[key] for key in identity if key in ("question_id", "ai_id", "run_id")):
        raise ValueError("Record identity differs from the review request")
    if hybrid:
        source = load_json(_inside(root, request["artifacts"]["teaching_source"]["file"]))
        config = load_json(_inside(root, request["artifacts"]["teaching_config"]["file"]))
        from .pipeline import validate_teaching_config
        validate_teaching_config(config)
        if (source.get("schema_version") != 1 or source.get("identity") != identity
                or source.get("workflow_variant") != "hybrid-rethlas-deeptutor-v1"
                or source.get("teaching_config") != config):
            raise ValueError("Hybrid teaching provenance belongs to another run or configuration")
        for name, group in (("problem", "inputs"), ("requirements", "inputs"),
                            ("proof", "artifacts"), ("proof_report", "artifacts")):
            if source.get(name) != request[group][name]:
                raise ValueError("Hybrid teaching provenance does not bind current " + name)
        teaching_rows = [row for row in record["iterations"] if row.get("settings", {}).get("stage") == "post_verification_teaching"]
        if not teaching_rows:
            raise ValueError("Hybrid teaching provenance has no recorded teaching role")
        row = teaching_rows[-1]
        settings = row.get("settings", {})
        if (source.get("stage") != row.get("iteration_id")
                or source.get("prompt_sha256") != hashlib.sha256(row.get("prompt", "").encode("utf-8")).hexdigest()
                or settings.get("role_workflow") != "deeptutor-chat-teaching-v1"
                or settings.get("actual_provider") != config["provider"]
                or settings.get("actual_model") != config["model"]
                or settings.get("status") != "completed"
                or settings.get("provider_request_attempts") != 1):
            raise ValueError("Hybrid teaching provenance differs from the recorded actual role")
    for name, key in (("problem", "question"), ("requirements", "requirements"), ("user_prompt", "user_prompt")):
        if _inside(root, request["inputs"][name]["file"]).read_bytes().decode("utf-8-sig") != record.get(key):
            raise ValueError(f"Review input differs from the original recorded {key}")
    if _inside(root, request["artifacts"]["proof"]["file"]).read_bytes().decode("utf-8-sig") != record.get("final_answer"):
        raise ValueError("Reviewed proof differs from the recorded final proof")
    iteration_id = "codex-final-" + _sha(report_path)[:16]
    replay = any(item.get("iteration_id") == iteration_id for item in record["iterations"])
    if record.get("status") != "pending_final_review" and not replay:
        raise ValueError("Only a complete bundle pending final review can be finalized")
    verification = {
        "stage": "final_review", "verdict": verdict, "verifier": verifier,
        "verification_report": {"summary": report["summary"], "critical_errors": errors, "gaps": gaps},
        "review_request_sha256": report["review_request_sha256"],
        "checks": checks, "report_file": str(report_path.relative_to(root)),
    }
    if teaching_review is not None:
        verification["teaching_review"] = teaching_review
    previous = record.get("verification")
    previous = previous if isinstance(previous, list) else []
    previous = [item for item in previous if not (
        isinstance(item, dict) and item.get("stage") == "final_review"
        and item.get("verdict") == "not_run"
        and item.get("verifier", {}).get("verifier_id") == "codex-current-chat")]
    if verification not in previous:
        previous.append(verification)
    iterations = [{"iteration_id": iteration_id, "prompt_file": str(prompt_path.relative_to(root)),
                   "answer_file": str(report_path.relative_to(root)), "verification": [verification],
                   "settings": {"stage": "final_review", "actual_model": verifier["model"],
                                "actual_provider": "未记录", "source": "current_codex_chat",
                                "review_request_sha256": _sha(request_path)}}]
    # Only supplement verification metadata; original teaching prompt and answer stay intact.
    teaching = [item for item in record["iterations"]
                if item.get("settings", {}).get("stage") in ("teaching", "post_verification_teaching")]
    if not teaching or _inside(root, request["artifacts"]["teaching"]["file"]).read_text(encoding="utf-8-sig") != teaching[-1].get("answer"):
        raise ValueError("Reviewed teaching differs from the recorded teaching answer")
    old = teaching[-1].get("verification", [])
    old = old if isinstance(old, list) else [old] if isinstance(old, dict) else []
    old = [item for item in old if not (item.get("stage") == "final_review" and item.get("verdict") == "not_run")]
    iterations.append({"iteration_id": teaching[-1]["iteration_id"],
                       "verification": old if verification in old else old + [verification]})
    manifest = {**{key: identity[key] for key in ("question_id", "ai_id", "run_id")},
                "status": "completed" if verdict == "correct" and (
                    teaching_review is None or teaching_review["verdict"] == "pass") else "unverified",
                "verification": previous, "iterations": iterations,
                "task_review": report["task_review"] + (
                    "\n" + pedagogy.summary(teaching_review) if teaching_review is not None else ""),
                "evidence_files": [str(path.relative_to(root))
                                   for path in [request_path, prompt_path, report_path, *bound]]}
    if dry_run:
        return {"dry_run": True, "wrote": False, **identity, "verdict": verdict,
                "expected_status": manifest["status"],
                "final_teaching_verdict": teaching_review["verdict"] if teaching_review is not None else None,
                "notice": "Validated only; no model call or record update."}
    result = experiment_records.record_manifest(manifest, root)
    result.update({"status": manifest["status"], "final_verdict": verdict,
                   "reviewer": "codex-current-chat"})
    if teaching_review is not None:
        result["final_teaching_verdict"] = teaching_review["verdict"]
    return result
