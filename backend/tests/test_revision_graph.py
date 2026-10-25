"""阶段1批1 测试：RevisionGraph + PlanPatch 局部修改。

核心验收（产品化计划 16 节场景 5）：
- "第二天少走路"只产生第二天 Patch，第一天的 stable_id 与内容不变；
- Patch 校验失败时保留原版本；
- 解析失败/超范围回退 legacy revise_trip；
- add/replace 必须经高德 POI 校准，查不到拒绝修改（不伪造坐标）。
"""

import unittest
from datetime import date, timedelta
from unittest import mock

from app.graphs import revision_graph as rg
from app.graphs.revision_graph import run_revision_graph
from app.models.patch import PlanPatch, PatchOperation
from app.models.schemas import Attraction, Budget, DayPlan, Location, TripPlan, TripRequest
from app.services.patch_service import apply_plan_patch, ensure_stable_ids


def _attraction(name: str, ticket: int = 0) -> Attraction:
    return Attraction(
        name=name,
        address=f"{name}地址",
        location=Location(longitude=116.4, latitude=39.9),
        visit_duration=90,
        description="",
        ticket_price=ticket,
    )


def _request() -> TripRequest:
    start = (date.today() + timedelta(days=30)).isoformat()
    end = (date.today() + timedelta(days=31)).isoformat()
    return TripRequest(
        origin_city="上海",
        city="北京",
        start_date=start,
        end_date=end,
        travel_days=2,
        transportation="high_speed_train",
        accommodation="舒适型酒店",
        budget_limit=5000,
    )


def _plan() -> TripPlan:
    days = [
        DayPlan(
            date="2026-12-01",
            day_index=0,
            description="第一天：经典城区",
            transportation="地铁",
            accommodation="舒适型酒店",
            attractions=[_attraction("故宫博物院", 60), _attraction("景山公园", 2)],
        ),
        DayPlan(
            date="2026-12-02",
            day_index=1,
            description="第二天：文艺街区",
            transportation="公交",
            accommodation="舒适型酒店",
            attractions=[_attraction("南锣鼓巷"), _attraction("什刹海"), _attraction("雍和宫", 25)],
        ),
    ]
    plan = TripPlan(
        city="北京",
        start_date="2026-12-01",
        end_date="2026-12-02",
        days=days,
        overall_suggestions="测试计划",
        budget=Budget(total=800, per_person=800, over_budget=False),
    )
    ensure_stable_ids(plan)
    return plan


class ApplyPatchTestCase(unittest.TestCase):
    def test_move_place_only_touches_target_day(self):
        plan = _plan()
        day1_ids_before = [a.stable_id for a in plan.days[0].attractions]
        target = plan.days[1].attractions[0].stable_id
        patch = PlanPatch(
            base_version=1,
            operations=[PatchOperation(op="move_place", target_stable_id=target, day_index=0, position=0, reason="测试")],
            affected_day_indexes=[0],
            preserved_stable_ids=day1_ids_before,
            explanation="把南锣鼓巷移到第一天",
        )
        new_plan, issues = apply_plan_patch(plan, patch)
        self.assertEqual(issues, [])
        # 第一天多了一个移动过来的景点，但原有 stable_id 全部保留
        self.assertEqual(target, new_plan.days[0].attractions[0].stable_id)
        self.assertEqual(
            day1_ids_before,
            [a.stable_id for a in new_plan.days[0].attractions[1:]],
        )
        # 第二天少了一个
        self.assertEqual(len(new_plan.days[1].attractions), 2)

    def test_change_duration_and_pace(self):
        plan = _plan()
        target = plan.days[1].attractions[2].stable_id
        patch = PlanPatch(
            base_version=1,
            operations=[
                PatchOperation(op="change_duration", target_stable_id=target, duration_minutes=45),
                PatchOperation(op="change_pace", day_index=1, pace="relaxed"),
            ],
            affected_day_indexes=[1],
            explanation="雍和宫缩短，第二天放缓",
        )
        new_plan, issues = apply_plan_patch(plan, patch)
        self.assertEqual(issues, [])
        self.assertEqual(new_plan.days[1].attractions[2].visit_duration, 45)
        self.assertEqual(new_plan.days[1].pace, "relaxed")
        # 第一天未被触碰
        self.assertEqual(new_plan.days[0].pace, "")

    def test_preserved_ids_violation_rejected(self):
        plan = _plan()
        day1_id = plan.days[0].attractions[0].stable_id
        patch = PlanPatch(
            base_version=1,
            operations=[PatchOperation(op="remove_place", target_stable_id=day1_id)],
            affected_day_indexes=[0],
            preserved_stable_ids=[day1_id],
            explanation="试图删除受保护地点",
        )
        _, issues = apply_plan_patch(plan, patch)
        self.assertTrue(any(i.severity == "error" for i in issues))

    def test_add_place_without_poi_rejected(self):
        plan = _plan()
        with mock.patch("app.services.patch_service._search_poi", return_value=None):
            patch = PlanPatch(
                base_version=1,
                operations=[PatchOperation(op="add_place", day_index=1, replacement_name="不存在的景点")],
                affected_day_indexes=[1],
                explanation="新增无法确认的地点",
            )
            _, issues = apply_plan_patch(plan, patch)
        self.assertTrue(any(i.code == "UNVERIFIED_REQUIRED_POI" for i in issues))

    def test_add_place_with_poi_gets_real_coordinates(self):
        plan = _plan()
        from app.models.schemas import POIInfo

        poi = POIInfo(
            id="B000A8UIN8", name="颐和园", type="景点",
            address="新建宫门路", location=Location(longitude=116.27, latitude=39.99),
        )
        with mock.patch("app.services.patch_service._search_poi", return_value=poi):
            patch = PlanPatch(
                base_version=1,
                operations=[PatchOperation(op="add_place", day_index=1, replacement_name="颐和园")],
                affected_day_indexes=[1],
                explanation="第二天加颐和园",
            )
            new_plan, issues = apply_plan_patch(plan, patch)
        self.assertEqual(issues, [])
        added = new_plan.days[1].attractions[-1]
        self.assertEqual(added.poi_id, "B000A8UIN8")
        self.assertTrue(added.stable_id)


