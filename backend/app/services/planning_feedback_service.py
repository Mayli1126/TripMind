"""生成修复反馈并检查冻结范围；纯计算，不调用模型、地图或数据库。"""
from datetime import date, timedelta

from ..models.planning_feedback import PlanningConflict, PlanningRepairFeedback
from .day_pipeline_service import build_day_capacities
from .patch_service import ensure_stable_ids
from .route_optimizer import RouteOptimizer


def _snapshot_with_ids(plan):
    snapshot = plan.model_copy(deep=True)
    for day in snapshot.days:
        for index, attraction in enumerate(day.attractions):
            if not attraction.stable_id:
                # 旧计划可能只在时间轴保存ID；名称和索引均一致时沿用，不能另造ID误报漏排。
                ids = {item.stable_id for item in day.schedule if item.item_type == "attraction"
                       and item.related_index == index and item.title == attraction.name and item.stable_id}
                if len(ids) == 1:
                    attraction.stable_id = ids.pop()
    ensure_stable_ids(snapshot)
    return snapshot


def _minutes(value: str, offset: int) -> int:
    hour, minute = map(int, value.split(":"))
    if not 0 <= hour < 24 or not 0 <= minute < 60:
        raise ValueError("时间超出范围")
    return offset * 1440 + hour * 60 + minute


def collect_planning_conflicts(plan, request) -> list[PlanningConflict]:
    """先覆盖可确定判断的日期、重复地点、时间重叠和漏排，不推断未知营业规则。"""
    conflicts = []
    seen_places = set()
    seen_dates = set()
    start = date.fromisoformat(request.start_date)
    if len(plan.days) != request.travel_days:
        conflicts.append(PlanningConflict(type="date_mismatch", date=request.start_date, message="行程天数与请求不一致"))
    for index, day in enumerate(plan.days):
        expected = (start + timedelta(days=index)).isoformat()
        if day.date != expected or day.date in seen_dates or day.day_index != index:
            conflicts.append(PlanningConflict(type="date_mismatch", date=day.date, message=f"日期或日序号不连续，期望 {expected}"))
        seen_dates.add(day.date)
        scheduled = set()
        previous_end = None
        for item in day.schedule:
            # 顺序模式只检查地点及耗时，不把空钟点当格式错误；历史时间表仍保留旧校验。
            if day.schedule_mode == "timed":
                try:
                    begins = _minutes(item.start_time, item.start_day_offset)
                    ends = _minutes(item.end_time, item.end_day_offset)
                    if ends < begins or (ends == begins and item.item_type not in {"transport", "hotel"}):
                        raise ValueError("结束时间不晚于开始时间")
                except (ValueError, TypeError):
                    conflicts.append(PlanningConflict(type="invalid_time", date=day.date, place_id=item.stable_id, message=f"「{item.title}」时间格式或起止范围无效"))
                    continue
                if previous_end is not None and begins < previous_end:
                    conflicts.append(PlanningConflict(type="time_overlap", date=day.date, place_id=item.stable_id, message=f"「{item.title}」与前序安排重叠"))
                previous_end = max(previous_end or ends, ends)
            if item.item_type == "attraction" and item.related_index is not None:
                if 0 <= item.related_index < len(day.attractions):
                    attraction = day.attractions[item.related_index]
                    # 引用索引还须指向同一身份，避免旧时间轴误配到重排后的景点。
                    if item.stable_id == attraction.stable_id or (not item.stable_id and item.title == attraction.name):
                        scheduled.add(attraction.stable_id)
        for attraction in day.attractions:
            identity = attraction.poi_id or attraction.stable_id
            if identity in seen_places:
                conflicts.append(PlanningConflict(type="duplicate_place", date=day.date, place_id=attraction.stable_id, message=f"「{attraction.name}」重复安排"))
            seen_places.add(identity)
            if attraction.stable_id not in scheduled:
                conflicts.append(PlanningConflict(type="unscheduled_place", date=day.date, place_id=attraction.stable_id, message=f"「{attraction.name}」未进入游玩顺序"))
    return conflicts


