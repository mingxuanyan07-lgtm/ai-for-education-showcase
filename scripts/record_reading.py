"""Offline reading pages; preserve source records and typeset TeX as static MathML."""
from __future__ import annotations

from functools import lru_cache
from html import escape
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from markdown_it import MarkdownIt


_HERE = Path(__file__).resolve().parent
_PARTS = re.compile(
    r"(?P<code>^ {0,3}(?P<fence>`{3,}|~{3,})[^\n]*\n.*?^ {0,3}(?P=fence)[ \t]*$|"
    r"(?P<ticks>`+)[^`]*?(?P=ticks))|"
    r"(?P<display>\\\[.*?\\\]|(?<![\\$])\$\$.*?(?<!\\)\$\$)|"
    r"(?P<inline>\\\(.*?\\\)|(?<![\\$])\$(?!\$)(?:\\.|[^$\\\n])+?(?<!\\)\$(?!\$))",
    re.MULTILINE | re.DOTALL,
)

_STYLE = """
:root { color-scheme: light; font-family: 'Microsoft YaHei', 'Noto Sans CJK SC', sans-serif;
  color: #20272e; background: #f3f5f7; }
* { box-sizing: border-box; }
body { margin: 0; padding: 32px 20px; }
main { max-width: 1000px; margin: auto; padding: 42px 52px; background: white;
  border: 1px solid #e0e5e9; border-radius: 10px; font-size: 17px; line-height: 1.9; }
h1, h2, h3, h4 { line-height: 1.5; color: #163d57; }
h1 { font-size: 27px; margin-top: 0; }
h1:not(:first-child), h2 { font-size: 23px; margin-top: 36px; }
h3 { font-size: 21px; margin-top: 32px; border-bottom: 1px solid #e4e9ed; padding-bottom: 10px; }
h4 { font-size: 18px; margin: 26px 0 12px; }
p { margin: 14px 0; overflow-wrap: anywhere; }
li { margin: 6px 0; overflow-wrap: anywhere; }
a { color: #086ba1; text-decoration-thickness: 1px; text-underline-offset: 3px; }
code { font-family: Consolas, monospace; overflow-wrap: anywhere; font-size: .92em; }
pre { white-space: pre-wrap; padding: 15px; background: #f5f7f9; border-radius: 5px; }
blockquote { margin: 18px 0; border-left: 3px solid #adc6d7; padding-left: 18px; }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
th, td { padding: 10px; border: 1px solid #dce3e8; text-align: left; overflow-wrap: anywhere; }
th { background: #edf3f7; }
.equation { margin: 20px 0; overflow-x: auto; padding: 7px 0; }
.math-rendering-notice { margin: 0 0 24px; padding: 12px 16px;
  border: 1px solid #d9b96a; background: #fff9e9; font-size: 14px; }
.math-source-error { background: #fff9e9; border-bottom: 1px dotted #9b6a19;
  overflow-wrap: anywhere; }
.math-source-error-label { color: #86580b; font-size: .8em; }
.math-source-error code { white-space: pre-wrap; }
.math-source-error-detail { color: #6d6455; font-size: .75em; }
math { font-family: 'Cambria Math', 'STIX Two Math', math; font-size: 1.12em; }
math[display="block"] { margin: 0; }
annotation { display: none; }
@media (max-width: 700px) { body { padding: 10px; } main { padding: 22px 18px; font-size: 16px; } }
@media print { body { padding: 0; background: white; } main { max-width: none; padding: 0;
  border: none; font-size: 11pt; } h1, h2, h3, h4 { break-after: avoid; }
  .equation, tr { break-inside: avoid; } a { color: inherit; } }
"""


