"""地理基础类型（从 schemas.py 抽出，供 schemas 与 candidate 共同依赖）。

抽出目的：TripRequest 需要引用 CandidatePlace（阶段 2 候选池接入），
而 candidate.py 依赖 Location；把 Location 放进更底层的 geo 模块，
schemas -> candidate -> geo 单向依赖，避免循环导入。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Location(BaseModel):
    """地理位置"""

    longitude: float = Field(..., ge=-180, le=180, description="经度")
    latitude: float = Field(..., ge=-90, le=90, description="纬度")
