"""Strict, non-secret configuration for the hybrid host."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from deepseek_rethlas.pipeline import _inside, validate_config, validate_teaching_config


def load_config(root: Path, value="setup/hybrid-tutor.json"):
    path = _inside(root, value)
    raw = path.read_bytes()
    if len(raw) > 65536:
        raise ValueError("Hybrid config exceeds 64 KiB")
    data = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, dict) or set(data) != {"schema_version", "proof", "teaching", "math_runtime_config", "math_runtime_sha256"}:
        raise ValueError("Invalid hybrid configuration keys")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("Expected hybrid schema_version=1")
    proof = validate_config(data["proof"])
    if proof["architecture_profile"] != "verified-v2":
        raise ValueError("Hybrid requires verified-v2")
    teaching = validate_teaching_config(data["teaching"])
    if teaching["model"] != proof["model"]:
        raise ValueError("Hybrid teaching must use the explicit matching DeepSeek model and native adapter")
    math_file = _inside(root, data["math_runtime_config"])
    if math_file.suffix != ".json":
        raise ValueError("Math runtime must be project JSON")
    if hashlib.sha256(math_file.read_bytes()).hexdigest() != data["math_runtime_sha256"]:
        raise ValueError("Math runtime changed; frozen hybrid configuration is no longer applicable")
    return {**data, "proof": proof}, path, hashlib.sha256(raw).hexdigest(), math_file
