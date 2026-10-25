from unittest import TestCase

from app.models.schemas import Attraction, DayPlan, Hotel, Location, TripPlan
from app.services.route_optimizer import RouteOptimizer


def attraction(name: str, longitude: float) -> Attraction:
    return Attraction(
        name=name,
        address="测试地址",
        location=Location(longitude=longitude, latitude=30),
        visit_duration=60,
        description="测试",
    )


class RouteOptimizerTests(TestCase):
    def test_reorders_route_when_distance_is_shorter(self):
        # 酒店在 0 点，原顺序东1 -> 西3 -> 东2反复折返；优化后应先走完东侧。
        day = DayPlan(
            date="2026-08-01",
            day_index=0,
            description="测试",
            transportation="公共交通",
            accommodation="经济型酒店",
            hotel=Hotel(
                name="酒店",
                location=Location(longitude=110, latitude=30),
            ),
            attractions=[
                attraction("A", 110.01),
                attraction("C", 109.97),
                attraction("B", 110.02),
            ],
        )
        plan = TripPlan(
            city="测试",
            start_date="2026-08-01",
            end_date="2026-08-01",
            days=[day],
            overall_suggestions="测试",
        )

        result = RouteOptimizer().optimize(plan)

        self.assertEqual([item.name for item in result.days[0].attractions], ["A", "B", "C"])
        self.assertGreater(result.optimization_summary.saved_distance_km, 0)
        self.assertEqual(result.optimization_summary.optimized_days, 1)

    def test_haversine_distance_is_reasonable(self):
        optimizer = RouteOptimizer()
        distance = optimizer.distance(
            Location(longitude=116.4074, latitude=39.9042),
            Location(longitude=121.4737, latitude=31.2304),
        )
        self.assertGreater(distance, 1000)
        self.assertLess(distance, 1200)

    def test_next_day_starts_from_previous_night_hotel(self):
        first_day = DayPlan(
            date="2026-08-01", day_index=0, description="测试",
            transportation="公共交通", accommodation="经济型酒店",
            hotel=Hotel(name="第一晚酒店", location=Location(longitude=110, latitude=30)),
            attractions=[attraction("第一天景点", 110.01)],
        )
        second_day = DayPlan(
            date="2026-08-02", day_index=1, description="测试",
            transportation="公共交通", accommodation="经济型酒店",
            hotel=Hotel(name="第二晚酒店", location=Location(longitude=120, latitude=30)),
            attractions=[
                attraction("靠近第二晚酒店", 119.9),
                attraction("靠近前一晚酒店", 110.1),
            ],
        )
        plan = TripPlan(
            city="测试", start_date="2026-08-01", end_date="2026-08-02",
            days=[first_day, second_day], overall_suggestions="测试",
        )

        result = RouteOptimizer().optimize(plan)

        self.assertEqual(result.days[1].route_summary.start_anchor, "第一晚酒店")
        self.assertEqual(result.days[1].attractions[0].name, "靠近前一晚酒店")
        self.assertGreater(result.days[1].route_summary.end_transfer_km, 0)
