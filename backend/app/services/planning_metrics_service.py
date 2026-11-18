"""Redis 与内存任务共用阶段计时，读取不会改变计时状态。"""

from copy import deepcopy
from datetime import datetime

_PHASES = {
    "starting": "initializing", "initializing": "initializing",
    "parallel_research": "research", "research_joined": "capacity",
    "candidate_alignment": "capacity", "capacity": "capacity",
    "planning": "planning", "skeleton_generation": "skeleton_generation",
    "fallback_generation": "fallback_generation", "materializing": "materializing",
    "validating": "validating", "replanning": "repair_generation",
    "repair_generation": "repair_generation", "optimizing": "optimizing",
    "tips": "tips", "finalized": "finalized", "degraded": "finalized",
    "route_confirm": "waiting_input", "budget_confirm": "waiting_input",
    "waiting_input": "waiting_input", "queued": "queued", "retrying": "retrying",
    "cancelling": "cancelling",
    "parse_patch": "planning", "patch_parsed": "validating",
}
PHASE_LABELS = {
    "queued": "排队", "initializing": "准备服务", "research": "查询旅行资料",
    "capacity": "计算可用时间", "planning": "编排行程", "skeleton_generation": "生成行程骨架",
    "fallback_generation": "重新生成完整行程", "materializing": "核对地点与坐标",
    "validating": "校验行程", "repair_generation": "调整与修复",
    "optimizing": "优化路线与预算", "tips": "整理出行建议", "finalized": "保存前整理",
    "waiting_input": "等待你的确认", "retrying": "等待自动重试",
}


def new_metrics(now):
    return {
        "queue_wait_ms": 0, "total_duration_ms": None, "agent_duration_ms": {},
        "degraded_agents": [], "stage_duration_ms": {}, "current_stage": "queued",
        "stage_started_at": now.isoformat(), "stage_visits": {"queued": 1}, "retry_count": 0,
    }


def elapsed_ms(start, end):
    if isinstance(start, str):
        start = datetime.fromisoformat(start)
    return max(0, round((end - start).total_seconds() * 1000))


def advance_stage(metrics, stage, status, now, job_id, *, agent=None):
    old = metrics.get("current_stage", "")
    if status in {"completed", "failed", "cancelled"}:
        phase = ""
    elif status == "waiting_input":
        phase = "waiting_input"
    elif agent:
        # 并行分支共用同一个资料查询阶段，不能把四个分支耗时直接相加。
        phase = "research" if old in {"queued", "initializing", "research"} else old
    else:
        phase = _PHASES.get(stage, stage)
    if phase == old:
        return
    durations = metrics.setdefault("stage_duration_ms", {})
    if old and metrics.get("stage_started_at"):
        duration = elapsed_ms(metrics["stage_started_at"], now)
        durations[old] = durations.get(old, 0) + duration
        print(f"[planning:{job_id}] {old} finished {duration}ms")
    metrics["current_stage"] = phase
    metrics["stage_started_at"] = now.isoformat() if phase else None
    metrics["queue_wait_ms"] = durations.get("queued", 0)
    if phase:
        visits = metrics.setdefault("stage_visits", {})
        visits[phase] = visits.get(phase, 0) + 1
        print(f"[planning:{job_id}] {phase} started (visit {visits[phase]})")


def project_metrics(metrics, created_at, updated_at, status, agents, now):
    result = deepcopy(metrics)
    phase = result.get("current_stage", "")
    age = elapsed_ms(result["stage_started_at"], now) if phase and result.get("stage_started_at") else 0
    result["stage_elapsed_ms"] = age
    result["elapsed_ms"] = result.get("total_duration_ms") if status in {"completed", "failed", "cancelled"} else elapsed_ms(created_at, now)
    result["idle_ms"] = elapsed_ms(updated_at, now)
    result["queue_wait_ms"] = result.get("stage_duration_ms", {}).get("queued", 0) + (age if phase == "queued" else 0)
    result["waiting_hint"] = ""
    if status == 'cancelling':
        result['waiting_hint']='已收到取消请求，正在终止本任务的执行进程，不会继续后续步骤。'
    elif phase == "waiting_input":
        result["waiting_hint"] = "正在等你的选择，确认后从当前步骤继续，不会重新查询已完成的资料。"
    elif phase == "retrying":
        result["waiting_hint"] = "上一次执行未成功，正在等待自动重试；不需要再次提交。"
    elif phase == "queued" and age >= 30_000:
        result["waiting_hint"] = "任务尚未开始执行，可能有其他任务正在处理；重复提交会增加排队。"
    elif phase == "research" and age >= 45_000:
        labels = {"attractions": "景点", "weather": "天气", "hotels": "住宿", "transportation": "交通"}
        waiting = [labels.get(key, key) for key, value in agents.items() if value.get("status") == "running"]
        result["waiting_hint"] = f"仍在等待{'、'.join(waiting) or '外部资料'}返回，已完成的资料查询不会重复执行。"
    elif phase in {"planning", "skeleton_generation", "fallback_generation", "repair_generation", "tips"} and age >= 60_000:
        result["waiting_hint"] = "这一步正在等待模型生成完整结果，尚未收到完成通知。可以查看当前状态，不必重新提交。"
    elif phase == "initializing" and age >= 45_000:
        result["waiting_hint"] = "规划服务仍在准备中，首次启动可能需要安装地图工具依赖。"
    elif phase == "materializing" and age >= 45_000:
        result["waiting_hint"] = "仍在核对景点位置，地图接口超时或限流可能延长这一阶段。"
    return result
