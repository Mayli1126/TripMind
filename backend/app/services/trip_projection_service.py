"""历史行程读取投影：兼容旧字段，不改写已保存的版本。"""

import json
from math import ceil

from ..models.schemas import TripPlan, TripRequest
from .itinerary_schedule_service import itinerary_schedule_service
from .trip_quality_service import trip_quality_service


def project_stored_plan(plan_json: str, request_json: str) -> tuple[TripRequest, TripPlan]:
    """只修整展示所需数据，不查询实时价格或重新生成整份计划。"""
    plan = TripPlan.model_validate_json(plan_json)
    duplicate_warnings = trip_quality_service.remove_duplicate_attractions(plan)
    if duplicate_warnings:
        plan.warnings = [
            warning for warning in plan.warnings
            if "在多天行程中重复出现" not in warning
        ]
        plan.warnings.extend(duplicate_warnings)
    if any(not day.schedule for day in plan.days):
        plan = itinerary_schedule_service.build(plan)
    # 使用model_construct跳过验证，避免历史行程日期过期导致验证失败
    request_data = json.loads(request_json)
    request_data.setdefault('destination_only', False)
    request = TripRequest.model_construct(**request_data)
    # 历史请求的嵌套候选地点也要恢复为模型，供提醒规则读取。
    from ..models.candidate import CandidatePlace
    request.candidate_places = [CandidatePlace.model_validate(c) for c in request_data.get('candidate_places', [])]
    from .attraction_advisory_service import enrich_advisories
    plan.warnings = list(dict.fromkeys([*plan.warnings, *enrich_advisories(plan, request)]))
    # 仅修复旧记录的展示价格，不回写或覆盖历史版本。
    from .price_reference_service import apply_price_references
    apply_price_references(plan)
    if plan.budget:
        old_total = plan.budget.total_attractions
        from .price_reference_service import attraction_cost
        ticket_total = ceil(sum(
            attraction_cost(attraction)
            for day in plan.days
            for index, attraction in enumerate(day.attractions)
            if not day.schedule or any(
                item.item_type == 'attraction' and item.related_index == index
                for item in day.schedule
            )
        ) * request.travelers)
        plan.budget.total += ticket_total - old_total
        plan.budget.total_attractions = ticket_total
        plan.budget.per_person = round(plan.budget.total / request.travelers)
        plan.budget.over_budget = request.budget_limit is not None and plan.budget.total > request.budget_limit
        note = '景点/住宿优先高德人均消费参考；未知部分未计入，非门票或酒店实时报价。'
        if note not in plan.budget.calculation_note:
            plan.budget.calculation_note += note
    return request, plan
