"""Derived benchmark metadata and an offline index UI; never mutate experiment evidence."""
from __future__ import annotations

import base64
import hashlib
import json
import math
from pathlib import Path
from benchmark_evaluation import assessment_view, output_items
from runtime_metrics import runtime_summary


def validate_test_sets(value, identifier):
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("index-settings.json requires schema_version=1")
    sets = value.get("test_sets", [])
    if not isinstance(sets, list):
        raise ValueError("test_sets must be a list")
    seen = set()
    result = []
    for item in sets:
        if not isinstance(item, dict):
            raise ValueError("Each test set must be an object")
        key = identifier(item.get("test_set_id"), "test_set_id")
        if key in seen:
            raise ValueError("Duplicate test_set_id")
        seen.add(key)
        title = item.get("name", key)
        if not isinstance(title, str) or not title.strip():
            raise ValueError("Test set name must be text")
        lists = {}
        for field in ("question_ids", "ai_ids"):
            items = item.get(field, [])
            if not isinstance(items, list):
                raise ValueError(field + " must be a list")
            lists[field] = [identifier(x, field) for x in items]
            if len(set(items)) != len(items):
                raise ValueError(field + " cannot contain duplicates")
        result.append({"test_set_id": key, "name": title.strip(), **lists})
    return result


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def _verdict(item):
    """Only canonical verdicts tied to this answer; no inference from 'passed' or exit codes."""
    reports = item.get("verification")
    reports = reports if isinstance(reports, list) else [reports]
    # An unrecognized last report must not revive an earlier correct verdict.
    last = reports[-1] if reports else None
    label = last.get("verdict") if isinstance(last, dict) else None
    return label if label in {"correct", "wrong", "partial", "not_run"} else "未记录"


