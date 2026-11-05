"""Conservative presentation-only comparison of an unchanged problem statement.

This is not mathematical equivalence checking. Only paired TeX math delimiters
can differ; prose, math contents, their order, whitespace and code remain exact.
Ambiguous dollar/currency notation fails closed instead of being rewritten.
"""
from __future__ import annotations

import hashlib
import json
import re


POLICY = "tex_math_delimiters_v1"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _escaped(text: str, index: int) -> bool:
    before = index - 1
    while before >= 0 and text[before] == "\\":
        before -= 1
    return (index - before - 1) % 2 == 1


def _dollar_math(content: str, display: bool) -> bool:
    # Standard inline math has no padding at the opening/closing dollar. This
    # rejects common currency text such as '$5 and $10'. Exact raw copies of
    # such statements are still accepted by the caller without reinterpretation.
    if not content.strip() or (not display and (content != content.strip() or "\n" in content)):
        return False
    candidate = content.strip()
    if candidate[-1] in "+-=*/,:;.<>|—–":
        return False
    # Multi-letter ordinary prose is not guessed to be dollar math. Explicit
    # TeX text/operator commands are unambiguous and stay byte-exact in tokens.
    candidate = re.sub(r"\\(?:text|operatorname|mathrm|mathit|mathbf)\{[^{}]*\}", "", candidate)
    candidate = re.sub(r"\\(?:begin|end)\{[A-Za-z*]+\}", "", candidate)
    candidate = re.sub(r"\\[A-Za-z]+", "", candidate)
    return re.search(r"[A-Za-z]{2,}", candidate) is None


def _tokens(text: str):
    result, delimiters, literal = [], [], []
    index = 0

    def flush():
        if literal:
            result.append(("text", "".join(literal)))
            literal.clear()

    while index < len(text):
        if text[index] == "`" and not _escaped(text, index):
            run_end = index
            while run_end < len(text) and text[run_end] == "`":
                run_end += 1
            marker = text[index:run_end]
            closing = re.search(r"(?<!`)" + re.escape(marker) + r"(?!`)", text[run_end:])
            if closing is None:
                return None
            end = run_end + closing.end()
            literal.append(text[index:end])
            index = end
            continue
        if text.startswith((r"\)", r"\]"), index) and not _escaped(text, index):
            return None
        opening = closing = kind = None
        if text[index] == "$" and not _escaped(text, index):
            run_end = index
            while run_end < len(text) and text[run_end] == "$":
                run_end += 1
            width = run_end - index
            if width not in (1, 2):
                return None
            opening = closing = "$" * width
            kind = "display" if width == 2 else "inline"
        elif text.startswith(r"\(", index) and not _escaped(text, index):
            opening, closing, kind = r"\(", r"\)", "inline"
        elif text.startswith(r"\[", index) and not _escaped(text, index):
            opening, closing, kind = r"\[", r"\]", "display"
        if opening is None:
            literal.append(text[index])
            index += 1
            continue
        start = index + len(opening)
        end = start
        while end < len(text):
            if text.startswith(closing, end) and not _escaped(text, end):
                if closing.startswith("$"):
                    run_end = end
                    while run_end < len(text) and text[run_end] == "$":
                        run_end += 1
                    if run_end - end != len(closing):
                        return None
                    if run_end < len(text) and text[run_end].isdigit():
                        # '$5.00$10.00' can be two currency amounts, not a
                        # trustworthy math span followed by ordinary prose.
                        return None
                break
            if (text[end] == "$" or text.startswith((r"\(", r"\)", r"\[", r"\]"), end)) and not _escaped(text, end):
                # Nested/mixed delimiter syntax is not a presentation rescue.
                return None
            end += 1
        else:
            return None
        content = text[start:end]
        if not content.strip() or (opening.startswith("$") and not _dollar_math(content, kind == "display")):
            return None
        flush()
        result.append((kind, content))
        delimiters.append({"kind": kind, "opening": opening, "closing": closing,
                           "content_sha256": _sha(content)})
        index = end + len(closing)
    flush()
    return result, delimiters


def compare_statement(problem: str, statement: str) -> dict:
    """Return an audit result, without mutating either supplied text."""
    original, proposed = problem.strip(), statement.strip()
    audit = {
        "schema_version": 1, "scope": "structure_only", "policy": POLICY,
        "accepted": False, "statement_match": "rejected", "raw_exact": original == proposed,
        "raw_exact_scope": "statement.strip()==problem.strip(); inner text is not normalized",
        "problem_input_sha256": _sha(problem), "problem_trimmed_sha256": _sha(original),
        "candidate_statement_sha256": _sha(proposed), "delimiter_changes": [],
        "proof_text_rewritten": False, "mathematical_verdict": "not_run",
    }
    if original == proposed:
        audit.update(accepted=True, statement_match="raw_exact")
        return audit
    source = _tokens(original)
    target = _tokens(proposed)
    if source is None or target is None or source[0] != target[0]:
        audit["reason"] = "not_exact_or_unambiguous_delimiter_only_equivalent"
        return audit
    changes = []
    for ordinal, (old, new) in enumerate(zip(source[1], target[1]), 1):
        if old != new:
            changes.append({"math_span": ordinal, "kind": old["kind"],
                            "from": {"opening": old["opening"], "closing": old["closing"]},
                            "to": {"opening": new["opening"], "closing": new["closing"]},
                            "unchanged_content_sha256": old["content_sha256"]})
    if changes:
        audit.update(accepted=True, statement_match="cosmetic_equivalent", delimiter_changes=changes,
                     comparison_tokens_sha256=_sha(json.dumps(source[0], ensure_ascii=False)))
    else:
        audit["reason"] = "no_permitted_delimiter_difference_explains_the_change"
    return audit
