"""Independent connections/locks against a disposable SQLite database."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest import TestCase
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api.routes import library

from app.models.schemas import SavedTripCreate, SavedTripUpdate, TripPlan, UserPreferences
from app.services.auth_service import AuthService
from app.services.trip_store_service import TripStoreService
from tests.test_trip_quality import make_request, make_day


class DatabaseConcurrencyChecks(TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'concurrency.db'
        self.first = TripStoreService(self.path)
        self.second = TripStoreService(self.path)  # Not the same Python lock.
        request = make_request()
        self.plan = TripPlan(city=request.city, start_date=request.start_date, end_date=request.end_date,
                             days=[make_day(0, 10), make_day(1, 20)], overall_suggestions='fixture')
        self.saved = self.first.create_trip(SavedTripCreate(request=request, plan=self.plan))

    def test_racing_versions_one_wins_and_no_write_lock_during_provider(self):
        barrier = Barrier(2)
        def finalize(plan, *args, **kwargs):
            # A separate writer must be able to acquire the database during slow enrichment.
            with self.first._connection() as db:
                db.execute('BEGIN IMMEDIATE')
            barrier.wait(timeout=5)
            return plan
        def save(store):
            try:
                store.add_version(self.saved.id, SavedTripUpdate(plan=self.plan, base_version=1))
                return 'saved'
            except ValueError:
                return 'conflict'
        with patch('app.services.trip_store_service.trip_quality_service.finalize', side_effect=finalize), ThreadPoolExecutor(2) as pool:
            results = list(pool.map(save, [self.first, self.second]))
        self.assertCountEqual(results, ['saved', 'conflict'])
        self.assertEqual(self.first.get_trip(self.saved.id).current_version, 2)
        with self.assertRaises(ValueError):
            self.second.restore_version(self.saved.id, 1, base_version=1)

    def test_progress_revision_prevents_stale_update_and_deleted_parent(self):
        one = self.first.set_progress(self.saved.id, 'step', True, None, 1)
        token = one.items[0].revision
        with self.assertRaises(ValueError):
            self.second.set_progress(self.saved.id, 'step', False, None, 1)
        with patch.object(self.second, '_now', return_value=one.items[0].completed_at):
            two = self.second.set_progress(self.saved.id, 'step', False, token, 1)
        self.assertNotEqual(two.items[0].revision, token)
        self.assertFalse(two.items[0].completed)
        with self.assertRaises(ValueError):
            self.first.set_progress(self.saved.id, 'step', True, token, 1)
        self.first.delete_trip(self.saved.id)
        with self.assertRaises(KeyError):
            self.second.set_progress(self.saved.id, 'step', True, None, 1)

    def test_preferences_concurrent_writes_and_account_isolation(self):
        first, second = AuthService(self.path), AuthService(self.path)
        first.initialize()
        def save(service):
            try:
                return service.save_preferences('alice', UserPreferences()).revision
            except ValueError:
                return 'conflict'
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(save, [first, second]))
        self.assertCountEqual(results, [1, 'conflict'])
        self.assertEqual(first.preferences('alice').revision, 1)
        self.assertEqual(first.preferences('bob').revision, 0)
        self.assertEqual(second.save_preferences('bob', UserPreferences()).revision, 1)

    def test_http_preconditions_and_conflicts(self):
        app = FastAPI()
        app.include_router(library.router)
        base = f'/library/trips/{self.saved.id}'
        with patch.object(library, 'trip_store_service', self.first), TestClient(app) as client:
            self.assertEqual(client.post(base + '/versions', json={'plan': self.plan.model_dump(mode='json')}).status_code, 428)
            self.assertEqual(client.put(base + '/progress/step', json={'completed': True}).status_code, 428)
            with patch('app.services.trip_store_service.trip_quality_service.finalize', side_effect=lambda p, *a, **k: p):
                accepted = client.post(base + '/versions', json={'plan': self.plan.model_dump(mode='json'), 'base_version': 1})
            self.assertEqual(accepted.status_code, 200)
            self.assertEqual(client.post(base + '/versions/1/restore?base_version=1').status_code, 409)
            self.assertEqual(client.put(base + '/progress/step', json={'completed': True, 'base_version': 1, 'expected_revision': None}).status_code, 409)
