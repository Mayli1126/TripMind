"""规划局部修复协议：冲突用枚举表达，可修改范围由服务端决定。"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PlanningConflict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal[
        "unscheduled_place", "duplicate_place", "date_mismatch", "invalid_time", "time_overlap",
        "time_overflow", "opening_hours", "reservation_conflict", "return_conflict", "missing_data",
        "frozen_change", "locked_place_change",
        "underfilled_day", "route_backtracking",
        "single_place_review", "excessive_transit", "overloaded_day",
    ]
    date: str
    place_id: str = ""
    message: str
    # 无法证明缺少多少时间时留空，不从自然语言警告猜测分钟数。
    missing_minutes: int | None = Field(default=None, ge=0)


class PlanningRepairFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conflicts: list[PlanningConflict]
    soft_warnings: list[PlanningConflict] = Field(default_factory=list)
    editable_dates: list[str]
    frozen_days: dict[str, dict]
    locked_places: dict[str, dict]
    available_minutes_by_date: dict[str, int] = Field(default_factory=dict)
    day_metrics: dict[str, dict[str, int]] = Field(default_factory=dict)
    scope_note: str = "仅允许修改 editable_dates；其余日期必须与 frozen_days 完全一致。"

    @model_validator(mode="after")
    def consistent_scope(self):
        if len(set(self.editable_dates)) != len(self.editable_dates):
            raise ValueError("可修改日期不能重复")
        if set(self.editable_dates) & self.frozen_days.keys():
            raise ValueError("同一日期不能同时可修改和被冻结")
        if any(value < 0 for value in self.available_minutes_by_date.values()):
            raise ValueError("可用分钟数不能为负")
        return self