class _StubLLM:
    """固定输出的 LLM 替身（parse_patch 现在直连 LLMClient，不再经 planner agent）。"""

    def __init__(self, payload: str) -> None:
        self.payload = payload

    def invoke(self, messages, **kwargs) -> str:
        return self.payload


class _ParseFailedPlanner:
    """LLM 输出不含 JSON，触发回退 legacy revise_trip。"""

    planner_agent = object()

    def _report_progress(self, callback, *args, **kwargs):
        pass

    def _friendly_error(self, exc):
        return str(exc)

    def revise_trip(self, request, current_plan, instruction, progress_callback=None):
        result = current_plan.model_copy(deep=True)
        result.warnings.append(f"[legacy] 已根据修改要求更新：{instruction}")
        return result


class _OutOfScopePlanner(_ParseFailedPlanner):
    pass


class RevisionGraphTestCase(unittest.TestCase):
    def setUp(self):
        self.plan = _plan()

    def test_parse_failure_falls_back_to_legacy(self):
        with mock.patch.object(rg, "_get_planner", return_value=_ParseFailedPlanner()), \
             mock.patch.object(rg, "_get_patch_llm", return_value=_StubLLM("抱歉，我无法完成")):
            result = run_revision_graph(_request(), self.plan, "改一下吧")
        self.assertTrue(any("[legacy]" in w for w in result.warnings))
        # 原计划内容未被破坏
        self.assertEqual(len(result.days), 2)

    def test_out_of_scope_falls_back_to_legacy(self):
        with mock.patch.object(rg, "_get_planner", return_value=_OutOfScopePlanner()), \
             mock.patch.object(rg, "_get_patch_llm", return_value=_StubLLM('{"out_of_scope": true, "reason": "目的地变更"}')):
            result = run_revision_graph(_request(), self.plan, "目的地改成青岛")
        self.assertTrue(any("[legacy]" in w for w in result.warnings))

    def test_patch_path_applies_and_recomputes_budget(self):
        planner = _ParseFailedPlanner()
        target = self.plan.days[1].attractions[2].stable_id

        def fake_parse(self_stub, agent, query):
            return PlanPatch(
                base_version=1,
                operations=[PatchOperation(op="change_duration", target_stable_id=target, duration_minutes=30)],
                affected_day_indexes=[1],
                explanation="雍和宫只逛半小时",
            ).model_dump_json()

        with mock.patch.object(rg, "_get_planner", return_value=planner), \
             mock.patch.object(rg, "_get_patch_llm", return_value=_StubLLM("raw")), \
             mock.patch.object(rg, "_parse_patch_response", side_effect=lambda raw: PlanPatch.model_validate_json(fake_parse(None, None, None))):
            result = run_revision_graph(_request(), self.plan, "雍和宫只逛半小时")
        self.assertFalse(any("[legacy]" in w for w in result.warnings))
        self.assertEqual(result.days[1].attractions[2].visit_duration, 30)
        self.assertIsNotNone(result.budget)
        # 差异摘要随计划返回，供前端确认
        self.assertEqual(len(result.revision_diff), 1)
        self.assertEqual(result.revision_diff[0].op, "change_duration")
        self.assertEqual(result.revision_diff[0].day_index, 1)
        # 第一天完全不受影响
        self.assertEqual(
            [a.name for a in result.days[0].attractions],
            ["故宫博物院", "景山公园"],
        )


if __name__ == "__main__":
    unittest.main()
