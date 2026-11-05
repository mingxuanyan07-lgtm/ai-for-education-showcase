"""Promote native Rethlas proofs only against the verifier's saved receipt."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .verification import load_json, text_sha, validate_binding, write_once


def check_or_publish(problem):
    candidate = problem.result_dir / "blueprint.md"
    published = problem.verified_path
    subject = published if published.is_file() else candidate
    if not subject.is_file():
        return False
    raw = subject.read_bytes()
    proof = raw.decode("utf-8-sig")
    statement = problem.file.read_text(encoding="utf-8-sig")
    directory = problem.generation_root / "verification_receipts"
    receipts = list(directory.glob(text_sha(proof) + "_*.json")) if directory.is_dir() else []
    if not receipts:
        if published.is_file():
            raise ValueError("Existing verified file has no bound verifier evidence; retained as legacy_unbound. Use a fresh run or explicitly reverify the unchanged proof.")
        return False
    choices = [load_json(path) for path in receipts]
    binding = max(choices, key=lambda value: value.get("created_at_utc", ""))
    validate_binding(binding, statement, proof, require_correct=False)
    if binding["report"]["verdict"] != "correct":
        if published.is_file():
            raise ValueError("Published proof has a failing verification report")
        return False
    # Generation-side receipts are only pointers. The verifier's sibling
    # workspace is the authoritative source, not a model-written completion flag.
    verify_root = problem.generation_root.parent / "verification"
    trusted = verify_root / "results" / binding["verification_id"]
    if not trusted.resolve().is_relative_to(verify_root.resolve()):
        raise ValueError("Verifier evidence escapes its workspace")
    canonical = load_json(trusted / "verification.binding.json")
    if canonical != binding or load_json(trusted / "verification.json") != binding["report"]:
        raise ValueError("Generation receipt differs from saved verifier evidence")
    validate_binding(canonical, statement, proof)
    if not published.exists():
        published.parent.mkdir(parents=True, exist_ok=True)
        with published.open("xb") as stream:
            stream.write(raw)
    if published.read_bytes() != raw:
        raise ValueError("Proof changed during publication")
    manifest = problem.result_dir / "blueprint_verified.binding.json"
    data = {"schema_version": 1, "verification_id": binding["verification_id"],
            "statement_sha256": binding["statement_sha256"], "proof_sha256": binding["proof_sha256"],
            "raw_proof_sha256": hashlib.sha256(published.read_bytes()).hexdigest(),
            "report_sha256": binding["report_sha256"]}
    if manifest.exists():
        if load_json(manifest) != data:
            raise ValueError("Published proof manifest changed or is bound to older verification")
    else:
        write_once(manifest, data)
    return True
