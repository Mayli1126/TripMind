"""阶段 E 测试：LangGraph Checkpointer + interrupt 暂停/恢复（预算确认场景）。

- 图执行层用 InMemorySaver 做 Checkpointer（不依赖 Redis Stack）；
- 任务状态层用 fakeredis 替换 redis_client._client；
- planner 用 Stub 替身，保证离线确定性（预算超/不超由住宿档次决定）。
"""

import unittest
from datetime import date, timedelta
from unittest import mock

import fakeredis
from langgraph.checkpoint.memory import InMemorySaver

from app.graphs import planning_graph as pg
from app.graphs.planning_graph import (
    PlanningWaitingInput,
    downgrade_accommodation,
    run_planning_graph,
)
from app.infrastructure import redis_client
from app.models.schemas import Budget, TripPlan, TripRequest
from app.services import planning_worker
from app.services.planning_job_service import RedisJobRecorder, planning_job_service
from app.services.trip_quality_service import trip_quality_service

_BUDGET_LIMIT = 1000


def _request(accommodation: str = "豪华酒店") -> TripRequest:
    start = (date.today() + timedelta(days=30)).isoformat()
    return TripRequest(
        origin_city="上海",
        city="北京",
        start_date=start,
        end_date=start,
        travel_days=1,
        transportation="high_speed_train",
        accommodation=accommodation,
        budget_limit=_BUDGET_LIMIT,
    )


def _plan(over_budget: bool) -> TripPlan:
    total = 2000 if over_budget else 800
    return TripPlan(
        city="北京",
        start_date="2026-12-01",
        end_date="2026-12-01",
        days=[],
        overall_suggestions="测试计划",
        budget=Budget(
            total=total,
            per_person=total,
            over_budget=over_budget,
            budget_limit=_BUDGET_LIMIT,
        ),
    )


class StubPlanner:
    """离线确定性替身：住宿降级到「民宿/经济型酒店」后才满足预算。"""

    planner_agent = object()  # generate_plan_node 会访问，仅作占位
    attraction_agent = object()
    weather_agent = object()
    hotel_agent = object()

    # 各研究分支的固定返回（label -> (value, warning)）
    branch_results: dict = {}

    def __init__(self):
        self.generated: list[str] = []

    def _report_progress(self, callback, *args, **kwargs):
        pass

    def _build_attraction_query(self, request):
        return "query"

    def _run_agent_safely(self, agent, query, label):
        return self.branch_results.get(label, {
            "景点检索": ("景点资料", None),
            "天气查询": ("天气资料", None),
            "酒店检索": ("酒店资料", None),
        }[label])

    def _query_transportation_context(self, request):
        return (("交通资料", 100, None), None)

    def _build_planner_query(self, request, *args):
        return "query"

    def _run_agent_fresh(self, agent, query):
        return "response"

    def _parse_response(self, response, request):
        self.generated.append(request.accommodation)
        return _plan(over_budget=request.accommodation not in ("经济型酒店", "民宿"))

    def _needs_repair(self, plan, request):
        return False

    def _plan_score(self, plan, request):
        return 1

    def _repair_plan(self, request, response):
        return _plan(over_budget=False)

    def _create_fallback_plan(self, request, reason=""):
        plan = _plan(over_budget=False)
        plan.is_fallback = True
        plan.warnings.append(reason)
        return plan

    def _friendly_error(self, exc):
        return str(exc)


