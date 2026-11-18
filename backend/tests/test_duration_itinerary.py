"""顺序时长模式的业务回归，不调用模型或真实地图。"""
import json
import unittest
from unittest.mock import Mock

from test_planning_feedback import fixture
from app.models.schemas import Hotel, IntercityJourney, RouteLeg
from app.services.itinerary_schedule_service import itinerary_schedule_service
from app.services.planning_feedback_service import build_planning_feedback
from app.services.trip_projection_service import project_stored_plan
from app.services.day_pipeline_service import build_day_capacities


class DurationItineraryTests(unittest.TestCase):
    def test_long_visits_are_kept_without_clock_cutoff(self):
        plan, request = fixture()
        day = plan.days[0]
        first = day.attractions[0]
        first.visit_duration = 600
        first.closing_time = '12:00'
        day.attractions.append(first.model_copy(update=dict(stable_id='extra', poi_id='extra',
            name='夜市', visit_duration=360, suitable_time='night')))
        itinerary_schedule_service.build(plan)
        visits = [s for s in day.schedule if s.item_type == 'attraction']
        self.assertEqual([s.duration_minutes for s in visits], [600, 360])
        self.assertTrue(all(not s.start_time and not s.end_time for s in day.schedule))
        self.assertFalse(any(s.item_type in {'meal', 'break'} for s in day.schedule))
        feedback = build_planning_feedback(plan, request)
        self.assertEqual(feedback.conflicts, [])
        self.assertTrue(any(w.type == 'overloaded_day' for w in feedback.soft_warnings))
        self.assertTrue(any('夜市' in note and '晚间' in note for note in day.timing_notes))

    def test_transport_totals_include_hotel_and_keep_provider_provenance(self):
        plan, _ = fixture()
        day = plan.days[0]
        day.hotel = Hotel(name='酒店', address='杭州', location=day.attractions[0].location,
                          price_range='', type='舒适型酒店', estimated_cost=300)
        shared_leg = RouteLeg(mode='transit', duration_minutes=45, distance_km=10,
                              source='amap_route', is_estimated=False, note='高德参考')
        resolver = Mock()
        resolver.resolve.return_value = shared_leg
        itinerary_schedule_service.build(plan, route_resolver=resolver)
        self.assertEqual(day.transport_minutes, 90)
        self.assertEqual(day.total_minutes, day.visit_minutes + 90)
        self.assertFalse(day.schedule[0].route_leg.is_estimated)
        self.assertIn('参考时段', day.schedule[0].notes)
        self.assertEqual(shared_leg.note, '高德参考')

    def test_confirmed_return_is_visible_without_invented_finish_time(self):
        plan, request = fixture()
        plan.return_journey = IntercityJourney(mode='train', leg='return', name='已确认车次',
            departure_date=request.end_date, arrival_date=request.end_date,
            departure_time='16:00', arrival_time='18:00', departure_station='杭州站', arrival_station='上海站',
            station_transfer_minutes=30, check_in_buffer_minutes=30,
            arrival_transfer_minutes=20, is_estimated=False, source='user_confirmed')
        itinerary_schedule_service.build(plan)
        self.assertTrue(any('16:00' in note for note in plan.days[-1].timing_notes))
        self.assertTrue(all(not s.end_time for s in plan.days[-1].schedule))

    def test_capacity_is_pace_reference_without_default_return_clock(self):
        _, request = fixture()
        self.assertEqual(build_day_capacities(request)[0].available_minutes, 480)
        self.assertEqual(build_day_capacities(request.model_copy(update={'pace':'relaxed'}))[0].available_minutes, 360)
        self.assertNotIn('17:00', build_day_capacities(request)[-1].note)

    def test_history_projection_preserves_saved_route_data_without_network(self):
        plan, request = fixture()
        resolver = Mock()
        resolver.resolve.return_value = RouteLeg(mode='walking', duration_minutes=12,
            distance_km=1, source='amap_route', is_estimated=False)
        itinerary_schedule_service.build(plan, route_resolver=resolver)
        for day in plan.days:
            for item in day.schedule:
                item.start_time, item.end_time = '09:00', '10:00'
        source = plan.model_dump_json()
        _, projected = project_stored_plan(source, request.model_dump_json())
        self.assertFalse(projected.days[0].schedule[0].route_leg.is_estimated)
        self.assertTrue(all(not s.start_time for d in projected.days for s in d.schedule))
        self.assertTrue(json.loads(source)['days'][0]['schedule'][0]['start_time'])


if __name__ == '__main__':
    unittest.main()
