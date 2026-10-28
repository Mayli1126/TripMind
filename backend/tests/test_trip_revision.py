from datetime import date, timedelta
from unittest import TestCase

from app.agents.trip_planner_agent import MultiAgentTripPlanner
from app.models.schemas import Attraction, DayPlan, Location, Meal, TripPlan, TripRequest


class FakePlannerAgent:
    def __init__(self, response: str):
        self.response = response

    def run(self, _prompt: str) -> str:
        return self.response


class TripRevisionTests(TestCase):
    def setUp(self):
        day = (date.today() + timedelta(days=30)).isoformat()
        self.request = TripRequest(
            origin_city="上海",
            city="北京",
            start_date=day,
            end_date=day,
            travel_days=1,
            transportation="high_speed_train",
            accommodation="经济型酒店",
        )
        self.plan = TripPlan(
            city="北京",
            start_date=day,
            end_date=day,
            days=[DayPlan(
                date=day,
                day_index=0,
                description="原行程",
                transportation="公共交通",
                accommodation="经济型酒店",
                attractions=[Attraction(
                    name="故宫",
                    address="北京",
                    location=Location(longitude=116.397, latitude=39.916),
                    visit_duration=120,
                    description="测试",
                    category="历史文化",
                    ticket_price=60,
                ), Attraction(
                    name="景山公园",
                    address="北京",
                    location=Location(longitude=116.396, latitude=39.925),
                    visit_duration=90,
                    description="测试",
                    category="城市公园",
                    ticket_price=2,
                ), Attraction(
                    name="北海公园",
                    address="北京",
                    location=Location(longitude=116.389, latitude=39.925),
                    visit_duration=60,
                    description="测试",
                    category="城市公园",
                    ticket_price=10,
                )],
                meals=[
                    Meal(type=meal_type, name=meal_type, estimated_cost=20)
                    for meal_type in ("breakfast", "lunch", "dinner")
                ],
            )],
            overall_suggestions="原建议",
        )

    def test_valid_revision_is_revalidated_and_rebudgeted(self):
        revised_response = self.plan.model_copy(deep=True)
        revised_response.days[0].description = "调整后的轻松行程"
        planner = object.__new__(MultiAgentTripPlanner)
        planner.planner_agent = FakePlannerAgent(revised_response.model_dump_json())

        result = planner.revise_trip(self.request, self.plan, "安排轻松一点")

        self.assertEqual(result.days[0].description, "调整后的轻松行程")
        self.assertIsNotNone(result.budget)
        self.assertTrue(any("安排轻松一点" in warning for warning in result.warnings))

    def test_invalid_revision_preserves_current_plan(self):
        planner = object.__new__(MultiAgentTripPlanner)
        planner.planner_agent = FakePlannerAgent('{"days": []}')

        result = planner.revise_trip(self.request, self.plan, "删除所有行程")

        self.assertEqual(result.days[0].description, "原行程")
        self.assertTrue(any("保留原计划" in warning for warning in result.warnings))
