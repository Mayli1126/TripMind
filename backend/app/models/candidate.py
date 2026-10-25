"""候选 POI 中间层数据契约（产品化计划 6.1 节）。

三种输入管线（快速规划 / 多攻略融合 / 平台模板适配）统一产出
CandidatePlace 候选池，共享 PlanningGraph 只消费候选池，
禁止各入口自带行程生成 Prompt。
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from .geo import Location

# 候选在用户侧的选择状态：必去 / 想去 / 可选 / 排除
UserChoice = Literal["required", "preferred", "optional", "excluded"]

# 事实核验状态：已校验（地图命中）/ 部分校验 / 未校验
FactStatus = Literal["verified", "partially_verified", "unverified"]

# 候选类别：热门打卡街道等作为正式类别，不再全部归为"景点"
CandidateCategory = Literal[
    "attraction",       # 景观/观光
    "museum",           # 博物馆/展馆
    "street",           # 商业街/打卡街
    "natural",          # 自然风光
    "food",             # 餐饮
    "night",            # 夜市/夜景
    "leisure",          # 休闲/乐园
    "transport_hub",    # 车站/机场接驳
    "hotel",
]


class CandidatePlace(BaseModel):
    """候选地点：进入规划池前的统一中间形态。

    设计要点：
    - 社交攻略只提供 recommendation_reasons（推荐理由），
      名称/地址/坐标一律以地图数据（poi_id + location）为准；
    - 无法在地图确认的地点 fact_status 必须是 unverified，
      不得伪造坐标参与路线计算；
    - source_refs 可追溯到具体攻略、模板或地图检索。
    """

    candidate_id: str = Field(..., description="候选池内唯一 ID，与最终 stable_id 解耦")
    canonical_name: str = Field(..., description="归一化后的标准名称（以地图数据为准）")
    aliases: list[str] = Field(default_factory=list, description="各来源中的别名/简称")
    city: str
    poi_id: Optional[str] = Field(default=None, description="高德 POI ID，命中即视为 verified")
    location: Optional[Location] = Field(default=None, description="无坐标时必须保持 None")
    category: CandidateCategory = "attraction"
    suggested_duration_minutes: int = Field(default=90, ge=15, le=720)
    source_refs: list[str] = Field(default_factory=list, description="来源引用: guide:{id} / template:{slug} / amap:{poi_id}")
    recommendation_reasons: list[str] = Field(default_factory=list, description="为什么推荐（来自攻略或偏好匹配）")
    warnings: list[str] = Field(default_factory=list, description="已知风险: 需预约/闭馆日/体力强度等")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    user_choice: UserChoice = "preferred"
    fact_status: FactStatus = "unverified"

    def is_usable_for_routing(self) -> bool:
        """只有 verified 的候选才能参与真实路线与通勤计算。"""
        return self.location is not None and self.fact_status == "verified"
