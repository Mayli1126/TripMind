from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch, MagicMock

from app.models.schemas import Location, RouteLeg, TripPlan, ScheduleItem
from app.services.road_route_service import RoadRouteResolver, _CACHE
from app.services.trip_quality_service import TripQualityService
from app.services.itinerary_schedule_service import itinerary_schedule_service
from test_trip_quality import make_request, make_day


class RoadRouteTests(TestCase):
    def setUp(self):
        _CACHE.clear()
        self.settings = SimpleNamespace(cache_enabled=False, road_routes_enabled=True, amap_api_key='test-only',
                                        road_routes_max_seconds=12, road_routes_max_calls=24)
        self.config = patch('app.services.road_route_service.get_settings', return_value=self.settings)
        self.config.start()
        self.addCleanup(self.config.stop)
        self.a = Location(longitude=116.4, latitude=39.9)
        self.b = Location(longitude=116.45, latitude=39.92)

    def test_transit_price_distance_directional_cache(self):
        data = {'status': '1', 'route': {'distance': '99999', 'transits': [{
            'duration': '1201', 'cost': '4.5', 'segments': [
                {'walking': {'distance': '200'}, 'bus': {'buslines': [{'distance': '4800'}]}}
            ]}]}}
        with patch('app.services.road_route_service.httpx.Client') as client:
            client.return_value.__enter__.return_value.get.return_value.json.return_value = data
            resolver = RoadRouteResolver('北京')
            leg = resolver.resolve(self.a, self.b, '2026-12-01', '09:05')
            self.assertEqual(leg.duration_minutes, 21)
            self.assertEqual(leg.distance_km, 5)
            self.assertEqual(leg.cost, 4.5)
            self.assertFalse(leg.is_estimated)
            self.assertTrue(leg.cost_is_estimated)
            resolver.resolve(self.a, self.b, '2026-12-01', '09:25')
            self.assertEqual(client.call_count, 1)
            resolver.resolve(self.b, self.a, '2026-12-01', '09:25')
            self.assertEqual(client.call_count, 2)
            resolver.resolve(self.a, self.b, '2026-12-02', '09:25')
            self.assertEqual(client.call_count, 3)

    def test_missing_price_not_free_and_failures_not_cached(self):
        with patch('app.services.road_route_service.httpx.Client') as client:
            response = client.return_value.__enter__.return_value.get.return_value
            response.json.return_value = {'status': '1', 'route': {'transits': [
                {'duration': '600', 'cost': [], 'segments': []}]}}
            leg = RoadRouteResolver('北京').resolve(self.a, self.b, '2026-12-01', '09:00')
            self.assertEqual(leg.cost, 5)
            self.assertIn('缺价格', leg.note)
            _CACHE.clear()
            response.json.return_value = {'status': '0'}
            resolver = RoadRouteResolver('北京')
            self.assertTrue(resolver.resolve(self.a, self.b, '2026-12-01', '09:00').is_estimated)
            self.assertFalse(_CACHE)

    def test_limits_missing_coordinates_and_self_drive(self):
        self.settings.road_routes_max_calls = 0
        with patch('app.services.road_route_service.httpx.Client') as client:
            resolver = RoadRouteResolver('北京', preference='自驾')
            leg = resolver.resolve(self.a, self.b, '2026-12-01', '09:00')
            self.assertEqual(leg.cost_unit, 'vehicle')
            self.assertEqual(leg.mode, 'driving')
            self.assertTrue(leg.is_estimated)
            self.assertEqual(resolver.resolve(None, self.b, '2026-12-01', '09:00').duration_minutes, 30)
            client.assert_not_called()

    def test_route_duration_drives_schedule_and_budget_counts_vehicle(self):
        request = make_request(travelers=5, rooms=2)
        day = make_day(0, 10)
        day.hotel.location = self.a
        resolver = MagicMock()
        resolver.resolve.return_value = RouteLeg(mode='driving', duration_minutes=41,
            distance_km=10, cost=25, cost_unit='vehicle', source='amap_route', is_estimated=False)
        plan = TripPlan(city='北京', start_date=request.start_date, end_date=request.end_date,
                        days=[day], overall_suggestions='测试')
        itinerary_schedule_service.build(plan, route_resolver=resolver)
        legs = [item for item in day.schedule if item.route_leg]
        self.assertEqual(len(legs), 2)
        self.assertTrue(all(item.duration_minutes == 41 for item in legs))
        budget = TripQualityService().calculate_budget(plan, request, 50)
        self.assertEqual(budget.local_transportation, 100)  # 两段接驳，每段二十五元，共两辆车。
        self.assertEqual(budget.intercity_transportation, 500)
        self.assertIn('逐段', budget.calculation_note)

    def test_unscheduled_ticket_not_charged(self):
        request = make_request()
        day = make_day(0, 100)
        day.schedule = [ScheduleItem(start_time='12:00', end_time='13:00', item_type='break', title='休息')]
        plan = TripPlan(city='北京', start_date=request.start_date, end_date=request.end_date,
                        days=[day], overall_suggestions='测试')
        self.assertEqual(TripQualityService().calculate_budget(plan, request).total_attractions, 0)

    def test_revision_keeps_roundtrip_price_and_untouched_schedule(self):
        from app.graphs.revision_graph import finalize_revision_node
        from app.models.patch import PlanPatch, PatchOperation
        from app.models.schemas import Budget
        request = make_request()
        plan = TripPlan(city='北京', start_date=request.start_date, end_date=request.end_date,
                       days=[make_day(0, 10), make_day(1, 20)], overall_suggestions='测试',
                       budget=Budget(intercity_transportation=200))
        itinerary_schedule_service.build(plan)
        before = plan.days[0].model_dump()
        instruction = PlanPatch(base_version=1, operations=[PatchOperation(
            op='change_start_time', day_index=1, start_time='10:00')],
            affected_day_indexes=[1], explanation='晚出发')
        resolver = MagicMock()
        resolver.resolve.return_value = RouteLeg(mode='transit', duration_minutes=20, cost=5)
        with patch('app.graphs.revision_graph.MultiAgentTripPlanner.revise_trip', side_effect=AssertionError('不能自动整份重生成')), \
             patch('app.services.road_route_service.RoadRouteResolver', return_value=resolver):
            result = finalize_revision_node({'request': request, 'patched_plan': plan,
                'plan_patch': instruction}, {})['result']
        self.assertEqual(result.budget.intercity_transportation, 200)
        self.assertEqual(result.days[0].model_dump(), before)
        # 未修改的旧行程日期仍保留交通预留费用，不能从预算中消失。
        self.assertGreaterEqual(result.budget.local_transportation, 100)