def build_planning_feedback(plan, request, *, editable_dates=None, locked_place_ids=()):
    """复制后补ID，避免只读诊断改变原计划；冻结快照由代码生成，不能由模型填写。"""
    snapshot = _snapshot_with_ids(plan)
    days = {day.date: day for day in snapshot.days}
    if len(days) != len(snapshot.days):
        raise ValueError("日期重复时无法建立冻结快照")
    conflicts = collect_planning_conflicts(snapshot, request)
    editable = list(editable_dates) if editable_dates is not None else sorted({c.date for c in conflicts} & days.keys())
    if set(editable) - days.keys():
        raise ValueError("可修改范围包含不存在的日期")
    places = {a.stable_id: dict(date=d.date, position=i, attraction=a.model_dump(mode="json"))
              for d in snapshot.days for i, a in enumerate(d.attractions)}
    if len(places) != sum(len(d.attractions) for d in snapshot.days):
        raise ValueError("地点稳定ID重复，无法建立冻结快照")
    if set(locked_place_ids) - places.keys():
        raise ValueError("锁定地点不存在")
    capacities = {c.date: c.available_minutes for c in build_day_capacities(request)}
    available = {d.date: max(0, capacities.get(d.date, 0) - sum(
        s.duration_minutes for s in d.schedule if s.item_type in {"attraction", "transport", "hotel"})) for d in snapshot.days}
    soft_warnings = []
    day_metrics = {}
    for day in snapshot.days:
        visit_minutes = sum(s.duration_minutes for s in day.schedule if s.item_type == "attraction")
        transit_minutes = sum(s.duration_minutes for s in day.schedule if s.item_type in {"transport", "hotel"})
        day_metrics[day.date] = dict(visit_minutes=visit_minutes, transit_minutes=transit_minutes,
                                    spare_minutes=available[day.date])
        scheduled_visits = [s for s in day.schedule if s.item_type == "attraction"]
        if visit_minutes + transit_minutes > capacities.get(day.date, 480):
            soft_warnings.append(PlanningConflict(type="overloaded_day", date=day.date,
                message=f"当天游玩加交通约{visit_minutes + transit_minutes}分钟，超过当前节奏参考总量，安排偏紧；这是时长提醒，不是钟点冲突或删点指令。"))
        # 普通节奏下不足半天的安排提示复核；轻松节奏、短抵达日不靠数量凑满。
        # 明确要求留白时也尊重用户意图，不因为选择了普通节奏就要求填满。
        wants_free_time = any(text in (request.free_text_input or "") for text in
                              ("保留自由活动", "留出自由时间", "安排自由活动", "不要排太满", "休息为主"))
        partial_day = ((day.day_index == 0 and request.arrival_time)
                       or (day.day_index == request.travel_days - 1 and request.return_departure_time))
        # 数量与内容一起看：普通完整日只有一个经典地点，即使写了理由也须比较附近组合。
        sparse = (visit_minutes < 240 or (len(day.attractions) == 1
                  and day.attractions[0].visit_mode == "classic" and visit_minutes <= 300))
        if request.pace != "relaxed" and not wants_free_time and not partial_day and scheduled_visits and sparse:
            soft_warnings.append(PlanningConflict(type="underfilled_day", date=day.date,
                message=f"当天游玩约{visit_minutes}分钟、通勤约{transit_minutes}分钟，游玩偏少；可确认是否保留自由活动或选择同片区备选。"))
        if transit_minutes >= 120 and transit_minutes >= visit_minutes * .6:
            soft_warnings.append(PlanningConflict(type="excessive_transit", date=day.date,
                message="当天交通占游玩时长的比例较高，建议比较顺路地点及交通方式；耗时仍需导航复核。"))
        # 五小时是经典游复核线，不是静默截断上限；填写一个理由也不能绕过复核。
        for attraction in day.attractions:
            if (attraction.visit_mode == "classic" and attraction.visit_duration > 300
                    and attraction.duration_source != "user_confirmed"):
                soft_warnings.append(PlanningConflict(type="single_place_review", date=day.date,
                    place_id=attraction.stable_id,
                    message=f"「{attraction.name}」经典游估时超过五小时，需核对具体内部内容，排除餐饮和外部交通，比较附近晚间街区组合；确需长时游览应说明依据。"))
        if (request.pace != "relaxed" and not wants_free_time and len(day.attractions) == 1
                and day.attractions[0].visit_mode == "classic" and day.attractions[0].visit_duration >= 240
                and day.attractions[0].visit_duration <= 300
                and not day.single_place_reason):
            soft_warnings.append(PlanningConflict(type="single_place_review", date=day.date,
                place_id=day.attractions[0].stable_id, message="经典体验仅安排一个长时间地点，缺少单地点理由，需比较附近候选组合。"))
        visits = day.attractions
        for left, middle, right in zip(visits, visits[1:], visits[2:]):
            nearby = RouteOptimizer.distance(left.location, right.location)
            outward = RouteOptimizer.distance(left.location, middle.location)
            if nearby <= 2 and outward >= 5:
                # 坐标只提示“近处→远处→近处”；预约/开放时间可能使折返合理，不强制重排。
                soft_warnings.append(PlanningConflict(type="route_backtracking", date=day.date,
                    place_id=middle.stable_id,
                    message=f"「{left.name}→{middle.name}→{right.name}」可能折返；依据坐标估计，需结合道路、预约和开放时段复核。"))
                break
    return PlanningRepairFeedback(conflicts=conflicts, editable_dates=editable,
        soft_warnings=soft_warnings,
        day_metrics=day_metrics,
        frozen_days={key: day.model_dump(mode="json") for key, day in days.items() if key not in editable},
        locked_places={key: places[key] for key in locked_place_ids}, available_minutes_by_date=available,
        scope_note="剩余分钟是容量估算，不代表连续可插入时段。跨天移动或酒店接驳需先由服务端显式扩展 editable_dates，再生成新快照。")


