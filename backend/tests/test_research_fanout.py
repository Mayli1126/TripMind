"""阶段1批1 测试：research 拆成四个独立 LangGraph 节点并行扇出。

验证点（对应产品化计划 15 节阶段1批1）：
- 四节点并行执行，各自写 context key，merge_context reducer 正确合并；
- 单分支降级只产生自己的 warning，不影响其他分支与最终结果；
- 无 origin_city 时 intercity 分支静默跳过；
- 四个分支都被真实调用（扇出生效，而非旧的单节点串行包装）。
"""

import unittest
from datetime import date, timedelta
from unittest import mock

from app.graphs import planning_graph as pg
from app.graphs.planning_graph import run_planning_graph
from app.models.schemas import Budget, TripPlan, TripRequest
from app.services.trip_quality_service import trip_quality_service


def _request(origin_city: str = "上海") -> TripRequest:
    start = (date.today() + timedelta(days=30)).isoformat()
    return TripRequest(
        destination_only=False,  # 旧版跨城行程测试数据。
        origin_city=origin_city,
        city="北京",
        start_date=start,
        end_date=start,
        travel_days=1,
        transportation="high_speed_train",
        accommodation="舒适型酒店",
        budget_limit=5000,
    )


def _plan() -> TripPlan:
    return TripPlan(
        city="北京",
        start_date="2026-12-01",
        end_date="2026-12-01",
        days=[],
        overall_suggestions="测试计划",
        budget=Budget(total=800, per_person=800, over_budget=False),
    )


class FanoutStubPlanner:
    """记录每个研究分支的调用；分支行为可按 label 注入。"""

    planner_agent = object()
    attraction_agent = object()
    weather_agent = object()
    hotel_agent = object()

    def __init__(self):
        self.branch_calls: list[str] = []
        # label -> 抛出的异常 或 (value, warning) 返回值；缺省为正常返回
        self.behaviors: dict[str, Exception | tuple] = {}

    def report_progress(self, callback, *args, **kwargs):
        pass

    def _branch(self, label: str, default_value):
        self.branch_calls.append(label)
        behavior = self.behaviors.get(label)
        if isinstance(behavior, Exception):
            raise behavior
        if isinstance(behavior, tuple):
            return behavior
        return (default_value, None)

    def build_attraction_query(self, request):
        return "query"

    def run_agent_safely(self, agent, query, label):
        return self._branch(label, {"景点检索": "景点资料", "天气查询": "天气资料", "酒店检索": "酒店资料"}[label])

    def query_transportation_context(self, request):
        return self._branch("交通查询", ("交通资料", 100, None))

    def build_planner_query(self, request, *args):
        return "query"

    def run_agent_fresh(self, agent, query):
        return "response"

    def parse_response(self, response, request):
        return _plan()

    def needs_repair(self, plan, request):
        return False

    def plan_score(self, plan, request):
        return 1

    def repair_plan(self, request, response):
        return _plan()

    def create_fallback_plan(self, request, reason=""):
        plan = _plan()
        plan.is_fallback = True
        plan.warnings.append(reason)
        return plan

    def friendly_error(self, exc):
        return str(exc)


class ResearchFanoutTestCase(unittest.TestCase):
    def setUp(self):
        self.planner = FanoutStubPlanner()
        self.patches = [
            mock.patch.object(pg, "research_hotels", side_effect=self._hotels),
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

    def _hotels(self, request):
        # 酒店已切换固定检索；此处仍验证它作为独立研究分支被调用和降级。
        _, warning = self.planner._branch("酒店检索", "酒店资料")
        return [], warning

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        pg.reset_compiled_graph()

    def test_four_branches_all_called_and_context_merged(self):
        plan = run_planning_graph(_request(), None, job_id="fanout-1")
        self.assertFalse(plan.is_fallback)
        self.assertEqual(
            sorted(self.planner.branch_calls),
            sorted(["交通查询", "景点检索", "天气查询", "酒店检索"]),
        )

    def test_single_branch_degradation_only_its_warning(self):
        self.planner.behaviors["天气查询"] = ("天气查询暂不可用", "天气 Agent未完成：超时。")
        plan = run_planning_graph(_request(), None, job_id="fanout-2")
        self.assertFalse(plan.is_fallback)
        degraded = [w for w in plan.warnings if "天气" in w]
        self.assertEqual(len(degraded), 1)
        self.assertFalse(any("景点" in w or "酒店" in w for w in plan.warnings))

    def test_branch_exception_degrades_to_default(self):
        self.planner.behaviors["酒店检索"] = RuntimeError("网络中断")
        plan = run_planning_graph(_request(), None, job_id="fanout-3")
        self.assertFalse(plan.is_fallback)
        self.assertTrue(any("酒店" in w for w in plan.warnings))

    def test_no_origin_city_skips_intercity(self):
        # origin_city 是必填字段，这里模拟"出发地为空"的防御分支
        request = _request()
        object.__setattr__(request, "origin_city", "")
        plan = run_planning_graph(request, None, job_id="fanout-4")
        self.assertFalse(plan.is_fallback)
        self.assertNotIn("交通查询", self.planner.branch_calls)


if __name__ == "__main__":
    unittest.main()
