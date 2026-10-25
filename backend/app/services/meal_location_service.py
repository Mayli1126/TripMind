"""用餐地点地理绑定服务 - 将餐食与附近景点关联。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

from ..models.schemas import DayPlan, Location, Meal, TripPlan
from .amap_service import AmapService


class MealLocationService:
    """用餐地点地理绑定服务。

    功能：
    1. 根据景点位置搜索附近餐厅
    2. 午餐绑定到上午最后一个景点附近
    3. 晚餐绑定到下午最后一个景点附近
    4. 餐食作为必经节点参与路线优化
    """

    def __init__(self, amap_service: Optional[AmapService] = None):
        self.amap_service = amap_service or AmapService()

    def bind_meal_locations(self, plan: TripPlan) -> TripPlan:
        """为所有餐食绑定地理位置（按天并发）。

        每天固定 2 次高德餐厅检索（午/晚），串行时长行程（30 天 = 60 次往返）
        要数分钟；MCP 会话池为 4 路轮询线程安全，按天并发 4 路即可。
        各天只原地写自己的 meals，无共享写冲突。
        """
        if len(plan.days) > 1:
            with ThreadPoolExecutor(max_workers=min(4, len(plan.days))) as pool:
                list(pool.map(lambda d: self._bind_day_meal_locations(d, plan.city), plan.days))
        else:
            for day in plan.days:
                self._bind_day_meal_locations(day, plan.city)
        return plan

    def _bind_day_meal_locations(self, day: DayPlan, city: str) -> DayPlan:
        """为单日餐食绑定地理位置。"""
        if not day.attractions:
            return day

        # 获取上午和下午的景点
        morning_attractions = day.attractions[:2]  # 前2个景点通常是上午的
        afternoon_attractions = day.attractions[2:]  # 后面的景点通常是下午的

        # 午餐绑定到上午最后一个景点附近
        if morning_attractions:
            lunch_location = morning_attractions[-1].location
            day = self._bind_meal_location(day, "lunch", lunch_location, city)

        # 晚餐绑定到下午最后一个景点附近，如果下午没有景点则绑定到全天最后一个景点附近
        if afternoon_attractions:
            dinner_location = afternoon_attractions[-1].location
        else:
            dinner_location = day.attractions[-1].location
        day = self._bind_meal_location(day, "dinner", dinner_location, city)

        return day

    def _bind_meal_location(self, day: DayPlan, meal_type: str, location: Optional[Location], city: str) -> DayPlan:
        """为特定类型餐食绑定地理位置。"""
        if not location:
            return day

        # 搜索附近餐厅
        restaurants = self.amap_service.search_restaurants(
            keywords="美食",
            city=city,
            location=location,
            radius=2000
        )

        # 查找对应的餐食并更新
        for meal in day.meals:
            if meal.type == meal_type and not meal.location:
                if restaurants:
                    # 选择最近的餐厅；跳过本日已绑过的同名餐厅，
                    # 避免午/晚绑到同一家触发"跨餐重复"校验警告
                    used_names = {m.name for m in day.meals if m.location and m.name != meal.name}
                    candidates = [r for r in restaurants if r.name not in used_names] or restaurants
                    nearest = min(candidates, key=lambda r: self._distance(r.location, location))
                    meal.name = nearest.name
                    meal.address = nearest.address
                    meal.location = nearest.location
                else:
                    # 使用景点位置作为备用
                    meal.location = location
                    meal.address = f"{city}附近"
        return day

    def _distance(self, loc1: Location, loc2: Location) -> float:
        """计算两点间距离（简单欧氏距离）。"""
        return ((loc1.longitude - loc2.longitude) ** 2 + 
                (loc1.latitude - loc2.latitude) ** 2) ** 0.5


meal_location_service = MealLocationService()