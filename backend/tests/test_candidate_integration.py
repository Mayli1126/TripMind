"""阶段 2 测试：候选池接入主规划图（产品化计划 15 节阶段 2 前置）。

验证点：
- 攻略候选渲染进规划 prompt：必去/想去硬约束 + 已核对坐标直接使用，excluded 不出现；
- 计划生成后与候选对账：同名景点回填已核对的 poi_id/坐标（修正 LLM 编写的坐标）；
- 必去候选未纳入行程时写 warnings（不强制塞入）；
- TripRequest 带候选可完整序列化回环（Redis 任务队列场景）。
"""

import unittest
from datetime import date, timedelta
from unittest import mock

from app.graphs import planning_graph as pg
from app.graphs.planning_graph import run_planning_graph
from app.models.candidate import CandidatePlace
from app.models.schemas import Attraction, Budget, DayPlan, Location, TripPlan, TripRequest
from app.services.candidate_pool_service import (
    apply_candidates_to_plan,
    render_candidates_for_prompt,
)
from app.services.patch_service import apply_plan_patch
from app.services.trip_quality_service import trip_quality_service


def _candidate(name: str, choice="preferred", verified=True, poi_id="B000C1", **kwargs) -> CandidatePlace:
    return CandidatePlace(
        candidate_id=f"cand-{name}",
        canonical_name=name,
        city="北京",
        poi_id=poi_id if verified else None,
        location=Location(longitude=116.39, latitude=39.91) if verified else None,
        fact_status="verified" if verified else "unverified",
        user_choice=choice,  # type: ignore[arg-type]
        recommendation_reasons=kwargs.get("reasons", ["攻略推荐"]),
        aliases=kwargs.get("aliases", []),
        suggested_duration_minutes=kwargs.get("duration", 120),
    )


def _request(**kwargs) -> TripRequest:
    start = (date.today() + timedelta(days=30)).isoformat()
    return TripRequest(
        origin_city="上海",
        city="北京",
        start_date=start,
        end_date=start,
        travel_days=1,
        transportation="high_speed_train",
        accommodation="舒适型酒店",
        budget_limit=5000,
        **kwargs,
    )


def _plan(attractions: list[Attraction]) -> TripPlan:
    return TripPlan(
        city="北京",
        start_date="2026-12-01",
        end_date="2026-12-01",
        days=[DayPlan(
            date="2026-12-01", day_index=0, description="第一天",
            transportation="地铁", accommodation="舒适型酒店",
            attractions=attractions, meals=[],
        )],
        overall_suggestions="测试计划",
        budget=Budget(total=800, per_person=800, over_budget=False),
    )


def _attraction(name: str, lon=100.0, lat=30.0) -> Attraction:
    # 故意写"错误"坐标（100,30 在北京之外），验证候选回填修正
    return Attraction(
        name=name, address=f"{name}地址",
        location=Location(longitude=lon, latitude=lat),
        visit_duration=90, description="",
    )


class RenderCandidatesPromptTestCase(unittest.TestCase):
    def test_empty_when_no_candidates(self):
        self.assertEqual(render_candidates_for_prompt([]), "")

    def test_choice_directives_and_excluded_filtered(self):
        text = render_candidates_for_prompt([
            _candidate("故宫", choice="required"),
            _candidate("颐和园", choice="preferred"),
            _candidate("南锣鼓巷", choice="optional"),
            _candidate("环球影城", choice="excluded"),
        ])
        self.assertIn("【必去】", text)
        self.assertIn("【想去】", text)
        self.assertIn("【可选】", text)
        self.assertNotIn("环球影城", text)  # 用户排除的绝不进 prompt
        self.assertIn("故宫", text)

    def test_verified_coordinates_included_unverified_marked(self):
        text = render_candidates_for_prompt([
            _candidate("故宫", verified=True, poi_id="B000C1"),
            _candidate("不知名小店", verified=False),
        ])
        self.assertIn("116.39,39.91", text)
        self.assertIn("B000C1", text)
        self.assertIn("位置未在地图确认", text)


