"""体力曲线建模服务 - 考虑疲劳衰减和强度分配。"""

from __future__ import annotations


from ..models.schemas import Attraction, DayPlan, TripPlan, TripRequest


class PhysicalStrengthService:
    """体力曲线建模服务。
    
    功能：
    1. 为景点分配强度等级
    2. 每天前2个景点安排高强度
    3. 午餐后安排室内/休闲景点（体力低谷期）
    4. 连续高强度天后穿插轻松日
    5. 大型景点不安排在第一天或最后一天
    """

    # 景点强度定义
    HIGH_INTENSITY_CATEGORIES = {"山脉", "山峰", "长城", "爬山", "登山", "公园", "风景区", "自然"}
    MEDIUM_INTENSITY_CATEGORIES = {"历史文化", "博物馆", "故居", "纪念馆", "寺庙", "古城"}
    LOW_INTENSITY_CATEGORIES = {"商场", "购物", "美食", "咖啡馆", "室内", "展览", "剧院", "夜景"}

    def __init__(self):
        pass

    def calculate_attraction_intensity(self, attraction: Attraction) -> int:
        """计算景点强度等级 (1-5)。
        
        1: 非常轻松（购物、咖啡馆）
        2: 轻松（室内展览、美食）
        3: 适中（博物馆、历史景点）
        4: 较高（普通公园、街区漫步）
        5: 高强度（爬山、长城、大型景区）
        """
        name = attraction.name
        category = attraction.category or ""
        duration = attraction.visit_duration or 60

        # 根据名称判断
        for keyword in ["长城", "山", "峰", "岭", "坡", "攀登", "徒步", "栈道"]:
            if keyword in name:
                return 5

        for keyword in ["故宫", "颐和园", "天坛", "圆明园", "大型景区", "主题公园"]:
            if keyword in name:
                if duration >= 180:
                    return 5
                return 4

        for keyword in ["博物馆", "纪念馆", "故居", "寺庙", "古城", "古镇"]:
            if keyword in name:
                return 3

        for keyword in ["商场", "购物", "美食", "咖啡馆", "餐厅", "室内", "展览"]:
            if keyword in name:
                return 2

        for keyword in ["广场", "街", "路", "胡同", "夜景"]:
            if keyword in name:
                return 2

        # 根据类别判断
        if any(c in category for c in self.HIGH_INTENSITY_CATEGORIES):
            return 5
        if any(c in category for c in self.MEDIUM_INTENSITY_CATEGORIES):
            return 3
        if any(c in category for c in self.LOW_INTENSITY_CATEGORIES):
            return 2

        # 根据时长判断
        if duration >= 240:
            return 5
        if duration >= 180:
            return 4
        if duration >= 120:
            return 3
        if duration >= 60:
            return 2
        return 1

    def optimize_strength_distribution(self, plan: TripPlan, request: TripRequest) -> TripPlan:
        """优化每日体力分配。"""
        total_days = request.travel_days
        
        for day_index, day in enumerate(plan.days):
            if not day.attractions:
                continue

            # 判断这一天的角色
            is_first_day = day_index == 0
            is_last_day = day_index == total_days - 1
            is_middle_day = not is_first_day and not is_last_day

            # 如果是第一天或最后一天，降低整体强度
            if is_first_day or is_last_day:
                day = self._reduce_day_intensity(day, max_intensity=4)
            
            # 如果是中间天且前一天强度高，安排轻松日
            if is_middle_day and day_index > 0:
                prev_day = plan.days[day_index - 1]
                prev_intensity = sum(self.calculate_attraction_intensity(a) for a in prev_day.attractions) / len(prev_day.attractions) if prev_day.attractions else 0
                if prev_intensity >= 4:
                    day = self._reduce_day_intensity(day, max_intensity=3)

            # 调整当天景点顺序：上午高强度，下午低强度
            day = self._reorder_by_intensity(day)

            plan.days[day_index] = day

        return plan

    def _reduce_day_intensity(self, day: DayPlan, max_intensity: int) -> DayPlan:
        """降低单日最大强度。"""
        for attraction in day.attractions:
            intensity = self.calculate_attraction_intensity(attraction)
            if intensity > max_intensity:
                # 降低游览时长来减少强度
                if attraction.visit_duration and not attraction.match_evidence:
                    attraction.visit_duration = int(attraction.visit_duration * 0.7)
        return day

    def _reorder_by_intensity(self, day: DayPlan) -> DayPlan:
        """重新排序景点：上午高强度，下午低强度。"""
        if len(day.attractions) <= 2:
            return day

        # 获取景点和强度
        indexed = [(i, a, self.calculate_attraction_intensity(a)) for i, a in enumerate(day.attractions)]
        
        # 前2个位置放高强度景点
        high_intensity = [item for item in indexed if item[2] >= 4]
        low_intensity = [item for item in indexed if item[2] <= 2]
        medium_intensity = [item for item in indexed if item[2] == 3]

        # 重新排序：高强度 -> 中等强度 -> 低强度
        ordered = high_intensity + medium_intensity + low_intensity
        
        # 提取排序后的景点
        day.attractions = [item[1] for item in ordered]
        return day


physical_strength_service = PhysicalStrengthService()
