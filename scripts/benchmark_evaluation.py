"""Shared external assessment contract. Internal verifications never become benchmark scores."""
from __future__ import annotations

import hashlib
import json
import math
import re

from runtime_metrics import runtime_summary, summary_markdown

DIMENSIONS = ("mathematical_correctness", "teaching_quality", "interaction", "reliability", "cost")
VERDICTS = ("correct", "wrong", "partial", "unanswered", "not_reviewed")


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + " must be nonempty text")


def _number(value, label):
    if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
        raise ValueError(label + " must be a finite nonnegative number or null")


def validate_evaluation(value):
    if not isinstance(value, dict):
        raise ValueError("evaluation must be an object")
    for name in ("task_materials", "budget", "usage", "external_review_plan"):
        if name in value and not isinstance(value[name], dict):
            raise ValueError("evaluation." + name + " must be an object")
    roles = value.get("roles", [])
    if not isinstance(roles, list):
        raise ValueError("evaluation.roles must be a list")
    ids = []
    for role in roles:
        if not isinstance(role, dict):
            raise ValueError("Each role must be an object")
        _text(role.get("role_id"), "role_id")
        ids.append(role["role_id"])
        if "tools" in role and (not isinstance(role["tools"], list) or any(not isinstance(x, str) for x in role["tools"])):
            raise ValueError("role.tools must be a string list")
        for key in ("calls", "input_tokens", "output_tokens", "cached_input_tokens", "duration_seconds", "cost"):
            _number(role.get(key), "role." + key)
    if len(ids) != len(set(ids)):
        raise ValueError("role_id cannot repeat")
    for key in ("max_calls", "max_tokens", "timeout_seconds", "max_retries", "max_revisions", "max_cost"):
        _number(value.get("budget", {}).get(key), "budget." + key)
    for key in ("calls", "input_tokens", "output_tokens", "total_tokens", "wall_seconds", "cost"):
        _number(value.get("usage", {}).get(key), "usage." + key)
    for key in ("independent_sessions", "answers_hidden", "same_task_materials"):
        if key in value.get("task_materials", {}) and value["task_materials"][key] is not None and type(value["task_materials"][key]) is not bool:
            raise ValueError("task_materials." + key + " must be boolean or null")


def validate_reviews(reviews):
    if not isinstance(reviews, list):
        raise ValueError("external_reviews must be a list")
    for review in reviews:
        if not isinstance(review, dict):
            raise ValueError("Each external review must be an object")
        for key in ("review_id", "protocol_id", "rubric_id", "report"):
            _text(review.get(key), "external_review." + key)
        if review.get("scope") != "final_output" or review.get("independent") is not True:
            raise ValueError("External review requires scope=final_output and independent=true")
        if review.get("verdict") not in VERDICTS:
            raise ValueError("Unknown external verdict")
        target = review.get("target")
        if not isinstance(target, dict):
            raise ValueError("External review target is required")
        _text(target.get("iteration_id"), "target.iteration_id")
        if not isinstance(target.get("answer_sha256"), str) or not re.fullmatch("[a-f0-9]{64}", target["answer_sha256"]):
            raise ValueError("External review requires target.answer_sha256")
        reviewer = review.get("reviewer")
        if not isinstance(reviewer, dict) or reviewer.get("kind") not in ("model", "human"):
            raise ValueError("External reviewer.kind must be model or human")
        _text(reviewer.get("reviewer_id"), "reviewer_id")
        if not isinstance(review.get("dimensions", {}), dict):
            raise ValueError("External dimensions must be an object")
        for name in ("teaching_quality", "interaction"):
            dimension = review.get("dimensions", {}).get(name, {})
            if not isinstance(dimension, dict) or dimension.get("status", "pending") not in ("pending", "evaluated", "not_evaluated"):
                raise ValueError("Invalid external dimension status")
            items = dimension.get("items", [])
            if not isinstance(items, list):
                raise ValueError("Dimension items must be a list")
            if dimension.get("status") == "evaluated" and not items:
                raise ValueError("Evaluated dimension requires criterion evidence")
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("Dimension item must be an object")
                for key in ("criterion", "reason", "evidence"):
                    _text(item.get(key), "dimension." + key)
                _number(item.get("score"), "dimension.score")
                _number(item.get("max_score"), "dimension.max_score")
                if item.get("score") is None or item.get("max_score") is None or item["max_score"] <= 0 or item["score"] > item["max_score"]:
                    raise ValueError("Dimension score must be within the declared scale")
        audit = review.get("human_audit", {})
        if not isinstance(audit, dict) or audit.get("status", "pending") not in ("pending", "not_sampled", "completed"):
            raise ValueError("Invalid human audit status")
        if audit.get("status") == "completed":
            for key in ("reviewer_id", "sampling_method", "report"):
                _text(audit.get(key), "human_audit." + key)
            if audit.get("verdict") not in VERDICTS[:-1]:
                raise ValueError("Completed human audit needs a verdict")


