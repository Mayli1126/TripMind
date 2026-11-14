"""Small offline regressions for the new user-facing workflows; temporary DB only."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api.auth import AuthMiddleware
from app.api.routes import auth, conversations, library, planner_chat
from app.services.auth_service import auth_service
from app.infrastructure.database import application_database
from app.services import conversation_store, advisory_refresh_service as refresh
from app.services.trip_store_service import TripStoreService
from app.models.schemas import SavedTripCreate, TripPlan
from tests.test_trip_quality import make_request, make_day


class AccountConversationTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / 'integration.db'
        patcher = mock.patch.object(application_database, 'path', path)
        patcher.start(); self.addCleanup(patcher.stop)
        self.store = TripStoreService(path)
        auth_service.initialize(); conversation_store.initialize(); refresh.initialize()
        auth_service.create_user('admin', 'fixture-password', role='admin')
        self.user = auth_service.create_user('alice', 'fixture-password')
        self.other = auth_service.create_user('bruce', 'fixture-password')
        patcher = mock.patch.object(library, 'trip_store_service', self.store)
        patcher.start(); self.addCleanup(patcher.stop)
        app = FastAPI()
        app.add_middleware(AuthMiddleware)
        for router in (auth.router, conversations.router, library.router, planner_chat.router):
            app.include_router(router, prefix='/api')
        app.include_router(auth.admin_router, prefix='/api')
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.login('alice')

    def login(self, username):
        response = self.client.post('/api/auth/login', json={'username': username, 'password': 'fixture-password'})
        self.assertEqual(response.status_code, 200)

    def create_trip(self):
        request = make_request()
        plan = TripPlan(city=request.city, start_date=request.start_date, end_date=request.end_date,
                        days=[make_day(0, 10), make_day(1, 20)], overall_suggestions='测试')
        return self.store.create_trip(SavedTripCreate(request=request, plan=plan), owner_id=self.user['id'])

    def test_login_required(self):
        self.client.cookies.clear()
        self.assertEqual(self.client.get('/api/library/conversations').status_code, 401)

    def test_runtime_diagnostics_is_admin_only(self):
        self.assertEqual(self.client.get('/api/admin/runtime/diagnostics').status_code,403)
        self.login('admin')
        with mock.patch('app.services.operations_service.operational_snapshot', return_value={'queues':{'available':True}}):
            response=self.client.get('/api/admin/runtime/diagnostics')
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.json()['queues']['available'])

    def test_conversation_restore_conflict_and_delete(self):
        payload = {'kind': 'planner', 'title': '北京旅行', 'snapshot': {'messages': [{'role': 'user', 'content': '玩三天'}], 'draft': {'city': '北京'}}, 'revision': 0}
        url = '/api/library/conversations/session-1'
        self.assertEqual(self.client.put(url, json=payload).json()['revision'], 1)
        self.assertEqual(self.client.get(url).json()['snapshot'], payload['snapshot'])
        self.assertEqual(self.client.put(url, json=payload).status_code, 409)
        payload['revision'] = 1
        self.assertEqual(self.client.put(url, json=payload).json()['revision'], 2)
        self.assertEqual(len(self.client.get('/api/library/conversations').json()), 1)
        self.client.delete(url)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_conversation_isolation(self):
        url = '/api/library/conversations/private'
        payload = {'kind': 'planner', 'snapshot': {'messages': ['private']}}
        self.assertEqual(self.client.put(url, json=payload).status_code, 200)
        self.login('bruce')
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.put(url, json=payload).status_code, 404)
        self.client.delete(url)
        self.login('alice')
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_memory_recall_pause_delete_and_isolation(self):
        url = '/api/library/memories/preference-1'
        payload = {'content': '喜欢博物馆', 'city': '北京', 'enabled': True}
        self.assertEqual(self.client.put(url, json=payload).status_code, 200)
        self.assertEqual(conversation_store.memory_context(self.user['id'], '北京'), '喜欢博物馆')
        self.assertEqual(conversation_store.memory_context(self.user['id'], '上海'), '')
        self.login('bruce')
        self.assertEqual(self.client.get('/api/library/memories').json(), [])
        self.assertEqual(self.client.put(url, json=payload).status_code, 404)
        self.login('alice'); payload['enabled'] = False; payload['revision'] = 1
        self.client.put(url, json=payload)
        self.assertEqual(conversation_store.memory_context(self.user['id'], '北京'), '')
        self.client.delete(url, params={'revision': 2})
        self.assertEqual(self.client.get('/api/library/memories').json(), [])

    def test_trip_chat_binding_and_stale_version(self):
        trip = self.create_trip()
        url = f'/api/library/trips/{trip.id}/assistant/messages'
        payload = {'base_version': 99, 'messages': [{'role': 'user', 'content': '第二天太赶了吗'}]}
        self.assertEqual(self.client.post(url, json=payload).status_code, 409)
        self.login('bruce')
        self.assertEqual(self.client.post(url, json=payload).status_code, 404)
        self.assertEqual(self.client.put('/api/library/conversations/foreign-trip', json={'kind': 'trip', 'trip_id': trip.id}).status_code, 404)

    def test_trip_answer_cites_real_fields_without_saving_version(self):
        trip = self.create_trip()
        raw = '{"answer":"预算是估算。", "evidence_paths":["plan.city","invented.path"], "uncertainties":[], "proposed_instruction":null}'
        with mock.patch('app.services.trip_assistant_service._client') as model:
            model.return_value.invoke.return_value = raw
            result = self.client.post(f'/api/library/trips/{trip.id}/assistant/messages', json={'base_version': 1, 'messages': [{'role': 'user', 'content': '预算怎么算'}]})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['evidence'], [{'path': 'plan.city', 'value': trip.city}])
        self.assertFalse(result.json()['can_apply'])
        self.assertEqual(self.store.get_trip(trip.id).current_version, 1)

    def test_save_rejects_stale_base_version(self):
        trip = self.create_trip()
        result = self.client.post(f'/api/library/trips/{trip.id}/versions', json={'plan': trip.plan.model_dump(mode='json'), 'base_version': 5})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.store.get_trip(trip.id).current_version, 1)

    def test_live_hours_do_not_verify_reservation_and_cache_is_reused(self):
        from app.config import get_settings
        settings = get_settings()
        with mock.patch.object(settings, 'amap_api_key', 'fixture-key'), mock.patch.object(settings, 'advisory_provider_url', ''), mock.patch('app.services.public_advisory_cache.httpx.Client') as http:
            response = http.return_value.__enter__.return_value.get.return_value
            response.json.return_value = {'status': '1', 'pois': [{'id': 'B123', 'business': {'opentime_week': '周二至周日9:00-17:00'}}]}
            refresh.refresh('B123', '2026-12-01')
            refresh.refresh('B123', '2026-12-01')
            self.assertEqual(http.return_value.__enter__.return_value.get.call_count, 1)
        advice = refresh.overlay(None, 'B123', '2026-12-01')
        self.assertEqual(advice.reservation_required, 'unknown')
        self.assertEqual(advice.fact_status, 'unverified')
        self.assertIn('9:00', advice.opening_hours_note)

    def test_rules_mode_skips_extra_model_call(self):
        from app.config import get_settings
        from app.services.trip_quality_service import trip_quality_service
        trip = self.create_trip()
        with mock.patch.object(get_settings(), 'planning_tips_mode', 'rules'), mock.patch('app.services.trip_quality_service._get_suggestions_llm') as model:
            self.assertTrue(trip_quality_service.refine_overall_suggestions(trip.plan, trip.request))
            model.assert_not_called()

    def test_real_app_startup_and_health_without_external_services(self):
        from app.api import main
        with mock.patch.object(main.settings, 'redis_enabled', False), mock.patch.object(main.settings, 'mcp_warmup_on_startup', False), mock.patch.object(main, 'print_config'), mock.patch.object(main, 'validate_config'), mock.patch.object(main.planning_job_service, 'shutdown'), mock.patch('builtins.print'):
            with TestClient(main.app) as client:
                self.assertEqual(client.get('/health').status_code, 200)
                self.assertEqual(client.get('/api/library/conversations').status_code, 401)
                self.assertEqual(client.get('/api/auth/status').json()['admin_ready'], True)
