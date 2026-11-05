"""Readable, scope-labelled metering from actual saved fields; no model calls.

Unknown components never erase a measured subtotal or become zero. This module
does not modify the record or interpret bookkeeping dates as model runtime.
"""
from datetime import datetime
import json
import math
import re


def number(value):
    return value if type(value) in (int,float) and math.isfinite(value) and value >= 0 else None


def _first(*values):
    return next((v for v in values if number(v) is not None),None)


def _known(value):
    return isinstance(value,str) and value.strip() not in ("","未记录","unknown","not_recorded")


def _unique(values):
    return list(dict.fromkeys(v.strip() for v in values if _known(v)))


def _sum(values):
    values=[v for v in values if number(v) is not None]
    return sum(values) if values else None


def _usage(value):
    value=value if isinstance(value,dict) else {}
    value=value.get("total",value)
    value=value if isinstance(value,dict) else {}
    result={
        "input_tokens":_first(value.get("input_tokens"),value.get("prompt_tokens"),value.get("inputTokens")),
        "output_tokens":_first(value.get("output_tokens"),value.get("completion_tokens"),value.get("outputTokens")),
        "cached_input_tokens":_first(value.get("cached_input_tokens"),value.get("cache_read_input_tokens"),value.get("cacheReadTokens")),
        "total_tokens":_first(value.get("total_tokens"),value.get("totalTokens")),
    }
    # DSH total includes separately reported cache reads. Never silently apply
    # the OpenAI input+output convention to an incomplete DSH usage object.
    if result["total_tokens"] is None and "inputTokens" not in value and all(result[k] is not None for k in ("input_tokens","output_tokens")):
        result["total_tokens"]=result["input_tokens"]+result["output_tokens"]
    return result


def _elapsed(settings):
    timing=settings.get("timing") if isinstance(settings.get("timing"),dict) else {}
    value=_first(timing.get("duration_seconds"),settings.get("elapsed_seconds"),settings.get("duration_seconds"))
    if value is not None:
        return value,False
    # These are actual per-run settings, not record created_at/updated_at.
    for item in (timing,settings):
        start,end=item.get("started_at"),item.get("finished_at")
        if not isinstance(start,str) or not isinstance(end,str):
            continue
        try:
            a,b=(datetime.fromisoformat(v.replace("Z","+00:00")) for v in (start,end))
            if a.tzinfo is not None and b.tzinfo is not None:
                delta=(b-a).total_seconds()
                if delta >= 0:
                    return delta,True
        except (ValueError,OverflowError):
            pass
    return None,False


def format_number(value,decimals=0):
    if number(value) is None:
        return "未记录"
    return f"{value:,.{decimals}f}"


