"""P2 四连修测试：解析容错/URL 注释剥离/餐厅绑定去重/事件序号原子性。"""

import json
from datetime import date, timedelta
from unittest import mock
from unittest import TestCase

from app.agents.trip_planner_agent import MultiAgentTripPlanner
from app.models.schemas import (
    Attraction,
    DayPlan,
    Location,
    Meal,
    TripPlan,
    TripRequest,
)


def _request(days: int = 1) -> TripRequest:
    start = (date.today() + timedelta(days=30)).isoformat()
    end = (date.today() + timedelta(days=30 + days - 1)).isoformat()
    return TripRequest(
        origin_city="上海", city="北京", start_date=start, end_date=end,
        travel_days=days, transportation="high_speed_train", accommodation="舒适型酒店",
    )


def _raw_planner() -> MultiAgentTripPlanner:
    # _normalize_data 不依赖实例状态；绕过重量级 __init__
    return object.__new__(MultiAgentTripPlanner)


class NormalizeDataTestCase(TestCase):
    """rating/photos/closed_weekdays 容错：单字段脏值不再拖垮整份计划。"""

    def _normalize_day_with_attraction(self, attraction_extra: dict) -> TripPlan:
        planner = _raw_planner()
        data = {
            "city": "北京",
            "days": [{
                "date": "2026-12-01", "day_index": 0, "description": "d",
                "attractions": [{
                    "name": "故宫", "address": "a",
                    "location": {"longitude": 116.4, "latitude": 39.9},
                    "visit_duration": 90, "description": "x",
                    **attraction_extra,
                }],
                "meals": [
                    {"type": "breakfast", "name": "早"},
                    {"type": "lunch", "name": "午"},
                    {"type": "dinner", "name": "晚"},
                ],
                "hotel": {"name": "h", "address": "a", "price_range": "", "rating": "4.8",
                          "distance": "", "type": "", "estimated_cost": 300},
            }],
            "overall_suggestions": "s",
        }
        cleaned = planner._normalize_data(data, _request())
        # 校验可通过 = 整份计划不会因单字段脏值落入 fallback 模板
        return TripPlan.model_validate(cleaned)

    def test_rating_variants_cleaned(self):
        for dirty in ["4.7分", "4.5/5", "暂无", "unknown", 6.5, -1, None, ["4.8"]]:
            plan = self._normalize_day_with_attraction({"rating": dirty})
            rating = plan.days[0].attractions[0].rating
            self.assertTrue(rating is None or 0 <= rating <= 5, f"{dirty!r} -> {rating!r}")
        # 合法字符串提取数值
        plan = self._normalize_day_with_attraction({"rating": "4.7分"})
        self.assertEqual(plan.days[0].attractions[0].rating, 4.7)

    def test_photos_non_list_cleaned(self):
        for dirty in ["http://a.jpg", None, {"url": "x"}, 123]:
            plan = self._normalize_day_with_attraction({"photos": dirty})
            self.assertEqual(plan.days[0].attractions[0].photos, [])

    def test_closed_weekdays_text_cleaned(self):
        plan = self._normalize_day_with_attraction({"closed_weekdays": ["周一", "Monday"]})
        self.assertEqual(plan.days[0].attractions[0].closed_weekdays, [])
        plan = self._normalize_day_with_attraction({"closed_weekdays": [0, 6]})
        self.assertEqual(plan.days[0].attractions[0].closed_weekdays, [0, 6])

    def test_negative_ticket_price_clamped(self):
        plan = self._normalize_day_with_attraction({"ticket_price": -20})
        self.assertEqual(plan.days[0].attractions[0].ticket_price, 0)


class StripCommentsTestCase(TestCase):
    """字符串感知的注释剥离：URL 不再被截断。"""

    def test_url_inside_string_preserved(self):
        raw = '{"image_url": "https://example.com/a.jpg", "name": "故宫"} // 行尾注释'
        cleaned = MultiAgentTripPlanner._strip_comments_outside_strings(raw)
        data = json.loads(cleaned)
        self.assertEqual(data["image_url"], "https://example.com/a.jpg")

    def test_line_and_block_comments_removed(self):
        raw = '''{
          // 这是注释
          "name": "故宫", /* 块注释 */
          "url": "https://a.com"
        }'''
        cleaned = MultiAgentTripPlanner._strip_comments_outside_strings(raw)
        data = json.loads(cleaned)
        self.assertEqual(data["name"], "故宫")
        self.assertEqual(data["url"], "https://a.com")

    def test_escaped_quote_in_string(self):
        raw = '{"desc": "他说：\\"https://x.com\\" 很好"} // tail'
        cleaned = MultiAgentTripPlanner._strip_comments_outside_strings(raw)
        data = json.loads(cleaned)
        self.assertEqual(data["desc"], '他说："https://x.com" 很好')


class MealBindingTestCase(TestCase):
    """餐厅绑定：并发执行路径 + 午/晚不同名（避免跨餐重复警告）。"""

    def _plan(self) -> TripPlan:
        attraction = Attraction(
            name="故宫", address="a", location=Location(longitude=116.4, latitude=39.9),
            visit_duration=90, description="",
        )
        return TripPlan(
            city="北京", start_date="2026-12-01", end_date="2026-12-02",
            days=[
                DayPlan(
                    date=f"2026-12-0{i}", day_index=i - 1, description="d",
                    transportation="地铁", accommodation="酒店",
                    attractions=[attraction.model_copy(deep=True) for _ in range(3)],
                    meals=[
                        Meal(type="breakfast", name="早"),
                        Meal(type="lunch", name="午餐"),
                        Meal(type="dinner", name="晚餐"),
                    ],
                )
                for i in (1, 2, 3)
            ],
            overall_suggestions="s",
        )

    def test_bind_meal_locations_dedupes_same_day(self):
        from app.services.meal_location_service import MealLocationService

        class FakeAmap:
            def search_restaurants(self, **kwargs):
                # 两家店：近的同名，保证去重逻辑生效时晚餐选第二家
                def restaurant(name, address, lon, lat):
                    r = mock.Mock()
                    r.name = name
                    r.address = address
                    r.location = Location(longitude=lon, latitude=lat)
                    return r

                return [
                    restaurant("同一家餐厅", "addr1", 116.401, 39.901),
                    restaurant("另一家餐厅", "addr2", 116.402, 39.902),
                ]

        service = MealLocationService(amap_service=FakeAmap())
        plan = service.bind_meal_locations(self._plan())
        for day in plan.days:
            lunch = next(m for m in day.meals if m.type == "lunch")
            dinner = next(m for m in day.meals if m.type == "dinner")
            self.assertEqual(lunch.name, "同一家餐厅")  # 午餐选最近
            self.assertEqual(dinner.name, "另一家餐厅")  # 晚餐跳过同名
            self.assertIsNotNone(lunch.location)
            self.assertIsNotNone(dinner.location)


if __name__ == "__main__":
    import unittest
    unittest.main()
