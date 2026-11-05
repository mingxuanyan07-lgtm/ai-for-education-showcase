"""Mathematical report validation and content-bound verification receipts."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "Rethlas/agents/verification/schemas/verification_output.schema.json"
REPORT_KEYS = ("verification_report", "verdict", "repair_hints")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key: " + key)
        result[key] = value
    return result


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=unique_object)


def text_sha(text: str) -> str:
    """Content hash ignores only outer whitespace and CRLF, never internal text.

    Raw file SHA256s remain separately recorded; this policy permits stdin/file
    newline differences without silently altering original source materials.
    """
    return hashlib.sha256(text.replace("\r\n", "\n").strip().encode("utf-8")).hexdigest()


def object_sha(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def validate_math_report(payload: dict, schema=None, *, proof=None, require_claims=False) -> dict:
    if schema is None:
        schema = load_json(SCHEMA)
    if isinstance(payload, dict) and ("claim_checks" in payload or require_claims):
        from . import claims
        ledger = payload.get("claim_checks")
        version = ledger.get("contract_version") if isinstance(ledger, dict) else claims.VERSION
        schema = claims.report_schema(schema, version)
    Draft202012Validator(schema).validate(payload)
    detail = payload["verification_report"]
    defects = bool(detail["critical_errors"] or detail["gaps"])
    if (payload["verdict"] == "correct") == defects:
        raise ValueError("Verification verdict disagrees with errors/gaps")
    hints = payload["repair_hints"]
    if payload["verdict"] == "correct" and hints != "":
        raise ValueError("correct requires empty repair_hints")
    if payload["verdict"] == "wrong" and not hints.strip():
        raise ValueError("wrong requires substantive repair_hints")
    if proof is not None and ("claim_checks" in payload or require_claims):
        from .claims import validate_claim_checks
        validate_claim_checks(payload, proof)
    return payload


def make_binding(statement: str, proof: str, report: dict, verification_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", verification_id):
        raise ValueError("Unsafe verification ID")
    return {"schema_version": 1, "verification_id": verification_id,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "text_hash_policy": "utf8-lf-outer-strip-v1",
            "statement_sha256": text_sha(statement), "proof_sha256": text_sha(proof),
            "report_sha256": object_sha(report), "report": report}


def validate_binding(binding, statement: str, proof: str, *, require_correct=True, schema=None):
    if (not isinstance(binding, dict) or binding.get("schema_version") != 1
            or binding.get("text_hash_policy") != "utf8-lf-outer-strip-v1"):
        raise ValueError("Missing or unsupported proof verification binding")
    identifier = binding.get("verification_id")
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", identifier):
        raise ValueError("Invalid bound verification ID")
    report = binding.get("report")
    validate_math_report(report, schema, proof=proof)
    if binding.get("statement_sha256") != text_sha(statement) or binding.get("proof_sha256") != text_sha(proof):
        raise ValueError("Verification binding belongs to a different statement or proof")
    if binding.get("report_sha256") != object_sha(report):
        raise ValueError("Bound verification report changed")
    if require_correct and report["verdict"] != "correct":
        raise ValueError("Bound proof has not passed mathematical verification")
    return binding


def write_once(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
