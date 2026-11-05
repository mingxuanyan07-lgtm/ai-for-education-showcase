"""UTF-8 mathematics experiment folders; offline reading pages, no model calls.

record_manifest(manifest, project_root) merges append-only iterations. Text fields
may use *_file project-relative paths. ZIPs are created only by the explicit
package command. See --help and docs/自动记录与打包.md.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import sys
import time
import uuid
import zipfile

from record_reading import render_reading
from index_dashboard import sample as metric_sample, render_dashboard, validate_test_sets, validate_grading, accuracy_text
from benchmark_evaluation import validate_evaluation, validate_reviews, merge_assessments, record_summary, detail_markdown, dimension_text, assessment_view
from runtime_metrics import runtime_summary
from runtime_observations import load_observation


POLICY_DOCS = ("README.md", "AGENTS.md", "docs/数学教学AI测试记录.md", "docs/实验计划.md", "docs/测试集指标说明.md", "docs/测试集指标说明.html", "docs/templates/统一评测记录模板.md", "docs/templates/统一评测记录模板.html", "evaluations/record-template.json", "evaluations/external-review.example.json", "evaluations/index-settings.json",
               "docs/自动记录与打包.md", "docs/templates/数学教学AI测试记录_原始模板.md")
EXTENSIONS = {".md", ".html", ".txt", ".tex", ".json", ".jsonl", ".log", ".csv", ".png", ".jpg", ".jpeg", ".svg", ".pdf"}
BLOCKED_PARTS = {".git", ".venv", ".uv-cache", ".codex", "node_modules", "__pycache__"}
DEVICE = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?\Z", re.I)
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")
RECORD_KINDS = {"math_experiment", "installation_test"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _digest(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest()


def _json(data):
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _identifier(value, label):
    if not isinstance(value, str) or not ID.fullmatch(value) or DEVICE.fullmatch(value) or value.endswith("."):
        raise ValueError(f"{label} must be a safe ASCII ID, not a Windows device name: {value!r}")
    return value


def _safe(root, value, *, exists=False):
    root = Path(root).resolve(strict=True)
    value = Path(value)
    candidate = value if value.is_absolute() else root / value
    if ".." in candidate.parts:
        raise ValueError(f"Parent traversal is forbidden: {value}")
    if not candidate.is_relative_to(root):
        raise ValueError(f"Path is outside project root: {value}")
    current = root
    for part in candidate.relative_to(root).parts:
        current /= part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise ValueError(f"Links are forbidden in experiment paths: {value}")
    resolved = candidate.resolve(strict=exists)
    if not resolved.is_relative_to(root):
        raise ValueError(f"Path resolves outside project root: {value}")
    return resolved


def _evidence(root, value):
    path = _safe(root, value, exists=True)
    parts = {part.lower() for part in path.relative_to(root).parts}
    filename = path.name.lower()
    sensitive = (filename.startswith(".env") or filename in {"auth.json", "config.toml"}
                 or any(word in filename for word in ("credential", "api_key", "apikey", "secret", "token")))
    if parts & BLOCKED_PARTS or sensitive or path.suffix.lower() not in EXTENSIONS or not path.is_file():
        raise ValueError(f"Not an allowed experiment evidence file: {value}")
    return path


def _read(root, value):
    return _evidence(root, value).read_text(encoding="utf-8-sig")


def _write(root, path, content):
    path = _safe(root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        if isinstance(content, str):
            temporary.write_text(content, encoding="utf-8", newline="\n")
        else:
            temporary.write_bytes(content)
        # Antivirus/indexing can briefly deny replacement on Windows. Retry
        # only its transient access/sharing codes, keeping the old file until
        # the same atomic replacement succeeds. Other failures remain fatal.
        for delay in (.02, .04, .08, .16, .20, None):
            try:
                os.replace(temporary, path)
                break
            except OSError as error:
                if getattr(error, "winerror", None) not in {5, 32, 33} or delay is None:
                    raise
                time.sleep(delay)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _locked(root):
    path = _safe(root, "docs/.experiment-records.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 60
    while True:
        try:
            handle = path.open("x", encoding="utf-8")
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Record lock is busy: {path}; check the owning process before removing a stale lock")
            time.sleep(0.05)
    try:
        with handle:
            handle.write(_json({"pid": os.getpid(), "time": _now()}))
            handle.flush()
            yield
    finally:
        path.unlink(missing_ok=True)


def _files(root, directory):
    directory = _safe(root, directory)
    if not directory.exists():
        return []
    return [_evidence(root, item) for item in sorted(directory.rglob("*")) if item.is_file() or item.is_symlink()]


def _zip(root, destination, files):
    destination = _safe(root, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(set(files)):
                path = _evidence(root, path)
                archive.write(path, path.relative_to(root).as_posix())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _records(root):
    records = []
    for path in _files(root, "docs/experiments"):
        if path.name == "record.json" and path.parent.parent.parent.parent.name == "experiments":
            record = json.loads(path.read_text(encoding="utf-8-sig"))
            for key in ("question_id", "ai_id", "run_id"):
                _identifier(record[key], key)
            _record_kind(record)
            _record_tags(record)
            _index_visible(record)
            if "test_set_id" in record:
                _identifier(record["test_set_id"], "test_set_id")
            expected = root / "docs/experiments" / record["question_id"] / record["ai_id"] / record["run_id"]
            if path.parent != expected:
                raise ValueError(f"Record IDs disagree with directory: {path}")
            observation,warning=load_observation(root,path,record)
            if observation is not None:
                record["runtime_observations"]=observation
            if warning:
                record["runtime_observation_warning"]=warning
            records.append((path, record))
    return records


def _verification_labels(record):
    """Return objective verdict labels without treating generation success as correctness."""
    reports = []
    top_level = record.get("verification")
    if isinstance(top_level, list):
        reports.extend(top_level)
    elif top_level not in (None, "", "未记录", []):
        reports.append(top_level)
    for item in record.get("iterations", []):
        value = item.get("verification")
        if isinstance(value, list):
            reports.extend(value)
        elif value not in (None, "", "未记录", []):
            reports.append(value)
    labels = []
    for report in reports:
        if isinstance(report, dict):
            label = report.get("verdict") or report.get("status")
            if not label and report.get("timed_out"):
                label = "timeout"
        else:
            label = str(report).strip()
        label = label or "未记录"
        if label not in labels:
            labels.append(label)
    return labels or ["未记录"]


def _human_review_status(root, record_dir):
    """Classify only whether a review file has substantive text; never score its content."""
    path = _safe(root, Path(record_dir) / "人工评价.md")
    if not path.is_file():
        return "未记录"
    text = path.read_text(encoding="utf-8-sig").strip()
    text = re.sub(r"^#\s*人工评价\s*", "", text).strip()
    placeholder = "待本人填写：首次感受、各轮新评价与两轮对比。"
    return "待填写" if not text or text == placeholder or text == "待本人填写" else "已填写"


def _record_metrics(root, path, record):
    """Derived index metrics. They are deliberately not written back to record.json."""
    iterations = record.get("iterations", [])
    prompt_revisions = sum(_prompt_iteration(item) is not None for item in iterations)
    answered = [item for item in iterations if isinstance(item.get("answer"), str)
                and item["answer"].strip() not in {"", "未记录"}]
    teaching_rounds = sum(_is_teaching(item) for item in answered)
    answers = len(answered)
    human_review = _human_review_status(root, path.parent)
    for name in record.get("human_review_supplements", []):
        supplement = _safe(root, path.parent / name)
        if not supplement.is_relative_to(path.parent):
            raise ValueError("Human review supplement must stay in its experiment directory")
        if supplement.is_file() and supplement.read_text(encoding="utf-8-sig").strip():
            human_review = "已填写"
    return {
        "verification": _verification_labels(record),
        "rounds": answers,
        "prompt_revisions": prompt_revisions,
        "teaching_rounds": teaching_rounds,
        "answers": answers,
        "human_review": human_review,
    }


def _test_sets(root):
    path = _safe(root, "evaluations/index-settings.json")
    if not path.is_file():
        return []
    return validate_test_sets(json.loads(path.read_text(encoding="utf-8-sig")), _identifier)


def _math_question_labels(root, records, test_sets):
    """Stable reader-facing labels; raw question_id remains the record identity."""
    ordered = []
    for test_set in test_sets:
        for question_id in test_set.get("question_ids", []):
            if question_id.startswith("math_test_") and question_id not in ordered:
                ordered.append(question_id)
    for _, record, _ in records:
        question_id = record["question_id"]
        if question_id.startswith("math_test_") and question_id not in ordered:
            ordered.append(question_id)
    return {question_id: f"MATH{number}" for number, question_id in enumerate(ordered, 1)}


def _math_question_topics(root, records, labels):
    """Use saved tags where available, with a small ID-derived fallback for old records."""
    topics = {}
    for _, record, _ in records:
        question_id = record["question_id"]
        if question_id not in labels or question_id in topics:
            continue
        tags = [tag for tag in _record_tags(record) if tag != "MATH"]
        if tags:
            topics[question_id] = "、".join(tags)
    fallback = {
        "algebra": "代数", "counting_and_probability": "计数与概率", "geometry": "几何",
        "intermediate_algebra": "中间代数", "number_theory": "数论", "prealgebra": "预备代数",
        "precalculus": "预备微积分",
    }
    for question_id in labels:
        if question_id in topics:
            continue
        stem = question_id.removeprefix("math_test_")
        topics[question_id] = next((label for key, label in fallback.items() if stem.startswith(key)), "未分类")
    return topics


def _update_index(root, records=None):
    records = _records(root) if records is None else records
    visible = [(path, record, _record_metrics(root, path, record))
               for path, record in records
               if _record_kind(record) != "installation_test" and _index_visible(record)]
    visible.sort(key=lambda item: (item[1]["question_id"], item[1]["ai_id"], item[1]["run_id"]))
    test_sets = _test_sets(root)
    question_labels = _math_question_labels(root, visible, test_sets)
    question_topics = _math_question_topics(root, visible, question_labels)
    summary = {
        "样本数": len(visible),
        "运行完成": sum(record.get("status") == "completed" for _, record, _ in visible),
        "失败或中断": sum(record.get("status") not in {"completed", "reused", "imported"} for _, record, _ in visible),
        "流程证据：记录含内部 correct": sum("correct" in metrics["verification"] for _, _, metrics in visible),
        "流程证据：记录含内部 wrong": sum("wrong" in metrics["verification"] for _, _, metrics in visible),
        "未核验或未记录": sum(bool(set(metrics["verification"]) & {"not_run", "未记录"})
                               for _, _, metrics in visible),
        "含提示词迭代": sum(metrics["prompt_revisions"] > 0 for _, _, metrics in visible),
        "人工评价已填写": sum(metrics["human_review"] == "已填写" for _, _, metrics in visible),
    }
    rows = ["# 数学教学 AI 实验索引", "", "自动生成；仅列数学实验。点击“打开”查看已排版的测试记录，点击“原文”查看 Markdown 源文件，点击“评价”直达该次人工评价；补充评价另列链接。无需解压。", "",
            "交互筛选、指标选项与模型汇总见 [排版版索引](实验索引.html)；[指标口径与测试集配置](测试集指标说明.md)。", "",
            "## 先看这里", "", "题目显示为 MATH1、MATH2 等短编号；原始 question_id 仍保存在记录、详情和导出数据中。A=Rethlas 证明流程，B=DeepTutor 原生教学流程，C=Rethlas 证明＋DeepTutor 教学组合。", "",
            "### MATH 题目对照", "", "| 显示编号 | 题型 |", "|---|---|"]
    rows.extend(f"| {question_labels[question_id]} | {question_topics.get(question_id, '未分类')} |"
                for question_id in question_labels)
    rows.extend(["", "### 测试批次", "", "| 批次 | 说明 |", "|---|---|"])
    rows.extend(f"| `{test_set['test_set_id']}` | {test_set['name']} |" for test_set in test_sets)
    rows.extend(["", "### 状态图例", "", "`completed` 已完成；`failed` 失败；`timeout` 超时；`unverified` 尚未完成终审；`pending` 等待外审或人工评价。", "",
            "## 主指标：共同外部审阅准确率", "", accuracy_text([assessment_view(record) for _, record, _ in visible], "external_verdict"), "",
            "准确率只取绑定最终输出的共同外审。内部 correct、旧 grading、流程终审不自动转为外审评分；待外审样本单列。", "",
            "## 现有记录概况", "",
            "| 指标 | 数量 |", "|---|---:|"])
    rows.extend(f"| {name} | {value} |" for name, value in summary.items())
    rows.extend(["", "指标说明：核验来自记录中的实际独立核验字段；轮次为已保存的实际回答轮数；提示迭代只统计明确标记的 prompt_iteration；教学稿为实际教学阶段轮数；人工评价只表示评价文件是否含用户文字，不表示质量分数。", "",
                 "| 题目 | 标签 | AI／工作流 | 测试编号 | 状态 | 模型 | 已观测耗时 | 已观测Token | 核验 | 轮次 | 提示迭代 | 教学稿 | 评价 | 记录 | 人工评价 |", "|---|---|---|---|---|---|---|---|---|---:|---:|---:|---|---|---|"])
    samples = []
    for path, record, metrics in visible:
        runtime=runtime_summary(record)
        values = [question_labels.get(record["question_id"], record["question_id"]), "、".join(_record_tags(record)) or "未分类",
                  *[record[k] for k in ("ai_id", "run_id", "status")],runtime["model"],runtime["seconds_label"],runtime["tokens_label"],
                  "、".join(metrics["verification"]), metrics["rounds"], metrics["prompt_revisions"],
                  metrics["teaching_rounds"], metrics["human_review"]]
        values = [str(value).replace("|", "\\|").replace("\n", " ") for value in values]
        link = (path.parent / "测试记录.html").relative_to(root / "docs").as_posix()
        source_link = (path.parent / "测试记录.md").relative_to(root / "docs").as_posix()
        review_link = (path.parent / "人工评价.md").relative_to(root / "docs").as_posix()
        review_links = [f"[评价]({review_link})"]
        supplement_links = []
        for number, name in enumerate(record.get("human_review_supplements", []), 1):
            supplement = _safe(root, path.parent / name)
            if not supplement.is_relative_to(path.parent):
                raise ValueError("Human review supplement must stay in its experiment directory")
            supplement_link = supplement.relative_to(root / "docs").as_posix()
            supplement_links.append(supplement_link)
            review_links.append(f"[补充{number}]({supplement_link})")
        rows.append("| " + " | ".join(values + [f"[打开]({link}) · [原文]({source_link})", " ".join(review_links)]) + " |")
        samples.append(metric_sample(record, metrics, {"record_link": link, "source_link": source_link,
                                                       "review_link": review_link, "supplement_links": supplement_links}))
    rows.extend(["", "## 五个评价维度", "", "| 题目／运行 | 数学正确性（外审） | 教学质量 | 互动能力 | 运行可靠性 | 成本与效率 |", "|---|---|---|---|---|---|"])
    for row in samples:
        values = [row["question_id"] + " / " + row["run_id"], row["external_verdict"],
                  dimension_text(row["external_dimensions"].get("teaching_quality", {})),
                  dimension_text(row["external_dimensions"].get("interaction", {})), row["status"],
                  row["runtime"]["seconds_label"]+"；Token "+row["runtime"]["tokens_label"]]
        rows.append("| " + " | ".join(str(v).replace("|", "\\|").replace("\n", " ") for v in values) + " |")
    index = root / "docs/实验索引.md"
    for name in ("docs/测试集指标说明.md", "docs/templates/统一评测记录模板.md"):
        guide = _safe(root, name)
        if guide.is_file():
            _write(root, guide.with_suffix(".html"), render_reading(guide.read_text(encoding="utf-8-sig"), guide.stem))
    _write(root, index, "\n".join(rows) + "\n")
    for sample_row in samples:
        sample_row["question_label"] = question_labels.get(sample_row["question_id"], sample_row["question_id"])
    reading, _ = render_dashboard(render_reading("\n".join(rows) + "\n", "数学教学 AI 实验索引"), samples, test_sets,
                                  question_labels, question_topics)
    _write(root, index.with_suffix(".html"), reading)
    return index


def _index_visible(record):
    """Hide only an explicitly selected record; never deduplicate by question ID."""
    visible = record.get("index_visible", True)
    if not isinstance(visible, bool):
        raise ValueError("index_visible must be a boolean")
    if "index_visibility_reason" in record and not isinstance(record["index_visibility_reason"], str):
        raise ValueError("index_visibility_reason must be text")
    return visible


def _record_tags(record):
    """Normalize classification labels without inferring them from model outputs."""
    tags = record.get("tags", [])
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise ValueError("tags must be a list of strings")
    normalized = []
    for tag in tags:
        tag = tag.strip()
        if re.search(r"[\x00-\x1f\x7f]", tag):
            raise ValueError("tags cannot contain control characters")
        if tag and tag not in normalized:
            normalized.append(tag)
    return normalized


def _record_kind(record):
    """Classify for display without rewriting historical record.json files."""
    kind = record.get("record_kind")
    if "record_kind" in record:
        if not isinstance(kind, str) or kind not in RECORD_KINDS:
            raise ValueError("record_kind must be math_experiment or installation_test")
        return kind
    # Only this documented installation fixture is recognized retrospectively.
    smoke = "Rethlas/agents/generation/data/windows_smoke.md"
    if any(isinstance(item.get("source"), str) and item["source"].replace("\\", "/") == smoke
           for item in record.get("artifacts", [])):
        return "installation_test"
    if any(isinstance(item.get("settings"), dict) and isinstance(item["settings"].get("problem_file"), str) and
           item["settings"]["problem_file"].replace("\\", "/") == "data/windows_smoke.md"
           for item in record.get("iterations", [])):
        return "installation_test"
    return "math_experiment"


def _refresh_records(root):
    records = _records(root)
    for path, record in records:
        markdown = _markdown(record, path.parent)
        reading = render_reading(markdown, f"题目 {record['question_id']}｜提示词与解答")
        _write(root, path.with_name("测试记录.md"), markdown)
        _write(root, path.with_name("测试记录.html"), reading)
        details = detail_markdown(record)
        _write(root, path.with_name("评测详情.md"), details)
        _write(root, path.with_name("评测详情.html"), render_reading(details, "统一评测详情"))
    return records, _update_index(root, records)


def refresh_records(project_root: Path) -> dict:
    """Regenerate readable records and index without touching raw data or ZIPs."""
    root = Path(project_root).resolve(strict=True)
    with _locked(root):
        records, index = _refresh_records(root)
    return {"record_count": len(records), "index_path": str(index), "index_reading_path": str(index.with_suffix(".html"))}


def package_records(project_root: Path) -> dict:
    """Explicitly export records and policy documents using ASCII ZIP names."""
    root = Path(project_root).resolve(strict=True)
    with _locked(root):
        records, index = _refresh_records(root)
        policy = [_evidence(root, name) for name in POLICY_DOCS if _safe(root, name).is_file()]
        total = root / "exports/ai_math_records.zip"
        _zip(root, total, _files(root, "docs/experiments") + policy + [index, index.with_suffix(".html")])
        singles = []
        for path, record in records:
            filename = "__".join(record[k] for k in ("question_id", "ai_id", "run_id")) + ".zip"
            single = root / "exports" / filename
            _zip(root, single, _files(root, path.parent) + policy)
            singles.append(str(single))
    return {"record_count": len(records), "index_path": str(index),
            "zip_paths": singles, "total_zip_path": str(total)}


def _display(value):
    if value in (None, "", []):
        return "未记录"
    return _json(value).strip() if isinstance(value, (dict, list)) else str(value)


def _is_teaching(item):
    settings = item.get("settings")
    stage = settings.get("stage") if isinstance(settings, dict) else None
    return (stage in {"teaching", "post_verification_teaching", "核验后的外层教学整理"}
            or bool(re.match(r"teaching(?:[-_]|$)", item.get("iteration_id", ""))))


def _prompt_iteration(item):
    """Only an explicit integer marks a user-authorized prompt revision."""
    settings = item.get("settings")
    number = settings.get("prompt_iteration") if isinstance(settings, dict) else None
    return number if type(number) is int and number >= 2 else None


def _present(value):
    return value not in (None, "", "未记录", [])


def _input_snapshot(record, item, run_dir=None):
    """Link to the immutable input for this stage, never its mutable source."""
    source = item.get("referenced_task_prompt_file") or item.get("prompt_file")
    if not source or run_dir is None:
        return None
    root = run_dir.parents[4]
    source = str(source).replace("\\", "/")
    prefix = root.as_posix().rstrip("/") + "/"
    if source.lower().startswith(prefix.lower()):
        source = source[len(prefix):]
    candidates = [entry for entry in record.get("artifacts", [])
                  if entry.get("source", "").replace("\\", "/") == source]
    if not candidates:
        return None
    # A direct prompt's saved text identifies its snapshot if a path was reused.
    expected = _digest(item.get("prompt", ""))
    entry = next((entry for entry in candidates if entry.get("sha256") == expected), candidates[0])
    path = _safe(root, run_dir / entry["snapshot"], exists=True)
    if not path.is_relative_to(run_dir):
        raise ValueError("Input snapshot must stay in its experiment directory")
    text = _read(root, path)
    return path.relative_to(run_dir).as_posix(), text


def _prompt_excerpt(text):
    """Select original requirement text at explicit boundaries; never summarize."""
    selected = text
    opening, closing = "【本轮教学要求开始】", "【本轮教学要求结束】"
    if opening in text and closing in text:
        start = text.index(opening) + len(opening)
        end = text.find(closing, start)
        if end >= start:
            selected = text[start:end]
    else:
        marker = re.search(r"(?m)^请按以下(?:要求|[一二三四五六七八九十0-9]+部分)[^\n]*", text)
        if marker:
            selected = text[marker.start():]
    if selected == text:
        return text, False
    paragraphs = re.split(r"\n\s*\n", selected.strip())
    administrative = re.compile(
        r"^(?:以下是未来发送时的教学要求，本次保存不触发生成。|"
        r"执行与保存要求|未来实际执行时|执行与记录说明|沿用 question_id=|"
        r"[0-9]+[.、]\s*读者正文只包含教学内容。真实执行时)"
    )
    selected = "\n\n".join(paragraph for paragraph in paragraphs
                           if not administrative.match(paragraph)).strip()
    selected = re.sub(r"(?m)^[0-9]+[.、]\s*读者正文只包含教学内容。真实执行时[^\n]*\n?", "", selected).strip()
    return (selected, True) if selected else (text, False)


def _short_verification(value):
    reports = value if isinstance(value, list) else [value]
    labels = []
    for report in reports:
        if not isinstance(report, dict):
            if _present(report):
                labels.append("未完成，详见原始记录" if str(report).startswith("未完成") else "详见原始记录")
            continue
        verdict = report.get("verdict") or report.get("status") or "未记录"
        detail = report.get("verification_report")
        detail = detail if isinstance(detail, dict) else report
        defects = []
        for key, label in (("critical_errors", "关键错误"), ("gaps", "缺口")):
            raw = detail.get(key, report.get(key))
            count = len(raw) if isinstance(raw, (dict, list)) else int(_present(raw))
            if count:
                defects.append(f"{label} {count} 项")
        labels.append(str(verdict) + (f"（{'、'.join(defects)}）" if defects else ""))
    return "数学复核：" + " → ".join(labels or ["未记录"]) + "。"


def _markdown(record, run_dir=None):
    """A reading view of prompts and original answers; raw evidence stays intact."""
    iterations = record.get("iterations", [])
    teaching = [item for item in iterations if _is_teaching(item)]
    solving = [item for item in iterations if not _is_teaching(item)]
    hybrid = record["ai_id"].startswith("hybrid-rethlas-deeptutor-")
    rethlas = hybrid or record.get("framework") == "Rethlas" or record["ai_id"].startswith("rethlas-")
    lines = [f"# 题目 {record['question_id']}｜提示词与解答", "",
             f"标签：{'、'.join(_record_tags(record)) or '未分类'}", ""]
    lines.extend(record_summary(record))
    if not _index_visible(record):
        lines.extend(["索引显示：本记录已从日常索引移除，原始实验材料仍保留。", ""])
        if record.get("index_visibility_reason"):
            lines.extend([f"说明：{record['index_visibility_reason']}", ""])
    if record.get("status") not in ("completed", "reused", "imported"):
        lines.extend([f"运行状态：{_display(record.get('status'))}。", ""])
    if _present(record.get("error")):
        lines.extend(["运行错误：详见原始记录。", ""])
    for item in solving:
        settings = item.get("settings") or {}
        if not isinstance(settings, dict):
            continue
        if settings.get("timed_out") or _present(settings.get("error")) or settings.get("exit_code") not in (None, 0):
            reason = settings.get("error") or ("超时" if settings.get("timed_out") else f"退出码 {settings['exit_code']}")
            lines.extend([f"求解轮次 {settings.get('iteration', item['iteration_id'])}：{str(reason).splitlines()[0]}", ""])
    first_teaching_has_user_prompt = bool(teaching and _prompt_iteration(teaching[0]) is None)
    if not first_teaching_has_user_prompt:
        lines.extend(["### 用户原始提示词（完整提问）", "", _display(record.get("user_prompt")), ""])
    if not teaching and not _present(record.get("user_prompt")):
        lines.extend(["### 题面", "", record["question"], ""])
    if rethlas and teaching and _present(record.get("verification")):
        lines.extend(["原证明的" + _short_verification(record["verification"]), ""])

    if not rethlas:
        for number, item in enumerate(solving, 1):
            heading = "### 首轮解答" if number == 1 else f"### Prompt 迭代（第 {number} 轮）"
            lines.extend([heading, "", "#### 提示词（实际输入）", "", _display(item.get("prompt")), "",
                          "#### 解答", "", _display(item.get("answer")), ""])
            if _present(item.get("verification")):
                lines.extend([_short_verification(item["verification"]), ""])
        if not solving and not teaching and _present(record.get("final_answer")):
            lines.extend(["#### 解答", "", record["final_answer"], ""])
        if not teaching and _present(record.get("verification")):
            lines.extend([_short_verification(record["verification"]), ""])
    elif not teaching:
        lines.extend(["### 题面", "", record["question"], "",
                      "#### 已保存的证明草稿（非最终教学稿）" if hybrid else "#### 解答", ""])
        if hybrid:
            lines.extend(["尚未交付最终教学稿。各角色的完整输入、原始核验JSON和失败材料保留在[原始记录](record.json)及[评测详情](评测详情.html)，内部核验不是用户的新一轮Prompt迭代。", ""])
        if _present(record.get("final_answer")):
            lines.extend([record["final_answer"], ""])
        else:
            lines.extend(["尚未保存正式解答。", ""])
        lines.extend([_short_verification(record.get("verification")), ""])

    for index, item in enumerate(teaching):
        number = _prompt_iteration(item)
        heading = "### 核验后的中文教学整理" if number is None else f"### Prompt 迭代（第 {number} 轮）"
        lines.extend([heading, ""])
        snapshot = _input_snapshot(record, item, run_dir)
        input_link = f"[完整教学输入]({snapshot[0]})" if snapshot else "[完整输入与来源](record.json)"
        if index == 0 and number is None:
            lines.extend(["#### 提示词（用户原文）", "", _display(record.get("user_prompt")), "", input_link, ""])
        else:
            prompt = snapshot[1] if snapshot else _display(item.get("prompt"))
            excerpt, shortened = _prompt_excerpt(prompt)
            label = "教学要求摘录" if shortened else "实际输入"
            lines.extend([f"#### 提示词（{label}）", "", excerpt, "", input_link, ""])
        lines.extend(["#### 解答", "", _display(item.get("answer")), "", _short_verification(item.get("verification")), ""])

    links = ["[人工评价](人工评价.md)"]
    links.extend(f"[补充评价{number}]({name})" for number, name in enumerate(record.get("human_review_supplements", []), 1))
    links.append("[原始记录与核验详情](record.json)")
    lines.extend([" · ".join(links), ""])
    return "\n".join(lines)

def record_manifest(manifest: dict, project_root: Path) -> dict:
    """Record actual material; same IDs append iterations and preserve human review."""
    root = Path(project_root).resolve(strict=True)
    manifest = dict(manifest)
    if "evaluation" in manifest:
        validate_evaluation(manifest["evaluation"])
    if "external_reviews" in manifest:
        validate_reviews(manifest["external_reviews"])
    _test_sets(root)
    if "test_set_id" in manifest:
        _identifier(manifest["test_set_id"], "test_set_id")
    if "record_kind" in manifest:
        _record_kind(manifest)
    if "tags" in manifest:
        manifest["tags"] = _record_tags(manifest)
    _index_visible(manifest)
    sources = list(manifest.get("evidence_files", []))
    def collect_files(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if (key == "file" or key.endswith("_file")) and isinstance(child, str) and child.strip() not in ("", "未记录"):
                    sources.append(child)
                else:
                    collect_files(child)
        elif isinstance(value, list):
            for child in value:
                collect_files(child)
    collect_files(manifest.get("evaluation", {}))
    collect_files(manifest.get("external_reviews", []))
    for key in ("question", "requirements", "user_prompt", "final_answer"):
        if key + "_file" in manifest:
            sources.append(manifest[key + "_file"])
            manifest[key] = _read(root, manifest[key + "_file"])
    incoming = manifest.get("iterations", [])
    if not isinstance(incoming, list):
        raise ValueError("iterations must be a list")
    iterations = []
    for raw in incoming:
        item = dict(raw)
        if "grading" in item:
            validate_grading(item["grading"])
        for key in ("prompt", "answer", "user_prompt"):
            if key + "_file" in item:
                sources.append(item[key + "_file"])
                item[key] = _read(root, item[key + "_file"])
            if key in item and not isinstance(item[key], str):
                raise ValueError(f"iteration {key} must be text")
        item["iteration_id"] = _identifier(item.get("iteration_id") or "i_" + _digest(_json(item))[:16], "iteration_id")
        iterations.append(item)
    evidence = [_evidence(root, value) for value in sources]
    question_id = _identifier(manifest["question_id"], "question_id")
    ai_id = _identifier(manifest["ai_id"], "ai_id")
    run_id = _identifier(manifest.get("run_id") or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:8], "run_id")
    run_dir = _safe(root, root / "docs/experiments" / question_id / ai_id / run_id)
    with _locked(root):
        record_path = _safe(root, run_dir / "record.json")
        record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() else {
            "format_version": 1, "question_id": question_id, "ai_id": ai_id, "run_id": run_id,
            "created_at": _now(), "iterations": [], "artifacts": [], "updates": []}
        if "record_kind" in manifest:
            record["record_kind"] = manifest["record_kind"]
        if "tags" in manifest:
            record["tags"] = manifest["tags"]
        for key in ("index_visible", "index_visibility_reason", "test_set_id"):
            if key in manifest:
                record[key] = manifest[key]
        for key in ("question", "requirements", "user_prompt", "model", "framework", "name", "ai_name", "final_answer", "status", "error", "verification", "tool_checks", "task_review"):
            if key in manifest:
                value = manifest[key]
                if key not in {"verification", "tool_checks", "error"} and not isinstance(value, str):
                    raise ValueError(f"{key} must be text")
                if key == "question" and record.get(key) not in (None, "未记录", value):
                    raise ValueError("Same record IDs cannot replace a different original question")
                if key == "user_prompt" and record.get(key) not in (None, "", "未记录", value):
                    raise ValueError("Refusing to overwrite original user_prompt; put the new request in a new iteration")
                record[key] = value
            record.setdefault(key, "未记录")
        by_id = {item["iteration_id"]: item for item in record["iterations"]}
        for item in iterations:
            previous = by_id.get(item["iteration_id"])
            if previous:
                for key in ("prompt", "answer", "user_prompt"):
                    if key in item and previous.get(key) not in (None, "", "未记录", item[key]):
                        raise ValueError(f"Refusing to overwrite original {key}; use a new iteration_id")
                previous.update(item)
            else:
                item.setdefault("prompt", "未记录")
                item.setdefault("answer", "未记录")
                record["iterations"].append(item)
                by_id[item["iteration_id"]] = item
        merge_assessments(record, manifest)
        known = {(item["source"], item["sha256"]) for item in record["artifacts"]}
        for path in evidence:
            content = path.read_bytes()
            digest = _digest(content)
            source = path.relative_to(root).as_posix()
            if (source, digest) in known:
                continue
            target = run_dir / "evidence" / Path(source).parent / (path.stem + "__" + digest[:16] + path.suffix)
            _write(root, target, content)
            record["artifacts"].append({"source": source, "snapshot": target.relative_to(run_dir).as_posix(), "sha256": digest})
            known.add((source, digest))
        review = _safe(root, run_dir / "人工评价.md")
        if not review.exists():
            _write(root, review, "# 人工评价\n\n" + (str(manifest.get("human_review") or "待本人填写：首次感受、各轮新评价与两轮对比。") + "\n"))
        elif manifest.get("human_review"):
            # Preserve hand edits and new explicitly supplied comments separately.
            comment = str(manifest["human_review"])
            supplement = "人工评价补充_" + _digest(comment)[:12] + ".md"
            _write(root, run_dir / supplement, comment + "\n")
            record.setdefault("human_review_supplements", [])
            if supplement not in record["human_review_supplements"]:
                record["human_review_supplements"].append(supplement)
        record["updated_at"] = _now()
        record["updates"].append({"time": record["updated_at"], "status": record["status"], "error": record.get("error")})
        markdown = _markdown(record, run_dir)
        reading = render_reading(markdown, f"题目 {record['question_id']}｜提示词与解答")
        _write(root, record_path, _json(record))
        _write(root, run_dir / "测试记录.md", markdown)
        _write(root, run_dir / "测试记录.html", reading)
        details = detail_markdown(record)
        _write(root, run_dir / "评测详情.md", details)
        _write(root, run_dir / "评测详情.html", render_reading(details, "统一评测详情"))
        index = _update_index(root)
    return {"experiment_id": f"{question_id}/{ai_id}/{run_id}", "question_id": question_id, "ai_id": ai_id,
            "run_id": run_id, "record_path": str(run_dir / "测试记录.md"),
            "reading_path": str(run_dir / "测试记录.html"),
            "record_dir": str(run_dir), "index_path": str(index), "index_reading_path": str(index.with_suffix(".html"))}


def _event_data(path):
    messages, checks = [], []
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        item = event.get("item", {})
        if event.get("type") != "item.completed" or not isinstance(item, dict):
            continue
        if item.get("type") == "agent_message":
            messages.append(str(item.get("text", "")))
        elif item.get("type") == "mcp_tool_call":
            checks.append({key: item.get(key) for key in ("server", "tool", "arguments", "result", "error", "status")})
    return "\n\n".join(messages) or "未记录（没有已保存的完成回答）", checks


def record_rethlas_run(project_root: Path, problem_file: str, status: str, error: str | None = None) -> dict:
    """Collect only this problem's real evidence; stable IDs make resume append-safe."""
    root = Path(project_root).resolve(strict=True)
    windows = PureWindowsPath(problem_file)
    parts = problem_file.replace("\\", "/").split("/")
    if windows.drive or windows.root or any(part in {"", ".", ".."} for part in parts) or len(parts) < 2 or parts[0] != "data" or not parts[-1].endswith(".md"):
        raise ValueError("problem_file must be a relative data/*.md path")
    gen = _safe(root, "Rethlas/agents/generation", exists=True)
    problem = _evidence(root, gen.joinpath(*parts))
    statement = problem.read_text(encoding="utf-8-sig")
    problem_id = Path(*parts[1:]).with_suffix("").as_posix()
    sidecar_path = problem.with_suffix(".experiment.json")
    sidecar = json.loads(_read(root, sidecar_path)) if sidecar_path.is_file() else {}
    artifacts = [problem]
    if sidecar_path.is_file():
        artifacts.append(sidecar_path)
    iterations, models = [], []
    logs = _safe(root, gen / "logs" / problem_id / "iter")
    run_metadata = list(logs.glob("*.run.json")) if logs.is_dir() else []
    if not run_metadata:
        run_metadata = [path for path in (gen / "logs").rglob("*.run.json")
                        if json.loads(_read(root, path)).get("problem_file") == problem_file] if (gen / "logs").is_dir() else []
    for path in sorted(run_metadata, key=lambda path: json.loads(_read(root, path)).get("iteration", -1)):
        metadata = json.loads(_read(root, path))
        if metadata.get("problem_file") != problem_file:
            continue
        base = str(path)[:-len(".run.json")]
        prompt_path, events_path = Path(base + ".prompt.txt"), Path(base + ".jsonl")
        answer, checks = _event_data(_evidence(root, events_path)) if events_path.is_file() else ("未记录", [])
        prompt = _read(root, prompt_path) if prompt_path.is_file() else "未记录"
        for file in (path, prompt_path, events_path, Path(base + ".stderr.log")):
            if file.is_file():
                artifacts.append(file)
        model = metadata.get("model") or "未记录"
        models.append(model)
        iterations.append({"iteration_id": f"rethlas_{metadata.get('iteration', len(iterations))}_" + _digest(prompt + answer)[:12],
                           "prompt": prompt, "answer": answer, "settings": metadata, "tool_checks": checks,
                           "change_reason": "按 runner 原始轮次继续；具体反馈见工具事件与核验材料。" if iterations else "首次已记录轮次"})
    verification = []
    memory_file = _safe(root, gen / "memory" / problem_id / "verification_reports.jsonl")
    for directory in (gen / "memory" / problem_id, gen / "results" / problem_id, problem.with_suffix(".refs")):
        artifacts.extend(_files(root, directory))
    if memory_file.is_file():
        for line in _read(root, memory_file).splitlines():
            try:
                record = json.loads(line).get("record", {})
            except ValueError:
                continue
            if record:
                verification.append(record)
                run_id = record.get("run_id")
                digest = _digest(str(record.get("statement", "")))[:12]
                verification_root = _safe(root, "Rethlas/agents/verification/results")
                # Match content hash, then require equality with this stored report.
                candidates = [verification_root / run_id] if run_id and ID.fullmatch(run_id) else list(verification_root.glob("*_*" + digest + "*"))
                for candidate in candidates:
                    report = candidate / "verification.json"
                    if report.is_file():
                        payload = json.loads(_read(root, report))
                        if payload.get("verdict") == record.get("verdict") and payload.get("verification_report") == record.get("verification_report"):
                            artifacts.extend(_files(root, candidate))
    for role in ("generation", "verification"):
        role_root = root / "Rethlas/agents" / role
        instruction = role_root / "AGENTS.md"
        if instruction.is_file():
            artifacts.append(instruction)
        skills = role_root / ".agents/skills"
        if skills.is_dir():
            artifacts.extend(path for path in skills.rglob("SKILL.md"))
    proof = gen / "results" / problem_id / "blueprint_verified.md"
    model = models[-1] if models else "未记录"
    first_model = next((value for value in models if value != "未记录"), "未记录")
    safe_model = re.sub(r"[^A-Za-z0-9_.-]", "-", first_model if first_model != "未记录" else "unrecorded")[:60]
    slug = re.sub(r"[^A-Za-z0-9_.-]", "-", problem_id)[:55].strip(".-") or "problem"
    manifest = {"question_id": "q_" + _digest(" ".join(statement.split()))[:12], "ai_id": "rethlas-" + safe_model,
                "run_id": "rethlas-" + slug + "-" + _digest(problem_file)[:8], "question": statement,
                "framework": "Rethlas", "model": model, "status": status, "error": error,
                "iterations": iterations, "verification": verification, "evidence_files": [str(path) for path in artifacts]}
    for key in ("question_id", "ai_id", "run_id", "requirements", "user_prompt", "name", "ai_name", "record_kind", "tags", "test_set_id", "evaluation"):
        if key in sidecar:
            manifest[key] = sidecar[key]
    # An early failure may predate model metadata. Reuse that record's identity
    # when the same problem file resumes, rather than silently create a new run.
    if not any(key in sidecar for key in ("question_id", "ai_id", "run_id")):
        source = problem.relative_to(root).as_posix()
        prior = []
        for path in _files(root, "docs/experiments"):
            if path.name == "record.json" and path.parent.parent.parent.parent.name == "experiments":
                existing = json.loads(_read(root, path))
                if any(item.get("source") == source for item in existing.get("artifacts", [])):
                    prior.append(existing)
        if len(prior) > 1:
            raise ValueError("Several records refer to this problem file; specify the exact IDs in .experiment.json")
        if prior:
            for key in ("question_id", "ai_id", "run_id"):
                manifest[key] = prior[0][key]
    if proof.is_file():
        manifest["final_answer_file"] = str(proof)
    return record_manifest(manifest, root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    descriptions = {"record": "Save actual inputs and outputs; no ZIP is created.",
                    "collect-rethlas": "Collect a Rethlas run into a readable folder; no ZIP is created.",
                    "refresh": "Refresh readable records and index only; preserve human review and raw data.",
                    "package": "Explicitly generate ASCII-named ZIP exports for sharing."}
    for command, description in descriptions.items():
        child = sub.add_parser(command, help=description, description=description)
        child.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parent.parent)
        if command == "record":
            child.add_argument("--manifest", required=True, help="project-relative JSON file; cannot escape project root")
        elif command == "collect-rethlas":
            child.add_argument("--problem-file", required=True, help="data/*.md relative to agents/generation")
            child.add_argument("--status", default="imported", help="actual state; default imported = historical evidence")
            child.add_argument("--error")
    args = parser.parse_args(argv)
    root = args.project_root.resolve(strict=True)
    if args.command == "record":
        result = record_manifest(json.loads(_read(root, args.manifest)), root)
    elif args.command == "collect-rethlas":
        result = record_rethlas_run(root, args.problem_file, args.status, args.error)
    elif args.command == "refresh":
        result = refresh_records(root)
    else:
        result = package_records(root)
    print(_json(result), end="")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f"Recording failed: {error}", file=sys.stderr)
        sys.exit(2)
