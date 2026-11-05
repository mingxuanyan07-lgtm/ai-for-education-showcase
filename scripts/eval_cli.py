"""模型与工作流评测接入入口（位于 scripts 下，便于直接运行）。

示例：
    .venv\\Scripts\\python.exe scripts\\eval_cli.py prepare --spec evaluations/specs/example.json --dry-run
    .venv\\Scripts\\python.exe scripts\\eval_cli.py dsh-info --probe
"""
from __future__ import annotations

from pathlib import Path
import sys

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from model_eval.cli import main  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")
sys.exit(main())
