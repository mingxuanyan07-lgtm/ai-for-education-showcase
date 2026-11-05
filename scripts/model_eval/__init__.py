"""模型与工作流评测接入（离线优先，不调用模型）。

本包只负责：读取评测规格、生成任务包、导入实际输入输出、构造 experiment_records
的 manifest 并调用其归档接口。它不启动任何模型、Rethlas、Codex 或核验服务，
也不推断实际模型名称。
"""
from __future__ import annotations

__all__ = ["spec", "staging", "manifest", "dsh", "cli"]
__version__ = "1.0.0"