class StageETestCase(unittest.TestCase):
    def setUp(self):
        self._orig_client = redis_client._client
        redis_client._client = fakeredis.FakeRedis(decode_responses=True)
        redis_client._group_ready = False

        self.planner = StubPlanner()
        self.patches = [
            mock.patch.object(pg, "get_checkpointer", return_value=InMemorySaver()),
            mock.patch.object(pg, "_get_planner", return_value=self.planner),
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
        redis_client._client = self._orig_client
        redis_client._group_ready = False

    # -- 降级映射 -----------------------------------------------------------

    def test_downgrade_accommodation_mapping(self):
        self.assertEqual(
            downgrade_accommodation(_request("豪华酒店")).accommodation, "豪华型酒店"
        )
        self.assertEqual(
            downgrade_accommodation(_request("舒适型酒店")).accommodation, "民宿"
        )
        self.assertIsNone(downgrade_accommodation(_request("经济型酒店")))

    # -- interrupt 暂停 / 恢复 ----------------------------------------------

    def test_budget_interrupt_payload(self):
        with self.assertRaises(PlanningWaitingInput) as ctx:
            run_planning_graph(_request("豪华酒店"), None, job_id="e2e-job")
        question = ctx.exception.question
        self.assertEqual(question["type"], "budget_confirm")
        self.assertEqual(question["over_budget_amount"], 1000)
        self.assertEqual(
            [opt["value"] for opt in question["options"]], ["downgrade", "keep"]
        )
        # 只生成过一次计划
        self.assertEqual(self.planner.generated, ["豪华酒店"])

    def test_resume_keep_keeps_over_budget_plan(self):
        with self.assertRaises(PlanningWaitingInput):
            run_planning_graph(_request("豪华酒店"), None, job_id="e2e-job")
        plan = run_planning_graph(
            _request("豪华酒店"), None, job_id="e2e-job", resume_value="keep"
        )
        self.assertEqual(plan.budget.total, 2000)
        self.assertTrue(
            any("保留" in warning for warning in plan.warnings), plan.warnings
        )
        self.assertEqual(self.planner.generated, ["豪华酒店"])

    def test_resume_downgrade_regenerates_within_budget(self):
        with self.assertRaises(PlanningWaitingInput):
            run_planning_graph(_request("舒适型酒店"), None, job_id="e2e-job")
        plan = run_planning_graph(
            _request("舒适型酒店"), None, job_id="e2e-job", resume_value="downgrade"
        )
        # 舒适型酒店 → 民宿，重生成后不再超预算
        self.assertEqual(self.planner.generated, ["舒适型酒店", "民宿"])
        self.assertEqual(plan.budget.total, 800)
        self.assertTrue(
            any("降级" in warning for warning in plan.warnings), plan.warnings
        )

    def test_no_interrupt_when_within_budget(self):
        plan = run_planning_graph(_request("经济型酒店"), None, job_id="e2e-job")
        self.assertEqual(plan.budget.total, 800)
        self.assertEqual(self.planner.generated, ["经济型酒店"])

    def test_without_checkpointer_graph_has_no_budget_node(self):
        with mock.patch.object(pg, "get_checkpointer", return_value=None):
            pg.reset_compiled_graph()
            try:
                plan = run_planning_graph(_request("豪华酒店"), None, job_id="e2e-job")
            finally:
                pg.reset_compiled_graph()
        # 无 Checkpointer 时保持旧行为：不 interrupt，直接完成（预算问题留给 finalize 复核）
        self.assertEqual(plan.budget.total, 2000)

    # -- Worker / 任务状态层 -------------------------------------------------

    def test_worker_marks_waiting_input(self):
        redis_client.enqueue_job(
            "job-w", "plan", {"request": _request().model_dump(mode="json")}
        )
        question = {"type": "budget_confirm", "question": "是否降级？", "progress": 90}
        with mock.patch(
            "app.graphs.planning_graph.run_planning_graph",
            side_effect=PlanningWaitingInput(question),
        ):
            planning_worker.process_once(block_ms=50)

        response = RedisJobRecorder("job-w").response()
        self.assertEqual(response.status, "waiting_input")
        self.assertEqual(response.waiting_input["type"], "budget_confirm")
        self.assertEqual(response.message, "是否降级？")

    def test_full_resume_flow_via_service_and_worker(self):
        job_id = "job-r"
        request = _request()
        recorder = RedisJobRecorder(job_id)
        recorder.init("plan")
        redis_client.set_job_state(job_id, request=request.model_dump(mode="json"))
        recorder.waiting_input(
            {"type": "budget_confirm", "question": "是否降级？", "progress": 90}
        )

        # resume 接口的服务层路径
        stored = planning_job_service.get_stored_request(job_id)
        self.assertEqual(stored.city, "北京")
        planning_job_service.mark_resumed(job_id)
        self.assertEqual(RedisJobRecorder(job_id).response().status, "queued")
        self.assertIsNone(RedisJobRecorder(job_id).response().waiting_input)

        # Worker 消费 resume 任务并完成
        redis_client.enqueue_job(
            job_id,
            "resume",
            {
                "request": request.model_dump(mode="json"),
                "resume_value": "downgrade",
            },
        )
        final = _plan(over_budget=False)
        with mock.patch(
            "app.graphs.planning_graph.run_planning_graph", return_value=final
        ):
            planning_worker.process_once(block_ms=50)

        response = RedisJobRecorder(job_id).response()
        self.assertEqual(response.status, "completed")
        self.assertEqual(response.data.city, "北京")
        self.assertIsNone(response.waiting_input)


class _RecordingRequestPlanner(StubPlanner):
    """在 StubPlanner 之上额外记录每次生成时的 (住宿, 节奏)。"""

    def __init__(self):
        super().__init__()
        self.generated_profiles: list[tuple[str, str]] = []

    def _parse_response(self, response, request):
        self.generated.append(request.accommodation)
        self.generated_profiles.append((request.accommodation, request.pace))
        return _plan(over_budget=request.accommodation not in ("经济型酒店", "民宿"))


class RouteConfirmTestCase(StageETestCase):
    """多路线选择 interrupt：生成前选行程方向（骨架先行，方案为确定性变体）。"""

    def setUp(self):
        # 复制基类基建（fakeredis 任务层 + InMemorySaver），桩换成记录 (住宿, 节奏) 的版本
        self._orig_client = redis_client._client
        redis_client._client = fakeredis.FakeRedis(decode_responses=True)
        redis_client._group_ready = False
        self.planner = _RecordingRequestPlanner()
        self.patches = [
            mock.patch.object(pg, "_get_planner", return_value=self.planner),
            mock.patch.object(
                trip_quality_service,
                "finalize",
                side_effect=lambda plan, request, **kwargs: plan,
            ),
            mock.patch.object(pg, "get_checkpointer", return_value=InMemorySaver()),
        ]
        for patch in self.patches:
            patch.start()
        pg.reset_compiled_graph()

    def _route_request(self, accommodation: str = "舒适型酒店") -> TripRequest:
        return _request(accommodation).model_copy(update={"ask_route_options": True})

    def test_route_confirm_interrupt_payload(self):
        with self.assertRaises(PlanningWaitingInput) as ctx:
            run_planning_graph(self._route_request(), None, job_id="route-job-1")
        question = ctx.exception.question
        self.assertEqual(question["type"], "route_confirm")
        values = [opt["value"] for opt in question["options"]]
        # 舒适型酒店上下都有档位：三个方向齐全
        self.assertEqual(values, ["balanced", "comfort", "economy"])
        # 骨架先行：interrupt 时还没有任何 LLM 生成
        self.assertEqual(self.planner.generated, [])

    def test_route_resume_economy_downgrades_and_relaxes(self):
        with self.assertRaises(PlanningWaitingInput):
            run_planning_graph(self._route_request(), None, job_id="route-job-2")
        plan = run_planning_graph(
            self._route_request(), None, job_id="route-job-2", resume_value="economy"
        )
        # 舒适型酒店 → 民宿 + 轻松节奏；民宿不超预算，无二次 interrupt
        self.assertEqual(self.planner.generated_profiles, [("民宿", "relaxed")])
        self.assertFalse(plan.budget.over_budget)

    def test_route_resume_balanced_keeps_preferences(self):
        # 经济型酒店不触发预算 interrupt，干净验证 balanced 按原偏好生成
        with self.assertRaises(PlanningWaitingInput):
            run_planning_graph(self._route_request("经济型酒店"), None, job_id="route-job-3")
        run_planning_graph(
            self._route_request("经济型酒店"), None, job_id="route-job-3", resume_value="balanced"
        )
        self.assertEqual(self.planner.generated_profiles, [("经济型酒店", "balanced")])

    def test_route_resume_comfort_upgrades(self):
        with self.assertRaises(PlanningWaitingInput):
            run_planning_graph(self._route_request(), None, job_id="route-job-4")
        with self.assertRaises(PlanningWaitingInput) as ctx:
            run_planning_graph(
                self._route_request(), None, job_id="route-job-4", resume_value="comfort"
            )
        # 升级为高档型酒店后超预算，接力触发预算确认 interrupt（interrupt 链）
        self.assertEqual(self.planner.generated_profiles, [("高档型酒店", "balanced")])
        self.assertEqual(ctx.exception.question["type"], "budget_confirm")

    def test_route_off_skips_interrupt(self):
        # 开关关闭：即使带 Checkpointer 也直接生成（用不超预算的经济型酒店排除预算 interrupt 干扰）
        plan = run_planning_graph(_request("经济型酒店"), None, job_id="route-job-5")
        self.assertEqual(self.planner.generated, ["经济型酒店"])
        self.assertFalse(plan.is_fallback)

    def test_no_checkpointer_ignores_route_flag(self):
        # 内存模式（无 Checkpointer）下 interrupt 不可用：开关开启也保持旧行为
        with mock.patch.object(pg, "get_checkpointer", return_value=None):
            pg.reset_compiled_graph()
            try:
                plan = run_planning_graph(self._route_request("经济型酒店"), None, job_id="route-job-6")
            finally:
                pg.reset_compiled_graph()
        self.assertEqual(self.planner.generated, ["经济型酒店"])
        self.assertFalse(plan.is_fallback)

    def test_boundary_tier_has_no_upgrade_option(self):
        from app.graphs.planning_graph import _build_route_options

        # 豪华酒店已是最高档：没有 comfort 方向
        options = _build_route_options(_request("豪华酒店"))
        values = [opt["value"] for opt in options]
        self.assertNotIn("comfort", values)
        self.assertIn("economy", values)
        # 经济型酒店已是最低档：没有 economy 方向
        options = _build_route_options(_request("经济型酒店"))
        values = [opt["value"] for opt in options]
        self.assertNotIn("economy", values)
        self.assertIn("comfort", values)


if __name__ == "__main__":
    unittest.main()
