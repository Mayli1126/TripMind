"""约束校验结构化输出（产品化计划 8.4 节）。

Validator 不再输出一段自然语言，而是输出 PlanIssue 列表，
让 repair 条件边与前端都能按 code/severity 精确处理。
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

IssueSeverity = Literal["error", "warning", "info"]

# 首批错误码：error 会触发修复循环，warning 只提示，info 仅展示
PLAN_ISSUE_CODES = {
    "DUPLICATE_POI",
    "OUTSIDE_OPENING_HOURS",
    "INSUFFICIENT_TRANSFER_TIME",
    "INTERCITY_TIME_CONFLICT",
    "RETURN_TIME_CONFLICT",
    "DAILY_OVERLOAD",
    "EXCESSIVE_COMMUTE",
    "MEAL_WINDOW_MISSING",
    "BUDGET_EXCEEDED",
    "UNVERIFIED_REQUIRED_POI",
    "WEATHER_MISMATCH",
    "BASE_VERSION_CONFLICT",
}


class PlanIssue(BaseModel):
    """单个结构化校验问题。

    - repairable=True 时 repair 条件边会尝试自动修复；
    - suggested_action 给出可执行的修复方向（如"将 D2 的 X 移至 D3 上午"）；
    - BUDGET_EXCEEDED 额外要求 issue 携带超限来源，
      禁止静默删掉用户必去点来满足预算。
    """

    code: str = Field(..., description=f"错误码，取值范围: {sorted(PLAN_ISSUE_CODES)}")
    severity: IssueSeverity
    day_index: Optional[int] = Field(default=None, ge=0, description="问题所在日，跨日问题时为 None")
    item_ids: list[str] = Field(default_factory=list, description="相关 stable_id / candidate_id")
    message: str = Field(..., description="面向用户的中文说明")
    repairable: bool = False
    suggested_action: Optional[str] = Field(default=None, description="修复方向说明，repairable 时应填写")
