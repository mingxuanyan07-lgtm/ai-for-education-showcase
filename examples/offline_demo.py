"""Demonstrate host-side contracts with synthetic fixtures; no model or network.

The fixture verdict is test data, never a mathematical assessment. Demonstrates
content binding, rejection of changed evidence, bounded admission and archiving.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from experiment_records import record_manifest
from record_reading import render_reading
from rethlas_core.budget import BudgetLimit, RunBudget
from rethlas_core.verification import make_binding, validate_binding


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("demo-output"))
    args = parser.parse_args()
    output = args.output.resolve()
    if output == ROOT or output in ROOT.parents:
        parser.error("Choose a dedicated output directory, not the source root or its parents")
    output.mkdir(parents=True, exist_ok=True)
    statement = "Synthetic statement P (not a benchmark problem)."
    proof = "Synthetic proof fixture (not mathematical evidence)."
    report = {"verification_report": {"summary": "Synthetic report for contract demonstration only.",
                                      "critical_errors": [], "gaps": []},
              "verdict": "correct", "repair_hints": ""}
    binding = make_binding(statement, proof, report, "offline-demo-fixture")
    validate_binding(binding, statement, proof)
    checks = {"unchanged_evidence_accepted": True}
    for name, value, candidate in (
        ("changed_proof_rejected", binding, proof + " Modified."),
        ("changed_report_rejected", deepcopy(binding), proof),
    ):
        if name == "changed_report_rejected":
            value["report"]["verification_report"]["summary"] = "Mutated report fixture."
        try:
            validate_binding(value, statement, candidate)
        except ValueError:
            checks[name] = True
        else:
            raise RuntimeError("Contract unexpectedly accepted altered evidence")
    # Admission is simulated; no provider, subprocess or model is dispatched.
    budget = RunBudget(seconds=30, model_calls=1, tool_calls=1)
    budget.admit_model(budget.deadline)
    try:
        budget.admit_model(budget.deadline)
    except BudgetLimit:
        checks["excess_admission_rejected"] = True
    else:
        raise RuntimeError("Budget unexpectedly admitted an excess attempt")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    answer = ("# 离线合成夹具\n\n本页面没有调用模型，也不是数学正确性评测。\n\n"
              "公式排版示例：\\(x^2\\)。\n\n"
              "证据绑定、篡改拒绝和调用准入由真实宿主代码执行；verdict 是预先写入的测试数据。")
    record = record_manifest({
        "question_id": "offline_contract_demo", "ai_id": "synthetic-fixture", "run_id": stamp,
        "record_kind": "installation_test", "question": statement, "model": "not_applicable",
        "framework": "Offline synthetic contract demonstration", "status": "completed",
        "requirements": "Demonstrate host contracts without a model call.",
        "user_prompt": "合成夹具：展示内容绑定、准入与离线排版；不进行数学求解。",
        "tags": ["合成演示"], "iterations": [{"iteration_id": "synthetic-1",
            "prompt": "Synthetic fixture; no real user conversation.", "answer": answer,
            "verification": {"verdict": "not_run"}}],
    }, output)
    result = {"synthetic": True, "model_calls": 0, "checks": checks,
              "simulated_admission": budget.snapshot(), "record_kind": "installation_test",
              "record_directory": Path(record["record_dir"]).relative_to(output).as_posix()}
    for name, payload in (("checks.json", result), ("binding.fixture.json", binding)):
        (output / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    record_link = (Path(record["record_path"]).with_suffix(".html")).relative_to(output).as_posix()
    page = "# 离线演示结果\n\n这是合成夹具，不是模型解答或数学评判。\n\n"
    page += "| 宿主合同检查 | 结果 |\n|---|---|\n"
    page += "\n".join(f"| {name} | PASS |" for name in checks)
    page += f"\n\n实际模型调用：0。\n\n[查看排版与归档示例]({record_link}) · [结构化结果](checks.json)\n"
    (output / "index.html").write_text(render_reading(page, "Offline synthetic demo"), encoding="utf-8")
    print(json.dumps({"synthetic": True, "model_calls": 0, "checks": checks,
                      "open": str(output / "index.html")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
