"""旅行计划的确定性预算计算与可执行性检查。"""

from __future__ import annotations

from datetime import datetime, timedelta
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
    ) -> TripPlan:
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
        
        plan = itinerary_schedule_service.build(plan)
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
        plan.data_quality = "fallback" if plan.is_fallback else "mixed"
        return plan

    @staticmethod
    def _build_intercity_journey(
        request: TripRequest,
        option: TransportationOption | None,
        existing: IntercityJourney | None,
    ) -> IntercityJourney | None:
        if request.origin_city.strip() == request.city.strip():
            return None
        if not (request.departure_time and request.arrival_time) and option is None:
            return existing if existing and existing.source in {"user_confirmed", "live", "static"} else None
        mode = request.transportation
        transfer, check_in, after = TripQualityService._journey_buffers(mode)
        confirmed = bool(request.departure_time and request.arrival_time)
        departure_raw = request.departure_time or (option.departure_time if option else None)
        arrival_raw = request.arrival_time or (option.arrival_time if option else None)
        departure, _ = TripQualityService._parse_transport_time(departure_raw)
        arrival, explicit_offset = TripQualityService._parse_transport_time(arrival_raw)
        if not departure or not arrival:
            return existing
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
        if request.trip_type == "one_way" or request.origin_city.strip() == request.city.strip():
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
        warnings = []
        if request.origin_city != request.city and not plan.intercity_journey:
            warnings.append("未获得可信的去程班次，首日未自动占用跨城交通时间；请填写已购票时刻后重新规划。")
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
                if (
                    len(keep) < self._max_attractions(request.pace)
                    and self._estimated_day_load(plan, day_index, candidate) <= available
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
                    if donor_index == target_index or len(donor_day.attractions) <= targets[donor_index]:
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
        attraction_cost = sum(
            attraction.ticket_price
            for day in plan.days
            for attraction in day.attractions
        ) * request.travelers

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
        hotel_cost = sum(
            day.hotel.estimated_cost * request.rooms
            for day in plan.days[:hotel_nights]
            if day.hotel is not None
        )

        journey_count = 2 if request.trip_type == "round_trip" else 1
        intercity_cost = (
            max(intercity_cost_per_person, 0) * request.travelers * journey_count
        )
        local_cost = (
            self.LOCAL_TRANSPORT_PER_PERSON_PER_DAY
            * request.travelers
            * request.travel_days
        )
        transport_cost = intercity_cost + local_cost
        total = attraction_cost + meal_cost + hotel_cost + transport_cost

        return Budget(
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
                f"住宿按 {hotel_nights} 晚，市内交通按每人每天 "
                f"¥{self.LOCAL_TRANSPORT_PER_PERSON_PER_DAY} 估算；跨城交通按"
                f"{'往返' if request.trip_type == 'round_trip' else '单程'}参考价计算。"
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
            arrival_day_without_capacity = (
                index == 0 and plan.intercity_journey is not None
                and not day.attractions
            )
            if not day.attractions and not arrival_day_without_capacity:
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
        return {"relaxed": 2, "balanced": 3, "intensive": 5}[pace]

    @staticmethod
    def _min_attractions(pace: str) -> int:
        return {"relaxed": 1, "balanced": 2, "intensive": 3}[pace]

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


trip_quality_service = TripQualityService()
