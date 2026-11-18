"""只生成顺序与参考耗时，不从默认出发钟点推导虚假的精确时间表。"""
from hashlib import sha1

from ..models.schemas import ScheduleItem
from .route_optimizer import route_optimizer


class ItineraryScheduleService:
    def build(self, plan, route_resolver=None, day_indexes=None, *, preserve_order=False):
        from .patch_service import ensure_stable_ids
        ensure_stable_ids(plan)
        previous_location = None
        for index, day in enumerate(plan.days):
            start = previous_location or (day.hotel.location if day.hotel else None)
            if day_indexes is None or index in day_indexes:
                self._build_day(plan, day, start, route_resolver)
            previous_location = day.hotel.location if day.hotel else (
                (day.attractions[-1].route_exit.location if day.attractions[-1].route_exit
                 else day.attractions[-1].location) if day.attractions else previous_location)
        return plan

    def _build_day(self, plan, day, start, resolver):
        day.schedule_mode = "duration"
        day.meals = []
        choices = {(str(s.route_origin), str(s.route_destination)): s.model_copy(deep=True)
                   for s in day.schedule if s.selected_route_mode and s.route_origin and s.route_destination}
        day.schedule = []
        day.timing_notes = []
        # 不知道车站接驳坐标时，不把酒店坐标冒充跨城抵达位置。
        journey = plan.intercity_journey
        if journey and journey.arrival_date == day.date:
            start = None
            if journey.source == "user_confirmed":
                day.timing_notes.append(f"已填写到达时间 {journey.arrival_time}，请在抵达及接驳后开始游玩。")
        if day.preferred_start_time:
            day.timing_notes.append(f"偏好出发时间 {day.preferred_start_time}；本路线不生成精确起止时刻。")
        returning = plan.return_journey
        if returning and returning.departure_date == day.date and returning.source == "user_confirmed":
            buffer = returning.station_transfer_minutes + returning.check_in_buffer_minutes
            day.timing_notes.append(f"已填写返程时间 {returning.departure_time}，需另留约{buffer}分钟接驳和进站；本路线不保证具体钟点可行。")
        for index, attraction in enumerate(day.attractions):
            entry = attraction.route_entry.location if attraction.route_entry else attraction.location
            self._append_transport(day, start, entry, attraction.name, resolver,
                                   attraction.suitable_time, choices=choices)
            day.schedule.append(ScheduleItem(item_type="attraction", title=attraction.name,
                description=attraction.description, address=attraction.address, related_index=index,
                duration_minutes=attraction.visit_duration, stable_id=attraction.stable_id))
            if attraction.suitable_time in {"evening", "night", "day"}:
                slot = {"evening": "傍晚或晚间", "night": "晚间", "day": "白天"}[attraction.suitable_time]
                day.timing_notes.append(f"{attraction.name}建议{slot}游览。")
            if attraction.advisory and attraction.advisory.reservation_required == "true":
                day.timing_notes.append(f"{attraction.name}可能需要预约，请以实际预约时段为准。")
            start = attraction.route_exit.location if attraction.route_exit else attraction.location
        if day.hotel and day.attractions and not (returning and returning.departure_date == day.date):
            self._append_transport(day, start, day.hotel.location, day.hotel.name, resolver,
                                   day.attractions[-1].suitable_time, returning_hotel=True, choices=choices)
        day.visit_minutes = sum(s.duration_minutes for s in day.schedule if s.item_type == "attraction")
        day.transport_minutes = sum(s.duration_minutes for s in day.schedule if s.item_type in {"transport", "hotel"})
        day.total_minutes = day.visit_minutes + day.transport_minutes
        day.timing_notes = list(dict.fromkeys(day.timing_notes))

    @staticmethod
    def _append_transport(day, start, end, destination, resolver, suitable_time, *, returning_hotel=False, choices=None):
        # 公交接口需要参考出发时段；这只是查询条件，不能变成用户的行程钟点。
        reference_time = "18:00" if suitable_time in {"evening", "night"} else "10:00"
        choice = (choices or {}).get((str(start), str(end)))
        selected = choice.selected_route_mode if choice else ""
        # 显式选择优先于全局偏好；新建解析器仍复用道路缓存，保存时重新核验。
        if selected and resolver:
            from .road_route_service import RoadRouteResolver
            leg = RoadRouteResolver(resolver.city).resolve(start, end, day.date, reference_time, selected)
            if leg.source != "amap_route" or leg.is_estimated:
                raise ValueError("已选交通方式暂时无法核验，未静默替换原选择，请稍后重试")
        elif choice:
            leg = choice.route_leg.model_copy(deep=True) if choice.route_leg else None
        else:
            leg = resolver.resolve(start, end, day.date, reference_time, day.transportation) if resolver else None
        if leg:
            leg = leg.model_copy(deep=True)
            leg.note += "；未指定实际出发时间，按参考时段查询，实际耗时以出行时导航为准"
            duration = leg.duration_minutes
            mode = leg.transport_label or {"walking": "步行", "transit": "公交/地铁", "driving": "驾车", "cycling": "骑行"}[leg.mode]
            description = f"{mode} · 约{leg.distance_km:g}公里 · 约{duration}分钟"
            note = leg.note
        else:
            distance = route_optimizer.distance(start, end) if start and end else None
            duration = max(5, round(distance / 25 * 60 + 12)) if distance is not None else 30
            mode = "交通方式待导航确认"
            description = f"约{duration}分钟 · 坐标估算" if distance is not None else "接驳位置待确认，暂按约30分钟参考"
            note = "未取得道路路线，属于粗略估算；不是实时导航数据"
        title = f"返回酒店 · {destination}" if returning_hotel else f"前往 {destination}"
        identity = sha1(f"{day.date}|{len(day.schedule)}|{title}".encode()).hexdigest()[:16]
        day.schedule.append(ScheduleItem(item_type="hotel" if returning_hotel else "transport",
            title=title, description=description, duration_minutes=duration, notes=note,
            suggested_transport=mode, route_leg=leg, stable_id=identity,
            route_origin=start, route_destination=end, route_reference_time=reference_time, selected_route_mode=selected))


itinerary_schedule_service = ItineraryScheduleService()
