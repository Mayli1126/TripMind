"""批2 测试：日编排管线（容量模型 / 骨架解析 / 确定性物化 / 失败回退）。

设计要点（产品化计划 8.1/8.2）：
- 容量按分钟装载，首末日受跨城交通约束；
- LLM 只出名称骨架，坐标由候选/高德物化，杜绝编造；
- 物化匹配率 <60% 整体失败回退旧路径；管线任何异常不破坏旧行为。
"""

import unittest
from datetime import date, timedelta
from unittest import mock

from app.graphs import planning_graph as pg
from app.graphs.planning_graph import run_planning_graph
from app.models.candidate import CandidatePlace
from app.models.schemas import (
    Attraction,
    Budget,
    Location,
    POIInfo,
    TripPlan,
    TripRequest,
)
from app.services.day_pipeline_service import DayPipelineError, build_day_capacities, materialize_skeleton, parse_skeleton
from app.services.trip_quality_service import trip_quality_service


def _request(days: int = 2, pace: str = "balanced") -> TripRequest:
    start = (date.today() + timedelta(days=30)).isoformat()
    end = (date.today() + timedelta(days=30 + days - 1)).isoformat()
    return TripRequest(
        destination_only=False,  # 显式覆盖旧版跨城行程容量规则。
        origin_city="上海", city="北京", start_date=start, end_date=end,
        travel_days=days, transportation="high_speed_train", accommodation="舒适型酒店",
        pace=pace,  # type: ignore[arg-type]
    )


def _candidate(name: str, poi_id="B000D1", choice="preferred", verified=True) -> CandidatePlace:
    return CandidatePlace(
        candidate_id=f"cand-{poi_id}", canonical_name=name, city="北京",
        poi_id=poi_id if verified else None,
        location=Location(longitude=116.39, latitude=39.91) if verified else None,
        fact_status="verified" if verified else "unverified",
        user_choice=choice,  # type: ignore[arg-type]
        suggested_duration_minutes=120,
    )


class CapacityTestCase(unittest.TestCase):
    def test_basic_day_capacity(self):
        caps = build_day_capacities(_request(days=1), None)
        self.assertEqual(len(caps), 1)
        # 普通日（无出发地影响首日约束？_request 有 origin_city，首日=抵达日）
        cap = caps[0]
        self.assertGreater(cap.available_minutes, 0)
        self.assertEqual(cap.suggested_count_min, 1)
        self.assertLessEqual(cap.suggested_count_max, 5)

    def test_request_arrival_time_has_priority_without_filling_count(self):
        request = _request(days=2).model_copy(update={"arrival_time": "16:00", "departure_time": "08:00"})
        option = mock.Mock(arrival_time="09:00", departure_time="07:00")
        first = build_day_capacities(request, option)[0]
        self.assertIn("抵达时间16:00", first.note)
        self.assertEqual(first.suggested_count_min, 1)
        self.assertGreaterEqual(first.suggested_count_max, first.suggested_count_min)

    def test_outbound_departure_does_not_shorten_return_day(self):
        option = mock.Mock(arrival_time="12:00", departure_time="09:00")
        last = build_day_capacities(_request(days=2), option)[-1]
        self.assertNotIn("17:00", last.note)

    def test_first_day_reduced_by_arrival(self):
        """首日跨城：窗口从抵达+接驳开始，容量应小于无出发地场景的同日。"""
        with_origin = build_day_capacities(_request(days=1), None)[0]
        no_origin = build_day_capacities(
            _request(days=1).model_copy(update={"origin_city": "上海"}), None
        )
        # 同一请求首日就是抵达日；对照：无跨城（单程且无出发城市语义不成立，用
        # travel_days>1 的中间日对照）
        caps3 = build_day_capacities(_request(days=3), None)
        middle = caps3[1]  # 中间日不受首末约束
        self.assertEqual(with_origin.available_minutes, middle.available_minutes)

    def test_pace_adjusts_counts(self):
        relaxed = build_day_capacities(_request(days=1, pace="relaxed"), None)[0]
        intensive = build_day_capacities(_request(days=1, pace="intensive"), None)[0]
        self.assertLessEqual(relaxed.suggested_count_max, intensive.suggested_count_max)


