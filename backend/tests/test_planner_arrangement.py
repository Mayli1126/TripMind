"""Planner统筹、时间轴和一次修复的离线回归。"""
import json
import unittest
from unittest.mock import Mock, patch

from test_planning_feedback import fixture
from app.services.planner_arrangement_service import arrange_researched_plan
from app.services.planning_repair_service import repair_planning_once
from app.services.tourism_research_service import TourismResearchError
from app.services.trip_quality_service import TripQualityService


class PlannerArrangementTests(unittest.TestCase):
    def test_transport_soft_warning_does_not_search_or_call_repair_model(self):
        from app.services.planning_feedback_service import build_planning_feedback
        from app.services.planning_supplement_service import supplement_candidates_once
        plan, request = fixture()
        for day in plan.days:
            place = day.attractions[0]
            place.visit_duration = 150
            day.attractions.append(place.model_copy(update=dict(stable_id=place.stable_id+'-extra',
                poi_id=place.poi_id+'-extra', name=place.name+'附近景点', visit_duration=150)))
        plan = TripQualityService().rebuild_schedule(plan,request,preserve_order=True)
        for day in plan.days:
            for item in day.schedule:
                if item.item_type in {'transport','hotel'}:
                    item.duration_minutes = 90
        feedback = build_planning_feedback(plan,request)
        self.assertTrue(any(w.type=='excessive_transit' for w in feedback.soft_warnings))
        llm = Mock(max_tokens=8192)
        with patch('app.services.planning_supplement_service.research_tourism') as search:
            supplement_candidates_once(plan,request)
            result, _, changed = repair_planning_once(plan,request,llm)
        search.assert_not_called()
        llm.invoke.assert_not_called()
        self.assertFalse(changed)
        self.assertEqual([[a.poi_id for a in d.attractions] for d in result.days],
                         [[a.poi_id for a in d.attractions] for d in plan.days])

    def test_parent_child_duplicate_is_repaired_once_without_losing_required_child(self):
        from app.models.candidate import CandidatePlace
        _, request = fixture()
        from app.models.schemas import Location
        request.candidate_places = [CandidatePlace(candidate_id=key,canonical_name=key,city=request.city,
            poi_id=key,location=Location(longitude=120,latitude=30),fact_status='verified',
            user_choice='required' if key=='child' else 'optional') for key in ['parent','child','other']]
        valid = {'days':[
            {'date':request.start_date,'theme':'景区','places':[{'candidate_id':'parent','duration_minutes':240,'recommended_child_ids':['child']}]},
            {'date':request.end_date,'theme':'街区','places':[{'candidate_id':'other','duration_minutes':90}]}]}
        invalid = json.loads(json.dumps(valid))
        invalid['days'][0]['places'].append({'candidate_id':'child','duration_minutes':0})
        llm=Mock(max_tokens=8192)
        llm.invoke.side_effect=[json.dumps(invalid),json.dumps(valid)]
        result,_=arrange_researched_plan(request,{},llm)
        self.assertEqual(llm.invoke.call_count,2)
        self.assertEqual([a.poi_id for a in result.days[0].attractions],['parent'])
        self.assertIn('child',result.days[0].attractions[0].description)
        self.assertEqual(len(request.candidate_places),3)

    def test_parent_keeps_child_recommendation_without_extra_visit_or_time(self):
        from app.models.candidate import CandidatePlace
        from app.services.planner_arrangement_service import PlaceSlot, apply_internal_visit
        plan, request = fixture()
        location = plan.days[0].attractions[0].location
        request.candidate_places = [CandidatePlace(candidate_id=key, canonical_name=name,
            city=request.city, poi_id=key, location=location, fact_status='verified',
            user_choice='required' if key == 'child' else 'optional')
            for key, name in [('parent', '西湖风景名胜区'), ('child', '断桥'), ('other', '宋城')]]
        llm = Mock(max_tokens=8192)
        llm.invoke.return_value = json.dumps({'days': [
            {'date': request.start_date, 'theme': '西湖经典游', 'places': [
                {'candidate_id': 'parent', 'duration_minutes': 240, 'recommended_child_ids': ['child'],
                 'internal_visit_suggestions': '从断桥沿白堤漫步，按体力选择湖畔游览范围'}]},
            {'date': request.end_date, 'theme': '宋城', 'places': [
                {'candidate_id': 'other', 'duration_minutes': 180}]}]})
        result, _ = arrange_researched_plan(request, {}, llm)
        self.assertEqual(len(result.days[0].attractions), 1)
        parent = result.days[0].attractions[0]
        self.assertEqual(parent.visit_duration, 240)
        self.assertIn('推荐内部游玩：断桥', parent.description)
        self.assertIn('AI游玩建议', parent.description)
        # 展示建议不能引用不存在的候选，也不能把同一子点同时作为独立行程项。
        candidates = {c.candidate_id: c for c in request.candidate_places}
        for child_id in ['invented', 'other']:
            with self.assertRaises(ValueError):
                apply_internal_visit(parent, PlaceSlot(candidate_id='parent', duration_minutes=240,
                    recommended_child_ids=[child_id]), candidates, {'parent', 'other'})
        llm.invoke.assert_called_once()

    def test_day_scenic_area_and_two_night_streets_fit_without_meals(self):
        from app.services.itinerary_schedule_service import ItineraryScheduleService
        from app.services.day_pipeline_service import build_day_capacities
        plan, request = fixture()
        day = plan.days[0]
        scenic = day.attractions[0]
        scenic.visit_duration = 240
        day.attractions += [scenic.model_copy(update=dict(stable_id='street-1', poi_id='street-1',
            name='河坊街', category='street', suitable_time='night', visit_duration=90)),
            scenic.model_copy(update=dict(stable_id='street-2', poi_id='street-2', name='南宋御街',
                category='street', suitable_time='night', visit_duration=60))]
        # 用固定交通耗时复现长接驳；不调用地图或模型，也不靠零交通掩盖容量问题。
        resolver = Mock()
        from app.models.schemas import RouteLeg
        resolver.resolve.side_effect = [RouteLeg(mode='transit', duration_minutes=m) for m in [79, 79, 4, 79]]
        with patch('app.services.road_route_service.RoadRouteResolver', return_value=resolver):
            plan = TripQualityService().rebuild_schedule(plan, request, preserve_order=True, resolve_routes=True)
        visits = [s for s in day.schedule if s.item_type == 'attraction']
        self.assertEqual(len(visits), 3)
        self.assertFalse(any(s.item_type == 'meal' for s in day.schedule))
        self.assertTrue(all(not s.start_time and not s.end_time for s in day.schedule))
        self.assertEqual(build_day_capacities(request)[0].available_minutes, 480)

    def test_evening_visit_keeps_hint_without_exact_clock(self):
        plan, request = fixture()
        plan.days[1].attractions[0].suitable_time = 'evening'
        plan = TripQualityService().rebuild_schedule(plan, request, preserve_order=True)
        visit = next(s for s in plan.days[1].schedule if s.item_type == 'attraction')
        self.assertEqual(visit.start_time, '')
        self.assertTrue(any('晚间' in note for note in plan.days[1].timing_notes))

    def test_reestimate_is_allowed_only_for_ai_duration(self):
        for source in ['ai_estimate', 'user_confirmed']:
            with self.subTest(source=source):
                plan, request = fixture()
                plan.days[0].attractions[0].visit_duration = 360
                plan.days[0].attractions[0].duration_source = source
                plan = TripQualityService().rebuild_schedule(plan, request, preserve_order=True)
                llm = Mock(max_tokens=8192)
                llm.invoke.return_value = json.dumps({'days': [
                    {'date': request.start_date, 'ordered_place_ids': ['place-0'], 'single_place_reason': '只游览核心区域，保留当地体验时间'},
                    {'date': request.end_date, 'ordered_place_ids': ['place-1']}],
                    'duration_updates': [{'place_id': 'place-0', 'duration_minutes': 240, 'visit_mode': 'classic', 'reason': '经典路线不覆盖整个景区'}]})
                with patch('app.services.road_route_service.RoadRouteResolver', return_value=None):
                    result, _, changed = repair_planning_once(plan, request, llm)
                self.assertEqual(result.days[0].attractions[0].visit_duration, 240 if source == 'ai_estimate' else 360)
                self.assertEqual(changed, source == 'ai_estimate')
                self.assertEqual(plan.days[0].attractions[0].visit_duration, 360)
                if source == 'ai_estimate':
                    llm.invoke.assert_called_once()
                else:
                    llm.invoke.assert_not_called()

    def _underfilled_with_backup(self):
        from app.models.candidate import CandidatePlace
        plan, request = fixture()
        plan = TripQualityService().rebuild_schedule(plan, request, preserve_order=True)
        request = request.model_copy(update={'candidate_places': [CandidatePlace(
            candidate_id='backup-1', canonical_name='附近博物馆', city='杭州', poi_id='backup-poi',
            location=plan.days[0].attractions[0].location, fact_status='verified',
            suggested_duration_minutes=120)]})
        return plan, request

    def _addition_response(self, request, candidate_id='backup-1', duration=120):
        return json.dumps({'days': [
            {'date': request.start_date, 'ordered_place_ids': ['place-0', candidate_id],
             'additional_places': [{'candidate_id': candidate_id, 'duration_minutes': duration}]},
            {'date': request.end_date, 'ordered_place_ids': ['place-1']}]})

    def test_underfilled_day_can_add_verified_backup_without_shortening_original(self):
        plan, request = self._underfilled_with_backup()
        llm = Mock(max_tokens=8192)
        llm.invoke.return_value = self._addition_response(request)
        with patch('app.services.road_route_service.RoadRouteResolver', return_value=None):
            result, feedback, changed = repair_planning_once(plan, request, llm)
        self.assertTrue(changed)
        self.assertEqual(result.days[0].attractions[0].visit_duration, 90)
        self.assertEqual(result.days[0].attractions[1].poi_id, 'backup-poi')
        self.assertEqual(result.days[0].attractions[1].visit_duration, 120)
        self.assertEqual(feedback.conflicts, [])
        self.assertEqual(len(plan.days[0].attractions), 1)
        llm.invoke.assert_called_once()

    def test_invented_addition_keeps_valid_original_without_retry(self):
        for candidate_id, duration in [('invented', 120)]:
            with self.subTest(candidate_id=candidate_id, duration=duration):
                plan, request = self._underfilled_with_backup()
                llm = Mock(max_tokens=8192)
                llm.invoke.return_value = self._addition_response(request, candidate_id, duration)
                with patch('app.services.road_route_service.RoadRouteResolver', return_value=None):
                    result, _, changed = repair_planning_once(plan, request, llm)
                self.assertFalse(changed)
                self.assertEqual([a.poi_id for d in result.days for a in d.attractions], ['poi-0', 'poi-1'])
                llm.invoke.assert_called_once()

    def test_relaxed_explicit_free_time_and_unverified_backups_do_not_trigger_addition(self):
        plan, request = self._underfilled_with_backup()
        unverified = request.candidate_places[0].model_copy(update={'fact_status': 'unverified'})
        variants = [request.model_copy(update={'pace': 'relaxed'}),
                    request.model_copy(update={'free_text_input': '请保留自由活动'}),
                    request.model_copy(update={'candidate_places': [unverified]}),
                    request.model_copy(update={'candidate_places': []})]
        for req in variants:
            llm = Mock(max_tokens=8192)
            result, _, changed = repair_planning_once(plan, req, llm)
            self.assertFalse(changed)
            llm.invoke.assert_not_called()

    def test_long_visit_keeps_duration_without_clock_or_restaurant_search(self):
        plan, request = fixture()
        for day in plan.days:
            day.attractions[0].visit_duration = 300
        from app.models.schemas import Meal
        for day in plan.days:
            day.meals = [Meal(type="lunch", name="旧版午餐", estimated_cost=50)]
        with patch('app.services.meal_location_service.meal_location_service.bind_meal_locations') as meals:
            plan = TripQualityService().finalize(plan, request, bind_meals=False, preserve_arrangement=True)
        meals.assert_not_called()
        visits = [s for s in plan.days[0].schedule if s.item_type == 'attraction']
        self.assertTrue(visits)
        self.assertEqual(visits[0].start_time, '')
        self.assertEqual(visits[0].duration_minutes, 300)
        self.assertTrue(all(not d.meals for d in plan.days))
        self.assertFalse(any(s.item_type == "meal" for d in plan.days for s in d.schedule))
        self.assertEqual(plan.budget.total_meals, 0)

    def test_invalid_candidate_output_is_not_materialized_after_one_schema_repair(self):
        _, request = fixture()
        llm = Mock(max_tokens=8192)
        llm.invoke.return_value = json.dumps({'days': [{'date': request.start_date, 'theme': '一天',
            'places': [{'candidate_id': 'invented', 'duration_minutes': 90}]},
            {'date': request.end_date, 'theme': '一天', 'places': [{'candidate_id': 'invented2', 'duration_minutes': 90}]}]})
        with self.assertRaises(ValueError):
            arrange_researched_plan(request, {}, llm)
        self.assertEqual(llm.invoke.call_count, 2)

    def test_repair_failure_does_not_drop_unscheduled_place_or_retry(self):
        plan, request = fixture()
        llm = Mock(max_tokens=8192)
        llm.invoke.return_value = json.dumps({'days': [
            {'date': request.start_date, 'ordered_place_ids': []},
            {'date': request.end_date, 'ordered_place_ids': ['place-1']} ]})
        before = plan.model_dump()
        with self.assertRaises(TourismResearchError):
            repair_planning_once(plan, request, llm)
        self.assertEqual(plan.model_dump(), before)
        llm.invoke.assert_called_once()

    def test_one_valid_repair_rechecks_schedule_and_preserves_places(self):
        plan, request = fixture()
        llm = Mock(max_tokens=8192)
        llm.invoke.return_value = json.dumps({'days': [
            {'date': request.start_date, 'ordered_place_ids': ['place-0']},
            {'date': request.end_date, 'ordered_place_ids': ['place-1']}]})
        with patch('app.services.road_route_service.RoadRouteResolver', return_value=None):
            result, feedback, repaired = repair_planning_once(plan, request, llm)
        self.assertTrue(repaired)
        self.assertEqual(feedback.conflicts, [])
        self.assertEqual([a.stable_id for d in result.days for a in d.attractions], ['place-0', 'place-1'])
        llm.invoke.assert_called_once()
