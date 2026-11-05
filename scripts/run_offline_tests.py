"""Run every published Python test using synthetic fixtures and mocked providers.

This runner does not filter failures or select only a passing subset. Node tests
and the standalone demo are separate CI steps. A JSON report is opt-in and masks
local installation paths; test inputs and original research records are not read.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import sys
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


def sanitize(text: str) -> str:
    """Keep diagnostic detail without recording the contributor's local paths."""
    replacements = [(ROOT, '<project>'), (Path(sys.prefix), '<python-env>'),
                    (Path(sys.base_prefix), '<python>'), (Path.home(), '<user>')]
    for path, label in replacements:
        for spelling in {str(path), path.as_posix(), str(path).replace('\\', '\\\\')}:
            text = text.replace(spelling, label)
    # Failure messages may contain paths to deliberately missing fixture inputs.
    return re.sub(r'(?i)\b[A-Z]:[\\/][^\r\n\"\']+', '<absolute-path>', text)


class OfflineResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.successes = 0

    def addSuccess(self, test):
        self.successes += 1
        super().addSuccess(test)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json-report', type=Path, help='Optional local diagnostic report')
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT / 'scripts'))
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / 'scripts/tests'), pattern='test_*.py')
    discovered = suite.countTestCases()
    if discovered == 0:
        parser.error('No tests discovered; a zero-test run is not a passing check')
    print(f'Running all {discovered} discovered Python tests (no model credentials required).', flush=True)
    started = time.perf_counter()
    runner = unittest.TextTestRunner(verbosity=2 if args.verbose else 1, resultclass=OfflineResult)
    result = runner.run(suite)
    complete = result.testsRun == discovered
    summary = {
        'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'scope': 'Full published Python unittest discovery; synthetic fixtures and mocked providers',
        'platform': platform.system(),
        'python': platform.python_version(),
        'discovered_tests': discovered,
        'tests_run': result.testsRun,
        'passed': result.successes,
        'failures': len(result.failures),
        'errors': len(result.errors),
        'skipped': len(result.skipped),
        'expected_failures': len(result.expectedFailures),
        'unexpected_successes': len(result.unexpectedSuccesses),
        'complete': complete,
        'successful': result.wasSuccessful() and complete,
        'elapsed_seconds': round(time.perf_counter() - started, 3),
        'failure_details': [{'test': test.id(), 'traceback': sanitize(detail)}
                            for test, detail in result.failures],
        'error_details': [{'test': test.id(), 'traceback': sanitize(detail)}
                          for test, detail in result.errors],
        'skip_details': [{'test': test.id(), 'reason': sanitize(reason)}
                         for test, reason in result.skipped],
    }
    print(json.dumps({key: summary[key] for key in
                      ('discovered_tests', 'tests_run', 'passed', 'failures', 'errors',
                       'skipped', 'expected_failures', 'unexpected_successes', 'successful')},
                     ensure_ascii=False), flush=True)
    if args.json_report:
        output = args.json_report if args.json_report.is_absolute() else ROOT / args.json_report
        if output.is_file():
            try:
                previous = json.loads(output.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                raise RuntimeError('Existing report cannot be parsed; refusing to overwrite it')
            history = previous.pop('previous_runs', [])
            summary['previous_runs'] = [*history, previous]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0 if summary['successful'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