def _node_binary():
    node = shutil.which("node")
    if node:
        return node
    bundled = Path(os.environ.get("USERPROFILE", str(Path.home()))) / (
        ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe")
    if bundled.is_file():
        return str(bundled)
    raise ValueError("Reading-page math rendering needs Node.js; no source-only fallback was written.")


@lru_cache(maxsize=64)
def _mathml(expressions):
    if not expressions:
        return ()
    result = subprocess.run(
        [_node_binary(), str(_HERE / "render_record_math.cjs")],
        input=json.dumps([{"tex": tex, "display": display, "isolateSourceErrors": True}
                          for tex, display in expressions]),
        text=True, encoding="utf-8", capture_output=True, timeout=60, check=False,
    )
    if result.returncode:
        raise ValueError("Reading-page math rendering failed: " + result.stderr.strip()[:600])
    rendered = json.loads(result.stdout)
    if not isinstance(rendered, list) or len(rendered) != len(expressions):
        raise ValueError("Reading-page math renderer returned an incomplete result")
    for item in rendered:
        if (not isinstance(item, dict)
                or item.get("status") not in {"rendered", "source_error"}
                or not isinstance(item.get("mathml" if item.get("status") == "rendered" else "error"), str)):
            raise ValueError("Reading-page math renderer returned an invalid item")
    return tuple(rendered)


def render_reading(markdown_text, title):
    """Render a derived page without executing HTML or altering original text."""
    expressions = []
    source_spans = []
    prefix = "EDUMATH" + hashlib.sha256(markdown_text.encode("utf-8")).hexdigest()[:16] + "TOKEN"
    while prefix in markdown_text:
        prefix += "X"

    def extract(match):
        if match.group("code") is not None:
            return match.group()
        display = match.group("display") is not None
        original = match.group()
        width = 2 if display or original.startswith("\\(") else 1
        tex = original[width:-width].strip()
        marker = prefix + str(len(expressions)) + "END"
        expressions.append((tex, display))
        source_spans.append((original, match.start(), match.end()))
        return "\n\n" + marker + "\n\n" if display else marker

    protected = _PARTS.sub(extract, markdown_text)
    # These labels are translations in the reading view, never edits to raw verdicts.
    protected = re.sub(r"(?m)^数学复核：not_run。$", "数学复核：尚未独立核验。", protected)
    body = MarkdownIt("commonmark", {"html": False}).enable("table").render(protected)
    error_count = 0
    for number, ((_, display), rendered) in enumerate(zip(expressions, _mathml(tuple(expressions)))):
        marker = prefix + str(number) + "END"
        if rendered["status"] == "source_error":
            error_count += 1
            original, start, end = source_spans[number]
            line = markdown_text.count("\n", 0, start) + 1
            column = start - markdown_text.rfind("\n", 0, start)
            # Keep the exact matched source, including delimiters and whitespace.
            # Escaping is only for HTML presentation; it is not a TeX repair.
            error = rendered["error"]
            attributes = (f' data-math-render-error="true" data-source-start="{start}"'
                          f' data-source-end="{end}" data-source-line="{line}"'
                          f' data-source-column="{column}"')
            tag = "div" if display else "span"
            classes = "equation math-source-error" if display else "math-source-error"
            replacement = (f'<{tag} class="{classes}"{attributes}>'
                           '<span class="math-source-error-label">公式排版失败（原文保留）：</span> '
                           '<code class="math-source-original">' + escape(original) + '</code> '
                           '<span class="math-source-error-detail">'
                           + escape(f"〔源文第{line}行第{column}列；{error}〕") + f'</span></{tag}>')
            if display:
                body = body.replace("<p>" + marker + "</p>", replacement)
            else:
                body = body.replace(marker, replacement)
            continue
        mathml = rendered["mathml"]
        if display:
            body = body.replace("<p>" + marker + "</p>", '<div class="equation">' + mathml + "</div>")
        else:
            body = body.replace(marker, '<span class="inline-math">' + mathml + "</span>")
    if prefix in body:
        raise ValueError("Reading page contains an unrendered math placeholder")
    if error_count:
        body = (f'<aside class="math-rendering-notice" data-math-render-error-count="{error_count}">'
                f'本页有{error_count}处公式无法排版，已在原位置保留完整原文及错误详情。'
                '此提示仅涉及公式格式，不是数学正确性判定；原始记录未改写。</aside>\n' + body)
    # No browser-side scripts, network resources, or font downloads are needed.
    return ("<!doctype html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset=\"utf-8\">\n"
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
            "style-src 'unsafe-inline'; img-src 'self' data:\">\n"
            "<title>" + escape(title) + "</title>\n<style>" + _STYLE + "</style>\n</head>\n"
            "<body><main>\n" + body + "</main></body>\n</html>\n")
