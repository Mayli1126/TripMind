"""逐段切换只改交通与派生合计，不改地点、游玩时长或日期。"""
from .road_route_service import RoadRouteResolver
from .trip_quality_service import trip_quality_service

LABELS = {"walking": "步行", "cycling": "骑行", "driving": "驾车", "transit": "公共交通"}


def select_transport(plan, request, segment_id, mode):
    result = plan.model_copy(deep=True)
    matches = [(day, item) for day in result.days for item in day.schedule
               if item.stable_id == segment_id and item.item_type in {"transport", "hotel"}]
    if len(matches) != 1 or mode not in LABELS:
        raise ValueError("交通路段不存在或方式无效，请刷新行程")
    day, item = matches[0]
    if not item.route_origin or not item.route_destination:
        raise ValueError("旧行程缺少接驳坐标，请先重新生成路线")
    leg = RoadRouteResolver(result.city).resolve(item.route_origin, item.route_destination,
                                               day.date, item.route_reference_time, mode)
    if leg.source != "amap_route" or leg.is_estimated:
        raise ValueError("未取得该方式的道路路线，保留原交通方案")
    item.selected_route_mode = mode
    item.route_leg = leg
    item.duration_minutes = leg.duration_minutes
    item.suggested_transport = LABELS[mode]
    item.description = f"{LABELS[mode]} · 约{leg.distance_km:g}公里 · 约{leg.duration_minutes}分钟"
    item.notes = leg.note + "；按参考时段查询，驾车不含停车时间，骑行不含找车时间"
    day.transport_minutes = sum(s.duration_minutes for s in day.schedule if s.item_type in {"transport", "hotel"})
    day.total_minutes = day.visit_minutes + day.transport_minutes
    # 保留原有跨城费用，仅重算本次改变的市内交通等派生预算。
    count = 2 if request.trip_type == "round_trip" else 1
    intercity = round(plan.budget.intercity_transportation / request.travelers / count) if plan.budget else 0
    result.budget = trip_quality_service.calculate_budget(result, request, intercity)
    # 保留原修改状态；查看交通方案不进入修改确认和版本保存流程。
    return result
