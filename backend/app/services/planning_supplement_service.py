"""一次补搜入口：先用核验备用池，不足时合并所有偏空日的附近搜索需求。"""
from .planning_feedback_service import build_planning_feedback
from .route_optimizer import RouteOptimizer
from .tourism_research_service import research_tourism, TourismResearchError


def nearby_backup(candidate, day, used_pois):
    """距离只控制是否顺路；旅游价值、城市和身份仍由研究核验负责。"""
    return (candidate.is_usable_for_routing() and candidate.user_choice != "excluded"
            and candidate.poi_id not in used_pois
            and any(RouteOptimizer.distance(candidate.location, a.route_exit.location if a.route_exit else a.location) <= 5 for a in day.attractions))


def supplement_candidates_once(plan, request):
    feedback = build_planning_feedback(plan, request)
    dates = {w.date for w in feedback.soft_warnings if w.type == "underfilled_day"}
    days = [d for d in plan.days if d.date in dates]
    used = {a.poi_id for d in plan.days for a in d.attractions}
    # 任一偏空日没有顺路备用就补搜；一次覆盖所有偏空日，不按景点逐个搜索。
    offers = {d.date: {c.poi_id for c in request.candidate_places if nearby_backup(c, d, used)} for d in days}
    # 同一备用点不能同时补给两天；用小规模匹配检查数量，避免“每一天都有同一个备选”误判够用。
    owners = {}
    def assign(day_date, visited):
        for poi_id in sorted(offers[day_date]):
            if poi_id in visited:
                continue
            visited.add(poi_id)
            if poi_id not in owners or assign(owners[poi_id], visited):
                owners[poi_id] = day_date
                return True
        return False
    if not days or all(assign(day_date, set()) for day_date in sorted(offers, key=lambda date: len(offers[date]))):
        return request
    needs = [dict(date=d.date, spare_minutes=feedback.available_minutes_by_date[d.date],
                  anchors=[dict(name=a.name, location=a.location.model_dump()) for a in d.attractions]) for d in days]
    from ..infrastructure.task_trace import emit
    emit("planning_supplement", "nearby_search", "started", reason=f"补充日期数:{len(days)}")
    try:
        additions, _ = research_tourism(request, supplement_context=dict(
            needs=needs, excluded_names=[a.name for d in plan.days for a in d.attractions]))
    except TourismResearchError:
        plan.warnings.append("附近补充搜索未取得可核验地点，保留现有行程并标记空余，不编造地点。")
        emit("planning_supplement", "nearby_search", "degraded")
        return request
    existing = {c.poi_id for c in request.candidate_places}
    from .planning_repair_service import record_planning_diagnostic
    record_planning_diagnostic("supplement_filter", candidates=[dict(
        name=c.canonical_name, poi_id=c.poi_id,
        reasons=(["城市不一致"] if c.city != request.city else [])
                + (["已有候选重复"] if c.poi_id in existing else [])
                + (["与已选地点重复或缺少五公里内可接驳地点"] if not any(nearby_backup(c, d, used) for d in days) else []))
        for c in additions])
    accepted = [c for c in additions if c.city == request.city and c.poi_id not in existing
                and any(nearby_backup(c, d, used) for d in days)][:max(0, 100-len(request.candidate_places))]
    emit("planning_supplement", "nearby_search", "completed", reason=f"新增核验候选数:{len(accepted)}")
    if not accepted:
        plan.warnings.append("本轮补搜未找到同城且顺路的有效新地点，未采用远距离或重复候选。")
    return request.model_copy(update={"candidate_places": [*request.candidate_places, *accepted][:100]})
