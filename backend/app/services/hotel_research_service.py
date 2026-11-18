"""固定流程酒店检索：一次高德请求，无模型、无详情追查、无自动重试。

地图只提供酒店地点，不能据此声称入住日期有库存或把人均消费当房价。
"""
from __future__ import annotations

import json
import re

from ..infrastructure.task_trace import span
from ..models.schemas import Hotel
from .amap_service import AmapService
from .day_pipeline_service import _HOTEL_NIGHTLY_ESTIMATE
from .route_optimizer import RouteOptimizer


def _city(value):
    return re.sub(r"(特别行政区|自治州|地区|市)$", "", value.strip())


def research_hotels(request, amap=None):
    """最多取五家有坐标的同城酒店；网络异常直接降级，不回到酒店 Agent。"""
    amap = amap or AmapService()
    keyword = "民宿" if request.accommodation == "民宿" else "酒店"
    nightly = _HOTEL_NIGHTLY_ESTIMATE.get(request.accommodation, 500)
    try:
        with span("hotel_research", "bounded_search"):
            # 直接接口保留坐标，避免 MCP 文本检索后再逐家查详情。
            # 请求沿用全局高德并发/限速保护，只允许一次 HTTP 尝试。
            pois = amap.search_hotel_pois(keyword, request.city)
    except Exception:
        return [], "酒店地点检索暂不可用，住宿仅按所选档次预留预算，酒店待确认。"
    hotels = []
    seen = set()
    for poi in pois:
        if not poi.id or poi.id in seen or _city(poi.city_name) != _city(request.city):
            continue
        if not (poi.type_code.startswith("10") or any(w in poi.type for w in ("酒店", "宾馆", "民宿", "旅馆", "客栈"))):
            continue
        if any(w in poi.name + poi.type for w in ("停车场", "售楼", "用品", "管理公司", "公寓住宅")):
            continue
        seen.add(poi.id)
        hotels.append(Hotel(name=poi.name, address=poi.address, location=poi.location,
            poi_id=poi.id, type=request.accommodation, estimated_cost=nightly,
            price_source="estimate", price_policy="budget_only",
            price_note="按住宿偏好预留预算，未核实酒店星级、房型、实时房价或库存"))
        if len(hotels) == 5:
            break
    warning = None if hotels else "未找到可核对的同城酒店，住宿仅预留档次预算，酒店待确认。"
    return hotels, warning


def hotels_digest(hotels):
    """给旧编排入口提供短资料，不再让模型把酒店数据扩写成长期介绍。"""
    return json.dumps([{"name": h.name, "address": h.address, "poi_id": h.poi_id,
                        "location": h.location.model_dump() if h.location else None}
                       for h in hotels], ensure_ascii=False, separators=(",", ":")) if hotels else "酒店地点暂不可用，不能编造具体酒店。"


def bind_hotels_to_plan(plan, request, candidates):
    """把已检索地点回填计划；未指定合法酒店时按景点距离选同一家，减少换宿。"""
    hotels = [Hotel.model_validate(h) for h in candidates]
    locations = [a.location for day in plan.days for a in day.attractions]
    chosen = min(hotels, key=lambda h: sum(RouteOptimizer.distance(h.location, p) for p in locations)) if hotels else None
    by_name = {h.name: h for h in hotels}
    for day in plan.days:
        selected = by_name.get(day.hotel.name) if day.hotel else None
        if selected is None:
            selected = chosen
        day.hotel = selected.model_copy(deep=True) if selected else Hotel(
            name=f"{request.city}住宿待确认", address=request.city, type=request.accommodation,
            price_policy="budget_only",
            estimated_cost=_HOTEL_NIGHTLY_ESTIMATE.get(request.accommodation, 500))