def output_items(record):
    result = []
    for item in record.get("iterations", []):
        settings = item.get("settings") if isinstance(item.get("settings"), dict) else {}
        if settings.get("output_kind") == "internal_draft":
            # Keep failed/internal drafts as evidence without treating them as
            # delivered student answers. Unmarked historical outputs retain
            # their existing behavior.
            continue
        stage = settings.get("stage", "")
        if re.search(r"verif|review|check|核验|复核|终审", str(stage), re.I) and "post_verification_teaching" != stage:
            # Chinese stage '核验后的外层教学整理' is a delivered teaching answer.
            if "教学整理" not in str(stage):
                continue
        if isinstance(item.get("answer"), str) and item["answer"].strip() not in ("", "未记录"):
            result.append(item)
    return result


def merge_assessments(record, manifest):
    """Merge only supplied fields. Review IDs are immutable; changed answers require new reviews."""
    if "evaluation" in manifest:
        record["evaluation"] = merge_design(record.get("evaluation", {}), manifest["evaluation"])
        validate_evaluation(record["evaluation"])
    reviews = record.setdefault("external_reviews", []) if "external_reviews" in manifest else record.get("external_reviews", [])
    known = {r["review_id"]: r for r in reviews}
    for review in manifest.get("external_reviews", []):
        previous = known.get(review["review_id"])
        if previous is not None and previous != review:
            raise ValueError("Refusing to overwrite external review; use a new review_id")
        if previous is None:
            reviews.append(review)
            known[review["review_id"]] = review
    if reviews:
        validate_reviews(reviews)
        candidates = {i["iteration_id"]: i.get("answer", "") for i in record.get("iterations", [])}
        role_ids = {r["role_id"] for r in record.get("evaluation", {}).get("roles", [])} | {record["ai_id"]}
        for review in reviews:
            target = review["target"]
            answer = "" if target["iteration_id"] == "no-output" else candidates.get(target["iteration_id"])
            if not isinstance(answer, str) or digest(answer) != target["answer_sha256"]:
                raise ValueError("External review does not match its recorded answer")
            if review["reviewer"]["reviewer_id"] in role_ids:
                raise ValueError("External reviewer cannot be a tested system role")
            if (review["verdict"] == "unanswered") != (target["iteration_id"] == "no-output") and review["verdict"] != "not_reviewed":
                raise ValueError("unanswered verdict requires the no-output target")


def merge_design(old, new):
    out = dict(old)
    for key, value in new.items():
        out[key] = merge_design(out.get(key, {}), value) if isinstance(value, dict) and isinstance(out.get(key, {}), dict) else value
    return out


def assessment_view(record):
    design = record.get("evaluation", {})
    answers = output_items(record)
    target = answers[-1] if answers else {"iteration_id": "no-output", "answer": ""}
    protocol = design.get("external_review_plan", {}).get("protocol_id", "")
    rubric = design.get("external_review_plan", {}).get("rubric_id", "")
    valid = []
    for review in record.get("external_reviews", []):
        try:
            validate_reviews([review])
        except (ValueError, TypeError):
            continue
        internal_ids = {r["role_id"] for r in design.get("roles", [])} | {record["ai_id"]}
        if review["reviewer"]["reviewer_id"] in internal_ids:
            continue
        if review["target"] != {"iteration_id": target["iteration_id"], "answer_sha256": digest(target["answer"])}:
            continue
        if protocol and review["protocol_id"] != protocol or rubric and review["rubric_id"] != rubric:
            continue
        reviewer_id = design.get("external_review_plan", {}).get("reviewer_id")
        if reviewer_id not in (None, "", "未记录", review["reviewer"]["reviewer_id"]):
            continue
        if design.get("final_output_iteration_id", target["iteration_id"]) != target["iteration_id"]:
            continue
        valid.append(review)
    review = valid[-1] if valid else {}
    dims = review.get("dimensions", {})
    audit = review.get("human_audit", {})
    # Human audit is preserved separately. Conflicts remain unresolved until a new common review adjudicates them.
    conflict = audit.get("status") == "completed" and audit.get("verdict") != review.get("verdict")
    verdict = "disputed" if conflict else review.get("verdict", "not_reviewed")
    return {"external_verdict": verdict, "external_protocol": review.get("protocol_id", protocol),
            "external_rubric": review.get("rubric_id", rubric), "external_reviewer": review.get("reviewer", {}).get("reviewer_id", "未记录"),
            "external_review_id": review.get("review_id", ""), "human_audit_status": audit.get("status", "pending"),
            "external_dimensions": dims, "evaluation": design,
            "system_id": design.get("system_id", "未记录"), "budget_condition": design.get("budget", {}).get("condition", "未记录")}