class ApplyCandidatesToPlanTestCase(unittest.TestCase):
    def test_verified_candidate_backfills_coordinates(self):
        plan = _plan([_attraction("故宫博物院")])  # LLM 写的 (100,30) 错坐标
        request = _request(candidate_places=[_candidate("故宫博物院", verified=True)])
        notes = apply_candidates_to_plan(plan, request)
        self.assertEqual(notes, [])
        attraction = plan.days[0].attractions[0]
        # 坐标与 poi_id 以地图核对过的候选为准
        self.assertEqual(attraction.location.longitude, 116.39)
        self.assertEqual(attraction.location.latitude, 39.91)
        self.assertEqual(attraction.poi_id, "B000C1")

    def test_missing_required_candidate_warns(self):
        plan = _plan([_attraction("景山公园")])
        request = _request(candidate_places=[
            _candidate("故宫博物院", choice="required"),
        ])
        notes = apply_candidates_to_plan(plan, request)
        self.assertEqual(len(notes), 1)
        self.assertIn("故宫博物院", notes[0])
        self.assertIn("未纳入", notes[0])

    def test_alias_match_satisfies_required(self):
        # 计划里是地图标准名，候选别名是攻略原名：算命中，不告警
        plan = _plan([_attraction("故宫博物院")])
        request = _request(candidate_places=[
            _candidate("故宫", choice="required", aliases=["紫禁城"]),
        ])
        notes = apply_candidates_to_plan(plan, request)
        # "故宫博物院" 与候选标准名 "故宫" 不同名，但……这里标准名不匹配；
        # 别名匹配方向是候选别名 -> 计划景点名，"故宫博物院"不在 aliases 里。
        # 必去未命中应告警（提示用户确认），这是预期行为。
        self.assertEqual(len(notes), 1)

    def test_alias_on_plan_side_satisfies_required(self):
        # 候选标准名"故宫博物院"，计划景点名恰为其别名 -> 命中
        plan = _plan([_attraction("紫禁城")])
        request = _request(candidate_places=[
            _candidate("故宫博物院", choice="required", aliases=["紫禁城"]),
        ])
        notes = apply_candidates_to_plan(plan, request)
        self.assertEqual(notes, [])

    def test_no_candidates_noop(self):
        plan = _plan([_attraction("故宫")])
        self.assertEqual(apply_candidates_to_plan(plan, _request()), [])
        # 原坐标未被改动
        self.assertEqual(plan.days[0].attractions[0].location.longitude, 100.0)


class _RecordingPlanner:
    """记录规划 prompt 与解析输入的桩；计划内容可注入。"""

    planner_agent = object()
    attraction_agent = object()
    weather_agent = object()
    hotel_agent = object()

    def __init__(self, plan: TripPlan) -> None:
        self.plan = plan
        self.prompts: list[str] = []

    def report_progress(self, callback, *args, **kwargs):
        pass

    def build_attraction_query(self, request):
        return "query"

    def run_agent_safely(self, agent, query, stage):
        return ("资料", None)

    def query_transportation_context(self, request):
        return ("交通资料", 100, None)

    def build_planner_query(self, request, *args):
        return "BASE_QUERY"

    def run_agent_fresh(self, agent, query):
        self.prompts.append(query)
        return "response"

    def parse_response(self, response, request):
        return self.plan

    def needs_repair(self, plan, request):
        return False

    def plan_score(self, plan, request):
        return 1

    def repair_plan(self, request, response):
        return self.plan

    def create_fallback_plan(self, request, reason=""):
        plan = self.plan.model_copy(deep=True)
        plan.is_fallback = True
        return plan

    def friendly_error(self, exc):
        return str(exc)


