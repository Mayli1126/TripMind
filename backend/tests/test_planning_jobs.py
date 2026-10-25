import time
from unittest import TestCase

from app.models.schemas import TripPlan
from app.services.planning_job_service import PlanningJobService


class PlanningJobServiceTests(TestCase):
    def setUp(self):
        self.service = PlanningJobService()

    def tearDown(self):
        self.service._executor.shutdown(wait=True)

    def test_job_records_real_progress_and_result(self):
        def operation(callback):
            callback(25, "parallel_research", "景点 Agent 已开始工作", "attractions", "running")
            time.sleep(0.01)
            callback(55, "parallel_research", "景点 Agent 已完成", "attractions", "completed")
            callback(80, "validating", "正在校验")
            return TripPlan(
                city="北京",
                start_date="2026-08-01",
                end_date="2026-08-01",
                days=[],
                overall_suggestions="测试",
            )

        created = self.service._create_job(operation)
        deadline = time.monotonic() + 2
        current = created
        while current.status not in {"completed", "failed"} and time.monotonic() < deadline:
            time.sleep(0.01)
            current = self.service.get(created.job_id)

        self.assertEqual(current.status, "completed")
        self.assertEqual(current.progress, 100)
        self.assertEqual(current.data.city, "北京")
        self.assertEqual(current.agents["attractions"].status, "completed")
        self.assertGreaterEqual(current.agents["attractions"].duration_ms, 1)
        self.assertGreaterEqual(current.metrics.total_duration_ms, 1)
        self.assertEqual(
            current.metrics.agent_duration_ms["attractions"],
            current.agents["attractions"].duration_ms,
        )
        self.assertEqual(current.metrics.degraded_agents, [])
        events = self.service.events_since(created.job_id, 0)
        self.assertEqual(events[0]["status"], "queued")
        self.assertEqual(events[-1]["status"], "completed")
        self.assertEqual([event["sequence"] for event in events], list(range(len(events))))
        completed_agent_event = next(
            event for event in events if event.get("agent_status") == "completed"
        )
        self.assertGreaterEqual(completed_agent_event["duration_ms"], 1)