def dimension_text(value):
    status = value.get("status", "pending")
    if status != "evaluated":
        return "未评测" if status == "not_evaluated" else "待评"
    return "；".join(f"{x['criterion']} {x['score']}/{x['max_score']}（{x['reason']}；证据：{x['evidence']}）" for x in value.get("items", []))


def record_summary(record):
    view = assessment_view(record)
    runtime = runtime_summary(record)
    safe = lambda value: str(value).replace("|", "\\|").replace("\n", " ")
    usage = view["evaluation"].get("usage", {})
    amount = usage.get("cost")
    fee = (f"费用 {amount:g} {usage.get('currency') or '币种未记录'}"
           if type(amount) in (int, float) and math.isfinite(amount) and amount >= 0
           else "费用未记录（无账单依据）")
    cost = (runtime["seconds_label"] + "；Token " + runtime["tokens_label"]
            + "；" + fee)
    rows = summary_markdown(record) + ["### 统一评测摘要", "", "| 评价维度 | 结果 |", "|---|---|",
            "| 数学正确性（共同外审） | " + safe(view["external_verdict"]) + " |",
            "| 教学质量 | " + safe(dimension_text(view["external_dimensions"].get("teaching_quality", {}))) + " |",
            "| 互动能力 | " + safe(dimension_text(view["external_dimensions"].get("interaction", {}))) + " |",
            "| 运行可靠性 | " + safe(record.get("status", "未记录")) + " |",
            "| 成本与效率 | " + safe(cost) + " |", "",
            "共同外审协议／量表：" + safe(view["external_protocol"] or "未记录") + " / " + safe(view["external_rubric"] or "未记录") + "。人工抽查：" + safe(view["human_audit_status"]) + "。", "",
            "内部 correct、旧 grading 和流程终审均保留为过程证据，不自动作为共同外审评分。", "",
            "[统一任务材料、各角色模型与预算详情](评测详情.html) · [结构化原始记录](record.json)", ""]
    return rows


def _cell(value):
    """Keep runtime metadata literal inside a Markdown table, including pipes."""
    if value is None or value == "":
        return "未记录"
    return re.sub(r"([\\`*{}\[\]<>()#+.!_|])", r"\\\1", str(value)).replace("\r", " ").replace("\n", " ")


