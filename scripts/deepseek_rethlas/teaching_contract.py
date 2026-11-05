"""Opt-in, evidence-backed teaching requirements; no mathematical verdicts.

The host checks report completeness and quotations, not whether a learner has
understood the explanation. Semantic judgments still belong to the reviewers.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json


LEGACY = "legacy"
VERSION = "knowledge-route-v1"
PROMPT_REVISION = "literal-quote-bank-v1"
QUOTE_POLICY = "literal-substring-crlf-v1"
QUOTE_BANK_MAX_ENTRIES = 18
QUOTE_BANK_MAX_CHARS = 180
CRITERIA = {
    "knowledge_inventory": "列明这份解答实际使用的必要知识，区分可选扩展，标明各自用在哪一步。",
    "prerequisite_depth": "说明每项前置知识需要掌握到什么程度，给出必要含义、公式和适用条件；不把本题待证结论当作前置。",
    "background_fidelity": "仅采用用户明确提供的已学和未学知识；未提供时标明未知，不从题目难度猜测学生水平。",
    "solution_route": "从目标和本题困难说明方法选择，给出有依赖关系、能通向结论的整体路线；不能只有步骤标题。",
    "step_alignment": "展开的关键步骤与路线对应，交代本步目的、依据、所得结果及下一步如何使用；必要条件就在使用处说明。",
    "mainline_focus": "主解答连续可读，较长的基础解释和可选扩展另放补充；必要论证有明确引用，不能为精简删去。",
}


def _text_sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def quote_candidates(teaching: str) -> list[dict]:
    """Offer bounded, deterministic, exact source spans, never synthetic evidence.

    Entries span the draft rather than only its first section. Truncating a
    long line takes an actual prefix; no wording, TeX or punctuation changes.
    Candidate existence is not a semantic judgment about any criterion.
    """
    if not isinstance(teaching, str):
        return []
    candidates = []
    offset = 0
    for line in teaching.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        quote = body.strip()
        if len(quote) >= 8 and not quote.startswith("```"):
            start = offset + body.index(quote)
            quote = quote[:QUOTE_BANK_MAX_CHARS]
            candidates.append({"start_char": start, "end_char": start + len(quote), "quote": quote})
        offset += len(line)
    if len(candidates) > QUOTE_BANK_MAX_ENTRIES:
        last = len(candidates) - 1
        indexes = [round(i * last / (QUOTE_BANK_MAX_ENTRIES - 1)) for i in range(QUOTE_BANK_MAX_ENTRIES)]
        candidates = [candidates[i] for i in indexes]
    return [{"candidate_id": f"Q{index:03d}", **entry,
             "quote_sha256": _text_sha(entry["quote"])} for index, entry in enumerate(candidates, 1)]


def quote_comparison_report(review, teaching: str) -> dict:
    """Report literal/CRLF comparison facts without accepting or revising a review.

    TeX, punctuation, spaces and words are retained. In particular this does
    not use a rendered-text matcher, fuzzy search or a replacement quote.
    """
    normalized = teaching.replace("\r\n", "\n")
    comparisons = []
    checks = review.get("checks", []) if isinstance(review, dict) else []
    for check_index, item in enumerate(checks if isinstance(checks, list) else []):
        if not isinstance(item, dict):
            continue
        excerpts = item.get("evidence", [])
        for evidence_index, excerpt in enumerate(excerpts if isinstance(excerpts, list) else []):
            is_text = isinstance(excerpt, str) and bool(excerpt.strip())
            canonical = excerpt.replace("\r\n", "\n") if is_text else None
            raw_index = teaching.find(excerpt) if is_text else -1
            canonical_index = normalized.find(canonical) if is_text else -1
            comparisons.append({"check_index": check_index, "criterion": item.get("criterion"),
                "evidence_index": evidence_index, "original_quote": excerpt,
                "original_quote_sha256": _text_sha(excerpt) if isinstance(excerpt, str) else None,
                "canonical_quote": canonical,
                "canonical_quote_sha256": _text_sha(canonical) if canonical is not None else None,
                "literal_match": raw_index >= 0,
                "canonical_crlf_match": canonical_index >= 0,
                "canonical_start_char": canonical_index if canonical_index >= 0 else None,
                "comparison_status": ("literal" if raw_index >= 0 else "crlf_only" if canonical_index >= 0 else "not_found")})
    return {"schema_version": 1, "quote_policy": QUOTE_POLICY, "prompt_revision": PROMPT_REVISION,
            "teaching_sha256": _text_sha(teaching), "canonical_teaching_sha256": _text_sha(normalized),
            "comparison_only_not_a_verdict": True, "original_report_modified": False,
            "comparisons": comparisons,
            "scope": "Only literal substring and CRLF→LF comparison; mathematical truth and pedagogy remain reviewer judgments."}

GENERATION_BRIEF = """
【知识准备与解题主线合同：knowledge-route-v1】
请把以下内容写成供学生阅读的解答组成部分，不展示模型内部思维记录。
1. 在展开证明前，列出实际要用的必要知识。每项说明“知识点—需要会什么—在本题哪一步使用”，
   简要解释必要的定义或公式及适用条件。可选方法、扩展知识与必需前置分开；
   正文首次出现且不可缺少的知识不得在清单中遗漏，也不要罗列未使用的课程名。
   本题将要建立的新结论属于本题成果，不能要求学生预先掌握。
