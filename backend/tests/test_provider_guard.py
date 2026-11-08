"""Offline admission checks; real Redis scripts and multiprocess load are deferred."""
from unittest import TestCase
from unittest.mock import patch
from app.config import get_settings
from app.infrastructure.provider_guard import ProviderGuard, ProviderBusyError


class ProviderGuardChecks(TestCase):
    def setUp(self):
        self.settings = get_settings().model_copy(update=dict(redis_enabled=False,
            provider_map_concurrency=1, provider_map_interval_seconds=0, provider_wait_seconds=0))
        self.guard = ProviderGuard()
        patcher = patch('app.infrastructure.provider_guard.get_settings', return_value=self.settings)
        patcher.start(); self.addCleanup(patcher.stop)

    def test_concurrency_rejection_and_exception_release(self):
        with self.assertRaises(ValueError):
            with self.guard.slot('amap', 'fixture'):
                with self.assertRaises(ProviderBusyError):
                    self.guard.acquire('amap', 'fixture')
                raise ValueError('fixture')
        with self.guard.slot('amap', 'fixture'):
            pass

    def test_shared_credential_scope_and_cooldown(self):
        with self.guard.slot('amap', 'fixture') as permit:
            self.guard.inspect_map(permit, {'info': 'CUQPS_HAS_EXCEEDED_THE_LIMIT'})
        with self.assertRaises(ProviderBusyError):
            self.guard.acquire('amap', 'fixture')
        with self.guard.slot('amap', 'other-fixture'):
            pass
        self.assertNotIn('fixture', self.guard.scope('amap', 'fixture'))

    def test_distributed_failure_never_falls_back_local(self):
        self.settings.redis_enabled = True
        with patch.object(self.guard, '_redis', side_effect=ConnectionError('fixture')):
            with self.assertRaises(ProviderBusyError):
                self.guard.acquire('amap', 'fixture')
        self.assertFalse(self.guard._local)

    def test_rate_spacing_independent_of_concurrency(self):
        self.settings.provider_map_concurrency = 3
        self.settings.provider_map_interval_seconds = 1
        with self.guard.slot('amap', 'fixture'):
            with self.assertRaises(ProviderBusyError):
                self.guard.acquire('amap', 'fixture')