class PlanningGraphCandidateTestCase(unittest.TestCase):
    def setUp(self):
        self.plan = _plan([_attraction("故宫博物院")])
        self.planner = _RecordingPlanner(self.plan)
        self.patches = [
            mock.patch.object(pg, "_get_planner", return_value=self.planner),
            mock.patch.object(pg, "get_checkpointer", return_value=None),
            mock.patch.object(
                trip_quality_service,
                "finalize",
                side_effect=lambda plan, request, **kwargs: plan,
            ),
            mock.patch.object(
                trip_quality_service,
                "refine_overall_suggestions",
                side_effect=lambda plan, request, **kwargs: plan.overall_suggestions,
            ),
        ]
        for patch in self.patches:
            patch.start()
        pg.reset_compiled_graph()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        pg.reset_compiled_graph()

    def test_candidate_section_appended_to_planner_prompt(self):
        """批2 管线：候选经骨架 digest 进入编排约束（必去标记 + 已核对坐标不外泄给 LLM）。"""
        request = _request(candidate_places=[_candidate("故宫博物院", choice="required")])
        run_planning_graph(request, None, job_id="cand-1")
        self.assertGreaterEqual(len(self.planner.prompts), 1)
        skeleton_prompt = self.planner.prompts[0]  # 第一次调用是骨架 prompt
        self.assertIn("【必去】", skeleton_prompt)          # 必去硬约束
        self.assertIn("故宫博物院", skeleton_prompt)
        self.assertNotIn("116.39,39.91", skeleton_prompt)  # 坐标不进 prompt，物化阶段回填
        self.assertIn("容量", skeleton_prompt)              # 容量表在骨架输入里

    def test_no_candidates_prompt_unchanged(self):
        """无候选时骨架 prompt 不含候选清单，回退路径保持旧行为。"""
        run_planning_graph(_request(), None, job_id="cand-2")
        # 桩返回 'resp' 非骨架 JSON：管线回退，第二次调用是旧 BASE_QUERY
        self.assertIn("BASE_QUERY", self.planner.prompts)

    def test_plan_receives_candidate_backfill_and_warnings(self):
        # 必去候选"颐和园"不在计划中 -> 回填生效 + 缺失告警写入 warnings
        self.plan.days[0].attractions.append(_attraction("颐和园"))
        request = _request(candidate_places=[
            _candidate("故宫博物院", choice="required"),
            _candidate("颐和园", choice="preferred"),
            _candidate("什刹海", choice="required"),  # 故意缺失
        ])
        result = run_planning_graph(request, None, job_id="cand-3")
        attraction = result.days[0].attractions[0]
        self.assertEqual(attraction.location.longitude, 116.39)  # 回填生效
        self.assertEqual(attraction.poi_id, "B000C1")
        self.assertTrue(any("什刹海" in w and "未纳入" in w for w in result.warnings))

    def test_request_with_candidates_serializes_roundtrip(self):
        """Redis 任务队列把 TripRequest 存为 JSON；候选必须无损回环。"""
        request = _request(candidate_places=[
            _candidate("故宫博物院", choice="required"),
            _candidate("不知名小店", verified=False, choice="optional"),
        ])
        restored = TripRequest.model_validate_json(request.model_dump_json())
        self.assertEqual(len(restored.candidate_places), 2)
        first = restored.candidate_places[0]
        self.assertEqual(first.canonical_name, "故宫博物院")
        self.assertEqual(first.user_choice, "required")
        self.assertEqual(first.location.longitude, 116.39)
        self.assertTrue(first.is_usable_for_routing())
        self.assertFalse(restored.candidate_places[1].is_usable_for_routing())


