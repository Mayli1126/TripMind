from datetime import date, timedelta
from unittest import TestCase

from app.agents.trip_planner_agent import MultiAgentTripPlanner
from app.models.schemas import DayPlan, TripPlan, TripRequest


class PlannerPolicyTests(TestCase):
    def setUp(self):
        day = (date.today() + timedelta(days=30)).isoformat()
        self.day = day
        self.request = TripRequest(
            origin_city="上海",
            city="北京",
            start_date=day,
            end_date=day,
            travel_days=1,
            transportation="high_speed_train",
            accommodation="经济型酒店",
        )
        self.planner = object.__new__(MultiAgentTripPlanner)

    def test_requests_repair_for_empty_day(self):
        plan = TripPlan(
            city="北京",
            start_date=self.day,
            end_date=self.day,
            days=[DayPlan(
                date=self.day,
                day_index=0,
                description="空计划",
                transportation="公共交通",
                accommodation="经济型酒店",
            )],
            overall_suggestions="测试",
        )
        self.assertTrue(self.planner._needs_repair(plan, self.request))

    def test_fallback_plan_scores_lower(self):
        fallback = self.planner._create_fallback_plan(self.request)
        normal = fallback.model_copy(deep=True)
        normal.is_fallback = False
        self.assertGreater(
            self.planner._plan_score(normal, self.request),
            self.planner._plan_score(fallback, self.request),
        )

    def test_truncated_response_reports_specific_fallback_reason(self):
        plan = self.planner._parse_response('{"city":"北京","days":[', self.request)
        self.assertTrue(plan.is_fallback)
        self.assertTrue(any("长度限制截断" in item for item in plan.warnings))

    def test_agent_step_failure_is_local_and_sanitized(self):
        class FailedAgent:
            def run(self, _query):
                raise TimeoutError("request timed out with secret headers")

        response, warning = self.planner._run_agent_safely(FailedAgent(), "query", "天气查询")
        self.assertIn("暂不可用", response)
        self.assertEqual(warning, "天气查询未完成：服务请求超时。")

    def test_fallback_does_not_repeat_attractions_across_days(self):
        request = self.request.model_copy(update={
            "city": "宜昌",
            "end_date": (date.today() + timedelta(days=34)).isoformat(),
            "travel_days": 5,
        })
        plan = self.planner._create_fallback_plan(request)
        names = [item.name for day in plan.days for item in day.attractions]
        self.assertEqual(len(names), len(set(names)))
