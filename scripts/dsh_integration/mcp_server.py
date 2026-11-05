"""DSH MCP server: AI for education project workflow tools.

Registered in the DSH desktop profile as serverName ``edu``; model-facing tool
names are ``mcp__edu__<tool>``. The server only wraps existing project entries
and never reads credentials. Real model-calling runs require an explicit
``allow_model_calls=True`` from the user's instruction.
"""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.server.mcpserver import MCPServer  # noqa: E402

from dsh_integration import core  # noqa: E402

INSTRUCTIONS = """AI for education 项目编排工具（DSH 外层）。

规则：
- 先读项目 AGENTS.md 与相关文档；本服务器不会替用户决定数学流程。
- 先用 project_health / service_control(status) / task_start(mode=dry_run|check) 做无模型检查；
  只有用户确认模型、凭据与预算后，才可 task_start(mode=run, allow_model_calls=true)。
- DeepSeek 分阶段链内部通过只到 pending_final_review，必须由当前 Codex 对话终审并用
  deepseek_rethlas_cli.py finalize 归档；不能用本服务器或 DeepSeek 的自评代替。
- 原版 Codex Rethlas 的内部核验 correct 不是共同外审；DeepTutor 的上游实际模型名不可得时记“未记录”。
- 失败、超时、中断保留证据，不自动重试，不把进程退出当作数学成功。
- 归档统一用 archive_record（experiment_records.py），安装/环境检查用 record_kind=installation_test。
- 不停止其他实验的服务；不修改登录、凭据、沙箱与审批设置；不生成 ZIP。
"""

server = MCPServer(
    name="ai-for-education",
    title="AI for education workflow",
    version="0.1.0",
    instructions=INSTRUCTIONS,
)


@server.tool(description="Read-only project health: DSH version/profile, project paths, services "
                         "(DeepTutor, Rethlas verification), recent tasks, math-local probe and "
                         "pending final-review candidates. Never calls a model.")
def project_health() -> dict:
    return core.project_health()


@server.tool(description="Start/stop/query a local service. service: deeptutor | rethlas_verification. "
                         "action: status | start | stop. start/stop require confirm=true (user-approved). "
                         "Status checks are bounded and never stop other experiments' services.")
def service_control(service: str, action: str = "status", confirm: bool = False,
                    wait_seconds: int = 0) -> dict:
    return core.service_control(service, action, confirm=confirm, wait_seconds=wait_seconds)


@server.tool(description="Start or dry-run a project workflow task. system: deepseek | rethlas_codex | "
                         "deeptutor. mode: dry_run/check (no model calls) or run (needs "
                         "allow_model_calls=true after user confirms model, credentials and budget). "
                         "options: files/prompt/budgets; identity: question_id/ai_id/run_id for Rethlas sidecar.")
def task_start(system: str, mode: str = "run", allow_model_calls: bool = False,
               options: dict | None = None, identity: dict | None = None) -> dict:
    return core.task_start(system=system, mode=mode, allow_model_calls=allow_model_calls,
                           options=options or {}, identity=identity)


@server.tool(description="Bounded status query for one task: process alive/exit, derived status from "
                         "artifacts (never from exit code alone), timings and pending final review flag.")
def task_status(task_id: str) -> dict:
    return core.task_status(task_id)


@server.tool(description="Read a task's results: artifact paths, bounded excerpts, model/usage evidence "
                         "and final-review requirements. Does not call models or finalize anything.")
def task_results(task_id: str) -> dict:
    return core.task_results(task_id)


@server.tool(description="Tail a task's stdout or stderr (bounded lines).")
def task_logs(task_id: str, source: str = "stdout", lines: int = 80) -> dict:
    return core.task_logs(task_id, source=source, lines=lines)


@server.tool(description="Cancel one task's process tree (requires confirm=true); marks interrupted, "
                         "does not retry and does not report success.")
def task_cancel(task_id: str, confirm: bool = False) -> dict:
    return core.task_cancel(task_id, confirm=confirm)


@server.tool(description="For a pending DeepSeek task, return the final-review request, review prompt, "
                         "record directory, checklist and finalize command. The current Codex chat must "
                         "do the review; this tool never calls or impersonates Codex.")
def prepare_final_review(task_id: str) -> dict:
    return core.prepare_final_review(task_id)


@server.tool(description="Archive a real experiment through scripts/experiment_records.py using a "
                         "project-relative manifest JSON. Supports record_kind=installation_test. "
                         "Does not create ZIPs.")
def archive_record(manifest: str, refresh: bool = True) -> dict:
    return core.archive_record(manifest=manifest, refresh=refresh)


@server.tool(description="Validate/prepare an evaluation spec through scripts/eval_cli.py. dry_run=true "
                         "only validates and prints identities/hashes; no model calls, no records.")
def evaluation_prepare(spec: str, dry_run: bool = True) -> dict:
    return core.evaluation_prepare(spec=spec, dry_run=dry_run)


@server.tool(description="Refresh the existing experiment index and reading pages (no model calls).")
def evaluation_refresh() -> dict:
    return core.evaluation_refresh()


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
