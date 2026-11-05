"""Shared host admission limits; unknown provider usage never becomes zero."""
import time
from runtime_metrics import _usage


class BudgetLimit(ValueError):
    pass


class RunBudget:
    def __init__(self, *, seconds=3600, model_calls=10, tool_calls=12, observed_tokens=500000, clock=time.monotonic):
        self.clock = clock
        self.started = clock()
        self.deadline = self.started + seconds
        self.limits = dict(seconds=seconds, model_calls=model_calls, tool_calls=tool_calls, observed_tokens=observed_tokens)
        self.model_attempts = self.tool_attempts = 0
        self.tokens = 0
        self.metered = 0
        self.unmetered = []

    def remaining(self, stage_deadline):
        value = min(self.deadline, stage_deadline) - self.clock()
        if value < 1:
            raise BudgetLimit("共享运行或阶段时间预算已耗尽")
        return int(value)

    def admit_model(self, stage_deadline):
        remaining = self.remaining(stage_deadline)
        if self.model_attempts >= self.limits["model_calls"]:
            raise BudgetLimit("共享模型角色调用次数已耗尽")
        if self.tokens >= self.limits["observed_tokens"]:
            raise BudgetLimit("已观测Token达到限额；停止后续调用")
        self.model_attempts += 1
        return remaining

    def admit_tools(self, count, stage_deadline):
        self.remaining(stage_deadline)
        if self.tool_attempts + count > self.limits["tool_calls"]:
            raise BudgetLimit("共享计算调用次数已耗尽")
        self.tool_attempts += count

    def observe(self, stage, result):
        total = _usage(result.get("usage"))["total_tokens"]
        if total is None:
            self.unmetered.append(stage)
        else:
            self.tokens += total
            self.metered += 1

    def snapshot(self):
        return {"limits": self.limits, "elapsed_seconds": max(0, self.clock() - self.started),
                "model_role_attempts": self.model_attempts, "admitted_tool_calls": self.tool_attempts,
                "observed_total_tokens": self.tokens if self.metered else None,
                "metered_roles": self.metered, "unmetered_roles": self.unmetered[:],
                "token_limit_scope": "Observed usage admission stop only; missing usage unknown, an in-flight response may exceed the limit.",
                "exclusions": ["Current Codex final review", "Common external review"]}