def runtime_summary(record):
    recovery=record.get("runtime_observations") or {}
    recovery=recovery if isinstance(recovery,dict) else {}
    design=record.get("evaluation") or {}
    design=design if isinstance(design,dict) else {}
    usage=design.get("usage") or {}
    usage=usage if isinstance(usage,dict) else {}
    roles=design.get("roles") or []
    roles=[r for r in roles if isinstance(r,dict)]
    iterations=record.get("iterations") or []
    settings=[i.get("settings") or {} for i in iterations if isinstance(i,dict)]
    settings=[s for s in settings if isinstance(s,dict)]
    setting_models=[s.get("actual_model") if "actual_model" in s else s.get("model") for s in settings]
    actual=_unique([r.get("actual_model") for r in roles]+setting_models+[record.get("model"),recovery.get("actual_model")])
    configured=_unique([r.get("configured_model") for r in roles]+[s.get("configured_model") for s in settings])
    missing_model=(any(not _known(r.get("actual_model")) for r in roles) if roles
                   else any(not _known(value) for value in setting_models))
    model=" / ".join(actual) if actual else ("配置 "+" / ".join(configured)+"（实际型号未返回）" if configured else "未记录（原记录无模型元数据）")
    if actual and missing_model:
        model+="（已记录阶段；另有角色型号未记录）"
    result={"model":model,"actual_model":" / ".join(actual) or "未记录","configured_model":" / ".join(configured) or "未记录",
            "model_source":"已保存的实际模型字段；配置型号单列，不用于猜测上游实际型号。",
            "seconds":None,"seconds_scope":"原记录没有可用的运行计时。","seconds_scope_id":"missing",
            "input_tokens":None,"output_tokens":None,"cached_input_tokens":None,"total_tokens":None,
            "tokens_scope":"原记录没有实际Token用量。","tokens_scope_id":"missing",
            "calls":None,"calls_scope":"原记录没有可用调用数。","calls_scope_id":"missing",
            "coverage":"missing","exclusions":[],"evidence_files":[],
            "input_tokens_label":"输入Token（原始字段）","cache_tokens_label":"缓存读取Token（计法随来源）",
            "estimated_total_tokens":None,"estimated_tokens_label":"无可用估计"}
    for key in ("evidence_file","host_timing_evidence_file"):
        if _known(usage.get(key)):
            result["evidence_files"].append(usage[key])
    observed=usage.get("observed_deepseek_roles") or {}
    observed=observed if isinstance(observed,dict) else {}
    if any(number(observed.get(k)) is not None for k in ("total_tokens","input_tokens_dsh_field","output_tokens")):
        result.update(input_tokens=number(observed.get("input_tokens_dsh_field")),output_tokens=number(observed.get("output_tokens")),
                      cached_input_tokens=number(observed.get("cached_input_tokens")),total_tokens=number(observed.get("total_tokens")),
                      calls=number(observed.get("host_role_invocations")),calls_scope_id="host_role_invocations",calls_scope="已观测DeepSeek阶段数，不是完整系统调用数。",
                      tokens_scope_id="deepseek_roles",tokens_scope="已观测DeepSeek角色小计，含已记录修订；不含Codex流程终审与共同外审。",
                      coverage="partial",input_tokens_label="输入Token（DSH原字段；缓存另列）",cache_tokens_label="缓存读取Token（DSH单独报告）")
        result["exclusions"].append("Codex流程终审、共同外审的型号/Token/耗时未计量；不能把阶段小计当作完整系统总量。")
        missing_roles = observed.get("unmetered_roles") or []
        if missing_roles:
            result["tokens_scope"] += f" 本次只有{observed.get('metered_roles', '未记录')}/{observed.get('attempted_roles', '未记录')}个受测阶段返回总Token。"
            result["exclusions"].append("以下受测阶段用量缺失，未并入已观测小计：" + "、".join(map(str, missing_roles)) + "；不等于零消耗。")
    elif any(number(usage.get(k)) is not None for k in ("total_tokens","input_tokens","output_tokens")) and usage.get("estimated") is not True:
        result.update(_usage(usage))
        native=number(usage.get("native_ledger_attempted_calls")) is not None
        result.update(tokens_scope_id="native_api_attempts" if native else "reported_system",
                      tokens_scope="该原生任务已归属API尝试的实际报告用量；缓存命中属于输入，不重复相加。" if native else "记录中的实际用量，范围见原始计量证据。",
                      calls=number(usage.get("native_ledger_attempted_calls")) if native else number(usage.get("calls")),
                      calls_scope_id="native_ledger_attempts" if native else "reported_calls",
                      calls_scope="原生账本尝试条数，不等同上游已接受请求数。" if native else "记录中的实际调用口径。",
                      coverage="complete" if usage.get("includes_all_system_roles") is True else "partial")
        if native:
            result["exclusions"].append("不含共同外审；标题/后台调用归属可能不全；原生未保留上游实际response.model。")
    else:
        # A render-only evidence observation can recover older archived meters.
        if isinstance(recovery,dict) and recovery.get("usage"):
            result.update(_usage(recovery["usage"]))
            result.update(coverage="partial",tokens_scope_id="archived_generation_events",tokens_scope=recovery.get("scope","从已归档生成事件恢复的观测用量；不含未计量教学/外审。"),
                          calls=number(recovery.get("calls")),calls_scope_id="archived_turn_completed",calls_scope="唯一归档turn.completed用量事件数。")
            result["evidence_files"]+=recovery.get("evidence_files",[])
            result["exclusions"]+=recovery.get("exclusions",[])
        else:
            meters=[_usage(s.get("usage")) for s in settings]
            if any(m["total_tokens"] is not None or m["input_tokens"] is not None for m in meters):
                for key in ("input_tokens","output_tokens","cached_input_tokens","total_tokens"):
                    result[key]=_sum(m[key] for m in meters)
                count=sum(m["total_tokens"] is not None for m in meters)
                result.update(tokens_scope_id="saved_iterations",tokens_scope=f"已保存轮次中的可计量小计（总Token可得{count}/{len(settings)}轮）；不是完整系统总成本。",
                              calls=count,calls_scope_id="metered_saved_iterations",calls_scope="有总Token记录的保存轮次，不等同底层请求数。",coverage="partial")
        estimate=usage.get("estimated_usage") or {}
        estimate=_first(estimate.get("total_tokens")) if isinstance(estimate,dict) else None
        if usage.get("estimated") is True:
            estimate=_first(estimate,usage.get("total_tokens"))
        if estimate is not None:
            result["estimated_total_tokens"]=estimate
            result["estimated_tokens_label"]=format_number(estimate)+"（估计，非API实际用量）"
            if result["total_tokens"] is None:
                result["coverage"]="estimated"
                result["tokens_scope_id"]="native_estimate_only"
                result["tokens_scope"]="仅有原生估计用量，实际API Token未返回；估计不计入实际用量汇总。"
    if result["calls"] is None and number(observed.get("host_role_invocations")) is not None:
        result.update(calls=number(observed["host_role_invocations"]),calls_scope_id="host_role_invocations",
                      calls_scope="已观测DeepSeek阶段尝试数；用量未返回不等于没有调用或零消耗。")
    if result["calls"] is None and number(usage.get("native_ledger_attempted_calls")) is not None:
        result.update(calls=number(usage["native_ledger_attempted_calls"]),calls_scope_id="native_ledger_attempts",
                      calls_scope="原生账本尝试条数；包括失败/估计条目，不等同上游已接受请求数。")
    wall=_first(usage.get("observed_host_staged_wall_seconds"),usage.get("wall_seconds"))
    if wall is not None:
        staged=number(usage.get("observed_host_staged_wall_seconds")) is not None
        native=number(usage.get("native_ledger_attempted_calls")) is not None
        result.update(seconds=wall,seconds_scope_id="deepseek_staged_wall" if staged else "native_active_wall" if native else "reported_wall",
                      seconds_scope="求解、证明核验、教学与教学复核及自动归档；不含后续Codex终审/共同外审。" if staged else "原生任务活动等待；准备、导出、共同外审另计。" if native else "原记录报告的墙钟耗时；具体范围见证据。")
        if staged and _known(usage.get("host_timing_scope")):
            result["seconds_scope"] = usage["host_timing_scope"]
    elif number(recovery.get("seconds")) is not None:
        result.update(seconds=number(recovery["seconds"]),seconds_scope_id="archived_generation_run",
                      seconds_scope=recovery.get("seconds_scope","原始生成run起止差值；不代表完整系统墙钟。"))
    else:
        durations=[_elapsed(s) for s in settings]
        total=_sum(v[0] for v in durations)
        if total is not None:
            count=sum(v[0] is not None for v in durations)
            derived=any(v[1] for v in durations)
            result.update(seconds=total,seconds_scope_id="saved_role_duration_sum",seconds_scope=f"已保存角色/轮次耗时之和（{count}/{len(settings)}轮可得），不是端到端墙钟。"+("部分由实际run起止时间推算。" if derived else ""))
    result["seconds_label"]=format_number(result["seconds"],2)+(" 秒" if result["seconds"] is not None else "")
    result["tokens_label"]=format_number(result["total_tokens"])+("（已观测小计）" if result["total_tokens"] is not None and result["coverage"] != "complete" else "")
    if result["total_tokens"] is None and result["estimated_total_tokens"] is not None:
        result["tokens_label"]="实际未记录；估计 "+format_number(result["estimated_total_tokens"])
    result["calls_label"]=format_number(result["calls"])+(" 个阶段" if result["calls_scope_id"]=="host_role_invocations" else " 条尝试" if result["calls_scope_id"]=="native_ledger_attempts" else " 条计量记录" if result["calls"] is not None else "")
    if result["calls"] is None and number(recovery.get("recorded_turns")) is not None:
        result["calls_label"]="上游请求数未记录；归档 "+format_number(recovery["recorded_turns"])+" 个turn"
    if record.get("runtime_observation_warning"):
        result["exclusions"].append(str(record["runtime_observation_warning"]))
    if not result["exclusions"]:
        result["exclusions"].append("未记录项不补零；费用、人工教学感受和学习效果不由Token数据推断。")
    result["evidence_files"]=list(dict.fromkeys(result["evidence_files"]))
    return result


