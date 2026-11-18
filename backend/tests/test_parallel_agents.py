import threading
import unittest

from app.agents.trip_planner_agent import MultiAgentTripPlanner
from app.models.schemas import TripRequest


def make_request(origin_city: str = "武汉") -> TripRequest:
    from datetime import date, timedelta
    start = date.today() + timedelta(days=30)
    return TripRequest(
        destination_only=False,  # 覆盖旧版四分支并发查询流程。
        origin_city=origin_city,
        city="宜昌",
        start_date=start.isoformat(),
        end_date=(start + timedelta(days=1)).isoformat(),
        travel_days=2,
        transportation="high_speed_train",
        accommodation="经济型酒店",
    )


class BarrierAgent:
    def __init__(self, value: str, barrier: threading.Barrier):
        self.value = value
        self.barrier = barrier

    def run(self, _query: str) -> str:
        self.barrier.wait(timeout=1)
        return self.value


class StaticAgent:
    def __init__(self, value: str = "ok", error: Exception | None = None):
        self.value = value
        self.error = error

    def run(self, _query: str) -> str:
        if self.error:
            raise self.error
        return self.value


class ParallelAgentTests(unittest.TestCase):
    def test_context_branches_really_run_in_parallel_and_keep_result_mapping(self):
        planner = object.__new__(MultiAgentTripPlanner)
        barrier = threading.Barrier(4)
        planner.attraction_agent = BarrierAgent("attraction-result", barrier)
        planner.weather_agent = BarrierAgent("weather-result", barrier)
        planner.hotel_agent = BarrierAgent("hotel-result", barrier)

        def transport(_request):
            barrier.wait(timeout=1)
            return ("transport-result", 88), None

        planner.query_transportation_context = transport
        events = []
        result = planner._collect_parallel_context(
            make_request(), lambda *event: events.append(event)
        )

        self.assertEqual(result["attractions"], "attraction-result")
        self.assertEqual(result["weather"], "weather-result")
        self.assertEqual(result["hotels"], "hotel-result")
        self.assertEqual(result["transportation"], "transport-result")
        self.assertEqual(result["intercity_cost_per_person"], 88)
        self.assertEqual(result["warnings"], [])
        self.assertEqual(
            {event[3] for event in events if len(event) == 5 and event[4] == "completed"},
            {"attractions", "weather", "hotels", "transportation"},
        )
        self.assertEqual(events[-1][1], "research_joined")

    def test_one_failed_branch_degrades_without_discarding_other_results(self):
        planner = object.__new__(MultiAgentTripPlanner)
        planner.attraction_agent = StaticAgent("attraction-result")
        planner.weather_agent = StaticAgent(error=TimeoutError("timed out"))
        planner.hotel_agent = StaticAgent("hotel-result")
        planner.query_transportation_context = lambda _request: (("transport-result", 88), None)
        events = []

        result = planner._collect_parallel_context(
            make_request(),
            lambda *event: events.append(event),
        )

        self.assertEqual(result["attractions"], "attraction-result")
        self.assertEqual(result["hotels"], "hotel-result")
        self.assertIn("暂不可用", result["weather"])
        self.assertEqual(len(result["warnings"]), 1)
        weather_events = [event for event in events if len(event) == 5 and event[3] == "weather"]
        self.assertEqual([event[4] for event in weather_events], ["running", "degraded"])


if __name__ == "__main__":
    unittest.main()
