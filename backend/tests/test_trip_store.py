from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from app.models.schemas import (
    SavedTripCreate,
    SavedTripUpdate,
    TripPlan,
    UserPreferences,
)
from app.services.trip_store_service import TripStoreService
from tests.test_trip_quality import make_day, make_request


class TripStoreTests(TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.store = TripStoreService(Path(self.temp_dir.name) / "trips.db")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_trip_store_create_version_restore_and_delete(self):
        request = make_request()
        plan = TripPlan(
            city=request.city,
            start_date=request.start_date,
            end_date=request.end_date,
            days=[make_day(0, 10), make_day(1, 20)],
            overall_suggestions="初始建议",
        )

        saved = self.store.create_trip(SavedTripCreate(request=request, plan=plan))
        self.assertEqual(saved.city, plan.city)
        self.assertEqual(saved.current_version, 1)
        self.assertEqual(len(self.store.list_trips()), 1)

        revised = deepcopy(plan)
        revised.overall_suggestions = "修改后的建议"
        saved = self.store.add_version(
            saved.id,
            SavedTripUpdate(plan=revised, change_note="AI 修改：轻松一点"),
        )
        self.assertEqual(saved.current_version, 2)
        self.assertEqual(saved.plan.overall_suggestions, "修改后的建议")
        self.assertEqual([item.version for item in saved.versions], [2, 1])

        restored = self.store.restore_version(saved.id, 1)
        self.assertEqual(restored.current_version, 3)
        self.assertEqual(restored.plan.overall_suggestions, plan.overall_suggestions)
        self.assertEqual(restored.versions[0].change_note, "恢复到版本 v1")

        self.store.delete_trip(saved.id)
        self.assertEqual(self.store.list_trips(), [])

    def test_trip_store_preferences_round_trip(self):
        self.assertEqual(self.store.get_preferences().transportation, "high_speed_train")
        preferences = UserPreferences(
            origin_city="武汉",
            transportation="plane",
            accommodation="舒适型酒店",
            preferences=["美食", "艺术"],
            travelers=2,
            rooms=1,
            optimization_goal="fastest",
            pace="relaxed",
        )
        self.assertEqual(self.store.save_preferences(preferences), preferences)
        self.assertEqual(self.store.get_preferences(), preferences)

    def test_trip_execution_progress_persists_and_can_be_unchecked(self):
        request = make_request()
        plan = TripPlan(
            city=request.city,
            start_date=request.start_date,
            end_date=request.end_date,
            days=[make_day(0, 10), make_day(1, 20)],
            overall_suggestions="测试",
        )
        saved = self.store.create_trip(SavedTripCreate(request=request, plan=plan))

        progress = self.store.set_progress(saved.id, "d0-a-0", True)
        self.assertTrue(progress.items[0].completed)
        self.assertIsNotNone(progress.items[0].completed_at)

        progress = self.store.set_progress(saved.id, "d0-a-0", False)
        self.assertFalse(progress.items[0].completed)
        self.assertIsNone(progress.items[0].completed_at)
        self.assertEqual(self.store.get_progress(saved.id), progress)

    def test_manual_version_recalculates_budget_and_schedule(self):
        request = make_request()
        plan = TripPlan(
            city=request.city, start_date=request.start_date, end_date=request.end_date,
            days=[make_day(0, 100), make_day(1, 200)], overall_suggestions="测试",
        )
        saved = self.store.create_trip(SavedTripCreate(request=request, plan=plan))
        revised = deepcopy(plan)
        revised.days[1].attractions[0].ticket_price = 0
        updated = self.store.add_version(
            saved.id, SavedTripUpdate(plan=revised, change_note="删除收费项目")
        )
        self.assertIsNotNone(updated.plan.budget)
        self.assertEqual(updated.plan.budget.total_attractions, 200)
        self.assertTrue(all(day.schedule for day in updated.plan.days))