def _safe(value):
    value=str(value).replace("\n"," ")
    value=re.sub(r"([\\`*_{}\[\]()#+|])",r"\\\1",value)
    return value.replace("<","&lt;").replace(">","&gt;")


def summary_markdown(record):
    r=runtime_summary(record)
    rows=["### 模型、耗时与Token", "", "| 项目 | 实际记录 |", "|---|---|",
          "| 模型 | "+_safe(r["model"])+" |",
          "| 配置模型 | "+_safe(r["configured_model"])+" |",
          "| 已观测耗时 | "+_safe(r["seconds_label"])+" |",
          "| 耗时范围 | "+_safe(r["seconds_scope"])+" |",
          "| "+r["input_tokens_label"]+" | "+format_number(r["input_tokens"])+" |",
          "| 输出Token | "+format_number(r["output_tokens"])+" |",
          "| "+r["cache_tokens_label"]+" | "+format_number(r["cached_input_tokens"])+" |",
          "| 总Token | "+_safe(r["tokens_label"])+" |",
          "| 用量范围 | "+_safe(r["tokens_scope"])+" |",
          "| 调用计数 | "+_safe(r["calls_label"])+"；"+_safe(r["calls_scope"])+" |", "",
          "未计量部分："+_safe("；".join(r["exclusions"])), ""]
    recovery=record.get("runtime_observations") or {}
    if isinstance(recovery,dict) and recovery.get("reading_link"):
        rows += ["归档计量恢复来源：[查看原始事件位置与SHA256](<"+recovery["reading_link"]+">)。原始record.json及数学回答未改写。", ""]
    return rows
