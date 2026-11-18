"""交通选择、失败保护和保存回读的离线检查。"""
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch
from types import SimpleNamespace
from app.models.schemas import Location, RouteLeg, TripPlan, SavedTripCreate
from app.services.itinerary_schedule_service import itinerary_schedule_service
from app.services.transport_selection_service import select_transport
from app.services.trip_store_service import TripStoreService
from app.services.road_route_service import RoadRouteResolver, _CACHE
from test_trip_quality import make_request, make_day

class TransportSelectionTests(TestCase):
    def test_cycling_api(self):
        settings = SimpleNamespace(cache_enabled=False, road_routes_enabled=True, amap_api_key='test-only',
                                   road_routes_max_seconds=12, road_routes_max_calls=24)
        _CACHE.clear()
        with patch('app.services.road_route_service.get_settings', return_value=settings), patch('app.services.road_route_service.httpx.Client') as client:
            client.return_value.__enter__.return_value.get.return_value.json.return_value = {
                'errcode': 0, 'data': {'paths': [{'distance': 2800, 'duration': 960}]}}
            leg = RoadRouteResolver('北京').resolve(Location(longitude=116.4, latitude=39.9),
                Location(longitude=116.45, latitude=39.92), '2026-12-01', '10:00', 'cycling')
            self.assertEqual((leg.mode, leg.distance_km, leg.duration_minutes, leg.cost), ('cycling', 2.8, 16, 0))
            self.assertFalse(leg.is_estimated)
            self.assertIn('/v4/direction/bicycling', client.return_value.__enter__.return_value.get.call_args.args[0])

    def test_view_selection_does_not_change_version_and_failure_keeps_original(self):
        request = make_request()
        plan = TripPlan(city=request.city, start_date=request.start_date, end_date=request.end_date,
                        days=[make_day(0, 10), make_day(1, 20)], overall_suggestions='测试')
        for day in plan.days:
            day.hotel.location = Location(longitude=116.4, latitude=39.9)
        resolver = Mock(city='北京')
        transit = RouteLeg(mode='transit', duration_minutes=40, cost=5, source='amap_route', is_estimated=False)
        resolver.resolve.return_value = transit
        itinerary_schedule_service.build(plan, route_resolver=resolver)
        segment = plan.days[0].schedule[0]
        before = [[a.model_dump() for a in d.attractions] for d in plan.days]
        driving = RouteLeg(mode='driving', distance_km=3.7, duration_minutes=20, cost=3,
                           cost_unit='vehicle', source='amap_route', is_estimated=False)
        with tempfile.TemporaryDirectory() as folder:
            store = TripStoreService(Path(folder) / 'trips.db')
            original = store.create_trip(SavedTripCreate(request=request, plan=plan))
            with patch('app.services.transport_selection_service.RoadRouteResolver', return_value=resolver):
                resolver.resolve.return_value = driving
                changed = select_transport(plan, request, segment.stable_id, 'driving')
            self.assertEqual(changed.days[0].transport_minutes, plan.days[0].transport_minutes - 20)
            self.assertEqual([[a.model_dump() for a in d.attractions] for d in changed.days], before)
            from app.services.revision_outcome import has_plan_changes
            self.assertFalse(has_plan_changes(plan, changed))
            self.assertEqual(changed.revision_status, plan.revision_status)
            loaded = store.get_trip(original.id)
            self.assertEqual(loaded.current_version, 1)
            self.assertEqual(loaded.plan.days[0].schedule[0].selected_route_mode, '')
            self.assertEqual(loaded.plan.days[0].schedule[0].duration_minutes, 40)
        with patch('app.services.transport_selection_service.RoadRouteResolver', return_value=resolver):
            resolver.resolve.return_value = transit.model_copy(update={'is_estimated': True})
            with self.assertRaises(ValueError):
                select_transport(plan, request, segment.stable_id, 'walking')
        self.assertEqual(plan.days[0].schedule[0].duration_minutes, 40)