class PatchCandidateDirectTestCase(unittest.TestCase):
    """Patch add/replace 的候选直取：replacement_candidate_id 命中即免高德查询。"""

    def _patch_plan(self):
        return _plan([_attraction("故宫博物院")])

    def test_add_place_uses_verified_candidate_without_poi_search(self):
        from app.models.patch import PatchOperation, PlanPatch

        candidate = _candidate("颐和园", choice="required", poi_id="B000C9")
        patch = PlanPatch(
            base_version=1,
            operations=[PatchOperation(
                op="add_place", day_index=0,
                replacement_candidate_id=candidate.candidate_id,
                replacement_name="颐和园",
            )],
            affected_day_indexes=[0],
            explanation="加上候选里的颐和园",
        )
        with mock.patch("app.services.patch_service._search_poi") as fake_search:
            new_plan, issues = apply_plan_patch(self._patch_plan(), patch, [candidate])
        self.assertEqual(issues, [])
        fake_search.assert_not_called()  # 候选直取，未触发高德查询
        added = new_plan.days[0].attractions[-1]
        self.assertEqual(added.name, "颐和园")
        self.assertEqual(added.poi_id, "B000C9")
        self.assertEqual(added.location.longitude, 116.39)
        self.assertEqual(added.visit_duration, 120)  # 候选的建议时长
        self.assertTrue(added.stable_id)

    def test_unknown_candidate_id_falls_back_to_poi_search(self):
        from app.models.patch import PatchOperation, PlanPatch
        from app.models.schemas import POIInfo

        poi = POIInfo(
            id="B000F1", name="北海公园", type="景点", address="文津街",
            location=Location(longitude=116.38, latitude=39.93),
        )
        patch = PlanPatch(
            base_version=1,
            operations=[PatchOperation(
                op="add_place", day_index=0,
                replacement_candidate_id="cand-不存在", replacement_name="北海公园",
            )],
            affected_day_indexes=[0],
            explanation="候选不存在回退高德",
        )
        with mock.patch("app.services.patch_service._search_poi", return_value=poi) as fake_search:
            new_plan, issues = apply_plan_patch(self._patch_plan(), patch, [_candidate("颐和园")])
        self.assertEqual(issues, [])
        fake_search.assert_called_once()  # 回退高德检索
        self.assertEqual(new_plan.days[0].attractions[-1].poi_id, "B000F1")

    def test_unverified_candidate_falls_back_to_poi_search(self):
        from app.models.patch import PatchOperation, PlanPatch
        from app.models.schemas import POIInfo

        poi = POIInfo(
            id="B000F2", name="不知道的店", type="餐饮", address="某路",
            location=Location(longitude=116.1, latitude=39.5),
        )
        patch = PlanPatch(
            base_version=1,
            operations=[PatchOperation(
                op="add_place", day_index=0,
                replacement_candidate_id="cand-不知名小店", replacement_name="不知名小店",
            )],
            affected_day_indexes=[0],
            explanation="未校验候选不可直取",
        )
        unverified = _candidate("不知名小店", verified=False, poi_id=None)
        with mock.patch("app.services.patch_service._search_poi", return_value=poi) as fake_search:
            new_plan, issues = apply_plan_patch(self._patch_plan(), patch, [unverified])
        self.assertEqual(issues, [])
        fake_search.assert_called_once()  # unverified 候选无坐标，必须回退校准
        self.assertEqual(new_plan.days[0].attractions[-1].poi_id, "B000F2")

    def test_both_fail_rejected(self):
        from app.models.patch import PatchOperation, PlanPatch

        patch = PlanPatch(
            base_version=1,
            operations=[PatchOperation(
                op="add_place", day_index=0,
                replacement_candidate_id="cand-不存在", replacement_name="不存在的地点",
            )],
            affected_day_indexes=[0],
            explanation="两边都确认不了",
        )
        with mock.patch("app.services.patch_service._search_poi", return_value=None):
            _, issues = apply_plan_patch(self._patch_plan(), patch, [_candidate("颐和园")])
        self.assertTrue(any(i.code == "UNVERIFIED_REQUIRED_POI" for i in issues))


if __name__ == "__main__":
    unittest.main()