def validate_grading(value):
    """Validate supplied reference/human grades; never generate a grade from model verification."""
    if not isinstance(value, dict) or value.get("verdict") not in ("correct", "wrong", "partial", "not_graded"):
        raise ValueError("grading.verdict must be correct, wrong, partial or not_graded")
    if value["verdict"] == "not_graded":
        return
    if value.get("method") not in ("exact_match", "numeric_tolerance", "human_rubric"):
        raise ValueError("grading.method must be exact_match, numeric_tolerance or human_rubric")
    for key in ("reference", "evaluator"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError("grading." + key + " must identify the actual grading reference/evaluator")


def _grading(item):
    value = item.get("grading")
    try:
        validate_grading(value)
    except ValueError:
        return {"verdict": "not_graded"}
    return value


def accuracy_text(rows, key):
    if key == "external_verdict" and len({(row.get("external_protocol"), row.get("external_rubric")) for row in rows
                                          if row.get("external_protocol") or row.get("external_rubric")}) > 1:
        return "外审协议／量表混合，请筛选后比较"
    scored = [row for row in rows if row.get(key) in ("correct", "wrong", "partial", "unanswered")]
    if not scored:
        return f"待判分（已判 0/{len(rows)}）"
    correct = sum(row[key] == "correct" for row in scored)
    tenths = (correct * 2000 + len(scored)) // (2 * len(scored))
    return (f"{tenths // 10}.{tenths % 10}% ({correct}/{len(scored)})；"
            f"已判 {len(scored)}/{len(rows)}，待判 {len(rows) - len(scored)}")


def _usage(settings):
    usage = settings.get("usage")
    if not isinstance(usage, dict):
        return None
    usage = usage.get("total", usage)
    if not isinstance(usage, dict):
        return None
    total = _number(usage.get("total_tokens"))
    if total is not None:
        return total
    counts = [_number(usage.get(k)) for k in ("input_tokens", "output_tokens")]
    return sum(counts) if all(x is not None for x in counts) else None


def sample(record, metrics, links, question_labels=None):
    iterations = record.get("iterations", [])
    answers = output_items(record)
    settings = [item.get("settings") if isinstance(item.get("settings"), dict) else {}
                for item in iterations]
    duration = [_number(item.get("timing", {}).get("duration_seconds"))
                if isinstance(item.get("timing"), dict) else None for item in settings]
    tokens = [_usage(item) for item in settings]
    categories = sorted({item["evaluation_category"] for item in settings
                         if isinstance(item.get("evaluation_category"), str)})
    batches = sorted({item["batch_id"] for item in settings if isinstance(item.get("batch_id"), str)})
    # These are generation-only totals and only available when every saved iteration has data.
    # Do not infer runtime from created_at/updated_at (which may include days of human edits).
    complete = lambda values: sum(values) if values and all(x is not None for x in values) else None
    grade = _grading(answers[-1]) if answers else {"verdict": "not_graded"}
    view = assessment_view(record)
    usage = view["evaluation"].get("usage", {})
    runtime = runtime_summary(record)
    question_id = record["question_id"]
    question_label = (question_labels or {}).get(question_id, links.get("question_label", question_id))
    return {"question_id": question_id, "question_label": question_label, "ai_id": record["ai_id"], "run_id": record["run_id"],
            "test_set_id": record.get("test_set_id", ""), "tags": list(dict.fromkeys(t.strip() for t in record.get("tags", []) if t.strip())),
            "model": runtime["model"], "recorded_model": record.get("model", "未记录"),
            "actual_model": runtime.get("actual_model", "未记录"), "configured_model": runtime.get("configured_model", "未记录"),
            "model_source": runtime.get("model_source", "未记录"), "framework": record.get("framework", "未记录"),
            "status": record.get("status", "未记录"), "categories": categories, "batches": batches,
            "first_verdict": _verdict(answers[0]) if answers else "未记录",
            "final_verdict": _verdict(answers[-1]) if answers else "未记录",
            "first_iteration": answers[0]["iteration_id"] if answers else "",
            "final_iteration": answers[-1]["iteration_id"] if answers else "",
            "grade_verdict": grade["verdict"], "grade_method": grade.get("method", "未记录"),
            "grade_reference": grade.get("reference", "未记录"), "grade_evaluator": grade.get("evaluator", "未记录"),
            "generation_seconds": complete(duration), "tokens": complete(tokens),
            "system_calls": _number(usage.get("calls")), "system_tokens": _number(usage.get("total_tokens")),
            "wall_seconds": _number(usage.get("wall_seconds")),
            "runtime": runtime, "observed_seconds": runtime.get("seconds"), "observed_seconds_label": runtime.get("seconds_label"),
            "observed_seconds_scope": runtime.get("seconds_scope"), "observed_seconds_scope_id": runtime.get("seconds_scope_id"),
            "observed_tokens": runtime.get("total_tokens"), "observed_tokens_label": runtime.get("tokens_label"),
            "observed_tokens_scope": runtime.get("tokens_scope"), "observed_tokens_scope_id": runtime.get("tokens_scope_id"),
            "estimated_tokens": runtime.get("estimated_total_tokens"),
            "observed_input_tokens": runtime.get("input_tokens"), "observed_output_tokens": runtime.get("output_tokens"),
            "observed_cached_input_tokens": runtime.get("cached_input_tokens"),
            "observed_calls": runtime.get("calls"), "observed_calls_label": runtime.get("calls_label"),
            "observed_calls_scope": runtime.get("calls_scope", "未记录"), "observed_calls_scope_id": runtime.get("calls_scope_id"),
            "metering_coverage": runtime.get("coverage"), "metering_exclusions": runtime.get("exclusions", []),
            "metering_evidence_files": runtime.get("evidence_files", []),
            "details_link": links.get("record_link", "测试记录.html").replace("测试记录.html", "评测详情.html"),
            **metrics, **links, **view}


def render_dashboard(reading_html, samples, test_sets, question_labels=None, question_topics=None):
    payload = {"schema_version": 1, "samples": samples, "test_sets": test_sets,
               "question_label_map": question_labels or {}, "question_topics": question_topics or {}}
    data = json.dumps(payload, ensure_ascii=False, allow_nan=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    directory = Path(__file__).parent
    script = (directory / "index_dashboard.js").read_text(encoding="utf-8")
    style = (directory / "index_dashboard.css").read_text(encoding="utf-8")
    panel = '''<section id="dashboard" hidden aria-label="测试指标面板">
<h1>数学教学 AI 实验索引</h1>
<section id="quick-guide" class="quick-guide" aria-label="索引快速说明">
<h2>一眼看懂</h2>
<p>这里按 <strong>MATH1、MATH2、MATH3…</strong> 标记题目。同一个 MATH 编号可能有多条记录，表示同一道题分别用不同架构、不同批次或不同运行尝试测试。</p>
<div id="overview-cards" class="overview-cards"></div>
<h3>本索引包含的测试</h3><div id="batch-summary" class="batch-summary"></div>
<h3>MATH 题目编号</h3><div id="question-map" class="question-map"></div>
<p class="hint">状态说明：<strong>completed</strong> 已完成，<strong>failed</strong> 失败，<strong>timeout</strong> 超时，<strong>unverified</strong> 尚未完成终审；详细原因和原始材料仍在对应记录中。</p>
</section>
<p id="selection" class="selection" role="status" aria-live="polite"></p>
<details class="advanced-controls"><summary>筛选、指标和导出（需要时展开）</summary>
<h2>测试集与模型指标</h2>
<p class="hint">按题目清单查看覆盖率；每个独立 run 算一个样本，提示词迭代与续跑不增加样本数。</p>
<div id="filters" class="filters"></div>
<fieldset id="metric-options"><legend>显示指标（可多选）</legend></fieldset>
<div class="toolbar"><button id="reset" type="button">重置筛选与指标</button><button id="export" type="button">导出筛选样本 CSV</button><a href="测试集指标说明.html">指标口径与测试集配置</a></div>
 </details>
<details class="run-details"><summary>逐次运行详情（模型、耗时、Token）</summary>
<h3>逐次运行：模型、耗时与 Token</h3>
<p class="hint">直接展示实际已保存的计量。配置模型与返回模型分开；主阶段或已观测部分不冒充系统总量，未测终审、外审等范围逐项注明。</p>
<div id="runtime-results" class="scroll-table"></div>
<div id="cards" class="cards"></div>
<p class="hint">主指标只取共同外部审阅：正确数 / 外审已判数，显示外审覆盖与待判数。内部 correct、旧 grading、流程终审只作为过程证据。未完成外审时不发布整套测试集最终准确率。</p>
<h3>模型／工作流对比</h3><p class="hint">按 AI／工作流与记录中的模型分组。各组题目不相同时，请先选择共同测试集后比较。</p>
<div id="comparison" class="scroll-table"></div><div id="missing"></div>
<h3>五个评价维度</h3><p class="hint">数学正确性、教学质量、互动能力、运行可靠性、成本与效率分别展示，保留逐项分数、理由和证据，不合成总分。</p><div id="dimension-results" class="scroll-table"></div>
 </details>
<details><summary>内部核验、轮次与其他运行详情</summary><div id="samples" class="scroll-table"></div></details>
<details class="definitions"><summary>指标计算口径</summary>
<p>共同外部审阅准确率 = 外审 correct 数 / 外审 correct、wrong、partial、unanswered 数；partial 和无输出按未答对计入分母。外审须声明独立审阅、协议、量表与评判者，绑定最终回答轮次和 SHA256。旧核验、grading 或流程终审不会自动迁移。人工抽查有分歧时列为 disputed，等待共同复议；不同协议／量表的结果不混算准确率。</p>
<p>完成率 = completed 样本数 / 筛选样本数；超时、失败、中断、unverified 分别保留。复用／历史导入不当作 completed。</p>
<p>首稿／末轮通过率 = 对应实际回答的 correct 数 / 全部筛选样本数；明确核验比例 = correct、wrong、partial 数 / 全部样本数。未核验仍保留在分母中。仅统计回答自身的规范 verdict，不用记录级旧证明判定代替末轮教学稿核验；passed 等旧字段显示未记录。wrong→correct 不会让末轮同时算对和错。</p>
<p>覆盖率按稳定 question_id 去重 / 配置清单题数；一次失败尝试也算已覆盖，重复运行不增加覆盖。共同题目仅表示各组均有记录。首稿通过率不是 pass@k；记录中的模型核验判定不等于标准答案准确率或形式化证明。</p>
<p>平均生成耗时／Token 只使用每个已保存轮次均有有效计量的样本，并标明 n；不包含未记录的独立核验与工具用量，不估算费用。人工评价只统计有无文字，不代替教学质量评分或学习效果。</p>
<p>已观测 Token、主阶段耗时与调用数保留计量范围；缺失不按0处理，估计值不当实际用量。只有相同口径的观测才显示均值，不同口径不合并；系统总量原字段与局部已观测量分开。</p>
</details></section>'''
    hashes = ["'sha256-" + base64.b64encode(hashlib.sha256(x.encode("utf-8")).digest()).decode() + "'"
              for x in (script, data)]
    result = reading_html.replace("style-src 'unsafe-inline';", "script-src " + " ".join(hashes) + "; style-src 'unsafe-inline';")
    result = result.replace("</style>", "\n" + style + "</style>", 1)
    result = result.replace("<body><main>", "<body><main>\n" + panel + '<div id="static-index">', 1)
    result = result.replace("</main></body>", '</div><script type="application/json" id="index-data">' + data + '</script>\n<script>' + script + '</script>\n</main></body>', 1)
    return result, payload