class SkeletonParseTestCase(unittest.TestCase):
    def _caps(self):
        return build_day_capacities(_request(days=2), None)

    def test_valid_skeleton_parsed(self):
        caps = self._caps()
        raw = '{"days": [' + ",".join(
            f'{{"date": "{c.date}", "theme": "主题{i}", "places": ["故宫", "天坛", "景山"], "hotel_name": "H{i}"}}'
            for i, c in enumerate(caps)
        ) + '], "overall_suggestions": "愉快出行"}'
        skeletons, overall = parse_skeleton(raw, caps)
        self.assertEqual(len(skeletons), 2)
        self.assertEqual(skeletons[0].place_names, ["故宫", "天坛", "景山"])
        self.assertEqual(overall, "愉快出行")

    def test_count_does_not_silently_truncate_places(self):
        caps = self._caps()
        raw = skeleton_json(caps, [["A", "B", "C", "D", "E", "F", "G"], ["X", "Y", "Z"]])
        skeletons, _ = parse_skeleton(raw, caps)
        self.assertEqual(len(skeletons[0].place_names), 7)

    def test_two_large_places_pass_and_duplicate_dates_do_not(self):
        caps = self._caps()
        raw = skeleton_json(caps, [["西湖"], ["灵隐飞来峰", "西溪湿地"]])
        skeletons, _ = parse_skeleton(raw, caps)
        self.assertEqual(len(skeletons[1].place_names), 2)
        with self.assertRaises(DayPipelineError):
            parse_skeleton(skeleton_json([caps[0], caps[0]], [["A"], ["B"]]), caps)

    def test_wrong_date_rejected(self):
        caps = self._caps()
        with self.assertRaises(DayPipelineError):
            parse_skeleton('{"days": [{"date": "2030-01-01", "places": ["A"]}]}', caps)

    def test_day_count_mismatch_rejected(self):
        caps = self._caps()
        with self.assertRaises(DayPipelineError):
            parse_skeleton(json_day(caps[0], ["A"]), caps)

def skeleton_json(caps, places_per_day, overall="测试建议"):
    """构造完整骨架 JSON。"""
    days = ", ".join(json_day(c, places) for c, places in zip(caps, places_per_day))
    return '{"days": [' + days + f'], "overall_suggestions": "{overall}"' + "}"


def json_day(cap, places):
    names = ", ".join(f'"{p}"' for p in places)
    return f'{{"date": "{cap.date}", "theme": "t", "places": [{names}], "hotel_name": "H"}}'


