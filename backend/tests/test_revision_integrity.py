"""回归验证明确顺序、服务端版本控制和失败草稿。"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

from tests import test_trip_revision as legacy
from tests import test_revision_graph as fixtures
from app.agents.trip_planner_agent import MultiAgentTripPlanner
from app.graphs import revision_graph as graph
from app.models.patch import PlanPatch
from app.models.schemas import Location, SavedTripCreate, SavedTripUpdate
from app.services.itinerary_schedule_service import itinerary_schedule_service
from app.services.patch_service import ensure_stable_ids
from app.services.revision_outcome import revision_context
from app.services.trip_quality_service import trip_quality_service
from app.services.trip_store_service import TripStoreService


class RevisionIntegrityTests(TestCase):
    def setUp(self):
        fixture = legacy.TripRevisionTests()
        fixture.setUp()
        self.plan, self.request = fixture.plan, fixture.request
        for index, attraction in enumerate(self.plan.days[0].attractions):
            attraction.name = f"Point-{index}"
            attraction.category = ""
            attraction.location = Location(longitude=116 + index * .01, latitude=39.9)
        ensure_stable_ids(self.plan)
        self.ids = [a.stable_id for a in self.plan.days[0].attractions]

    def revise(self, operations, *, version=1):
        raw = json.dumps(dict(base_version=1, operations=operations,
                              affected_day_indexes=[0], explanation="调整安排"))
        with mock.patch.object(graph.MultiAgentTripPlanner, "revise_trip", side_effect=AssertionError("不能自动整份重生成")), \
             mock.patch.object(graph, "_get_patch_llm", return_value=fixtures._StubLLM(raw)):
            return graph.run_revision_graph(self.request, self.plan, "调整顺序", base_version=version)

    def test_server_version_overrides_model_example(self):
        result = self.revise([dict(op="change_duration", target_stable_id=self.ids[0], duration_minutes=45)], version=4)
        self.assertEqual(result.days[0].attractions[0].visit_duration, 45)
        self.assertEqual(result.revision_status, "draft_ready")

    def test_prompt_contains_real_version_and_zero_based_day(self):
        llm = mock.Mock()
        llm.invoke.return_value = json.dumps(dict(base_version=1, operations=[dict(
            op="reorder_day", day_index=0, ordered_stable_ids=self.ids[::-1])],
            affected_day_indexes=[0], explanation="换顺序"))
        with mock.patch.object(graph.MultiAgentTripPlanner, "revise_trip", side_effect=AssertionError("不能自动整份重生成")), \
             mock.patch.object(graph, "_get_patch_llm", return_value=llm):
            state = graph.parse_patch_node(dict(plan=self.plan, revision_instruction="换顺序", base_version=4), {})
        self.assertEqual(state["plan_patch"].base_version, 4)
        prompt = llm.invoke.call_args.args[0][1]["content"]
        self.assertIn('"base_version": 4', prompt)
        self.assertIn("第1天是0", prompt)

    def test_reorder_survives_preview_save_and_reload(self):
        ordered = [self.ids[0], self.ids[2], self.ids[1]]
        result = self.revise([dict(op="reorder_day", day_index=0, ordered_stable_ids=ordered)])
        self.assertEqual([a.stable_id for a in result.days[0].attractions], ordered)
        with TemporaryDirectory() as temp:
            store = TripStoreService(Path(temp) / "trip.db")
            saved = store.create_trip(SavedTripCreate(request=self.request, plan=self.plan))
            with mock.patch("app.services.road_route_service.RoadRouteResolver", return_value=None):
                updated = store.add_version(saved.id, SavedTripUpdate(plan=result, base_version=1))
            self.assertEqual(updated.current_version, 2)
            self.assertEqual([a.stable_id for a in updated.plan.days[0].attractions], ordered)
            reloaded = store.get_trip(saved.id)
            self.assertEqual([a.stable_id for a in reloaded.plan.days[0].attractions], ordered)

    def test_revision_preserves_order_across_time_categories(self):
        for attraction, category in zip(self.plan.days[0].attractions, ["商业街", "博物馆", "夜市"]):
            attraction.category = category
        with mock.patch("app.services.road_route_service.RoadRouteResolver", return_value=None):
            result = trip_quality_service.finalize_revision(self.plan, self.request, changed_days={0})
        self.assertEqual([a.stable_id for a in result.days[0].attractions], self.ids)

    def test_duration_route_preserves_planner_order_across_categories(self):
        for attraction, category in zip(self.plan.days[0].attractions, ["商业街", "博物馆", "夜市"]):
            attraction.category = category
        result = itinerary_schedule_service.build(self.plan)
        self.assertEqual([a.stable_id for a in result.days[0].attractions], self.ids)

    def test_noop_patch_returns_unchanged_without_diff(self):
        result = self.revise([dict(op="reorder_day", day_index=0, ordered_stable_ids=self.ids)])
        self.assertEqual(result.revision_status, "unchanged")
        self.assertEqual(result.revision_diff, [])

    def test_rejected_patch_has_explicit_status(self):
        result = self.revise([dict(op="reorder_day", day_index=0, ordered_stable_ids=["missing"])])
        self.assertEqual(result.revision_status, "rejected")
        self.assertEqual(result.revision_diff, [])
        self.assertEqual(result.days, self.plan.days)

    def test_failed_legacy_result_never_gets_success_diff(self):
        with mock.patch.object(graph.MultiAgentTripPlanner, "revise_trip", side_effect=AssertionError("不能自动整份重生成")), \
             mock.patch.object(graph, "_get_patch_llm", return_value=fixtures._StubLLM("invalid JSON")):
            result = graph.run_revision_graph(self.request, self.plan, "换一下顺序")
        self.assertEqual(result.revision_status, "rejected")
        self.assertFalse(result.revision_diff)

    def test_store_rejects_failed_and_noop_drafts(self):
        with TemporaryDirectory() as temp:
            store = TripStoreService(Path(temp) / "trip.db")
            saved = store.create_trip(SavedTripCreate(request=self.request, plan=self.plan))
            for status in ("rejected", "unchanged", "clarify_needed", "draft_ready", "applied"):
                draft = self.plan.model_copy(update={"revision_status": status})
                with self.assertRaises(ValueError):
                    store.add_version(saved.id, SavedTripUpdate(plan=draft, base_version=1))
            self.assertEqual(store.get_trip(saved.id).current_version, 1)

    def test_legacy_two_attraction_revision_is_valid(self):
        candidate = self.plan.model_copy(deep=True)
        candidate.days[0].attractions.pop()
        planner = object.__new__(MultiAgentTripPlanner)
        planner.planner_agent = legacy.FakePlannerAgent(candidate.model_dump_json())
        with mock.patch("app.services.road_route_service.RoadRouteResolver", return_value=None):
            result = planner.revise_trip(self.request, self.plan, "只保留前两个景点")
        self.assertEqual(result.revision_status, "draft_ready")
        self.assertEqual(len(result.days[0].attractions), 2)

    def test_legacy_input_keeps_later_places_in_long_plan(self):
        self.plan.days[0].attractions[0].description = "x" * 20000
        parsed = json.loads(revision_context(self.plan))
        self.assertEqual([a["name"] for a in parsed["days"][0]["attractions"]], ["Point-0", "Point-1", "Point-2"])
        self.assertLessEqual(len(parsed["days"][0]["attractions"][0]["description"]), 300)

    def test_negative_affected_day_is_rejected(self):
        with self.assertRaises(ValueError):
            PlanPatch(base_version=1, operations=[dict(op="reorder_day", day_index=0,
                      ordered_stable_ids=self.ids)], affected_day_indexes=[-1], explanation="invalid")

    def test_remove_all_places_is_rejected(self):
        result = self.revise([dict(op="remove_place", target_stable_id=sid) for sid in self.ids])
        self.assertEqual(result.revision_status, "rejected")
        self.assertEqual(len(result.days[0].attractions), 3)

    def test_explicit_start_preference_is_not_a_default_clock_cutoff(self):
        self.plan.days[0].preferred_start_time = "20:00"
        self.plan.days[0].attractions[0].category = "博物馆"
        with mock.patch("app.services.road_route_service.RoadRouteResolver", return_value=None):
            result = trip_quality_service.finalize_revision(self.plan, self.request, changed_days={0})
        self.assertEqual(result.days[0].attractions[0].name, "Point-0")
        self.assertTrue(any("20:00" in note for note in result.days[0].timing_notes))
        self.assertEqual(len([s for s in result.days[0].schedule if s.item_type == "attraction"]), 3)

    def test_empty_changed_days_does_not_rebuild_other_days(self):
        self.plan.days[0].description = "unchanged"
        original = self.plan.days[0].model_dump()
        with mock.patch("app.services.road_route_service.RoadRouteResolver", return_value=None):
            trip_quality_service.finalize_revision(self.plan, self.request, changed_days=set())
        self.assertEqual(self.plan.days[0].model_dump(), original)

    def test_success_does_not_carry_previous_failure_as_current_warning(self):
        self.plan.warnings = ["本次自然语言修改未通过结构校验，已保留原计划。", "天气预报有雨"]
        result = self.revise([dict(op="reorder_day", day_index=0, ordered_stable_ids=self.ids[::-1])])
        self.assertEqual(result.revision_status, "draft_ready")
        self.assertIn("天气预报有雨", result.warnings)
        self.assertFalse(any("未通过结构校验" in warning for warning in result.warnings))
        self.assertTrue(any("未通过结构校验" in warning for warning in self.plan.warnings))

    def test_repeated_revision_stays_deserializable(self):
        self.plan.planning_iterations = 2
        for duration in (45, 60, 75):
            result = self.revise([dict(op="change_duration", target_stable_id=self.ids[0], duration_minutes=duration)])
            self.assertEqual(result.revision_status, "draft_ready")
            self.plan = type(result).model_validate_json(result.model_dump_json())
            self.assertEqual(self.plan.planning_iterations, 2)
