"""使用完整替身检查顺序修改的速度与语义保真。"""
from unittest import TestCase, mock

from tests import test_revision_graph as fixtures
from app.graphs import revision_graph as graph
from app.services.patch_service import apply_plan_patch
from app.services.revision_intent import fast_order_patch, order_postconditions, dependent_days


class FastRevisionTests(TestCase):
    def setUp(self):
        self.plan = fixtures._plan()
        self.request = fixtures._request()

    def run_fast(self, instruction):
        with mock.patch.object(graph, "_get_patch_llm", side_effect=AssertionError("No model calls")), \
             mock.patch.object(graph.MultiAgentTripPlanner, "__init__", side_effect=AssertionError("不能启动完整规划器")), \
             mock.patch("app.services.road_route_service.RoadRouteResolver", return_value=None):
            return graph.run_revision_graph(self.request, self.plan, instruction, base_version=4)

    def test_exact_before_command_avoids_model_and_keeps_others(self):
        result = self.run_fast("把景山公园放到故宫博物院前面")
        self.assertEqual(result.revision_status, "draft_ready")
        self.assertEqual([a.name for a in result.days[0].attractions], ["景山公园", "故宫博物院"])
        self.assertEqual(result.days[1].attractions, self.plan.days[1].attractions)

    def test_cross_day_move_rebuilds_source_and_destination(self):
        result = self.run_fast("把南锣鼓巷移到第一天末尾")
        self.assertEqual(result.revision_status, "draft_ready")
        self.assertEqual(result.days[0].attractions[-1].name, "南锣鼓巷")
        self.assertNotIn("南锣鼓巷", [a.name for a in result.days[1].attractions])
        self.assertTrue(any(item.title == "南锣鼓巷" for item in result.days[0].schedule))
        self.assertFalse(any(item.title == "南锣鼓巷" for item in result.days[1].schedule))

    def test_cross_day_swap_keeps_every_place(self):
        patch = fast_order_patch(self.plan, "把景山公园和雍和宫交换顺序", 4)
        after, errors = apply_plan_patch(self.plan, patch, current_version=4)
        self.assertFalse(errors)
        self.assertFalse(order_postconditions(self.plan, after, patch))
        self.assertEqual(after.days[0].attractions[1].name, "雍和宫")
        self.assertEqual(after.days[1].attractions[-1].name, "景山公园")
        tampered = after.model_copy(deep=True)
        tampered.days[0].attractions.reverse()
        self.assertTrue(order_postconditions(self.plan, tampered, patch))

    def test_unknown_place_asks_without_guessing(self):
        result = self.run_fast("把那个博物馆放到景山公园前面")
        self.assertEqual(result.revision_status, "clarify_needed")
        self.assertEqual(result.days, self.plan.days)

    def test_format_repair_is_bounded_and_never_regenerates(self):
        llm = mock.Mock()
        llm.invoke.return_value = "invalid JSON"
        with mock.patch.object(graph, "_get_patch_llm", return_value=llm), \
             mock.patch.object(graph.MultiAgentTripPlanner, "revise_trip", side_effect=AssertionError("不能自动整份重生成")):
            result = graph.run_revision_graph(self.request, self.plan, "安排轻松些", base_version=4)
        self.assertEqual(llm.invoke.call_count, 2)
        self.assertEqual(result.revision_status, "rejected")
        self.assertEqual(result.days, self.plan.days)

    def test_service_error_is_not_retried_as_regeneration(self):
        llm = mock.Mock()
        llm.invoke.side_effect = RuntimeError("401 unauthorized")
        with mock.patch.object(graph, "_get_patch_llm", return_value=llm):
            result = graph.run_revision_graph(self.request, self.plan, "安排轻松些")
        self.assertEqual(llm.invoke.call_count, 1)
        self.assertEqual(result.revision_status, "rejected")

    def test_next_day_origin_dependency_and_compound_scope(self):
        after = self.plan.model_copy(deep=True)
        after.days[0].attractions.reverse()
        after.days[0].attractions[-1].location.longitude += .1
        self.assertEqual(dependent_days(self.plan, after, {0}), {0, 1})
        self.assertIsNone(fast_order_patch(self.plan, "把景山公园放到故宫博物院前面，同时删除雍和宫", 1))
