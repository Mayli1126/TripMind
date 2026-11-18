"""缓存重建的小规模离线检查，真实 Redis 和多进程验收另行执行。"""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from unittest import TestCase
from unittest.mock import patch

from app.config import get_settings
from app.infrastructure.cache_rebuild import CacheRebuilder, CacheBusyError, _PUBLISH, _RELEASE
from app.infrastructure.shared_cache import SharedCache, make_key
from app.services.cache_policy import make_entry


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.lock = Lock()

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, nx=False, **kwargs):
        with self.lock:
            if nx and key in self.values:
                return False
            self.values[key] = value
            return True

    def eval(self, script, count, *args):
        with self.lock:
            if script == _PUBLISH:
                data, lock, token, raw, expiry = args
                if self.values.get(lock) != token:
                    return 0
                self.values[data] = raw
                self.values.pop(lock)
                return 1
            key, token, *rest = args
            if self.values.get(key) != token:
                return 0
            if script == _RELEASE:
                self.values.pop(key)
            return 1


class CacheRebuildChecks(TestCase):
    def setUp(self):
        self.settings = get_settings().model_copy(update=dict(cache_enabled=True, cache_rebuild_wait_seconds=.1))
        self.cache, self.redis, self.rebuild = SharedCache(), FakeRedis(), CacheRebuilder()
        for target, value in [
            ('app.infrastructure.shared_cache.get_settings', self.settings),
            ('app.infrastructure.cache_rebuild.get_settings', self.settings),
        ]:
            patcher = patch(target, return_value=value)
            patcher.start(); self.addCleanup(patcher.stop)
        patcher = patch('app.infrastructure.cache_rebuild.shared_cache', self.cache)
        patcher.start(); self.addCleanup(patcher.stop)
        patcher = patch.object(self.cache, '_redis', return_value=self.redis)
        patcher.start(); self.addCleanup(patcher.stop)
        self.key = make_key('poi', {'city': 'offline', 'keyword': 'fixture'})

    def test_publish_once_and_preserve_absolute_age(self):
        calls = []
        def load():
            calls.append(1)
            return make_entry('poi', [{'name': 'fixture'}], 'fixture')
        first = self.rebuild.get(self.key, load)
        second = self.rebuild.get(self.key, load)
        self.assertEqual(len(calls), 1)
        self.assertEqual(first.hard_until, second.hard_until)
        self.assertNotIn(self.key.lock_key, self.redis.values)

    def test_busy_and_unconfigured_never_call_provider(self):
        self.redis.set(self.key.lock_key, 'other-owner')
        def forbidden():
            self.fail('waiters must not penetrate upstream')
        with self.assertRaises(CacheBusyError):
            self.rebuild.get(self.key, forbidden, wait_seconds=0)
        with patch.object(self.cache, '_redis', return_value=None):
            with self.assertRaises(CacheBusyError):
                self.rebuild.get(self.key, forbidden)

    def test_lost_owner_cannot_publish_or_delete_new_owner(self):
        def load():
            self.redis.set(self.key.lock_key, 'new-owner')
            return make_entry('poi', [], 'fixture', empty=True)
        with self.assertRaises(CacheBusyError):
            self.rebuild.get(self.key, load)
        self.assertNotIn(self.key.data_key, self.redis.values)
        self.assertEqual(self.redis.get(self.key.lock_key), 'new-owner')

    def test_failure_not_negative_cached_but_actual_empty_is(self):
        def fail():
            raise ValueError('provider parsing failure')
        with self.assertRaises(ValueError):
            self.rebuild.get(self.key, fail)
        self.assertNotIn(self.key.data_key, self.redis.values)
        self.rebuild.get(self.key, lambda: make_entry('poi', [], 'fixture', empty=True))
        self.assertEqual(self.cache.read(self.key).status, 'negative')

    def test_local_waiter_shares_owner(self):
        entered, finish = Event(), Event()
        calls = []
        def load():
            calls.append(1); entered.set()
            finish.wait(1)
            return make_entry('poi', [], 'fixture', empty=True)
        with ThreadPoolExecutor(max_workers=2) as executor:
            owner = executor.submit(self.rebuild.get, self.key, load)
            self.assertTrue(entered.wait(1))
            waiter = executor.submit(self.rebuild.get, self.key, load)
            finish.set()
            self.assertEqual(owner.result().outcome, 'empty')
            self.assertEqual(waiter.result().outcome, 'empty')
        self.assertEqual(len(calls), 1)
