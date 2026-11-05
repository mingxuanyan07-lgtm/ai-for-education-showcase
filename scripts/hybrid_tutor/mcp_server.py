"""Narrow Hybrid Tutor MCP bridge; model-side arguments cannot authorize spending."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mcp.server.mcpserver import MCPServer
from hybrid_tutor.bridge import Bridge

bridge = Bridge()
server = MCPServer(name="hybrid-tutor", title="Rethlas + DeepTutor", version="0.1.0",
                   instructions="只编排本项目组合流程。默认 dry_run。run 须宿主授权文件绑定题面、配置、预算、题号与次数；模型不能自行授权。返回 pending_final_review 仍须当前 Codex 终审。失败不自动重试，旧材料保留。")


@server.tool(description="Submit one bounded hybrid task. Text inputs only; no commands or paths. Default dry_run has no model calls. run requires an existing host authorization for the exact inputs and frozen configuration. Returns a task_id immediately; poll status/results later. Reuse idempotency_key only for the same request.")
def task_start(problem: str, requirements: str, user_prompt: str, idempotency_key: str,
               question_id: str | None = None, tags: list[str] | None = None, mode: str = "dry_run") -> dict:
    return bridge.start(problem=problem, requirements=requirements, user_prompt=user_prompt,
                        idempotency_key=idempotency_key, question_id=question_id, tags=tags, mode=mode)


@server.tool(description="Read persisted status of one hybrid task. Process exit is not mathematical success.")
def task_status(task_id: str) -> dict:
    return bridge.status(task_id)


@server.tool(description="Read the task's actual inputs, stage answers, errors, model/usage evidence and archive references. Does not finalize or call models.")
def task_results(task_id: str) -> dict:
    return bridge.results(task_id)


@server.tool(description="Request cancellation of this bridge's own hybrid task. The owning worker stops only its own child process tree. Does not retry.")
def task_cancel(task_id: str) -> dict:
    return bridge.cancel(task_id)


if __name__ == "__main__":
    server.run("stdio")