def _count(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return "未记录"
    return f"{value:,}"


def _seconds(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return "未记录"
    return f"{value:.2f}"


def _known_model(value):
    return isinstance(value, str) and value.strip() not in ("", "未记录", "unknown", "not_recorded")


def _legacy_role_part(item):
    settings = item.get("settings") if isinstance(item.get("settings"), dict) else {}
    # The record-level model may only identify the generation role. It is not
    # evidence for an unmetered teaching or review role in the same record.
    model = settings.get("actual_model") if "actual_model" in settings else settings.get("model")
    model = model if _known_model(model) else "未记录"
    return runtime_summary({"model": model, "iterations": [item], "evaluation": {}})


def _recovery_target(iterations, recovery):
    deduplication = recovery.get("deduplication")
    thread_id = deduplication.get("thread_id") if isinstance(deduplication, dict) else None
    if not isinstance(thread_id, str) or not thread_id.strip():
        return None
    matches, generation = [], []
    for index, item in enumerate(iterations):
        settings = item.get("settings") if isinstance(item.get("settings"), dict) else {}
        if settings.get("session_id_after") == thread_id:
            matches.append(index)
        name = str(settings.get("stage", "")) + " " + str(item.get("iteration_id", ""))
        if not re.search(r"teaching|教学|verif|review|核验|终审", name, re.I):
            generation.append(index)
    if len(matches) != 1 or not generation or matches[0] != generation[0]:
        return None
    settings = iterations[matches[0]].get("settings") or {}
    if settings.get("iteration") not in (None, 0, "0"):
        return None
    return matches[0]


def _recovery_conflicts(part, restored):
    for key in ("input_tokens", "output_tokens", "cached_input_tokens", "total_tokens", "seconds"):
        old, new = part.get(key), restored.get(key)
        if type(old) in (int, float) and type(new) in (int, float) and not math.isclose(old, new, rel_tol=1e-9, abs_tol=1e-6):
            return True
    return (_known_model(part.get("actual_model")) and _known_model(restored.get("actual_model"))
            and part["actual_model"] != restored["actual_model"])


def _role_reading(record):
    """Readable facts from role metadata; no inferred unobserved system total."""
    roles = record.get("evaluation", {}).get("roles", [])
    if roles:
        rows = []
        for role in roles:
            estimated = role.get("estimated") is True
            values = [role.get(key) for key in ("input_tokens", "output_tokens", "cached_input_tokens", "total_tokens")]
            token_text = [_count(value) for value in values]
            if estimated:
                estimates = role.get("estimated_usage", {})
                for index, key in enumerate(("prompt_tokens", "completion_tokens", "cache_read_input_tokens", "total_tokens")):
                    value = estimates.get(key, values[index])
                    token_text[index] = "估计 " + _count(value) if _count(value) != "未记录" else "未记录"
            calls = role.get("calls")
            call_text = _count(calls) + " 次阶段调用" if _count(calls) != "未记录" else "未记录"
            if _count(role.get("native_ledger_attempted_calls")) != "未记录":
                call_text = _count(role["native_ledger_attempted_calls"]) + " 条原生尝试账本"
            role_name = role.get("role") or role.get("role_id") or "未记录角色"
            source = role.get("actual_model_source")
            if role.get("actual_model") in (None, "", "未记录"):
                model_note = "未取得实际响应型号；配置型号不能代替实际型号。"
            elif source and source != "未记录":
                model_note = str(source)
            else:
                model_note = "以已保存的实际运行元数据为准。"
            rows.append({"role": role_name, "configured_model": role.get("configured_model"),
                         "actual_model": role.get("actual_model"), "provider": role.get("provider"),
                         "reasoning": role.get("reasoning_effort"), "seconds": _seconds(role.get("duration_seconds")),
                         "tokens": token_text, "calls": call_text, "model_note": model_note,
                         "scope": role.get("usage_scope") or role.get("call_count_semantics") or "只覆盖本行已保存的阶段／尝试，缺失值未估算。"})
        return rows
    # Historical runners may only have per-iteration metadata. Use the same
    # normalizer as the overview, not created_at/updated_at or prose guesses.
    rows = []
    iterations = record.get("iterations", [])
    recovery = record.get("runtime_observations")
    recovery = recovery if isinstance(recovery, dict) else {}
    target = _recovery_target(iterations, recovery) if recovery else None
    restored = runtime_summary({"model": "未记录", "iterations": [], "evaluation": {},
                                "runtime_observations": recovery}) if recovery else None
    used_recovery = False
    conflict = False
    for index, item in enumerate(iterations):
        settings = item.get("settings") if isinstance(item.get("settings"), dict) else {}
        part = _legacy_role_part(item)
        note = part["model_source"]
        scope = part["seconds_scope"] + "；" + part["tokens_scope"]
        if index == target:
            conflict = _recovery_conflicts(part, restored)
            if not conflict:
                for key in ("input_tokens", "output_tokens", "cached_input_tokens", "total_tokens", "seconds"):
                    if part.get(key) is None:
                        part[key] = restored.get(key)
                if not _known_model(part.get("actual_model")):
                    part["actual_model"] = restored["actual_model"]
                if part.get("calls") is None:
                    part["calls_label"] = restored["calls_label"]
                note += " 已校验归档的thread_id与本首轮生成session_id_after唯一匹配；只补本行缺失字段，不用于教学或后续轮。"
                scope = restored["seconds_scope"] + "；" + restored["tokens_scope"]
                used_recovery = True
        rows.append({"role": settings.get("stage") or item.get("iteration_id", "未记录角色"),
                     "configured_model": part["configured_model"], "actual_model": part["actual_model"],
                     "provider": settings.get("actual_provider") or settings.get("provider"),
                     "reasoning": settings.get("reasoning_effort"), "seconds": _seconds(part["seconds"]),
                     "tokens": [_count(part[key]) for key in ("input_tokens", "output_tokens", "cached_input_tokens", "total_tokens")],
                     "calls": part["calls_label"], "model_note": note, "scope": scope})
    if restored and not used_recovery:
        reason = "与现有轮次的型号或计量不一致" if conflict else "thread_id未能唯一匹配首轮生成session_id_after"
        rows.append({"role": "归档恢复的首轮生成（未绑定到单个保存轮次）", "configured_model": restored["configured_model"],
                     "actual_model": restored["actual_model"], "provider": None, "reasoning": None,
                     "seconds": _seconds(restored["seconds"]),
                     "tokens": [_count(restored[key]) for key in ("input_tokens", "output_tokens", "cached_input_tokens", "total_tokens")],
                     "calls": restored["calls_label"], "model_note": reason + "；恢复项独立列出，不分配给教学或后续轮，也不与其他行相加。",
                     "scope": restored["seconds_scope"] + "；" + restored["tokens_scope"]})
    return rows


def detail_markdown(record):
    design = record.get("evaluation", {})
    rows = ["# 统一评测详情", "", f"题目：{record['question_id']}；AI／工作流：{record['ai_id']}；运行：{record['run_id']}。", ""]
    rows.extend(summary_markdown(record))
    roles = _role_reading(record)
    rows.extend(["## 各角色模型", "", "配置型号与实际响应型号分开列出；没有上游响应证据时，实际型号仍显示未记录。", "",
                 "| 角色／阶段 | 配置模型 | 实际响应模型 | 提供方 | 推理设置 |", "|---|---|---|---|---|"])
    for role in roles:
        values = [role[key] for key in ("role", "configured_model", "actual_model", "provider", "reasoning")]
        rows.append("| " + " | ".join(_cell(value) for value in values) + " |")
    if not roles:
        rows.append("| 未记录角色明细 | 未记录 | 未记录 | 未记录 | 未记录 |")
    rows.extend(["", "## 各角色耗时与 Token", "",
                 "本表显示保存的各阶段／API尝试计时，不替代上方有明确范围的墙钟耗时。输入、缓存与总量保持各提供方的原字段口径；尤其DSH输入字段不自动并入缓存读取。缺失值不是零，估计值明确标估计。", "",
                 "| 角色／阶段 | 记录秒数 | 输入 Token | 输出 Token | 缓存读取 Token | 总 Token | 调用口径 |", "|---|---:|---:|---:|---:|---:|---|"])
    for role in roles:
        values = [role["role"], role["seconds"], *role["tokens"], role["calls"]]
        rows.append("| " + " | ".join(_cell(value) for value in values) + " |")
    if not roles:
        rows.append("| 未记录角色明细 | 未记录 | 未记录 | 未记录 | 未记录 | 未记录 | 未记录 |")
    rows.extend(["", "### 模型来源与计量范围", ""])
    for role in roles:
        rows.extend(["- **" + _cell(role["role"]) + "**：" + _cell(role["model_note"]) + " 计量范围：" + _cell(role["scope"])])
    if not roles:
        rows.append("尚无可读取的角色元数据；不能从回答长度推算Token，也不能从记录编辑时间推算模型耗时。")
    rows.extend(["", "## 原始结构化详情", "", "以下保留完整记录字段，用于核对上方摘要；不要求通过阅读JSON寻找基本运行信息。", ""])
    for name, value in (("任务材料与会话隔离", design.get("task_materials")), ("各角色模型、工具及实际消耗", design.get("roles")),
                        ("预算与停止规则", design.get("budget")), ("消耗字段原文（完整性见上方范围）", design.get("usage")),
                        ("共同外审与人工抽查计划", design.get("external_review_plan")), ("共同外审原始结果", record.get("external_reviews"))):
        rows.extend(["### " + name, "", "```json", json.dumps(value, ensure_ascii=False, indent=2) if value else "未记录", "```", ""])
    rows.extend(["[返回测试记录](测试记录.html) · [实验索引](../../../../实验索引.html)", ""])
    return "\n".join(rows)
