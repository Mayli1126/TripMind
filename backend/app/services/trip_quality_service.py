"""旅行计划的确定性预算计算与可执行性检查。"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import re
from ..models.schemas import (
    Budget, DataSource, IntercityJourney, TransportationOption, TripPlan, TripRequest,
)
from .route_optimizer import route_optimizer
from .itinerary_schedule_service import itinerary_schedule_service
from .physical_strength_service import physical_strength_service
from .meal_location_service import meal_location_service
from .transportation_mode_service import transportation_mode_service


class TripQualityService:
    """把金额计算和基础约束从 LLM 输出中剥离出来。"""

    LOCAL_TRANSPORT_PER_PERSON_PER_DAY = 50

    def finalize(
        self,
        plan: TripPlan,
        request: TripRequest,
        intercity_cost_per_person: int = 0,
        transportation_option: TransportationOption | None = None,
        bind_meals: bool = True,
        resolve_routes: bool = False,
    ) -> TripPlan:
        from .price_reference_service import apply_price_references
        apply_price_references(plan)
        if resolve_routes:
            from .price_reference_service import enrich_amap_costs
            enrich_amap_costs(plan)
            from .price_reference_service import enrich_llm_estimates
            enrich_llm_estimates(plan, request)
        duplicate_warnings = self.remove_duplicate_attractions(plan)
        weather_warnings = self._apply_weather_rebalance(plan)
        plan.intercity_journey = self._build_intercity_journey(
            request, transportation_option, plan.intercity_journey
        )
        plan.return_journey = self._build_return_journey(
            request, transportation_option, plan.return_journey
        )
        transport_warnings = self._transport_warnings(plan, request)
        capacity_warnings = self._rebalance_transport_days(plan, request)
        
        # 体力曲线建模：优化每日景点强度分配
        plan = physical_strength_service.optimize_strength_distribution(plan, request)
        
        # 用餐地点地理绑定（离线评测/测试传 bind_meals=False 保持确定性）
        if bind_meals:
            plan = meal_location_service.bind_meal_locations(plan)
        
        # 路线优化（考虑餐食位置）
        plan = route_optimizer.optimize(plan)
        
        # 交通方式差异化
        plan = transportation_mode_service.optimize_daily_transport(plan)
        
        plan = self.rebuild_schedule(plan, request, resolve_routes=resolve_routes)
        if resolve_routes:
            live_count = sum(item.route_leg is not None and not item.route_leg.is_estimated
                             for day in plan.days for item in day.schedule)
            plan.data_sources = [s for s in plan.data_sources if s.name != "市内逐段道路规划"]
            plan.data_sources.append(DataSource(name="市内逐段道路规划", category="transportation",
                source_type="live" if live_count else "estimated",
                note=f"{live_count}段使用高德道路预期耗时，其余缺坐标或查询失败按估算；价格均为参考。地图连线及访问顺序仍为坐标示意，并非道路导航。"))
        warnings = list(dict.fromkeys([
            *plan.warnings, *duplicate_warnings, *weather_warnings,
            *transport_warnings, *capacity_warnings,
            *self.validate(plan, request)
        ]))
        plan.budget = self.calculate_budget(plan, request, intercity_cost_per_person)

        if plan.budget.over_budget:
            warnings.append(
                f"当前预估总费用约 ¥{plan.budget.total}，超出预算上限 ¥{request.budget_limit}。"
            )

        if not plan.data_sources:
            plan.data_sources = self.default_sources(plan.is_fallback)
        if not plan.is_fallback and not any(
            source.category == "transportation" for source in plan.data_sources
        ):
            plan.data_sources.append(DataSource(
                name="跨城交通参考价",
                category="transportation",
                source_type="estimated",
                note="未接入官方票务库存，班次和价格仅作预算参考。",
            ))

        plan.warnings = list(dict.fromkeys(warnings))
        from .attraction_advisory_service import enrich_advisories
        plan.warnings = list(dict.fromkeys([*plan.warnings, *enrich_advisories(plan, request)]))
        plan.data_quality = "fallback" if plan.is_fallback else "mixed"
        return plan

    def rebuild_schedule(
        self, plan: TripPlan, request: TripRequest, *,
        resolve_routes: bool = False, day_indexes: set[int] | None = None,
    ) -> TripPlan:
        """统一道路耗时与时间线入口；指定日期时不改动其他天。"""
        from .road_route_service import RoadRouteResolver

        resolver = (
            RoadRouteResolver(plan.city, preference="自驾" if request.transportation == "自驾" else "")
            if resolve_routes else None
        )
        return itinerary_schedule_service.build(plan, route_resolver=resolver, day_indexes=day_indexes)

    def finalize_revision(
        self, plan: TripPlan, request: TripRequest, *,
        changed_days: set[int] | None = None, intercity_cost_per_person: int = 0,
    ) -> TripPlan:
        """增量收尾只重建受影响日和预算，不重复全局景点重排。"""
        plan = self.rebuild_schedule(plan, request, resolve_routes=True, day_indexes=changed_days or None)
        plan.budget = self.calculate_budget(plan, request, intercity_cost_per_person)
        return plan

    @staticmethod
    def _build_intercity_journey(
        request: TripRequest,
        option: TransportationOption | None,
        existing: IntercityJourney | None,
    ) -> IntercityJourney | None:
        if request.destination_only or request.origin_city.strip() == request.city.strip():
            return None
        if not (request.departure_time and request.arrival_time) and option is None:
            if existing and existing.source in {"user_confirmed", "live", "static"}:
                return existing
        mode = request.transportation
        transfer, check_in, after = TripQualityService._journey_buffers(mode)
        confirmed = bool(request.departure_time and request.arrival_time)
        departure_raw = request.departure_time or (option.departure_time if option else None)
        arrival_raw = request.arrival_time or (option.arrival_time if option else None)
        departure, _ = TripQualityService._parse_transport_time(departure_raw)
        arrival, explicit_offset = TripQualityService._parse_transport_time(arrival_raw)
        if not departure or not arrival:
            # 未获取到班次时仍必须占用一段保守的跨城时间，不能让首日从早晨
            # 直接在目的地开始。这里只用于可执行性约束，并明确标为估算值。
            departure, arrival = {
                "plane": ("08:00", "11:00"),
                "high_speed_train": ("08:00", "12:00"),
                "train": ("08:00", "14:00"),
                "bus": ("08:00", "14:00"),
                "自驾": ("08:00", "13:00"),
                "公共交通": ("08:00", "12:00"),
            }.get(mode, ("08:00", "12:00"))
            explicit_offset = 0
        dep_minutes = TripQualityService._minutes(departure)
        arr_minutes = TripQualityService._minutes(arrival)
        arrival_offset = explicit_offset or int(arr_minutes <= dep_minutes)
        departure_date = datetime.strptime(request.start_date, "%Y-%m-%d").date()
        arrival_date = departure_date + timedelta(days=arrival_offset)
        if arrival_date > datetime.strptime(request.end_date, "%Y-%m-%d").date():
            return existing
        return IntercityJourney(
            mode=mode,
            leg="outbound",
            name=option.name if option else ({
                "plane": "航班", "high_speed_train": "高铁", "train": "火车",
                "bus": "长途汽车", "自驾": "自驾", "公共交通": "跨城公共交通",
            }.get(mode, "跨城交通")),
            departure_date=departure_date.isoformat(),
            arrival_date=arrival_date.isoformat(),
            departure_time=departure,
            arrival_time=arrival,
            departure_station=option.departure_station if option and option.departure_station else "出发站待确认",
            arrival_station=option.arrival_station if option and option.arrival_station else "到达站待确认",
            station_transfer_minutes=transfer,
            check_in_buffer_minutes=check_in,
            arrival_transfer_minutes=after,
            is_estimated=not confirmed,
            source="user_confirmed" if confirmed else (option.source if option else "estimated"),
        )

    @staticmethod
    def _build_return_journey(
        request: TripRequest,
        outbound_option: TransportationOption | None,
        existing: IntercityJourney | None,
    ) -> IntercityJourney | None:
        if request.destination_only or request.trip_type == "one_way" or request.origin_city.strip() == request.city.strip():
            return None
        if not (request.return_departure_time and request.return_arrival_time):
            return existing if existing and existing.source == "user_confirmed" else None
        departure, _ = TripQualityService._parse_transport_time(request.return_departure_time)
        arrival, explicit_offset = TripQualityService._parse_transport_time(request.return_arrival_time)
        if not departure or not arrival:
            return existing
        dep_minutes = TripQualityService._minutes(departure)
        arr_minutes = TripQualityService._minutes(arrival)
        arrival_offset = explicit_offset or int(arr_minutes <= dep_minutes)
        departure_date = datetime.strptime(request.end_date, "%Y-%m-%d").date()
        arrival_date = departure_date + timedelta(days=arrival_offset)
        mode = request.transportation
        transfer, check_in, after = TripQualityService._journey_buffers(mode)
        return IntercityJourney(
            mode=mode, leg="return",
            name=outbound_option.name if outbound_option else "返程交通",
            departure_date=departure_date.isoformat(), arrival_date=arrival_date.isoformat(),
            departure_time=departure, arrival_time=arrival,
            departure_station=(outbound_option.arrival_station if outbound_option and outbound_option.arrival_station else "返程出发站待确认"),
            arrival_station=(outbound_option.departure_station if outbound_option and outbound_option.departure_station else "返程到达站待确认"),
            station_transfer_minutes=transfer, check_in_buffer_minutes=check_in,
            arrival_transfer_minutes=after, is_estimated=False, source="user_confirmed",
        )

    @staticmethod
    def _journey_buffers(mode: str) -> tuple[int, int, int]:
        return {
            "plane": (60, 90, 75), "high_speed_train": (45, 45, 45),
            "train": (45, 45, 45), "bus": (40, 30, 40),
            "自驾": (0, 0, 30), "公共交通": (45, 45, 45),
        }.get(mode, (45, 45, 45))

    @staticmethod
    def _parse_transport_time(value: str | None) -> tuple[str | None, int]:
        if not value:
            return None, 0
        match = re.fullmatch(r"\s*(次日)?(\d{1,2}):(\d{2})\s*", value)
        if not match:
            return None, 0
        hour, minute = int(match.group(2)), int(match.group(3))
        if hour > 23 or minute > 59:
            return None, 0
        return f"{hour:02d}:{minute:02d}", int(bool(match.group(1)))

    @staticmethod
    def _minutes(value: str) -> int:
        hour, minute = map(int, value.split(":"))
        return hour * 60 + minute

    @staticmethod
    def _transport_warnings(plan: TripPlan, request: TripRequest) -> list[str]:
        if request.destination_only:
            return []
        warnings = []
        if request.origin_city != request.city and not plan.intercity_journey:
            warnings.append("未获得可信的去程班次，首日未自动占用跨城交通时间；请填写已购票时刻后重新规划。")
        elif plan.intercity_journey and plan.intercity_journey.source == "estimated":
            warnings.append(
                "未填写或未查到可靠去程班次，首日已按保守参考路程预留时间；"
                "购票后请补充实际出发和到达时刻。"
            )
        if request.trip_type == "round_trip" and not plan.return_journey:
            warnings.append("尚未填写返程班次，末日计划未锁定返程时间，请购票后补充。")
        return warnings

    def _rebalance_transport_days(self, plan: TripPlan, request: TripRequest) -> list[str]:
        """按每日可用时间、实际位置和跨日住宿衔接重新平衡景点。"""
        if not plan.days:
            return []
        capacities = {day.date: 10 * 60 for day in plan.days}
        if plan.intercity_journey:
            outbound = plan.intercity_journey
            if outbound.departure_date != outbound.arrival_date and outbound.departure_date in capacities:
                capacities[outbound.departure_date] = 0
            if outbound.arrival_date in capacities:
                ready = self._minutes(outbound.arrival_time) + outbound.arrival_transfer_minutes
                capacities[outbound.arrival_date] = min(capacities[outbound.arrival_date], max(0, 20 * 60 - ready))
        if plan.return_journey and plan.return_journey.departure_date in capacities:
            journey = plan.return_journey
            station_leave = self._minutes(journey.departure_time) - journey.check_in_buffer_minutes - journey.station_transfer_minutes
            capacities[journey.departure_date] = min(
                capacities[journey.departure_date], max(0, station_leave - 8 * 60 - 30)
            )

        overflow = []
        closed_moved = 0
        for day_index, day in enumerate(plan.days):
            available = capacities[day.date]
            keep = []
            for attraction in day.attractions:
                date_weekday = datetime.strptime(day.date, "%Y-%m-%d").date().weekday()
                if date_weekday in attraction.closed_weekdays:
                    overflow.append(attraction)
                    closed_moved += 1
                    continue
                candidate = [*keep, attraction]
                must_keep_minimum = len(keep) < self._min_attractions(request.pace)
                if len(keep) < self._max_attractions(request.pace) and (
                    must_keep_minimum
                    or self._estimated_day_load(plan, day_index, candidate) <= available
                ):
                    keep.append(attraction)
                else:
                    overflow.append(attraction)
            day.attractions = keep

        shifted = 0
        remaining = []
        for attraction in overflow:
            candidates = []
            for day_index, day in enumerate(plan.days):
                weekday = datetime.strptime(day.date, "%Y-%m-%d").date().weekday()
                if weekday in attraction.closed_weekdays:
                    continue
                existing_keys = {item.poi_id or item.name.strip().lower() for item in day.attractions}
                key = attraction.poi_id or attraction.name.strip().lower()
                proposed = [*day.attractions, attraction]
                if (
                    key not in existing_keys
                    and len(day.attractions) < self._max_attractions(request.pace)
                    and self._estimated_day_load(plan, day_index, proposed) <= capacities[day.date]
                ):
                    utilization = self._estimated_day_load(plan, day_index, proposed) / max(capacities[day.date], 1)
                    distance_delta = self._day_route_distance(plan, day_index, proposed) - self._day_route_distance(
                        plan, day_index, day.attractions
                    )
                    candidates.append((utilization * 100 + distance_delta, day_index))
            if candidates:
                _, best_index = min(candidates)
                plan.days[best_index].attractions.append(attraction)
                shifted += 1
            else:
                remaining.append(attraction)

        balanced = self._balance_sparse_days(plan, request, capacities)
        if not overflow and not balanced:
            return []
        message = f"已根据可用游玩时间、通勤距离和开放日期重新分配 {shifted + balanced} 个景点。"
        if closed_moved:
            message += f"其中 {closed_moved} 个原安排落在闭馆日。"
        if remaining:
            message += f"另有 {len(remaining)} 个景点超出全程容量，未强行塞入时间轴。"
        return [message]

    def _balance_sparse_days(
        self,
        plan: TripPlan,
        request: TripRequest,
        capacities: dict[str, int],
    ) -> int:
        """把 3/3/1 等失衡结果调成与每日可用时长相匹配的分布。"""
        total = sum(len(day.attractions) for day in plan.days)
        if total < 2 or not plan.days:
            return 0

        max_count = self._max_attractions(request.pace)
        average_visit = round(sum(
            min(max(item.visit_duration, 45), 240)
            for day in plan.days for item in day.attractions
        ) / total)
        unit = max(average_visit + 25, 70)
        slot_limits = [min(max_count, capacities[day.date] // unit) for day in plan.days]
        targets = [0 for _ in plan.days]
        for _ in range(total):
            eligible = [
                index for index, limit in enumerate(slot_limits)
                if targets[index] < limit
            ]
            if not eligible:
                break
            best = min(
                eligible,
                key=lambda index: (
                    (targets[index] + 1) * unit / max(capacities[plan.days[index].date], 1),
                    index,
                ),
            )
            targets[best] += 1

        moved = 0
        for target_index, target in enumerate(targets):
            target_day = plan.days[target_index]
            while len(target_day.attractions) < target:
                moves = []
                target_weekday = datetime.strptime(target_day.date, "%Y-%m-%d").date().weekday()
                for donor_index, donor_day in enumerate(plan.days):
                    donor_floor = max(targets[donor_index], self._min_attractions(request.pace))
                    if donor_index == target_index or len(donor_day.attractions) <= donor_floor:
                        continue
                    for attraction in donor_day.attractions:
                        if target_weekday in attraction.closed_weekdays:
                            continue
                        proposed_target = [*target_day.attractions, attraction]
                        if self._estimated_day_load(plan, target_index, proposed_target) > capacities[target_day.date]:
                            continue
                        proposed_donor = [item for item in donor_day.attractions if item is not attraction]
                        distance_delta = (
                            self._day_route_distance(plan, target_index, proposed_target)
                            + self._day_route_distance(plan, donor_index, proposed_donor)
                            - self._day_route_distance(plan, target_index, target_day.attractions)
                            - self._day_route_distance(plan, donor_index, donor_day.attractions)
                        )
                        moves.append((distance_delta, donor_index, attraction))
                if not moves:
                    break
                _, donor_index, attraction = min(moves, key=lambda item: item[0])
                plan.days[donor_index].attractions.remove(attraction)
                target_day.attractions.append(attraction)
                moved += 1
        return moved

    def _estimated_day_load(
        self, plan: TripPlan, day_index: int, attractions: list,
    ) -> int:
        visit_minutes = sum(
            min(max(item.visit_duration, 45), 240) for item in attractions
        )
        commute_minutes = round(
            self._day_route_distance(plan, day_index, attractions)
            / route_optimizer.AVERAGE_URBAN_SPEED_KMH * 60
        )
        return visit_minutes + commute_minutes

    @staticmethod
    def _day_route_distance(plan: TripPlan, day_index: int, attractions: list) -> float:
        if not attractions:
            return 0
        day = plan.days[day_index]
        previous = plan.days[day_index - 1] if day_index > 0 else None
        start = None
        if previous and previous.hotel and previous.hotel.location:
            start = previous.hotel.location
        elif day.hotel and day.hotel.location:
            start = day.hotel.location
        end = day.hotel.location if day.hotel and day.hotel.location else None
        points = [item.location for item in attractions]
        if start:
            points.insert(0, start)
        if end:
            points.append(end)
        return sum(
            route_optimizer.distance(left, right)
            for left, right in zip(points, points[1:])
        )

    @staticmethod
    def _apply_weather_rebalance(plan: TripPlan) -> list[str]:
        """有明确逐日天气时，把雨天户外项目与晴天室内项目互换一次。"""
        weather_by_date = {item.date: item.day_weather for item in plan.weather_info}
        rainy_days = [day for day in plan.days if any(token in weather_by_date.get(day.date, "") for token in ("雨", "雪", "雷"))]
        clear_days = [day for day in plan.days if day not in rainy_days]
        outdoor_tokens = ("公园", "山", "海", "湖", "古镇", "街", "自然", "风景", "户外")
        indoor_tokens = ("博物馆", "美术馆", "展览", "室内", "科技馆", "剧院")
        swaps = 0
        for rainy in rainy_days:
            outdoor = next((item for item in rainy.attractions if any(t in f"{item.name}{item.category}" for t in outdoor_tokens)), None)
            if not outdoor:
                continue
            for clear in clear_days:
                indoor = next((item for item in clear.attractions if any(t in f"{item.name}{item.category}" for t in indoor_tokens)), None)
                if indoor:
                    rainy_index = rainy.attractions.index(outdoor)
                    clear_index = clear.attractions.index(indoor)
                    rainy.attractions[rainy_index], clear.attractions[clear_index] = indoor, outdoor
                    swaps += 1
                    break
        return [f"已根据雨雪天气调整 {swaps} 组室内外活动。"] if swaps else []

    def calculate_budget(
        self,
        plan: TripPlan,
        request: TripRequest,
        intercity_cost_per_person: int = 0,
    ) -> Budget:
        from .price_reference_service import attraction_cost as price_for
        from math import ceil
        attraction_cost = ceil(sum(
            price_for(attraction)
            for day in plan.days
            for index, attraction in enumerate(day.attractions)
            if not day.schedule or any(item.item_type == "attraction" and item.related_index == index for item in day.schedule)
        ) * request.travelers)

        meal_cost = 0
        for day in plan.days:
            scheduled_meal_titles = {
                item.title for item in day.schedule if item.item_type == "meal"
            }
            meal_cost += sum(
                meal.estimated_cost for meal in day.meals
                if any(meal.name in title for title in scheduled_meal_titles)
            )
        meal_cost *= request.travelers

        # 入住晚数通常比旅行天数少 1。每天推荐的酒店只取对应入住夜晚。
        hotel_nights = max(request.travel_days - 1, 0)
        hotel_cost = ceil(sum(
            (day.hotel.amap_average_cost * request.travelers if day.hotel.amap_average_cost is not None
             else day.hotel.estimated_cost * request.rooms)
            for day in plan.days[:hotel_nights]
            if day.hotel is not None
        ))

        journey_count = 2 if request.trip_type == "round_trip" else 1
        intercity_cost = 0 if request.destination_only else (
            max(intercity_cost_per_person, 0) * request.travelers * journey_count
        )
        local_cost = (
            self.LOCAL_TRANSPORT_PER_PERSON_PER_DAY
            * request.travelers
            * request.travel_days
        )
        routed = any(item.route_leg for day in plan.days for item in day.schedule)
        if routed:
            from math import ceil
            segment_cost = 0.0
            for day in plan.days:
                if not any(item.route_leg for item in day.schedule):
                    segment_cost += self.LOCAL_TRANSPORT_PER_PERSON_PER_DAY * request.travelers
                    continue
                for item in day.schedule:
                    if item.route_leg:
                        leg = item.route_leg
                        units = ceil(request.travelers / 4) if leg.cost_unit == "vehicle" else request.travelers
                        segment_cost += leg.cost * units
                    elif item.item_type == "transport" and (
                        item.title.startswith("前往 ") or item.title.startswith("抵达后")
                    ) and item.duration_minutes > 0:
                        # Unknown station/airport connector: separate, visible reserve, not a ticket.
                        segment_cost += 15 * request.travelers
            local_cost = ceil(segment_cost)
        transport_cost = intercity_cost + local_cost
        total = attraction_cost + meal_cost + hotel_cost + transport_cost

        return Budget(
            unknown_ticket_count=sum(a.ticket_price is None and a.amap_average_cost is None for d in plan.days for a in d.attractions),
            total_attractions=attraction_cost,
            total_hotels=hotel_cost,
            total_meals=meal_cost,
            total_transportation=transport_cost,
            intercity_transportation=intercity_cost,
            local_transportation=local_cost,
            total=total,
            per_person=round(total / request.travelers),
            budget_limit=request.budget_limit,
            over_budget=request.budget_limit is not None and total > request.budget_limit,
            calculation_note=(
                f"按 {request.travelers} 人、{request.rooms} 间房计算；"
                f"住宿按 {hotel_nights} 晚；"
                f"景点价格未知 {sum(a.ticket_price is None and a.amap_average_cost is None for d in plan.days for a in d.attractions)} 处未计入总额；总额不是完整报价；"
                + ("市内按实际排程逐段汇总，公交按人数、驾车按每4人一车；未定位车站接驳按15元/人/段预留（机场实际费用可能远高于此），餐厅绕行未计；" if routed else
                   f"市内交通按每人每天¥{self.LOCAL_TRANSPORT_PER_PERSON_PER_DAY}估算；")
                + "景点/住宿优先按高德人均消费×人数计算，住宿另乘晚数；这不是门票或房价。缺失时使用已标注的静态参考/AI估价/房间预算；AI房费按间数乘晚数，未知部分未计入。AI价格依赖已有知识，未核验实时价格。"
                + ('仅含目的地内费用，不查询或计入往返目的地的车票。' if request.destination_only
                   else f"跨城交通按{'往返' if request.trip_type == 'round_trip' else '单程'}参考价计算。")
            ),
        )

    def validate(self, plan: TripPlan, request: TripRequest) -> list[str]:
        issues: list[str] = []
        start = datetime.strptime(request.start_date, "%Y-%m-%d").date()

        if len(plan.days) != request.travel_days:
            issues.append(
                f"计划包含 {len(plan.days)} 天，与请求的 {request.travel_days} 天不一致。"
            )

        seen_attractions: set[str] = set()
        categories: set[str] = set()
        main_meals: set[str] = set()
        for index, day in enumerate(plan.days):
            expected_date = (start + timedelta(days=index)).isoformat()
            if day.day_index != index:
                issues.append(f"第 {index + 1} 天的序号不连续。")
            if day.date != expected_date:
                issues.append(f"第 {index + 1} 天日期应为 {expected_date}，实际为 {day.date}。")
            if not day.attractions:
                issues.append(f"第 {index + 1} 天没有可执行的景点安排。")
            elif len(day.attractions) < self._min_attractions(request.pace):
                issues.append(f"第 {index + 1} 天内容偏少，未达到所选行程节奏的建议数量。")
            if len(day.attractions) > self._max_attractions(request.pace):
                issues.append(f"第 {index + 1} 天景点较多，可能不符合所选行程节奏。")

            meal_types = {meal.type for meal in day.meals}
            missing_meals = {"breakfast", "lunch", "dinner"} - meal_types
            if missing_meals:
                issues.append(f"第 {index + 1} 天缺少餐饮安排：{', '.join(sorted(missing_meals))}。")

            for attraction in day.attractions:
                normalized_name = attraction.name.strip().lower()
                if normalized_name in seen_attractions:
                    issues.append(f"景点“{attraction.name}”在多天行程中重复出现。")
                seen_attractions.add(normalized_name)
                if attraction.category:
                    categories.add(attraction.category.strip().lower())

            scheduled_attractions = {
                item.title.strip().lower() for item in day.schedule
                if item.item_type == "attraction"
            }
            for attraction in day.attractions:
                if attraction.name.strip().lower() not in scheduled_attractions:
                    issues.append(
                        f"第 {index + 1} 天的“{attraction.name}”受交通截止或开放时间限制，未进入可执行时间轴。"
                    )

            for meal in day.meals:
                if meal.type not in {"lunch", "dinner"}:
                    continue
                normalized_meal = meal.name.strip().lower()
                if normalized_meal in main_meals:
                    issues.append(f"餐饮“{meal.name}”在多天行程中重复出现。")
                main_meals.add(normalized_meal)

        attraction_total = sum(len(day.attractions) for day in plan.days)
        if attraction_total and len(categories) < min(3, attraction_total):
            issues.append("景点类别较单一，建议增加文化、自然、城市漫游或休闲体验的组合。")

        return issues

    @staticmethod
    def remove_duplicate_attractions(plan: TripPlan) -> list[str]:
        """POI ID优先、名称兜底，删除后续日期中的重复景点。"""
        issues: list[str] = []
        seen: set[str] = set()
        for day in plan.days:
            unique = []
            for attraction in day.attractions:
                normalized = "".join(attraction.name.lower().split())
                key = f"poi:{attraction.poi_id}" if attraction.poi_id else f"name:{normalized}"
                if key in seen:
                    issues.append(
                        f"已从第 {day.day_index + 1} 天移除重复景点“{attraction.name}”。"
                    )
                    continue
                seen.add(key)
                unique.append(attraction)
            day.attractions = unique
        return issues

    @staticmethod
    def _max_attractions(pace: str) -> int:
        return {"relaxed": 3, "balanced": 4, "intensive": 5}[pace]

    @staticmethod
    def _min_attractions(pace: str) -> int:
        return 3

    @staticmethod
    def default_sources(is_fallback: bool) -> list[DataSource]:
        if is_fallback:
            return [
                DataSource(
                    name="内置降级模板",
                    category="fallback",
                    source_type="static",
                    note="外部 Agent 或结构化解析失败，仅供参考，不代表实时信息。",
                )
            ]
        return [
            DataSource(
                name="高德地图 MCP",
                category="poi",
                source_type="live",
                note="用于检索 POI、酒店与天气候选；价格和开放信息仍需出行前复核。",
            ),
            DataSource(
                name="LLM 行程规划器",
                category="generation",
                source_type="generated",
                note="用于组织行程与生成说明，不作为实时事实来源。",
            ),
        ]


    # ------------------------------------------------------------------
    # 总体建议：行程概览页的实用贴士（多条文本，逐行一条）
    # ------------------------------------------------------------------

    def refine_overall_suggestions(self, plan: TripPlan, request: TripRequest) -> str:
        """基于完整行程生成 4~6 条出发前贴士；LLM 不可用时规则组装兜底。

        返回换行分隔的多条文本（前端按行渲染编号列表）。
        骨架阶段的 overall_suggestions 只有两三句话且缺预算/天气上下文，
        这里在 finalize 收尾时用完整行程重写。
        """
        context = self._suggestions_context(plan, request)
        from ..config import get_settings
        if get_settings().planning_tips_mode == 'rules':
            return self._rule_based_suggestions(plan, request)
        prompt = (
            "你是旅行顾问。根据下面这份已完成的行程，写 4~6 条出发前的实用贴士。\n"
            "要求：\n"
            "- 每条一句话、不超过 40 字，具体可执行（穿衣、天气、购票预约、避峰、交通、预算、体力）\n"
            "- 优先提示天气风险、跨城交通出发时间、热门景点预约、预算超支\n"
            "- 不要出现“祝您旅途愉快”之类的空话，不要罗列行程\n"
            "- 只输出 JSON 数组，例如 [\"贴士一\", \"贴士二\"]\n\n"
            f"行程摘要：\n{context}"
        )
        try:
            raw = _get_suggestions_llm().invoke([{"role": "user", "content": prompt}])
            items = self._parse_suggestions(raw)
            if len(items) >= 3:
                return "\n".join(items[:6])
        except Exception:
            pass
        return self._rule_based_suggestions(plan, request)

    @staticmethod
    def _parse_suggestions(raw: str) -> list[str]:
        """从 LLM 输出中提取字符串数组（容忍 ```json 包裹与前后杂文本）。"""
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return []
        if not isinstance(data, list):
            return []
        return [str(item).strip() for item in data if str(item).strip()][:8]

    @staticmethod
    def _suggestions_context(plan: TripPlan, request: TripRequest) -> str:
        """渲染给贴士模型的行程摘要：只留事实，不塞完整 JSON。"""
        lines = [
            f"目的地 {plan.city}，{plan.start_date} 至 {plan.end_date} 共 {len(plan.days)} 天",
            f"出发城市：{request.origin_city}",
        ]
        for day in plan.days:
            names = "、".join(a.name for a in day.attractions[:8])
            lines.append(
                f"第{day.day_index + 1}天「{day.theme or day.description}」：{names}；市内交通{day.transportation}"
            )
        for w in plan.weather_info:
            lines.append(
                f"天气 {w.date}：白天{w.day_weather}{w.day_temp}°C，夜间{w.night_temp}°C，{w.wind_direction}风{w.wind_power}"
            )
        if plan.budget:
            over = "，已超预算" if plan.budget.over_budget else ""
            lines.append(f"预算：总 {plan.budget.total} 元，人均 {plan.budget.per_person} 元{over}")
        if plan.intercity_journey:
            j = plan.intercity_journey
            lines.append(
                f"首日跨城：{j.name} {j.departure_time} 从{j.departure_station}出发，需提前{j.station_transfer_minutes}分钟到站"
            )
        for warning in plan.warnings[:5]:
            lines.append(f"提醒：{warning}")
        return "\n".join(lines)

    @staticmethod
    def _rule_based_suggestions(plan: TripPlan, request: TripRequest) -> str:
        """LLM 失败时的规则兜底：从天气/交通/预算/门票里能提炼多少提多少。"""
        tips: list[str] = []
        rainy = [w.date for w in plan.weather_info if "雨" in (w.day_weather or "")]
        if rainy:
            tips.append(f"{'、'.join(rainy[:3])} 有降雨，随身带伞")

        def _max_wind(power: str) -> int:
            numbers = [int(x) for x in re.findall(r"\d+", power or "")]
            return max(numbers) if numbers else 0

        gusts = [w for w in plan.weather_info if _max_wind(w.wind_power) >= 5]
        if gusts:
            tips.append(f"{'、'.join(w.date for w in gusts[:3])} 风力较大，注意防风保暖")
        temps = [
            (w.day_temp, w.night_temp)
            for w in plan.weather_info
            if w.day_temp is not None and w.night_temp is not None
        ]
        if temps and max(t[0] for t in temps) - min(t[1] for t in temps) >= 10:
            tips.append("行程内早晚温差超过 10°C，备一件方便穿脱的外套")
        if plan.intercity_journey:
            j = plan.intercity_journey
            tips.append(
                f"首日 {j.departure_time} 的{j.name}从{j.departure_station}出发，提前 {j.station_transfer_minutes} 分钟到站"
            )
        if plan.budget and plan.budget.over_budget:
            tips.append("当前安排略超预算，可减少收费景点或改住经济型酒店")
        paid = [
            a.name
            for day in plan.days
            for a in day.attractions
            if a.ticket_price and "免费" not in str(a.ticket_price)
        ]
        if paid:
            tips.append(f"{'、'.join(paid[:2])} 为收费景点，建议提前在线购票免排队")
        if sum(len(day.attractions) for day in plan.days) >= 12:
            tips.append("行程较满，建议每天保留半小时以上弹性时间")
        if not tips:
            tips.append("出发前请再确认各景点开放时间与预约要求")
        return "\n".join(tips[:6])


# 贴士生成专用客户端：优先 LLM_PATCH_MODEL（flash 级小模型），未配置回退主模型；进程级缓存
_suggestions_llm = None


def _get_suggestions_llm():
    global _suggestions_llm
    if _suggestions_llm is None:
        import os

        if (os.getenv("LLM_PATCH_MODEL") or "").strip():
            from ..infrastructure.llm_client import LLMClient

            _suggestions_llm = LLMClient(
                model=os.getenv("LLM_PATCH_MODEL").strip(),
                temperature=1.0,
                max_tokens=1024,
            )
        else:
            from .llm_service import get_llm

            _suggestions_llm = get_llm()
    return _suggestions_llm


trip_quality_service = TripQualityService()
