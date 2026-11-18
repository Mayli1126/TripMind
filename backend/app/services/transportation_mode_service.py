"""交通方式差异化服务 - 根据距离选择合适的市内交通方式。"""

from __future__ import annotations

from typing import Dict

from ..models.schemas import DayPlan, Location, TripPlan


class TransportationModeService:
    """交通方式差异化服务。
    
    根据距离选择交通方式：
    - < 1.5km：步行（15-20 min）
    - 1.5-5km：地铁/公交（30-45 min）
    - 5-10km：地铁或打车（45-60 min）
    - > 10km：打车或地铁（60+ min）
    """

    # 距离阈值（公里）
    WALKING_THRESHOLD = 1.5
    TRANSIT_THRESHOLD = 5.0
    TAXI_THRESHOLD = 10.0

    # 速度估算（公里/小时）
    WALKING_SPEED = 5.0
    TRANSIT_SPEED = 15.0
    TAXI_SPEED = 30.0

    def __init__(self):
        pass

    def calculate_transport_between_points(self, loc1: Location, loc2: Location) -> Dict[str, float]:
        """计算两点间的交通方式和时间。
        
        返回：
            dict: {"mode": 交通方式, "duration_minutes": 分钟, "distance_km": 公里}
        """
        distance = self._haversine_distance(loc1, loc2)
        
        if distance < self.WALKING_THRESHOLD:
            return {
                "mode": "步行",
                "duration_minutes": round(distance / self.WALKING_SPEED * 60),
                "distance_km": round(distance, 2),
                "cost": 0
            }
        elif distance < self.TRANSIT_THRESHOLD:
            return {
                "mode": "地铁/公交",
                "duration_minutes": round(distance / self.TRANSIT_SPEED * 60) + 10,  # +10分钟等车
                "distance_km": round(distance, 2),
                "cost": 5
            }
        elif distance < self.TAXI_THRESHOLD:
            return {
                "mode": "地铁或打车",
                "duration_minutes": round(distance / self.TAXI_SPEED * 60) + 15,
                "distance_km": round(distance, 2),
                "cost": round(distance * 2.5)  # 粗略估算打车费用
            }
        else:
            return {
                "mode": "打车或地铁",
                "duration_minutes": round(distance / self.TAXI_SPEED * 60) + 20,
                "distance_km": round(distance, 2),
                "cost": round(distance * 2.5)
            }

    def optimize_daily_transport(self, plan: TripPlan) -> TripPlan:
        """优化每日交通方式。"""
        for day in plan.days:
            day = self._optimize_day_transport(day)
        return plan

    def _optimize_day_transport(self, day: DayPlan) -> DayPlan:
        """优化单日交通方式。"""
        if len(day.attractions) < 2:
            return day

        transport_details = []

        # 计算景点间交通
        for i in range(len(day.attractions) - 1):
            current = day.attractions[i]
            next_attr = day.attractions[i + 1]
            
            if current.location and next_attr.location:
                transport = self.calculate_transport_between_points(current.location, next_attr.location)
                transport_details.append(transport)

        # 更新当日交通信息
        day.transportation = ", ".join(set(t["mode"] for t in transport_details)) if transport_details else "步行"
        
        return day

    def _haversine_distance(self, loc1: Location, loc2: Location) -> float:
        """Haversine 球面距离，单位公里。"""
        from math import asin, cos, radians, sin, sqrt
        
        earth_radius_km = 6371.0088
        lat1, lon1 = radians(loc1.latitude), radians(loc1.longitude)
        lat2, lon2 = radians(loc2.latitude), radians(loc2.longitude)
        delta_lat = lat2 - lat1
        delta_lon = lon2 - lon1
        
        value = (
            sin(delta_lat / 2) ** 2
            + cos(lat1) * cos(lat2) * sin(delta_lon / 2) ** 2
        )
        return 2 * earth_radius_km * asin(sqrt(value))


transportation_mode_service = TransportationModeService()
