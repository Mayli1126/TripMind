"""PlanningGraph 状态定义。

原则：State 只存可序列化业务数据，不存客户端/回调/锁；
进度回调通过 RunnableConfig.configurable 传入，不进 State。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Optional, TypedDict

from ..models.schemas import TripPlan, TripRequest


def merge_context(left: Optional[dict], right: Optional[dict]) -> dict:
    """context 字段级合并：并行节点各自写入自己的 key。"""
    merged: dict = dict(left or {})
    merged.update(right or {})
    return merged


class PlanningState(TypedDict, total=False):
    mode: str                                  # "plan" | "revision"
    job_id: str
    request: TripRequest

    # research 阶段产物（未来拆成 4 个并行节点时用 reducer 合并）
    context: Annotated[dict, merge_context]
    warnings: Annotated[list[str], operator.add]

    planner_response: str
    plan: Optional[TripPlan]
    replan_count: int
    max_repairs: int

    # 预算降级循环：budget_confirm 节点已执行的降级重生成次数
    budget_downgrade_attempts: int

    # RevisionGraph（自然语言增量修改）
    revision_instruction: str
    base_version: Optional[int]               # 行程库当前版本号（乐观锁，None 不校验）
    plan_patch: Optional[Any]                 # PlanPatch（避免循环导入用 Any）
    patch_error: Optional[str]
    patched_plan: Optional[TripPlan]
    patch_issues: list

    intercity_cost_per_person: int
    transportation_option: Any

    result: Optional[TripPlan]
    error: Optional[str]
