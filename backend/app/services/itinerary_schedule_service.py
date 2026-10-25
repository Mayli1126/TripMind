"""Build deterministic daily timelines around real transport constraints."""

from __future__ import annotations

from collections import Counter
from datetime import date
from hashlib import sha1

from ..models.schemas import DayPlan, IntercityJourney, Location, ScheduleItem, TripPlan
from .route_optimizer import route_optimizer


class ItineraryScheduleService:
    """Transport occupies the clock first; meals and attractions use the remainder."""

    def build(self, plan: TripPlan) -> TripPlan:
        previous_night_location: Location | None = None
        for index, day in enumerate(plan.days):
            start_location = previous_night_location
            if start_location is None and day.hotel and day.hotel.location:
                start_location = day.hotel.location
            day.theme = day.theme.strip() or self._derive_theme(day)
            day.schedule, ends_late = self._build_day(
                day, plan.intercity_journey, plan.return_journey, start_location
            )
            
            # 验证行程合理性
            self._validate_schedule(plan, day)
            
            if ends_late:
                plan.warnings.append(
                    f"第 {day.day_index + 1} 天预计结束较晚，请减少活动或调整交通班次。"
                )
            if day.hotel and day.hotel.location and index < len(plan.days) - 1:
                previous_night_location = day.hotel.location
            elif day.attractions:
                previous_night_location = day.attractions[-1].location
        return plan
    
    def _validate_schedule(self, plan: TripPlan, day: DayPlan) -> None:
        """验证每日行程安排的合理性，综合考虑时间、天气和体力因素"""
        # 获取当天天气信息
        day_weather = None
        if plan.weather_info:
            for weather in plan.weather_info:
                if weather.date == day.date:
                    day_weather = weather
                    break
        
        # 分析天气状况
        is_rainy = day_weather and ('雨' in day_weather.day_weather or '雨' in day_weather.night_weather)
        is_hot = day_weather and day_weather.day_temp and day_weather.day_temp >= 35
        is_cold = day_weather and day_weather.day_temp and day_weather.day_temp <= 5
        is_windy = day_weather and ('风' in day_weather.wind_direction)
        
        # 检查上午是否有足够的活动（早餐后到午餐前应有景点）
        morning_attractions = 0
        lunch_time = None
        
        for item in day.schedule:
            if item.item_type == 'meal' and '午餐' in item.title:
                lunch_time = self._minutes(item.start_time)
            elif item.item_type == 'attraction':
                start_minutes = self._minutes(item.start_time)
                # 上午时段（8:00-12:00）的景点
                if 8 * 60 <= start_minutes < 12 * 60:
                    morning_attractions += 1
        
        # 如果午餐在13:00之前且上午没有景点，发出警告
        if lunch_time and lunch_time < 13 * 60 and morning_attractions == 0:
            plan.warnings.append(
                f"第 {day.day_index + 1} 天上午行程较空（早餐后至午餐前无景点活动），建议增加上午活动。"
            )
        
        # 统计各类景点数量
        outdoor_attractions = 0
        indoor_attractions = 0
        natural_attractions = 0
        cultural_attractions = 0
        
        natural_categories = ['自然风光', '公园', '山脉', '长城', '登山', '峡谷', '海滩', '湖泊', '景区']
        cultural_categories = ['历史文化', '博物馆', '故居', '古迹', '古建筑', '寺庙', '纪念馆']
        night_categories = ['夜景', '灯光秀', '夜市', '夜游', '夜景灯光']
        
        for attraction in day.attractions:
            category = attraction.category or ''
            if any(cat in category for cat in natural_categories):
                natural_attractions += 1
                outdoor_attractions += 1
            elif any(cat in category for cat in cultural_categories):
                cultural_attractions += 1
                # 博物馆等通常是室内，其他可能是室外
                if '博物馆' in category or '纪念馆' in category or '艺术馆' in category:
                    indoor_attractions += 1
                else:
                    outdoor_attractions += 1
            elif '室内' in category or '商场' in category:
                indoor_attractions += 1
            else:
                outdoor_attractions += 1
        
        # 天气与行程匹配度检查
        if is_rainy and outdoor_attractions > 0 and indoor_attractions == 0:
            plan.warnings.append(
                f"第 {day.day_index + 1} 天预报有雨，当前行程全部为户外活动，建议调整部分行程至室内景点。"
            )
        
        if is_hot and outdoor_attractions > 1:
            plan.warnings.append(
                f"第 {day.day_index + 1} 天天气炎热（{day_weather.day_temp}°C），建议减少正午户外活动，或增加室内景点穿插休息。"
            )
        
        if is_cold and outdoor_attractions > 1:
            plan.warnings.append(
                f"第 {day.day_index + 1} 天天气寒冷（{day_weather.day_temp}°C），建议缩短户外停留时间，注意保暖。"
            )
        
        # 检查景点安排时间的合理性
        for attraction in day.attractions:
            category = attraction.category or ''
            for item in day.schedule:
                if item.item_type == 'attraction' and item.related_index == day.attractions.index(attraction):
                    start_minutes = self._minutes(item.start_time)
                    
                    # 自然景观类景点不应在晚上安排（17:00之后开始），除非是夜景类
                    is_natural = any(cat in category for cat in natural_categories)
                    is_night = any(cat in category for cat in night_categories)
                    
                    if is_natural and not is_night and start_minutes >= 17 * 60:
                        plan.warnings.append(
                            f"第 {day.day_index + 1} 天的「{attraction.name}」属于自然景观类，建议安排在白天（08:00-17:00）游览以获得更好的观景体验。"
                        )
                    
                    # 历史文化类景点不应在晚上安排
                    if any(cat in category for cat in cultural_categories) and start_minutes >= 17 * 60:
                        plan.warnings.append(
                            f"第 {day.day_index + 1} 天的「{attraction.name}」属于历史文化类景点，通常开放时间在白天，建议调整至日间游览。"
                        )
                    
                    # 高温天气下正午安排户外活动的警告
                    if is_hot and 11 * 60 <= start_minutes < 15 * 60 and any(cat in category for cat in natural_categories):
                        plan.warnings.append(
                            f"第 {day.day_index + 1} 天的「{attraction.name}」安排在正午时段，天气炎热，建议调整至早晚时段或替换为室内景点。"
                        )
                    break
            
            # 检查游玩时间是否合理
            duration = attraction.visit_duration or 0
            is_large_attraction = any(cat in category for cat in ['故宫', '长城', '颐和园', '天坛', '世界遗产', '大型景区'])
            is_medium_attraction = any(cat in category for cat in ['博物馆', '主题公园', '历史街区', '名胜古迹'])
            is_small_attraction = any(cat in category for cat in ['公园', '广场', '故居', '展馆', '纪念馆'])
            is_micro_attraction = any(cat in category for cat in ['打卡', '雕塑', '单体建筑'])
            
            if is_large_attraction and duration < 120:
                plan.warnings.append(
                    f"第 {day.day_index + 1} 天的「{attraction.name}」属于大型景区，建议游玩时间3-4小时，当前安排偏短。"
                )
            elif is_medium_attraction and (duration < 90 or duration > 200):
                if duration < 90:
                    plan.warnings.append(
                        f"第 {day.day_index + 1} 天的「{attraction.name}」属于中型景点，建议游玩时间2-3小时。"
                    )
            elif is_small_attraction and duration > 120:
                plan.warnings.append(
                    f"第 {day.day_index + 1} 天的「{attraction.name}」属于小型景点，建议游玩时间1-1.5小时，当前安排偏长。"
                    )
            elif is_micro_attraction and duration > 60:
                plan.warnings.append(
                    f"第 {day.day_index + 1} 天的「{attraction.name}」属于打卡类景点，建议游玩时间30-45分钟，当前安排偏长。"
                )
            # 特殊处理：公园通常不需要太长时间
            elif '公园' in category and duration > 90:
                plan.warnings.append(
                    f"第 {day.day_index + 1} 天的「{attraction.name}」作为公园类景点，通常1小时左右足够，除非是大型国家森林公园。"
                )

    def _build_day(
        self,
        day: DayPlan,
        outbound: IntercityJourney | None,
        return_journey: IntercityJourney | None,
        start_location: Location | None,
    ) -> tuple[list[ScheduleItem], bool]:
        items: list[ScheduleItem] = []
        meals = {meal.type: meal for meal in day.meals}

        # 夜间去程的出发日只承担跨城交通；抵达和游玩在真实到达日开始。
        if outbound and day.date == outbound.departure_date and outbound.arrival_date != day.date:
            self._append_journey_departure(items, day, outbound, meals.get("dinner"))
            return items, False

        current = 7 * 60 + 45
        arrival_day = bool(outbound and day.date == outbound.arrival_date)
        if outbound and day.date == outbound.departure_date == outbound.arrival_date:
            current = self._append_same_day_outbound(items, day, outbound, meals.get("breakfast"))
        elif arrival_day:
            arrival = self._minutes(outbound.arrival_time)
            current = self._append(
                items, day, arrival, outbound.arrival_transfer_minutes, "transport",
                "抵达后前往酒店或首个游玩片区",
                f"取行李、出站和市内接驳预留约 {outbound.arrival_transfer_minutes} 分钟。",
                outbound.arrival_station,
                notes="抵达延误时，优先取消当日最后一个景点。",
            )
        else:
            breakfast = meals.get("breakfast")
            if breakfast:
                current = self._append_meal(items, day, current, breakfast, "早餐", 45)
            current = max(current, 8 * 60 + 30)

        current = max(current, 8 * 60 + 30)
        # 用户 Patch 指定的当日偏好出发时间（change_start_time 写入）：
        # 晚于默认起排时顺延，早于默认不提前（早餐/跨城抵达约束优先）
        if day.preferred_start_time:
            try:
                preferred = self._minutes(day.preferred_start_time)
                if preferred > current:
                    current = preferred
            except ValueError:
                pass  # 非法 HH:MM 由 schema/patch 层拦截，此处容错跳过

        returning = bool(return_journey and day.date == return_journey.departure_date)
        departure = self._minutes(return_journey.departure_time) if returning else 0
        station_leave = (
            departure - return_journey.station_transfer_minutes - return_journey.check_in_buffer_minutes
            if returning else 24 * 60
        )

        lunch_added = bool(arrival_day and current >= 14 * 60)
        dinner_added = False
        current_location = start_location

        # 按时间约束重排景点：白天项目优先，夜间项目置后，
        # 防止 route_optimizer 的距离排序把博物馆挤到晚餐之后。
        day.attractions = ItineraryScheduleService._reorder_by_time_constraints(day.attractions)

        for index, attraction in enumerate(day.attractions):
            duration = max(45, min(attraction.visit_duration, 240))
            travel_minutes = self._travel_minutes(current_location, attraction.location)
            if attraction.opening_time:
                try:
                    opening = self._minutes(attraction.opening_time)
                    if current < opening and opening < station_leave:
                        current = self._append(
                            items, day, current, opening - current, "break", f"等待 {attraction.name} 开放",
                            "使用机动时间休息或在附近活动。",
                        )
                except (ValueError, TypeError):
                    pass
            latest_entry = attraction.last_entry_time or attraction.closing_time
            if latest_entry:
                try:
                    if current + travel_minutes > self._minutes(latest_entry):
                        continue
                except (ValueError, TypeError):
                    pass

            # 兜底：对没有开放时间的常识白天景点，强制 17:00 前必须开始，
            # 避免 LLM 未填 closing_time 时博物馆被排到夜间。
            if not latest_entry:
                cat = (attraction.category or "").lower()
                name = attraction.name.lower()
                day_only = any(t in name or t in cat for t in [
                    "博物馆", "故宫", "故居", "纪念馆", "天坛", "颐和园", "长城",
                    "寺庙", "古迹", "古建筑", "遗址", "陵墓", "文庙", "石窟", "石刻"
                ])
                if day_only and current >= 17 * 60:
                    continue

            meal_buffer = 60 if not lunch_added and current < 13 * 60 else 0
            if current + meal_buffer + travel_minutes + duration > station_leave:
                continue
            projected_end = current + travel_minutes + duration
            if not lunch_added and projected_end > 12 * 60 + 30:
                current = max(current, 11 * 60 + 45)
                current = self._append_meal(items, day, current, meals.get("lunch"), "午餐", 60)
                lunch_added = True
            if (
                not returning and not dinner_added
                and current + travel_minutes + duration > 18 * 60 + 30
            ):
                current = max(current, 17 * 60 + 45)
                current = self._append_meal(items, day, current, meals.get("dinner"), "晚餐", 75)
                dinner_added = True

            current = self._append(
                items, day, current, travel_minutes, "transport", f"前往 {attraction.name}",
                f"预计路上约 {travel_minutes} 分钟", attraction.address,
                notes="基于坐标估算；实时路况以导航为准",
            )
            current = self._append(
                items, day, current, duration, "attraction", attraction.name,
                attraction.description, attraction.address, related_index=index,
                notes="出发前核对开放时间、预约要求和停止入园时间",
            )
            current_location = attraction.location

        if not lunch_added and current + 60 <= station_leave:
            current = max(current, 12 * 60)
            current = self._append_meal(items, day, current, meals.get("lunch"), "午餐", 60)

        if returning:
            # 返程前不机械补晚餐；空余时间作为可取消的机动段。
            if current < station_leave:
                current = self._append(
                    items, day, current, station_leave - current, "break", "返程前机动时间",
                    "取行李、简单用餐并确认车次状态。",
                    notes="不要在此时段新增需要预约的景点。",
                )
            self._append_return_journey(items, day, return_journey)
            return items, False

        if not dinner_added:
            if current < 17 * 60 + 30:
                explore_duration = min(120, 17 * 60 + 30 - current)
                if explore_duration >= 30:
                    current = self._append(
                        items, day, current, explore_duration, "break", "周边自由探索与机动时间",
                        "可散步、休息，也可消化排队和天气造成的延误。",
                        notes="这段时间不绑定新景点。",
                    )
            current = max(current, 17 * 60 + 45)
            current = self._append_meal(items, day, current, meals.get("dinner"), "晚餐", 75)

        if day.hotel:
            return_minutes = self._travel_minutes(current_location, day.hotel.location)
            current = self._append(
                items, day, current, return_minutes, "hotel", f"返回酒店 · {day.hotel.name}",
                "办理入住或返回休息", day.hotel.address,
                notes="已按最后一个景点到酒店的坐标距离估算；实时路况请以导航为准",
            )
        return items, current > 22 * 60

    def _append_same_day_outbound(self, items, day, journey, breakfast) -> int:
        departure = self._minutes(journey.departure_time)
        arrival = self._minutes(journey.arrival_time)
        station_leave = departure - journey.station_transfer_minutes - journey.check_in_buffer_minutes
        current = max(0, station_leave)
        if breakfast and station_leave >= 6 * 60 + 15:
            current = station_leave - 45
            current = self._append_meal(items, day, current, breakfast, "早餐", 45)
        current = self._append_station_steps(items, day, current, journey)
        self._append_ride(items, day, journey, 0)
        return self._append(
            items, day, arrival, journey.arrival_transfer_minutes, "transport",
            "抵达后前往酒店或首个游玩片区",
            f"取行李、出站和市内接驳预留约 {journey.arrival_transfer_minutes} 分钟。",
            journey.arrival_station,
            notes="抵达延误时，优先取消当日最后一个景点。",
        )

    def _append_journey_departure(self, items, day, journey, dinner) -> None:
        departure = self._minutes(journey.departure_time)
        station_leave = departure - journey.station_transfer_minutes - journey.check_in_buffer_minutes
        current = max(0, station_leave)
        if dinner and 17 * 60 <= station_leave:
            meal_start = max(17 * 60 + 30, station_leave - 75)
            if meal_start + 75 <= station_leave:
                current = self._append_meal(items, day, meal_start, dinner, "晚餐", 75)
        self._append_station_steps(items, day, max(current, station_leave), journey)
        offset = (date.fromisoformat(journey.arrival_date) - date.fromisoformat(journey.departure_date)).days
        self._append_ride(items, day, journey, offset)

    def _append_station_steps(self, items, day, start, journey) -> int:
        current = start
        if journey.station_transfer_minutes:
            current = self._append(
                items, day, current, journey.station_transfer_minutes, "transport",
                f"前往 {journey.departure_station}", "预留市内前往车站或机场的时间。",
                journey.departure_station, notes="请结合实际出发地址和导航提前量复核。",
            )
        if journey.check_in_buffer_minutes:
            current = self._append(
                items, day, current, journey.check_in_buffer_minutes, "transport",
                "进站、安检与候车 / 值机", "为安检、取票、托运和候车预留缓冲。",
                journey.departure_station,
            )
        return current

    def _append_ride(self, items, day, journey, end_offset) -> None:
        departure = self._minutes(journey.departure_time)
        arrival = self._minutes(journey.arrival_time)
        duration = end_offset * 24 * 60 + arrival - departure
        self._append(
            items, day, departure, duration, "transport", f"{journey.name}：跨城交通",
            f"{journey.departure_station} → {journey.arrival_station}", journey.arrival_station,
            notes=("用户已确认的交通时刻。" if not journey.is_estimated else "静态参考时刻，购票后必须复核。"),
            end_day_offset=end_offset,
        )

    def _append_return_journey(self, items, day, journey) -> None:
        departure = self._minutes(journey.departure_time)
        station_leave = departure - journey.station_transfer_minutes - journey.check_in_buffer_minutes
        self._append_station_steps(items, day, station_leave, journey)
        offset = (date.fromisoformat(journey.arrival_date) - date.fromisoformat(journey.departure_date)).days
        self._append_ride(items, day, journey, offset)

    def _append_meal(self, items, day, start, meal, label, duration):
        if meal is None:
            return start
        return self._append(
            items, day, start, duration, "meal", f"{label} · {meal.name}",
            meal.description or f"安排{label}与休息", meal.address or "",
            notes="餐厅营业和排队情况需当天确认",
        )

    def _append(
        self, items, day, start, duration, item_type, title, description="", address="",
        related_index=None, notes="", end_day_offset=0,
    ) -> int:
        duration = max(0, duration)
        end_total = start + duration
        calculated_offset = end_total // (24 * 60)
        final_offset = max(end_day_offset, calculated_offset)
        stable_raw = f"{day.date}|{item_type}|{title}|{address}"
        items.append(ScheduleItem(
            start_time=self._time(start), end_time=self._time(end_total), item_type=item_type,
            title=title, description=description, address=address,
            related_index=related_index, duration_minutes=duration, notes=notes,
            stable_id=sha1(stable_raw.encode("utf-8")).hexdigest()[:16],
            start_day_offset=start // (24 * 60), end_day_offset=final_offset,
        ))
        return end_total

    @staticmethod
    def _infer_time_slot(attraction: Attraction, prefer_day: bool = False) -> str:
        """根据名称、品类和开放时间推断景点的时间属性."""
        cat = (attraction.category or "").lower()
        name = attraction.name.lower()

        night_tokens = ["夜景", "夜市", "灯光秀", "夜游", "夜场", "晚上", "夜间", "喷泉", "游船夜", "演出", "话剧", "音乐剧", "演唱会"]
        if any(t in name or t in cat for t in night_tokens):
            return "evening"

        if attraction.closing_time:
            try:
                close_h = int(attraction.closing_time.split(":")[0])
                if close_h <= 17:
                    return "daytime"
                if close_h >= 21:
                    return "flexible"
            except (ValueError, AttributeError):
                pass

        day_tokens = ["博物馆", "故宫", "故居", "纪念馆", "古迹", "古建筑", "寺庙", "天坛", "颐和园", "长城", "园林", "遗址", "陵墓", "文庙", "宗祠", "石窟", "石刻"]
        if any(t in name or t in cat for t in day_tokens):
            return "daytime"

        nature_tokens = ["山", "湖", "海", "森林", "峡谷", "海滩", "草原", "瀑布", "湿地", "自然保护"]
        if any(t in cat for t in nature_tokens):
            return "daytime"

        if "公园" in cat or "公园" in name:
            if any(t in name for t in ["国家", "森林", "自然", "风景区", "湿地", "地质"]):
                return "daytime"
            return "flexible"

        if any(t in cat for t in ["商业街", "美食街", "步行街", "购物", "商场"]):
            return "flexible"

        if prefer_day:
            return "daytime"
        return "flexible"

    @staticmethod
    def _reorder_by_time_constraints(attractions: list[Attraction]) -> list[Attraction]:
        """按时间属性粗排：daytime -> flexible -> evening，防止夜间项目抢占白天时段."""
        if not attractions:
            return attractions

        daytime: list[Attraction] = []
        evening: list[Attraction] = []
        flexible: list[Attraction] = []

        for attr in attractions:
            slot = ItineraryScheduleService._infer_time_slot(attr)
            if slot == "evening":
                evening.append(attr)
            elif slot == "daytime":
                daytime.append(attr)
            else:
                flexible.append(attr)

        # 避免上午真空：如果白天组为空，从 flexible 借调最适合白天的项目
        if not daytime and flexible:
            borrow: list[Attraction] = []
            remain: list[Attraction] = []
            for attr in flexible:
                if ItineraryScheduleService._infer_time_slot(attr, prefer_day=True) == "daytime" and len(borrow) < 2:
                    borrow.append(attr)
                else:
                    remain.append(attr)
            daytime.extend(borrow)
            flexible = remain

        return daytime + flexible + evening

    @staticmethod
    def _derive_theme(day: DayPlan) -> str:
        categories = [item.category or "城市探索" for item in day.attractions]
        common = [name for name, _ in Counter(categories).most_common(2)]
        return " · ".join(common) or "交通与休整"

    @staticmethod
    def _time(minutes: int) -> str:
        normalized = minutes % (24 * 60)
        return f"{normalized // 60:02d}:{normalized % 60:02d}"

    @staticmethod
    def _minutes(value: str) -> int:
        hour, minute = map(int, value.split(":"))
        return hour * 60 + minute

    @staticmethod
    def _travel_minutes(start: Location | None, end: Location | None) -> int:
        """按相邻地点计算通勤，不再把全天通勤平均摊给每个景点。"""
        if start is None or end is None:
            return 30
        distance_km = route_optimizer.distance(start, end)
        if distance_km < 0.3:
            return 8
        return max(10, min(120, round(
            distance_km / route_optimizer.AVERAGE_URBAN_SPEED_KMH * 60
        )))


itinerary_schedule_service = ItineraryScheduleService()