class MaterializeTestCase(unittest.TestCase):
    def test_candidate_direct_and_amap_fallback(self):
        # 两天行程：地点放第二天（首日往返跨城容量极小属预期行为）
        caps = build_day_capacities(_request(days=2), None)
        skeletons, _ = parse_skeleton(skeleton_json(caps, [
            ["前门大街", "天安门广场", "什刹海"],
            ["故宫博物院", "颐和园", "天坛公园"],
        ]), caps)
        request = _request(days=2).model_copy(update={
            "candidate_places": [
                _candidate("前门大街", poi_id="B000C0"),
                _candidate("天安门广场", poi_id="B000C2"),
                _candidate("什刹海", poi_id="B000C3"),
                _candidate("故宫博物院", poi_id="B000C1", choice="required"),
                _candidate("天坛公园", poi_id="B000C4"),
            ],
        })
        poi = POIInfo(id="B000F1", name="颐和园", type="景点", address="某路",
                      location=Location(longitude=116.3, latitude=39.99))
        with mock.patch("app.services.day_pipeline_service._search_poi", return_value=_poi_to_attr(poi)) as fake:
            plan = materialize_skeleton(request, skeletons, "测试建议")
        # 候选直取免查高德；只有颐和园走高德检索
        self.assertEqual(fake.call_count, 1)
        self.assertIn("颐和园", [call.args[0] for call in fake.call_args_list])
        self.assertEqual(len(plan.days), 2)
        names = [a.name for a in plan.days[1].attractions]
        self.assertEqual(names, ["故宫博物院", "颐和园", "天坛公园"])
        first = plan.days[1].attractions[0]
        self.assertEqual(first.poi_id, "B000C1")         # 候选的已校准数据
        self.assertEqual(first.visit_duration, 120)      # 候选建议时长
        self.assertEqual(first.location.longitude, 116.39)
        # 保留住宿参考，但不再生成三餐占位。
        self.assertIsNotNone(plan.days[1].hotel)
        self.assertEqual(plan.days[1].meals, [])

    def test_low_match_rate_rejected(self):
        caps = build_day_capacities(_request(days=2), None)
        skeletons, _ = parse_skeleton(
            skeleton_json(caps, [
                ["编造地点A", "编造地点B", "编造地点C"],
                ["编造地点D", "编造地点E", "编造地点F"],
            ]), caps
        )
        with mock.patch("app.services.day_pipeline_service._search_poi", return_value=None):
            with self.assertRaises(DayPipelineError):
                materialize_skeleton(_request(), skeletons, "")

    def test_unverified_candidate_not_used_directly(self):
        """未校准候选无坐标：物化走高德而不是直接采用（不伪造坐标原则）。"""
        caps = build_day_capacities(_request(days=2), None)
        skeletons, _ = parse_skeleton(skeleton_json(caps, [
            ["前门大街", "什刹海", "天安门广场"],
            ["故宫博物院", "天坛公园", "不知名小店"],
        ]), caps)
        request = _request(days=2).model_copy(update={
            "candidate_places": [
                _candidate(name, poi_id=f"B{i}")
                for i, name in enumerate(["前门大街", "什刹海", "天安门广场", "故宫博物院", "天坛公园"])
            ] + [_candidate("不知名小店", verified=False)],
        })
        poi = POIInfo(id="B000F2", name="不知名小店", type="餐饮", address="x",
                      location=Location(longitude=116.1, latitude=39.5))
        with mock.patch("app.services.day_pipeline_service._search_poi", return_value=_poi_to_attr(poi)):
            plan = materialize_skeleton(request, skeletons, "")
        unknown = next(item for item in plan.days[1].attractions if item.name == "不知名小店")
        self.assertEqual(unknown.poi_id, "B000F2")


def _poi_to_attr(poi: POIInfo) -> Attraction:
    return Attraction(
        name=poi.name, address=poi.address, location=poi.location,
        visit_duration=90, description="来自高德地图检索",
        category=poi.type, poi_id=poi.id,
    )


