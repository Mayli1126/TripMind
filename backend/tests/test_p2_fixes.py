"""P2 四连修测试：解析容错/URL 注释剥离/餐厅绑定去重/事件序号原子性。"""

import json
from datetime import date, timedelta
from pathlib import Path
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

class OptimisticLockTestCase(TestCase):
    """PlanPatch 乐观锁：base_version 与行程库版本不一致时拒绝修改。"""

    def _plan(self):
        return TripPlan(
            city="北京", start_date="2026-12-01", end_date="2026-12-01",
            days=[DayPlan(
                date="2026-12-01", day_index=0, description="d", transportation="地铁",
                accommodation="酒店",
                attractions=[Attraction(
                    name="故宫", address="a", location=Location(longitude=116.4, latitude=39.9),
                    visit_duration=90, description="",
                )],
                meals=[Meal(type="breakfast", name="早"), Meal(type="lunch", name="午"), Meal(type="dinner", name="晚")],
            )],
            overall_suggestions="s",
        )

    def _patch(self, base_version=1):
        from app.models.patch import PatchOperation, PlanPatch
        return PlanPatch(
            base_version=base_version,
            operations=[PatchOperation(op="change_pace", day_index=0, pace="relaxed")],
            affected_day_indexes=[0],
            explanation="放慢节奏",
        )

    def test_version_mismatch_rejected(self):
        from app.services.patch_service import apply_plan_patch
        new_plan, issues = apply_plan_patch(self._plan(), self._patch(1), current_version=3)
        self.assertTrue(any(i.code == "BASE_VERSION_CONFLICT" for i in issues))

    def test_version_match_applies(self):
        from app.services.patch_service import apply_plan_patch
        new_plan, issues = apply_plan_patch(self._plan(), self._patch(3), current_version=3)
        self.assertEqual(issues, [])
        self.assertEqual(new_plan.days[0].pace, "relaxed")

    def test_no_version_skips_check(self):
        # 未保存的会话快照场景：不传 current_version 不校验
        from app.services.patch_service import apply_plan_patch
        new_plan, issues = apply_plan_patch(self._plan(), self._patch(7), current_version=None)
        self.assertEqual(issues, [])


class CandidateChoiceSyncTestCase(TestCase):
    """融合后半：候选选择云端回写 + 跨篇时长冲突标记。"""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        from app.services.guide_store_service import GuideStoreService
        self.store = GuideStoreService(database_path=Path(self._tmp.name) / "t.db")

    def tearDown(self):
        self._tmp.cleanup()

    def _candidate(self, name, poi_id, duration):
        from app.models.candidate import CandidatePlace
        from app.models.schemas import Location as Loc
        return CandidatePlace(
            candidate_id=f"cand-{poi_id}", canonical_name=name, city="北京",
            poi_id=poi_id, location=Loc(longitude=116.4, latitude=39.9),
            fact_status="verified", suggested_duration_minutes=duration,
        )

    def test_choices_roundtrip(self):
        from app.models.guide import GuideInput, GuideParseResult

        record = self.store.create_record(GuideInput(input_type="text", content="x", city="北京"))
        result = GuideParseResult(
            source_id=record.id, parse_status="parsed", city="北京",
            candidates=[self._candidate("故宫", "B1", 90), self._candidate("天坛", "B2", 60)],
        )
        self.store.save_result(record.id, result)
        updated = self.store.update_candidate_choices(
            record.id, {"cand-B1": "required", "cand-B2": "excluded"}
        )
        self.assertIsNotNone(updated)
        choices = {c.canonical_name: c.user_choice for c in updated.result.candidates}
        self.assertEqual(choices, {"故宫": "required", "天坛": "excluded"})
        # 聚合口径读到同样状态
        again = self.store.get_record(record.id)
        self.assertEqual(again.result.candidates[0].user_choice, "required")

    def test_unknown_choice_id_ignored(self):
        from app.models.guide import GuideInput, GuideParseResult

        record = self.store.create_record(GuideInput(input_type="text", content="x"))
        self.store.save_result(record.id, GuideParseResult(
            source_id=record.id, parse_status="parsed",
            candidates=[self._candidate("故宫", "B1", 90)],
        ))
        updated = self.store.update_candidate_choices(record.id, {"cand-不存在": "required"})
        self.assertEqual(updated.result.candidates[0].user_choice, "preferred")  # 不变

    def test_conflicting_duration_marked(self):
        from app.services.candidate_pool_service import CandidatePool

        pool = CandidatePool()
        pool.add_or_merge(self._candidate("故宫", "B1", 90))
        cid, merged = pool.add_or_merge(self._candidate("故宫", "B1", 180))  # 同 poi 不同建议时长
        self.assertTrue(merged)
        place = pool.get(cid)
        self.assertTrue(any("时长不一致" in w for w in place.warnings), place.warnings)
        self.assertEqual(place.suggested_duration_minutes, 90)  # 先入池值保留，不猜答案
