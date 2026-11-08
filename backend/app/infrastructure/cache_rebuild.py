"""Request-owned, bounded distributed cache rebuilds; never fail open to a herd."""
import random
import threading
import time
from uuid import uuid4

from ..config import get_settings
from .shared_cache import shared_cache
from .task_trace import emit


class CacheBusyError(RuntimeError):
    """No fresh value and no safe rebuild permission; caller may degrade/retry."""


_RENEW = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
 return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""
_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
 return redis.call('DEL', KEYS[1])
end
return 0
"""
_PUBLISH = """
if redis.call('GET', KEYS[2]) ~= ARGV[1] then return 0 end
local now = redis.call('TIME')
if tonumber(ARGV[3]) <= tonumber(now[1])*1000 + tonumber(now[2])/1000 then
 redis.call('DEL', KEYS[2]); return 0
end
redis.call('SET', KEYS[1], ARGV[2], 'PXAT', ARGV[3])
redis.call('DEL', KEYS[2])
return 1
"""


class CacheRebuilder:
    def __init__(self):
        self._lock = threading.Lock()
        self._flights = {}

    def get(self, key, loader, *, allow_stale=False, wait_seconds=None):
        """Loader must return a validated success/actual-empty envelope, or raise."""
        settings = get_settings()
        if not settings.cache_enabled:
            return loader()
        wait = settings.cache_rebuild_wait_seconds if wait_seconds is None else min(
            settings.cache_rebuild_wait_seconds, max(0, wait_seconds))
        deadline = time.monotonic() + wait
        cached = shared_cache.read(key, allow_stale=allow_stale)
        if cached.hit and cached.status != 'stale':
            return cached.entry
        stale = cached.entry if cached.status == 'stale' else None
        with self._lock:
            event = self._flights.get(key.data_key)
            owner = event is None
            if owner:
                if len(self._flights) >= settings.cache_rebuild_max_local:
                    if stale is not None and stale.hard_until > time.time():
                        return stale
                    raise CacheBusyError('缓存重建繁忙，请稍后重试')
                event = self._flights[key.data_key] = threading.Event()
        if not owner:
            if stale is not None and stale.hard_until > time.time():
                return stale
            started = time.monotonic()
            emit('cache', 'local_singleflight', 'waiting')
            event.wait(max(0, deadline-time.monotonic()))
            result = shared_cache.read(key)
            if result.hit:
                emit('cache', 'local_singleflight', 'completed', duration_ms=round((time.monotonic()-started)*1000))
                return result.entry
            emit('cache', 'local_singleflight', 'timeout', duration_ms=round((time.monotonic()-started)*1000))
            raise CacheBusyError('相同查询正在处理，请稍后重试')
        try:
            return self._distributed(key, loader, stale, deadline)
        finally:
            with self._lock:
                self._flights.pop(key.data_key, None)
                event.set()

    def _distributed(self, key, loader, stale, deadline):
        settings = get_settings()
        token = uuid4().hex
        lease_ms = settings.cache_rebuild_lease_seconds * 1000
        client = None
        while True:
            if time.monotonic() < shared_cache._unavailable_until:
                if stale is not None and stale.hard_until > time.time():
                    return stale
                raise CacheBusyError('共享缓存暂不可用，未发起重复上游查询')
            try:
                client = shared_cache._redis()
                if client is None:
                    raise CacheBusyError('共享缓存尚未配置')
                acquired = client.set(key.lock_key, token, nx=True, px=lease_ms)
            except CacheBusyError:
                if stale is not None and stale.hard_until > time.time():
                    return stale
                raise
            except Exception as exc:
                shared_cache._failed(exc)
                if stale is not None and stale.hard_until > time.time():
                    return stale
                raise CacheBusyError('共享缓存暂不可用') from exc
            if acquired:
                break
            emit('cache', 'rebuild_lock', 'waiting')
            if stale is not None and stale.hard_until > time.time():
                emit('cache', 'rebuild_lock', 'stale', hit=True)
                return stale
            result = shared_cache.read(key)
            if result.hit:
                return result.entry
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                emit('cache', 'rebuild_lock', 'timeout')
                raise CacheBusyError('相同查询正在重建，请稍后重试')
            time.sleep(min(remaining, random.uniform(.08, .18)))

        stop, lost = threading.Event(), threading.Event()
        limit = time.monotonic() + settings.cache_rebuild_max_seconds

        def renew():
            while not stop.wait(10):
                try:
                    if time.monotonic() >= limit or not client.eval(_RENEW, 1, key.lock_key, token, lease_ms):
                        lost.set()
                        return
                except Exception as exc:
                    shared_cache._failed(exc)
                    lost.set()
                    return

        thread = threading.Thread(target=renew, name='cache-lease', daemon=True)
        thread.start()
        try:
            # Bypass L1: another process may have published between read and SET NX.
            result = shared_cache.read(key, bypass_local=True)
            if result.hit:
                return result.entry
            if result.status == 'unavailable':
                raise CacheBusyError('共享缓存二次检查失败，未调用上游')
            emit('cache', 'rebuild', 'started')
            entry = loader()
            if lost.is_set() or time.monotonic() >= limit:
                raise CacheBusyError('缓存重建超过租约预算')
            prepared = shared_cache.prepare(key, entry)
            if prepared is None:
                # Oversized values may serve this request, but never pollute caches.
                return entry
            entry, raw = prepared
            if lost.is_set() or time.monotonic() >= limit:
                raise CacheBusyError('缓存重建超过租约预算')
            import math
            try:
                published = client.eval(_PUBLISH, 2, key.data_key, key.lock_key, token, raw,
                                        math.floor(entry.hard_until*1000))
            except Exception as exc:
                shared_cache._failed(exc)
                raise CacheBusyError('共享缓存发布失败') from exc
            if not published:
                raise CacheBusyError('缓存重建所有权已失效')
            shared_cache._remember(key.data_key, raw, entry)
            emit('cache', 'rebuild', 'completed')
            return entry
        except CacheBusyError:
            raise
        except Exception as exc:
            # Provider failures are not empty results and are never cached.
            if stale is not None and stale.hard_until > time.time():
                emit('cache', 'rebuild', 'stale', hit=True, error_type=type(exc).__name__)
                return stale
            raise
        finally:
            stop.set()
            thread.join(timeout=settings.cache_read_timeout_seconds + .1)
            try:
                client.eval(_RELEASE, 1, key.lock_key, token)
            except Exception as exc:
                shared_cache._failed(exc)


cache_rebuilder = CacheRebuilder()
