"""不依赖 LLM 的每日景点顺序优化器。"""

from __future__ import annotations

from math import asin, cos, radians, sin, sqrt
from typing import List, Tuple

from ..models.schemas import Attraction, DailyRouteSummary, Location, OptimizationSummary, TripPlan


class RouteOptimizer:
    """用经纬度最近邻优化访问顺序，并保留优化前后指标。"""

    # 城市平均交通速度（公里/小时），用于估算通勤时间
    AVERAGE_URBAN_SPEED_KMH = 22.0

    # 交通方式速度（公里/小时）
    SPEED_MAP = {
        "walking": 5.0,
        "地铁/公交": 15.0,
        "地铁或打车": 20.0,
        "打车或地铁": 25.0,
        "driving": 30.0,
    }

    def optimize(self, plan: TripPlan) -> TripPlan:
        before_total = 0.0
        after_total = 0.0
        optimized_days = 0

        previous_night_location: Location | None = None
        previous_night_name = ""

        for index, day in enumerate(plan.days):
            if not day.attractions:
                day.route_summary = DailyRouteSummary()
                if day.hotel and day.hotel.location and index < len(plan.days) - 1:
                    previous_night_location = day.hotel.location
                    previous_night_name = day.hotel.name
                continue

            # 第二天应从前一晚住宿地出发，而不是机械使用"当天推荐酒店"。
            start = previous_night_location
            start_name = previous_night_name
            if start is None and day.hotel and day.hotel.location:
                start = day.hotel.location
                start_name = day.hotel.name

            # 有返程约束的末日不再强制绕回酒店；其余日期以当晚酒店为收尾点。
            returning_today = bool(
                plan.return_journey and day.date == plan.return_journey.departure_date
            )
            end = (
                day.hotel.location
                if day.hotel and day.hotel.location and not returning_today
                else None
            )
            end_name = day.hotel.name if end is not None and day.hotel else ""

            # 获取餐食位置（作为必经节点）
            meal_locations = self._get_meal_locations(day)

            original = list(day.attractions)
            candidate = self._nearest_neighbor_with_meals(original, start, end, meal_locations)
            before = self._route_distance(original, start, end)
            candidate_distance = self._route_distance(candidate, start, end)

            # 最近邻是启发式算法；如果极端情况下更差，就保留原顺序。
            if candidate_distance + 0.001 < before:
                day.attractions = candidate
                after = candidate_distance
                optimized_days += 1
            else:
                after = before

            # 根据距离选择交通方式并计算时间
            speed = self._determine_speed(day.transportation, after)
            saved_percent = ((before - after) / before * 100) if before else 0
            ordered = day.attractions
            start_transfer = self.distance(start, ordered[0].location) if start else 0
            end_transfer = self.distance(ordered[-1].location, end) if end else 0
            day.route_summary = DailyRouteSummary(
                distance_before_km=round(before, 2),
                distance_after_km=round(after, 2),
                estimated_travel_minutes=round(after / speed * 60),
                saved_percent=round(saved_percent, 1),
                start_anchor=start_name,
                end_anchor=end_name,
                start_transfer_km=round(start_transfer, 2),
                end_transfer_km=round(end_transfer, 2),
            )
            before_total += before
            after_total += after

            if day.hotel and day.hotel.location and index < len(plan.days) - 1:
                previous_night_location = day.hotel.location
                previous_night_name = day.hotel.name
            elif ordered:
                previous_night_location = ordered[-1].location
                previous_night_name = ordered[-1].name

        saved_distance = max(before_total - after_total, 0)
        plan.optimization_summary = OptimizationSummary(
            total_distance_before_km=round(before_total, 2),
            total_distance_after_km=round(after_total, 2),
            saved_distance_km=round(saved_distance, 2),
            saved_percent=round(saved_distance / before_total * 100, 1) if before_total else 0,
            optimized_days=optimized_days,
        )
        return plan

    def _get_meal_locations(self, day) -> List[Tuple[str, Location]]:
        """获取当日餐食位置列表。"""
        meal_locations = []
        for meal in day.meals:
            if meal.location:
                meal_locations.append((meal.type, meal.location))
        return meal_locations

    def _nearest_neighbor_with_meals(
        self,
        attractions: list[Attraction],
        start: Location | None,
        end: Location | None = None,
        meal_locations: List[Tuple[str, Location]] = None,
    ) -> list[Attraction]:
        """考虑餐食位置的最近邻算法。
        
        将午餐位置作为上午和下午的分隔点，晚餐位置作为一天的结束点。
        """
        if not meal_locations:
            return self._nearest_neighbor(attractions, start, end)

        remaining = list(attractions)
        if start is None:
            ordered = [remaining.pop(0)]
            current = ordered[0].location
        else:
            ordered = []
            current = start

        # 找到午餐和晚餐位置
        lunch_loc = None
        dinner_loc = None
        for meal_type, loc in meal_locations:
            if meal_type == "lunch":
                lunch_loc = loc
            elif meal_type == "dinner":
                dinner_loc = loc

        # 上午：从起点到午餐前的景点
        morning_count = max(1, len(remaining) // 2)
        for _ in range(morning_count):
            if not remaining:
                break
            next_index = min(
                range(len(remaining)),
                key=lambda index: (
                    self.distance(current, remaining[index].location)
                    + (0.5 * self.distance(remaining[index].location, lunch_loc) if lunch_loc else 0)
                ),
            )
            selected = remaining.pop(next_index)
            ordered.append(selected)
            current = selected.location

        # 如果有午餐位置，午餐后继续
        if lunch_loc:
            current = lunch_loc

        # 下午：午餐后的景点到晚餐前
        while remaining:
            next_index = min(
                range(len(remaining)),
                key=lambda index: (
                    self.distance(current, remaining[index].location)
                    + (0.3 * self.distance(remaining[index].location, dinner_loc) if dinner_loc else 0)
                ),
            )
            selected = remaining.pop(next_index)
            ordered.append(selected)
            current = selected.location

        return ordered

    def _determine_speed(self, transport_desc: str, distance_km: float) -> float:
        """根据距离和交通描述确定速度。"""
        # 根据距离判断
        if distance_km < 1.5:
            return self.SPEED_MAP["walking"]
        elif distance_km < 5.0:
            return self.SPEED_MAP["地铁/公交"]
        elif distance_km < 10.0:
            return self.SPEED_MAP["地铁或打车"]
        else:
            return self.SPEED_MAP["打车或地铁"]

        # 如果有交通描述，使用描述中的速度
        for mode, speed in self.SPEED_MAP.items():
            if mode in transport_desc:
                return speed

        return 22.0  # 默认速度

    def _nearest_neighbor(
        self,
        attractions: list[Attraction],
        start: Location | None,
        end: Location | None = None,
    ) -> list[Attraction]:
        remaining = list(attractions)
        if start is None:
            ordered = [remaining.pop(0)]
            current = ordered[0].location
        else:
            ordered = []
            current = start

        while remaining:
            next_index = min(
                range(len(remaining)),
                key=lambda index: (
                    self.distance(current, remaining[index].location)
                    + (0.2 * self.distance(remaining[index].location, end) if end else 0)
                ),
            )
            selected = remaining.pop(next_index)
            ordered.append(selected)
            current = selected.location
        return ordered

    def _route_distance(
        self,
        attractions: list[Attraction],
        start: Location | None,
        end: Location | None = None,
    ) -> float:
        if not attractions:
            return 0
        points = [attraction.location for attraction in attractions]
        if start is not None:
            points = [start, *points]
        if end is not None:
            points = [*points, end]
        return sum(self.distance(left, right) for left, right in zip(points, points[1:]))

    @staticmethod
    def distance(left: Location, right: Location) -> float:
        """Haversine 球面距离，单位公里。"""
        earth_radius_km = 6371.0088
        lat1, lon1 = radians(left.latitude), radians(left.longitude)
        lat2, lon2 = radians(right.latitude), radians(right.longitude)
        delta_lat = lat2 - lat1
        delta_lon = lon2 - lon1
        value = (
            sin(delta_lat / 2) ** 2
            + cos(lat1) * cos(lat2) * sin(delta_lon / 2) ** 2
        )
        return 2 * earth_radius_km * asin(sqrt(value))


route_optimizer = RouteOptimizer()
