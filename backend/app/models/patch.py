"""结构化增量修改契约（产品化计划 6.3 节）。

自然语言修改由 LLM 转译为 PlanPatch，由确定性代码应用；
Patch 校验失败时保留原版本，不产生半成品版本。
"""

from __future__ import annotations

from typing import Annotated, Literal, Optional

from pydantic import BaseModel, Field, model_validator

PatchOperationType = Literal[
    "add_place",        # 新增地点（需要 replacement/candidate 信息 + day_index + position）
    "remove_place",     # 删除地点（target_stable_id）
    "replace_place",    # 替换地点（target_stable_id + replacement）
    "move_place",       # 跨日/日内移动（target_stable_id + day_index + position）
    "reorder_day",      # 当日重排（day_index + ordered_stable_ids）
    "change_duration",  # 调整停留时长（target_stable_id + duration_minutes）
    "update_description", # 修改景点介绍，不改动地点和坐标
    "change_start_time",# 调整出发时间（day_index + start_time）
    "change_pace",      # 调整节奏（day_index + pace）
    "update_transport", # 更新交通（day_index 或 target_stable_id + transport）
]

# 修改后需要重新查询的数据：决定 revision 只补哪些 research
RequiredResearch = Literal["poi", "route", "weather", "hours", "budget"]

DayPace = Literal["relaxed", "standard", "intense"]


class PatchOperation(BaseModel):
    """单个修改操作。

    字段全部可选，由 op 类型决定哪些必填（见 model_validator），
    未填字段在 apply 阶段由确定性代码补全或拒绝。
    """

    op: PatchOperationType
    target_stable_id: Optional[str] = Field(default=None, description="被操作的活动 stable_id")
    day_index: Optional[int] = Field(default=None, ge=0, description="目标日（从 0 开始）")
    position: Optional[int] = Field(default=None, ge=0, description="目标日内插入位置")
    ordered_stable_ids: Optional[list[str]] = Field(default=None, description="reorder_day 时的完整顺序")
    replacement_name: Optional[str] = Field(default=None, description="add/replace 时的地点名称")
    replacement_candidate_id: Optional[str] = Field(default=None, description="add/replace 时引用候选池")
    duration_minutes: Optional[int] = Field(default=None, ge=15, le=720)
    description: Optional[str] = Field(default=None, max_length=5000)
    start_time: Optional[str] = Field(default=None, description="HH:MM")
    pace: Optional[DayPace] = None
    transport: Optional[str] = None
    reason: str = Field(default="", description="该操作的简短原因，用于差异预览展示")

    @model_validator(mode="after")
    def _check_required_fields(self) -> "PatchOperation":
        needs_target = self.op in {"remove_place", "replace_place", "move_place", "change_duration", "update_description"}
        if needs_target and not self.target_stable_id:
            raise ValueError(f"op={self.op} 需要 target_stable_id")
        if self.op == "change_duration" and self.duration_minutes is None:
            raise ValueError("调整时长需要 duration_minutes")
        if self.op == "update_description" and self.description is None:
            raise ValueError("修改介绍需要 description")
        if self.op == "reorder_day" and (self.day_index is None or not self.ordered_stable_ids):
            raise ValueError("op=reorder_day 需要 day_index 与 ordered_stable_ids")
        if self.op in {"add_place", "replace_place"} and not (
            self.replacement_candidate_id or self.replacement_name
        ):
            raise ValueError(f"op={self.op} 需要 replacement_candidate_id 或 replacement_name")
        if self.op in {"change_start_time"} and (self.day_index is None or not self.start_time):
            raise ValueError("op=change_start_time 需要 day_index 与 start_time")
        if self.op == "change_pace" and (self.day_index is None or not self.pace):
            raise ValueError("op=change_pace 需要 day_index 与 pace")
        if self.op == "update_transport" and not self.transport:
            raise ValueError("op=update_transport 需要 transport")
        return self


class PlanPatch(BaseModel):
    """一次自然语言修改的结构化表达。

    - base_version: 基于哪个版本修改，apply 时做乐观校验，不匹配即拒绝；
    - affected_day_indexes: 只允许触碰这些天，其余天的 stable_id
      与内容必须原样保留（preserved_stable_ids 是硬校验）；
    - 旅行范围变化（如换城市）不属于 Patch，走完整重规划。
    """

    base_version: int = Field(..., ge=1)
    operations: list[PatchOperation] = Field(..., min_length=1)
    affected_day_indexes: list[Annotated[int, Field(ge=0)]] = Field(..., min_length=1)
    required_research: list[RequiredResearch] = Field(default_factory=list)
    preserved_stable_ids: list[str] = Field(default_factory=list, description="不允许被触碰的活动 ID（第 1 节完成标准）")
    explanation: str = Field(..., description="面向用户的修改摘要，差异预览的标题")


class ManualRevisionRequest(BaseModel):
    """手动编辑只提交操作，原行程与受影响日期由服务端获取。"""

    base_version: int = Field(..., ge=1)
    operations: list[PatchOperation] = Field(..., min_length=1, max_length=100)

    @model_validator(mode="after")
    def _manual_operations_only(self):
        allowed = {"remove_place", "move_place", "reorder_day", "change_duration", "update_description"}
        if any(op.op not in allowed for op in self.operations):
            raise ValueError("手动编辑仅支持删除、移动、排序、时长和介绍修改")
        return self
