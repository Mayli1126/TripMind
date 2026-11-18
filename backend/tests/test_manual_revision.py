"""手动编辑的关键链路回归，全部离线，不调用模型或高德。"""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

from fastapi import HTTPException

from tests.test_revision_graph import _plan, _request
from app.graphs import revision_graph as graph
from app.models.patch import ManualRevisionRequest
from app.models.schemas import SavedTripCreate, SavedTripUpdate
from app.services.patch_service import ensure_stable_ids
from app.services.trip_store_service import TripStoreService


class ManualRevisionTests(TestCase):
    def setUp(self):
        self.plan, self.request = _plan(), _request()
        ensure_stable_ids(self.plan)
        self.ids = [[a.stable_id for a in day.attractions] for day in self.plan.days]

    def preview(self, operations):
        payload = ManualRevisionRequest(base_version=1, operations=operations)
        # 若手动入口意外调用模型，立即失败；交通在本地使用估算，不访问真实服务。
        with mock.patch.object(graph, "_get_patch_llm", side_effect=AssertionError("手动修改不能调用模型")), \
             mock.patch("app.services.road_route_service.RoadRouteResolver", return_value=None):
            return graph.run_manual_revision(self.request, self.plan, payload)

    def test_cross_day_duration_description_save_reload_and_conflict(self):
        moved = self.ids[0][0]
        ordered = [moved, *self.ids[1]]
        result = self.preview([
            dict(op="move_place", target_stable_id=moved, day_index=1),
            dict(op="reorder_day", day_index=1, ordered_stable_ids=ordered),
            dict(op="change_duration", target_stable_id=moved, duration_minutes=75),
            dict(op="update_description", target_stable_id=moved, description="修改后的介绍"),
        ])
        self.assertEqual(result.revision_status, "draft_ready")
        self.assertEqual([a.stable_id for a in result.days[0].attractions], self.ids[0][1:])
        self.assertEqual([a.stable_id for a in result.days[1].attractions], ordered)
        self.assertEqual(result.days[1].attractions[0].visit_duration, 75)
        self.assertTrue(all(day.schedule for day in result.days))
        self.assertIsNotNone(result.budget)
        # 预览不修改原对象；确认保存后再次读取，修改仍需保持正确。
        self.assertEqual(self.plan.days[0].attractions[0].stable_id, moved)
        with TemporaryDirectory() as temp:
            store = TripStoreService(Path(temp) / "trips.db")
            saved = store.create_trip(SavedTripCreate(request=self.request, plan=self.plan))
            self.assertEqual(store.get_trip(saved.id).current_version, 1)
            with mock.patch("app.services.road_route_service.RoadRouteResolver", return_value=None):
                store.add_version(saved.id, SavedTripUpdate(plan=result, base_version=1))
            loaded = store.get_trip(saved.id)
            self.assertEqual(loaded.current_version, 2)
            self.assertEqual([a.stable_id for a in loaded.plan.days[1].attractions], ordered)
            self.assertEqual(loaded.plan.days[1].attractions[0].visit_duration, 75)
            self.assertEqual(loaded.plan.days[1].attractions[0].description, "修改后的介绍")
            with self.assertRaises(ValueError):
                store.add_version(saved.id, SavedTripUpdate(plan=result, base_version=1))

    def test_delete_and_noop_and_invalid_target(self):
        result = self.preview([dict(op="remove_place", target_stable_id=self.ids[0][0])])
        self.assertEqual(result.revision_status, "draft_ready")
        self.assertNotIn(self.ids[0][0], [a.stable_id for a in result.days[0].attractions])
        noop = self.preview([dict(op="reorder_day", day_index=0, ordered_stable_ids=self.ids[0])])
        self.assertEqual(noop.revision_status, "unchanged")
        invalid = self.preview([dict(op="move_place", target_stable_id=self.ids[0][0], day_index=99)])
        self.assertEqual(invalid.revision_status, "rejected")
        self.assertEqual(invalid.days, self.plan.days)

    def test_manual_api_rejects_stale_base_and_does_not_save_preview(self):
        from app.api.routes import library
        saved = mock.Mock(current_version=2, request=self.request, plan=self.plan)
        payload = ManualRevisionRequest(base_version=1, operations=[dict(op="remove_place", target_stable_id=self.ids[0][0])])
        with mock.patch.object(library.trip_store_service, "get_trip", return_value=saved), \
             mock.patch.object(library.trip_store_service, "add_version") as save, \
             mock.patch.object(graph, "run_manual_revision", return_value=self.plan) as run:
            with self.assertRaises(HTTPException) as error:
                library.manual_revision_preview("trip", payload)
            self.assertEqual(error.exception.status_code, 409)
            run.assert_not_called()
            saved.current_version = 1
            self.assertIs(library.manual_revision_preview("trip", payload), self.plan)
            run.assert_called_once()
            save.assert_not_called()

    def test_manual_schema_rejects_unsupported_operations_and_bad_minutes(self):
        for operation in (dict(op="add_place", replacement_name="新景点", day_index=0),
                          dict(op="change_duration", target_stable_id=self.ids[0][0], duration_minutes=0)):
            with self.assertRaises(ValueError):
                ManualRevisionRequest(base_version=1, operations=[operation])
