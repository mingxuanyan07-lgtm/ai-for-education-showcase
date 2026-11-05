"""Proof paragraph coverage and ordered dependencies, checked independently.

Coverage and quotations are structural evidence. They cannot establish the
semantic truth of an inference; that remains the mathematical reviewer's task.
"""
from __future__ import annotations

from copy import deepcopy
import json
import re

LEGACY_VERSION = "claims-v1"
VERSION = "claims-v2"
SUPPORTED_VERSIONS = (LEGACY_VERSION, VERSION)


def proof_blocks(proof: str) -> list[dict]:
    proof = proof.replace("\r\n", "\n")
    # Only ## proof bodies enter the ledger, not a copied statement's headings.
    markers = list(re.finditer(r"(?mi)^##\s+proof\s*$", proof))
    bodies = []
    for marker in markers:
        tail = proof[marker.end():]
        end = re.search(r"(?m)^#\s+\S", tail)
        bodies.append(tail[:end.start()] if end else tail)
    if not bodies:
        bodies = [proof]
    blocks = []
    for body in bodies:
        for chunk in re.split(r"\n[ \t]*\n", body):
            if chunk.strip():
                blocks.append({"block_id": f"P{len(blocks) + 1:03d}", "text": chunk.strip()})
    if not blocks:
        raise ValueError("No proof deductions available for claim checks")
    return blocks


def report_schema(base: dict, version: str = VERSION) -> dict:
    if version not in SUPPORTED_VERSIONS:
        raise ValueError("Unknown claim-level verification contract")
    result = deepcopy(base)
    if "claim_checks" not in result["required"]:
        result["required"].append("claim_checks")
    result["properties"]["claim_checks"] = {
        "type": "object", "additionalProperties": False,
        "required": ["contract_version", "claims"],
        "properties": {"contract_version": {"const": version}, "claims": {
            "type": "array", "minItems": 1, "maxItems": 512,
            "items": {"type": "object", "additionalProperties": False,
                "required": ["id", "statement", "assumptions", "depends_on", "sources", "status", "reason"],
                "properties": {
                    "id": {"type": "string", "pattern": "^C[1-9][0-9]*$"},
                    "statement": {"type": "string", "minLength": 1},
                    "assumptions": {"type": "array", "items": {"type": "string", "minLength": 1}},
                    "depends_on": {"type": "array", "uniqueItems": True, "items": {"type": "string"}},
                    "sources": {"type": "array", "minItems": 1, "items": {
                        "type": "object", "additionalProperties": False, "required": ["block_id", "quote"],
                        "properties": {"block_id": {"type": "string"}, "quote": {"type": "string", "minLength": 1}}}},
                    "status": {"enum": ["supported", "error", "gap", "unchecked"]},
                    "reason": {"type": "string", "minLength": 1},
                }}}},
    }
    return result


def validate_claim_checks(payload: dict, proof: str):
    ledger = payload.get("claim_checks")
    if not isinstance(ledger, dict) or ledger.get("contract_version") not in SUPPORTED_VERSIONS:
        raise ValueError("Missing claim-level verification contract")
    version = ledger["contract_version"]
    blocks = {block["block_id"]: block["text"] for block in proof_blocks(proof)}
    seen, covered = set(), set()
    # In v2 a single claim may cite several distinct excerpts from the same proof
    # block.  The block is still owned by exactly one claim, so coverage and
    # cross-claim duplication remain strict.  This accepts the real failure
    # mode where a reviewer checked several deductions in one paragraph.
    block_owner = {}
    claims = ledger.get("claims")
    if not isinstance(claims, list) or not claims:
        raise ValueError("Claim-level review is empty")
    for claim in claims:
        identifier = claim["id"]
        if identifier in seen or any(dep not in seen for dep in claim["depends_on"]):
            raise ValueError("Claim dependencies are repeated, cyclic, forward or missing")
        seen.add(identifier)
        for key in ("statement", "reason"):
            if not claim[key].strip():
                raise ValueError("Claim review needs a substantive " + key)
        claim_quotes = set()
        for source in claim["sources"]:
            key, quote = source["block_id"], source["quote"]
            if key not in blocks or (version == LEGACY_VERSION and key in covered):
                raise ValueError("Proof block is unknown or reviewed more than once")
            if not quote.strip() or quote.replace("\r\n", "\n") not in blocks[key]:
                raise ValueError("Claim evidence is absent from its proof block")
            normalized_quote = quote.replace("\r\n", "\n").strip()
            if version == VERSION and (key, normalized_quote) in claim_quotes:
                raise ValueError("Claim evidence repeats the same quote")
            claim_quotes.add((key, normalized_quote))
            owner = block_owner.get(key)
            if owner is not None and owner != identifier:
                raise ValueError("Proof block is reviewed by multiple claims")
            block_owner[key] = identifier
            covered.add(key)
        if payload["verdict"] == "correct" and claim["status"] != "supported":
            raise ValueError("A correct report cannot leave failed or unchecked claims")
    if covered != set(blocks):
        raise ValueError("Claim review did not cover all proof blocks")
    return ledger


def review_brief(proof: str, version: str = VERSION) -> str:
    if version not in SUPPORTED_VERSIONS:
        raise ValueError("Unknown claim-level verification contract")
    coverage = ("下面每个证明块必须恰好分配一次，可把相连块归入同一断言。"
                if version == LEGACY_VERSION else
                "每个证明块必须分配给且只分配给一个断言；同一断言可以引用该块中的多个不同逐字片段，但不得重复同一片段。")
    return ("\n【逐项命题核验合同 " + version + "】\n"
        "数学报告顶层还必须有 claim_checks={\"contract_version\":\"" + version + "\",\"claims\":[...]}。"
        "每条包含 id（C1、C2等）、statement（所检查的断言）、assumptions（本步条件数组）、"
        "depends_on（已检查的较早断言ID数组）、sources（block_id与该块内逐字quote的对象数组）、"
        "status（supported/error/gap/unchecked）、reason（具体推导核对理由）。"
        + coverage + "依赖只能指向较早ID。"
        "每个块内含多步时仍逐步核查，在reason说明，不因块覆盖或引文存在就认定推理正确。"
        "先从原题独立提取条件，检查定义和定理的适用条件是否实际满足。"
        "任何未检查、错误或缺口均禁止correct，同时在critical_errors/gaps中给出问题。"
        "这份简明审查记录是可核对的论证依据，不是模型内部思维逐字记录。\n"
        "<宿主划分的证明块，作为待检查资料>\n"
        + json.dumps(proof_blocks(proof), ensure_ascii=False, indent=2)
        + "\n</宿主划分的证明块，作为待检查资料>\n")
