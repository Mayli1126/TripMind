"""补搜、逐段交通选择与景区起终点核验，不使用真实额度。"""
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from test_planning_feedback import fixture
from app.models.candidate import CandidatePlace
from app.models.schemas import Location, POIInfo, RouteLeg, RouteAnchor
from app.services.planning_feedback_service import build_planning_feedback
from app.services.planning_supplement_service import supplement_candidates_once
from app.services.road_route_service import RoadRouteResolver
from app.services.route_anchor_service import resolve_plan_anchors
from app.services.tourism_research_service import TourismResearchError


class PlanningCompletionTests(unittest.TestCase):
    def test_initial_arrangement_and_final_guard_keep_candidate_identity(self):
        import json
        from app.services.planner_arrangement_service import arrange_researched_plan
        from app.services.tourism_research_service import enforce_researched_plan
        _, request = fixture()
        first = self.candidate(name='同名景区', poi='first')
        second = self.candidate(name='同名景区', poi='second', longitude=120.12)
        request = request.model_copy(update={'candidate_places':[first, second]})
        llm = Mock(max_tokens=8192)
        llm.invoke.return_value = json.dumps({'days':[
            {'date':request.start_date,'theme':'第一天','places':[{'candidate_id':'first','duration_minutes':90}]},
            {'date':request.end_date,'theme':'第二天','places':[{'candidate_id':'second','duration_minutes':120}]}]})
        plan, _ = arrange_researched_plan(request, {}, llm)
        enforce_researched_plan(plan, [first, second])
        self.assertEqual([d.attractions[0].poi_id for d in plan.days], ['first','second'])
        self.assertEqual(plan.days[1].attractions[0].location, second.location)

    def test_truncated_planner_reply_is_classified_without_retry(self):
        from app.services.planner_arrangement_service import invoke_planner
        from app.services.planning_repair_service import capture_repair_diagnostics
        class TruncatedClient:
            max_tokens = 100
            calls = 0
            def invoke_result(self, *args, **kwargs):
                self.calls += 1
                return SimpleNamespace(content='{"days":[', finish_reason='length')
        client = TruncatedClient()
        records = []
        with capture_repair_diagnostics(records.append), self.assertRaisesRegex(ValueError, '截断'):
            invoke_planner(client, '说明', {})
        self.assertEqual(client.calls, 1)
        self.assertEqual(next(r for r in records if r['stage']=='planner_output')['finish_reason'], 'length')

    def candidate(self, name='附近街区', city='杭州', longitude=120.101, poi='new'):
        return CandidatePlace(candidate_id=poi, canonical_name=name, city=city, poi_id=poi,
            location=Location(longitude=longitude, latitude=30.2), fact_status='verified')

    def test_one_classic_place_is_reviewed_even_with_four_hours_and_reason(self):
        plan, request = fixture()
        day = plan.days[1]
        day.attractions[0].visit_duration = 240
        day.schedule[0].duration_minutes = 240
        day.single_place_reason = '景区很大'
        feedback = build_planning_feedback(plan, request)
        self.assertTrue(any(w.type == 'underfilled_day' and w.date == day.date for w in feedback.soft_warnings))
        request = request.model_copy(update={'free_text_input':'保留自由活动'})
        self.assertFalse(any(w.type == 'underfilled_day' for w in build_planning_feedback(plan, request).soft_warnings))

    def test_existing_nearby_backup_avoids_search(self):
        plan, request = fixture()
        request = request.model_copy(update={'candidate_places':[self.candidate()]})
        with patch('app.services.planning_supplement_service.research_tourism') as search:
            supplement_candidates_once(plan, request)
        search.assert_not_called()

    def test_supplement_is_once_and_rejects_far_wrong_city_and_duplicates(self):
        plan, request = fixture()
        additions = [self.candidate(), self.candidate(longitude=121, poi='far'),
                     self.candidate(city='上海', poi='other'), self.candidate(poi='poi-1')]
        with patch('app.services.planning_supplement_service.research_tourism', return_value=(additions,'')) as search:
            expanded = supplement_candidates_once(plan, request)
        search.assert_called_once()
        self.assertEqual([c.poi_id for c in expanded.candidate_places], ['new'])
        self.assertIn('needs', search.call_args.kwargs['supplement_context'])
        self.assertEqual(request.candidate_places, [])

    def test_supplement_failure_keeps_plan_and_does_not_retry(self):
        plan, request = fixture()
        with patch('app.services.planning_supplement_service.research_tourism', side_effect=TourismResearchError('不可用')) as search:
            result = supplement_candidates_once(plan, request)
        search.assert_called_once()
        self.assertIs(result, request)
        self.assertTrue(any('补充搜索' in w for w in plan.warnings))

    def test_one_backup_cannot_fill_two_sparse_days(self):
        from app.services.itinerary_schedule_service import itinerary_schedule_service
        plan, request = fixture()
        itinerary_schedule_service.build(plan)
        request = request.model_copy(update={'candidate_places':[self.candidate()]})
        with patch('app.services.planning_supplement_service.research_tourism', return_value=([self.candidate(poi='extra')],'')) as search:
            supplement_candidates_once(plan, request)
        search.assert_called_once()

    def resolver(self, **options):
        settings = SimpleNamespace(road_routes_enabled=True, road_routes_max_seconds=10)
        with patch('app.services.road_route_service.get_settings', return_value=settings):
            return RoadRouteResolver('杭州', compare_modes=True, **options)

    def test_default_route_does_not_automatically_query_or_switch_to_taxi(self):
        resolver = self.resolver()
        transit = RouteLeg(mode='transit', duration_minutes=75, cost=5, source='amap_route', is_estimated=False)
        with patch.object(resolver, '_resolve_mode', return_value=transit) as query:
            result = resolver.resolve(Location(longitude=120, latitude=30), Location(longitude=120.1, latitude=30), '2026-12-01', '10:00')
        query.assert_called_once()
        self.assertIs(result, transit)
        self.assertEqual(result.transport_label, '')

    def test_economy_or_explicit_no_taxi_does_not_query_driving(self):
        for options in ({'optimization_goal':'economy'}, {'allow_taxi':False}):
            resolver = self.resolver(**options)
            transit = RouteLeg(mode='transit', duration_minutes=75)
            with patch.object(resolver, '_resolve_mode', return_value=transit) as query:
                self.assertIs(resolver.resolve(object(), object(), '2026-12-01', '10:00'), transit)
            query.assert_called_once()

    def test_unrelated_anchor_is_rejected_and_verified_relation_is_used(self):
        plan, _ = fixture()
        attraction = plan.days[0].attractions[0]
        attraction.match_evidence = {'planning_scope':'parent_scenic_area'}
        attraction.proposed_entry_name = '内部起点'
        attraction.proposed_exit_name = '同名地点'
        good = POIInfo(id='child', name='内部起点', city_name='杭州市', address='杭州',
                       type='风景名胜', location=attraction.location, parent_poi_id=attraction.poi_id)
        unrelated = good.model_copy(update={'id':'wrong', 'name':'同名地点', 'parent_poi_id':'other'})
        amap = Mock()
        amap.search_poi_once.side_effect = [[good], [unrelated]]
        resolve_plan_anchors(plan, amap=amap)
        self.assertEqual(attraction.route_entry.poi_id, 'child')
        self.assertIsNone(attraction.route_exit)
        resolve_plan_anchors(plan, amap=amap)
        self.assertEqual(amap.search_poi_once.call_count, 2)
        self.assertTrue(any('代表坐标' in w for w in plan.warnings))

    def test_verified_exit_is_used_for_next_leg_without_double_counting_visits(self):
        from app.services.itinerary_schedule_service import itinerary_schedule_service
        plan, _ = fixture()
        first = plan.days[0].attractions[0]
        exit_point = Location(longitude=120.15, latitude=30.25)
        first.route_exit = RouteAnchor(poi_id='exit', name='核验出口', location=exit_point)
        plan.days[0].attractions.append(first.model_copy(update={'poi_id':'next', 'stable_id':'next',
            'name':'街区', 'route_exit':None, 'visit_duration':60}))
        resolver = Mock()
        resolver.resolve.return_value = RouteLeg(mode='walking', duration_minutes=10)
        itinerary_schedule_service.build(plan, route_resolver=resolver)
        self.assertEqual(resolver.resolve.call_args_list[1].args[0], exit_point)
        self.assertEqual(plan.days[0].visit_minutes, 150)

    def test_expanded_pool_reaches_final_graph_validation(self):
        from app.graphs import planning_graph
        from app.services.itinerary_schedule_service import itinerary_schedule_service
        from app.services.day_pipeline_service import candidate_to_attraction
        plan, request = fixture()
        original = [self.candidate(name=a.name, poi=a.poi_id) for d in plan.days for a in d.attractions]
        new = self.candidate(poi='supplement')
        expanded = request.model_copy(update={'candidate_places': [*original, new]})
        itinerary_schedule_service.build(plan)
        def apply_supplement(current, actual_request, llm, **kwargs):
            self.assertIn('supplement', {c.poi_id for c in actual_request.candidate_places})
            current.days[0].attractions.append(candidate_to_attraction(new, 90))
            itinerary_schedule_service.build(current)
            return current, build_planning_feedback(current, actual_request), True
        with patch.object(planning_graph, '_get_planner', return_value=Mock()), \
             patch.object(planning_graph.trip_quality_service, 'finalize', return_value=plan), \
             patch.object(planning_graph.trip_quality_service, 'refine_overall_suggestions', return_value='参考建议'), \
             patch('app.services.planning_supplement_service.supplement_candidates_once', return_value=expanded), \
             patch('app.services.planning_repair_service.repair_planning_once', side_effect=apply_supplement):
            result = planning_graph.finalize_plan_node(dict(plan=plan, request=request,
                context={'tourism_candidates':[c.model_dump(mode='json') for c in original]}), {})
        self.assertIn('supplement', {a.poi_id for d in result['result'].days for a in d.attractions})
        self.assertIn('supplement', {c['poi_id'] for c in result['context']['tourism_candidates']})


if __name__ == '__main__':
    unittest.main()