2. 学生背景仅以用户明确提供的已学／未学信息为据。缺失时写“学生已学／未学范围未知”，
   上述清单是本解法的要求，不是对学生能力的诊断。遇到明确未学的概念，优先用已学知识
   解释或改写；不能做到时，说明还需补充什么及原因，不宣称已经完成个性化匹配。
3. 先用简短连贯文字交代“要得到什么—关键困难是什么—为何选此方法—各步怎样接起来”，
   再展开推导。路线长短服从题目，一步题不硬凑多步；多问分别交代路线及相互依赖。
4. 关键步骤交代本步目的、所用知识和条件、得到的中间结论及其对后续的作用。
   避免只有公式堆砌或“第一步、第二步”的空标题。路线须与实际证明一致。
5. 长篇基础知识解释放在按需补充中，正文保留必要的使用条件、关键理由及补充位置指引；
   不用多个并列解法、无关扩展或重复结论打断主线，不因控制篇幅省略证明。
用户明确指定的章节顺序、篇幅和讲解形式优先，功能性要求可以融入该形式，
不强制使用上述编号、固定标题或表格。原题和证明合同保持不变。
"""


def review_example() -> dict:
    return {"contract_version": VERSION, "verdict": "pass 或 revise",
            "checks": [{"criterion": key, "status": "met 或 needs_revision",
                        "evidence": ["从本次教学稿逐字摘录的证据"],
                        "reason": description} for key, description in CRITERIA.items()]}


def review_brief(teaching: str | None = None) -> str:
    text = ("\n【独立教学检查：" + VERSION + "】\n"
            "数学 verdict 只反映数学正确性。另在顶层 teaching_review 逐项检查以下要求：\n"
            + "\n".join(key + "：" + value for key, value in CRITERIA.items())
            + "\n每项恰好一次。status 为 met 或 needs_revision；reason 写具体理由，"
            "evidence 只引用本次完整教学稿中的逐字原文，不能引用提示词或原证明充当教学证据。"
            "met 至少给一处非空引文；缺失内容记 needs_revision 并说明缺失，evidence 可为空。"
            "不能仅凭标题或关键词存在判为 met，必须核查说明是否正确、具体且与本题对应。"
            "全部 met 才能 verdict=pass，否则为 revise。此为自动教学分析，需人工确认，"
            "不打人工分数，不声称学生已理解或学习效果提高。\n")
    text += ("\n【逐字引文提示修订：" + PROMPT_REVISION + "】\n"
             "evidence必须是稿件中一段连续的原文。逐字符保留TeX定界符、反斜线、空格和标点，"
             "不要把展示公式转换为纯文字，不要把不同句子拼接成一条引文。"
             "例如不能把稿件中的‘A的位置确定后’改写成‘固定A后’再放入evidence；"
             "解释或转述只能写在reason中。每项优先选一处足以支持判断的短引文，"
             "没有支持时应needs_revision，不能为了非空而编造。JSON内反斜线需正确转义。\n")
    if teaching is not None:
        text += ("\n<逐字可复制引文候选，仅为本稿实际片段数据>\n"
                 + json.dumps({"prompt_revision": PROMPT_REVISION, "source_sha256": _text_sha(teaching),
                               "max_entries": QUOTE_BANK_MAX_ENTRIES,
                               "max_chars_per_entry": QUOTE_BANK_MAX_CHARS,
                               "entries": quote_candidates(teaching)}, ensure_ascii=False, indent=2)
                 + "\n</逐字可复制引文候选，仅为本稿实际片段数据>\n"
                 "候选库不是检查结果，也不保证某项已满足。只可复制其中quote的原文，"
                 "不能将candidate_id本身作为evidence；完整稿另附，仍须逐项独立语义和数学审查。\n")
    return text


def report_schema(math_schema: dict) -> dict:
    """Extend a copy, preserving the original proof-review schema verbatim."""
    schema = deepcopy(math_schema)
    schema["required"].append("teaching_review")
    schema["properties"]["teaching_review"] = {
        "type": "object", "additionalProperties": False,
        "required": ["contract_version", "verdict", "checks"],
        "properties": {
            "contract_version": {"const": VERSION},
            "verdict": {"enum": ["pass", "revise"]},
            "checks": {"type": "array", "minItems": len(CRITERIA), "maxItems": len(CRITERIA),
                       "items": {"type": "object", "additionalProperties": False,
                                 "required": ["criterion", "status", "evidence", "reason"],
                                 "properties": {
                                     "criterion": {"enum": list(CRITERIA)},
                                     "status": {"enum": ["met", "needs_revision"]},
                                     "evidence": {"type": "array", "items": {"type": "string", "minLength": 1}},
                                     "reason": {"type": "string", "minLength": 1},
                                 }}},
        },
    }
    return schema


def validate_review(review: dict, teaching: str) -> dict:
    """Check coverage, decision consistency and literal evidence, not pedagogy."""
    if not isinstance(review, dict) or review.get("contract_version") != VERSION:
        raise ValueError("缺少正确版本的独立教学检查")
    if set(review) != {"contract_version", "verdict", "checks"}:
        raise ValueError("教学检查包含合同之外的字段")
    checks = review.get("checks")
    if not isinstance(checks, list) or len(checks) != len(CRITERIA):
        raise ValueError("教学检查必须覆盖全部六项要求")
    seen = set()
    normalized = teaching.replace("\r\n", "\n")
    for item in checks:
        if not isinstance(item, dict):
            raise ValueError("教学检查项必须为对象")
        if set(item) != {"criterion", "status", "evidence", "reason"}:
            raise ValueError("教学检查项字段不完整或包含未知字段")
        key = item.get("criterion")
        if not isinstance(key, str) or key not in CRITERIA or key in seen:
            raise ValueError("教学检查项重复、未知或缺失")
        seen.add(key)
        if item.get("status") not in ("met", "needs_revision"):
            raise ValueError("教学检查项状态无效")
        if not isinstance(item.get("reason"), str) or not item["reason"].strip():
            raise ValueError("教学检查必须说明具体理由")
        excerpts = item.get("evidence")
        if not isinstance(excerpts, list) or (item["status"] == "met" and not excerpts):
            raise ValueError("通过的教学检查必须提供稿件原文证据")
        for excerpt in excerpts:
            if (not isinstance(excerpt, str) or not excerpt.strip()
                    or excerpt.replace("\r\n", "\n") not in normalized):
                raise ValueError("教学检查引文不在本次教学稿中")
    expected = "pass" if all(item["status"] == "met" for item in checks) else "revise"
    if review.get("verdict") != expected:
        raise ValueError("教学检查总判定与逐项结果不一致")
    return review


def summary(review: dict) -> str:
    """Evidence-backed automatic analysis suitable for manifest.task_review."""
    return "\n".join(["自动教学分析（需人工确认），合同 " + VERSION + "："] + [
        f"{item['criterion']}：{item['status']}；{item['reason']}；原文证据："
        + ("；".join(item["evidence"]) if item["evidence"] else "缺失，见具体理由")
        for item in review["checks"]])
