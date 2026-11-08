"""Bounded L1 and independent Redis transport; rebuild ownership is in cache_rebuild."""
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import math
import threading
import time
from urllib.parse import urlsplit

from ..config import get_settings
from ..services.cache_policy import CacheEnvelope, CacheKind, POLICIES
from .task_trace import emit


@dataclass(frozen=True)
class CacheKey:
    kind: str
    digest: str

    @property
    def data_key(self):
        return f'tripmind:cache:v1:{self.kind}:{{{self.digest}}}:data'

    @property
    def lock_key(self):
        return f'tripmind:cache:v1:{self.kind}:{{{self.digest}}}:lock'


def make_key(kind: CacheKind, parameters: dict) -> CacheKey:
    if kind not in POLICIES:
        raise ValueError('Unknown cache policy')
    raw = json.dumps(parameters, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return CacheKey(kind, hashlib.sha256(raw.encode('utf-8')).hexdigest())


@dataclass(frozen=True)
class CacheRead:
    status: str  # fresh / stale / negative / miss / disabled / unavailable / invalid / oversized
    entry: CacheEnvelope | None = None
    layer: str = ''

    @property
    def hit(self):
        return self.entry is not None


class SharedCache:
    def __init__(self):
        self._lock = threading.RLock()
        self._local = OrderedDict()
        self._local_bytes = 0
        self._pool = self._client = None
        self._signature = None
        self._unavailable_until = 0.0
        self._last_error_type = ''

    def _redis(self):
        settings = get_settings()
        if not settings.cache_redis_url:
            return None
        signature = (settings.cache_redis_url, settings.cache_max_connections,
                     settings.cache_connect_timeout_seconds, settings.cache_read_timeout_seconds)
        with self._lock:
            if signature != self._signature:
                if self._signature is not None:
                    self.close()
                import redis
                from redis.backoff import NoBackoff
                from redis.retry import Retry
                self._pool = redis.ConnectionPool.from_url(settings.cache_redis_url,
                    decode_responses=False, max_connections=max(1, settings.cache_max_connections),
                    socket_connect_timeout=max(.01, settings.cache_connect_timeout_seconds),
                    socket_timeout=max(.01, settings.cache_read_timeout_seconds),
                    retry=Retry(NoBackoff(), 0), retry_on_timeout=False)
                self._client = redis.Redis(connection_pool=self._pool)
                self._signature = signature
            return self._client

    def _failed(self, exc):
        with self._lock:
            self._last_error_type = type(exc).__name__
            self._unavailable_until = time.monotonic() + max(0, get_settings().cache_failure_cooldown_seconds)
        emit('cache', 'shared_backend', 'failed', error_type=type(exc).__name__)

    def _drop(self, name):
        old = self._local.pop(name, None)
        if old:
            self._local_bytes -= len(old[1])

    def _remember(self, name, raw, entry):
        settings = get_settings()
        ttl = min(settings.cache_l1_ttl_seconds, entry.hard_until-time.time())
        if ttl <= 0 or settings.cache_l1_max_entries <= 0 or len(raw) > settings.cache_l1_max_bytes:
            return
        with self._lock:
            self._drop(name)
            self._local[name] = (time.monotonic()+ttl, raw)
            self._local_bytes += len(raw)
            while len(self._local) > settings.cache_l1_max_entries or self._local_bytes > settings.cache_l1_max_bytes:
                self._drop(next(iter(self._local)))

    def _classify(self, key, raw, layer, allow_stale):
        if len(raw) > get_settings().cache_max_value_bytes:
            return CacheRead('oversized', layer=layer)
        try:
            entry = CacheEnvelope.model_validate_json(raw)
        except ValueError:
            return CacheRead('invalid', layer=layer)
        now = time.time()
        if entry.fetched_at > now or entry.hard_until <= now:
            return CacheRead('miss', layer=layer)
        if entry.outcome == 'empty':
            return CacheRead('negative', entry, layer) if now < entry.fresh_until else CacheRead('miss', layer=layer)
        if now < entry.fresh_until:
            return CacheRead('fresh', entry, layer)
        if allow_stale and POLICIES[key.kind].allow_stale:
            return CacheRead('stale', entry, layer)
        return CacheRead('miss', layer=layer)

    def read(self, key: CacheKey, *, allow_stale=False, bypass_local=False) -> CacheRead:
        if not get_settings().cache_enabled:
            return CacheRead('disabled')
        with self._lock:
            local = None if bypass_local else self._local.get(key.data_key)
            if local and local[0] <= time.monotonic():
                self._drop(key.data_key); local = None
            if local:
                result = self._classify(key, local[1], 'l1', allow_stale)
                if result.hit:
                    self._local.move_to_end(key.data_key)
                    emit('cache', 'shared_l1', result.status, hit=True)
                    return result
                self._drop(key.data_key)
        emit('cache', 'shared_l1', 'lookup', hit=False)
        if time.monotonic() < self._unavailable_until:
            return CacheRead('unavailable', layer='l2')
        try:
            client = self._redis()
            if client is None:
                return CacheRead('unavailable', layer='l2')
            raw = client.get(key.data_key)
            result = CacheRead('miss', layer='l2') if raw is None else self._classify(key, raw, 'l2', allow_stale)
            emit('cache', 'shared_l2', result.status, hit=result.hit)
            if result.hit:
                self._remember(key.data_key, raw, result.entry)
            return result
        except Exception as exc:
            self._failed(exc)
            return CacheRead('unavailable', layer='l2')

    def prepare(self, key: CacheKey, entry: CacheEnvelope):
        """Validate/cap before either explicit writes or fenced publication."""
        # Revalidate to avoid accepting an envelope mutated after creation.
        entry = CacheEnvelope.model_validate(entry.model_dump())
        policy = POLICIES[key.kind]
        hard_cap = entry.fetched_at + (policy.negative_seconds if entry.outcome == 'empty' else policy.hard_seconds)
        entry.hard_until = min(entry.hard_until, hard_cap)
        entry.fresh_until = min(entry.fresh_until, entry.hard_until,
                                entry.fetched_at + (policy.negative_seconds if entry.outcome == 'empty' else policy.fresh_seconds))
        raw = entry.model_dump_json().encode('utf-8')
        if len(raw) > get_settings().cache_max_value_bytes:
            return None
        if entry.hard_until <= time.time():
            return None
        return entry, raw

    def write(self, key: CacheKey, entry: CacheEnvelope) -> str:
        """Explicit administrative write; rebuilds use token-checked publication."""
        if not get_settings().cache_enabled:
            return 'disabled'
        prepared = self.prepare(key, entry)
        if prepared is None:
            return 'not_cacheable'
        entry, raw = prepared
        self._remember(key.data_key, raw, entry)
        if time.monotonic() < self._unavailable_until:
            return 'local_only'
        try:
            client = self._redis()
            if client is None:
                return 'local_only'
            # Absolute expiry preserves original age across layers and repeated writes.
            client.set(key.data_key, raw, pxat=math.floor(entry.hard_until*1000))
            emit('cache', 'shared_write', 'completed')
            return 'shared'
        except Exception as exc:
            self._failed(exc)
            return 'local_only'

    def status(self):
        settings = get_settings()
        def endpoint(url):
            parsed = urlsplit(url)
            return (parsed.hostname, parsed.port or 6379) if parsed.scheme in ('redis', 'rediss') else (parsed.scheme, parsed.path)
        try:
            isolation = ('unconfigured' if not settings.cache_redis_url else 'same_endpoint'
                         if endpoint(settings.cache_redis_url) == endpoint(settings.redis_url) else 'different_endpoint_unverified')
        except ValueError:
            isolation = 'invalid_configuration'
        with self._lock:
            return dict(enabled=settings.cache_enabled, configured=bool(settings.cache_redis_url),
                resource_isolation=isolation, l1_entries=len(self._local), l1_bytes=self._local_bytes,
                max_connections=settings.cache_max_connections,
                backend_state='cooldown' if time.monotonic() < self._unavailable_until else 'not_probed',
                last_error_type=self._last_error_type, business_adapters_migrated=['poi_search', 'poi_around', 'road_route', 'weather', 'opening_hours', 'official_policy'],
                rebuild_lease_seconds=settings.cache_rebuild_lease_seconds,
                rebuild_wait_seconds=settings.cache_rebuild_wait_seconds)

    def close(self):
        with self._lock:
            if self._client is not None:
                self._client.close()
            if self._pool is not None:
                self._pool.disconnect()
            self._client = self._pool = self._signature = None
            self._local.clear(); self._local_bytes = 0
            self._unavailable_until = 0


shared_cache = SharedCache()
