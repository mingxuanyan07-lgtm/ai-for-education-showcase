"""Offline formula-error isolation, without editing source records or calling models."""
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
import json
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from record_reading import _mathml, _node_binary, render_reading


class SourceFragments(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.originals = []
        self.positions = []
        self._capture = False
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if attrs.get("data-math-render-error") == "true":
            self.positions.append((int(attrs["data-source-start"]), int(attrs["data-source-end"])))
        if tag == "code" and attrs.get("class") == "math-source-original":
            self.originals.append("")
            self._capture = True

    def handle_endtag(self, tag):
        if tag == "code":
            self._capture = False

    def handle_data(self, value):
        if self._capture:
            self.originals[-1] += value


class RecordRenderingTests(unittest.TestCase):
    def test_actual_nested_delimiter_error_preserves_fragment_and_other_math(self):
        # Verbatim malformed sentence in the native 1407 response; no corrected fixture.
        broken = r"\(\(x,y,z\)"
        source = "# 思路\n\n三个方程关于 " + broken + " 完全对称。因此把\n\\[p=x+y+z\\]\n作为整体。"
        page = render_reading(source, "真实片段")
        inspected = SourceFragments(page)
        self.assertEqual(inspected.originals, [broken])
        self.assertEqual([source[a:b] for a, b in inspected.positions], [broken])
        self.assertIn('data-math-render-error-count="1"', page)
        self.assertEqual(page.count('<math xmlns='), 1)
        self.assertIn("公式排版失败（原文保留）", page)
        self.assertIn("不是数学正确性判定", page)
        self.assertIn("完全对称。因此把", page)
        self.assertIn("作为整体", page)
        self.assertNotIn("EDUMATH", page)

    def test_valid_math_stays_mathml_with_no_error_marker(self):
        source = r"行内 $x^2$、\(\frac{1}{2}\)；块式" + "\n\\[\\begin{pmatrix}1&2\\\\3&4\\end{pmatrix}\\]\n$$a+b=c$$"
        page = render_reading(source, "合法公式")
        self.assertEqual(page.count('<math xmlns='), 4)
        self.assertNotIn('data-math-render-error="true"', page)
        self.assertNotIn('data-math-render-error-count=', page)
        self.assertNotIn('EDUMATH', page)

    def test_invalid_display_preserves_whitespace_delimiters_and_order(self):
        fragment = "\\[\n  \\notARealCommand{x}\n\\]"
        source = "前文\n\n" + fragment + "\n\n后文 $1+1=2$。"
        page = render_reading(source, "失败块")
        self.assertEqual(SourceFragments(page).originals, [fragment])
        self.assertIn('<div class="equation math-source-error"', page)
        self.assertEqual(page.count('<math xmlns='), 1)
        self.assertLess(page.index("前文"), page.index("math-source-original"))
        self.assertLess(page.index("math-source-original"), page.index("后文"))

    def test_invalid_formula_is_html_escaped_and_code_is_not_parsed(self):
        fragment = r'$\notARealCommand{<script>alert("x")</script> & <img src=x>}$'
        source = fragment + "\n\n`$\\notARealCommand{x}$`\n\n```tex\n\\(\\notARealCommand{x}\\)\n```"
        page = render_reading(source, '<script>title</script>')
        self.assertEqual(SourceFragments(page).originals, [fragment])
        self.assertEqual(page.count('data-math-render-error="true"'), 1)
        self.assertNotIn('<script>', page)
        self.assertNotIn('<img src=x>', page)
        self.assertIn('&lt;script&gt;', page)

    def test_renderer_process_failure_still_fails_closed(self):
        _mathml.cache_clear()
        with patch('record_reading.subprocess.run') as run:
            run.return_value.returncode = 1
            run.return_value.stderr = 'runtime unavailable'
            with self.assertRaisesRegex(ValueError, 'runtime unavailable'):
                _mathml(((r'x', False),))

    def test_invalid_renderer_item_is_not_treated_as_a_source_error(self):
        _mathml.cache_clear()
        with patch('record_reading.subprocess.run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = json.dumps([{'status': 'rendered', 'mathml': None}])
            with self.assertRaisesRegex(ValueError, 'invalid item'):
                _mathml(((r'x', False),))

    def test_original_renderer_protocol_remains_compatible_for_active_runners(self):
        script = Path(__file__).resolve().parents[1] / 'render_record_math.cjs'
        result = subprocess.run([_node_binary(), str(script)],
                                input=json.dumps([{'tex': 'x^2', 'display': False}]),
                                text=True, encoding='utf-8', capture_output=True, check=True)
        rendered = json.loads(result.stdout)
        self.assertIsInstance(rendered[0], str)
        self.assertIn('<math xmlns=', rendered[0])


if __name__ == '__main__':
    unittest.main()