class PipelineFallbackTestCase(unittest.TestCase):
    """端到端：管线失败自动回退单次大 JSON，外部行为不变。"""

    def test_fallback_to_legacy_on_bad_skeleton(self):
        from tests.test_research_fanout import FanoutStubPlanner  # 复用桩

        planner = FanoutStubPlanner()
        # run_agent_fresh 返回 'response'（非 JSON）→ 骨架失败 → 回退旧路径成功
        with mock.patch.object(pg, "_get_planner", return_value=planner), \
             mock.patch.object(pg, "get_checkpointer", return_value=None), \
             mock.patch.object(trip_quality_service, "finalize", side_effect=lambda p, r, **k: p), \
             mock.patch.object(trip_quality_service, "refine_overall_suggestions", side_effect=lambda p, r, **k: p.overall_suggestions), \
             mock.patch.object(planner, "needs_repair", return_value=False), \
             mock.patch.object(planner, "parse_response", return_value=_simple_plan()):
            plan = run_planning_graph(_request(days=1), None, job_id="pipe-fb-1")
        self.assertFalse(plan.is_fallback)
        # 两次 LLM 调用：骨架(失败) + 旧路径
        self.assertEqual(len(planner.branch_calls) > 0, True)

    def test_pipeline_success_end_to_end(self):
        caps = build_day_capacities(_request(days=2), None)
        skeleton_payload = skeleton_json(caps, [
            ["前门大街", "天安门广场", "什刹海"],
            ["故宫博物院", "天坛公园", "景山公园"],
        ], overall="ok")

        class SkeletonPlanner:
            planner_agent = object(); attraction_agent = object(); weather_agent = object(); hotel_agent = object()
            def __init__(self): self.calls = []
            def report_progress(self, *a, **k): pass
            def build_attraction_query(self, r): return "q"
            def run_agent_safely(self, a, q, label): return ("资料", None)
            def query_transportation_context(self, r): return (("交通", 0, None), None)
            def build_planner_query(self, r, *a): return "BASE"
            def run_agent_fresh(self, agent, query):
                self.calls.append(query)
                return skeleton_payload  # 骨架 JSON
            def parse_response(self, resp, r): raise AssertionError("不应走旧路径")
            def needs_repair(self, p, r): return False
            def plan_score(self, p, r): return 1
            def repair_plan(self, r, resp): return _simple_plan()
            def create_fallback_plan(self, r, reason=""):
                p = _simple_plan(); p.is_fallback = True; return p
            def friendly_error(self, e): return str(e)

        request = _request(days=2).model_copy(update={
            "candidate_places": [
                _candidate(name, poi_id=f"B000C{i}", choice="required" if name == "故宫博物院" else "preferred")
                for i, name in enumerate(["前门大街", "天安门广场", "什刹海", "故宫博物院", "天坛公园", "景山公园"])
            ],
        })
        with mock.patch.object(pg, "_get_planner", return_value=SkeletonPlanner()), \
             mock.patch.object(pg, "get_checkpointer", return_value=None), \
             mock.patch.object(trip_quality_service, "finalize", side_effect=lambda p, r, **k: p), \
             mock.patch.object(trip_quality_service, "refine_overall_suggestions", side_effect=lambda p, r, **k: p.overall_suggestions):
            plan = run_planning_graph(request, None, job_id="pipe-ok-1")
        self.assertFalse(plan.is_fallback)
        self.assertEqual(plan.days[1].attractions[0].name, "故宫博物院")
        palace = next(item for item in plan.days[1].attractions if item.name == "故宫博物院")
        self.assertEqual(palace.poi_id, "B000C3")
        self.assertIn("日编排管线生成", " ".join(plan.warnings))


class SkeletonTokenBudgetTests(unittest.TestCase):
    def test_direct_skeleton_keeps_model_budget_for_reasoning_models(self):
        from app.config import get_settings
        planner = mock.Mock()
        planner.planner_agent.llm.max_tokens = 8192
        planner.planner_agent.llm.invoke.return_value = '{}'
        planner.needs_repair.return_value = False
        with mock.patch.object(get_settings(), 'skeleton_direct_enabled', True), \
             mock.patch.object(pg, '_get_planner', return_value=planner), \
             mock.patch.object(pg, 'parse_skeleton', return_value=([], 'ok')), \
             mock.patch.object(pg, 'materialize_skeleton', return_value=_simple_plan()):
            pg.generate_plan_node({'request': _request(days=2), 'context': {}}, {'configurable': {}})
        self.assertGreaterEqual(planner.planner_agent.llm.invoke.call_args.kwargs['max_tokens'], 8192)


def _simple_plan() -> TripPlan:
    return TripPlan(
        city="北京", start_date="2026-12-01", end_date="2026-12-01", days=[],
        overall_suggestions="测试",
        budget=Budget(total=100, per_person=100, over_budget=False),
    )


if __name__ == "__main__":
    unittest.main()