def validate_repair_scope(original, repaired, feedback: PlanningRepairFeedback) -> list[PlanningConflict]:
    """修复结果先过范围检查，再做可行性检查；模型声称遵守范围不能代替比较。"""
    issues = []
    repaired = _snapshot_with_ids(repaired)
    if original.model_dump(mode="json", exclude={"days"}) != repaired.model_dump(mode="json", exclude={"days"}):
        # 此协议仅授权修改日期内容；预算等派生汇总留给服务端另行重算。
        issues.append(PlanningConflict(type="frozen_change", date=original.start_date, message="修复越界更改了行程全局信息"))
    expected_dates = set(feedback.editable_dates) | feedback.frozen_days.keys()
    dates = [d.date for d in repaired.days]
    if len(dates) != len(set(dates)) or set(dates) != expected_dates:
        issues.append(PlanningConflict(type="frozen_change", date=original.start_date, message="修复增删或重复了行程日期"))
    by_date = {d.date: d for d in repaired.days}
    for day_date, frozen in feedback.frozen_days.items():
        day = by_date.get(day_date)
        if day is None or day.model_dump(mode="json") != frozen:
            issues.append(PlanningConflict(type="frozen_change", date=day_date, message="修复改动了冻结日期的内容"))
    for place_id, frozen in feedback.locked_places.items():
        found = [dict(date=d.date, position=i, attraction=a.model_dump(mode="json"))
                 for d in repaired.days for i, a in enumerate(d.attractions) if a.stable_id == place_id]
        if found != [frozen]:
            issues.append(PlanningConflict(type="locked_place_change", date=frozen["date"], place_id=place_id, message="修复删除、移动或改写了锁定地点"))
    return issues
