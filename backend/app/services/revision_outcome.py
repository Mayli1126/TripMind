"""明确的行程修改结果，与网络传输或任务完成状态分开。"""
from ..models.schemas import TripPlan


def revision_errors(plan, request):
    """修改是否有效不能套用初次生成每天三个景点的目标。"""
    from datetime import date, timedelta
    errors = []
    if plan.is_fallback:
        errors.append("模型返回了降级模板")
    if plan.city != request.city or plan.start_date != request.start_date or plan.end_date != request.end_date:
        errors.append("目的地或日期与原需求不一致")
    if len(plan.days) != request.travel_days:
        errors.append("行程天数不一致")
    keys = []
    for index, day in enumerate(plan.days):
        expected = (date.fromisoformat(request.start_date) + timedelta(days=index)).isoformat()
        if day.day_index != index or day.date != expected:
            errors.append(f"第{index + 1}天日期或序号不一致")
        if not day.attractions:
            errors.append(f"第{index + 1}天没有景点")
        keys.extend(a.poi_id or "".join(a.name.lower().split()) for a in day.attractions)
    if len(keys) != len(set(keys)):
        errors.append("存在重复景点")
    return errors


def revision_context(plan):
    """保留每天的地点标识、约束和坐标，移除图片与时间轴等无关内容。"""
    import json
    data = plan.model_dump(mode="json", exclude_none=True, exclude={
        "warnings", "data_sources", "optimization_summary", "revision_diff",
        "revision_status", "revision_message",
    })
    data["overall_suggestions"] = data.get("overall_suggestions", "")[:1000]
    for day in data["days"]:
        day.pop("schedule", None)
        for attraction in day["attractions"]:
            attraction.pop("image_url", None)
            attraction.pop("photos", None)
            if "description" in attraction:
                attraction["description"] = attraction["description"][:300]
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def has_plan_changes(before: TripPlan, after: TripPlan) -> bool:
    def content(plan):
        value = plan.model_dump(mode="json", exclude={
            "warnings", "revision_diff", "revision_status", "revision_message",
            "optimization_summary", "planning_iterations", "data_sources", "data_quality", "budget",
        })
        for day in value["days"]:
            # 交通选择属于查看状态，不参与行程版本变化判断。
            # 顺序/时长本身仍参与比较，汇总和提醒属于派生结果，不冒充用户修改。
            for key in ("schedule", "schedule_mode", "visit_minutes", "transport_minutes", "total_minutes", "timing_notes"):
                day.pop(key, None)
            for attraction in day["attractions"]:
                for key in ("route_entry", "route_exit", "route_anchors_checked"):
                    attraction.pop(key, None)
        return value
    return content(before) != content(after)


def reject_revision(plan: TripPlan, message: str, *, unchanged: bool = False) -> TripPlan:
    result = plan.model_copy(deep=True)
    result.revision_status = "unchanged" if unchanged else "rejected"
    result.revision_message = message
    result.revision_diff = []
    result.warnings = list(dict.fromkeys([*result.warnings, message]))
    return result


def mark_revision_ready(plan: TripPlan) -> None:
    plan.revision_status = "draft_ready"
    plan.revision_message = "修改草稿已生成，请确认后保存。"
    # 历史失败保留在原版本或原对话中，不能充当本次修改结果。
    prefixes = ("本次自然语言修改未通过结构校验", "本次修改未通过校验",
                "修改失败，已保留原计划", "本次修改未产生实际变化", "本次指令未改变行程")
    plan.warnings = [warning for warning in plan.warnings if not warning.startswith(prefixes)]
