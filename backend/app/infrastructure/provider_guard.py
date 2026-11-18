"""基于可靠 Redis 按凭据限制调用准入，并提供显式的本地单进程模式。"""
import asyncio
from contextlib import contextmanager, asynccontextmanager
import hashlib
import threading
import time
from uuid import uuid4
from ..config import get_settings
from .task_trace import emit


class ProviderBusyError(RuntimeError):
    pass


_ACQUIRE = """
local t=redis.call('TIME'); local now=t[1]*1000+t[2]/1000
redis.call('ZREMRANGEBYSCORE',KEYS[1],'-inf',now)
local next=tonumber(redis.call('GET',KEYS[2]) or '0')
local cool=tonumber(redis.call('GET',KEYS[3]) or '0')
if now < next or now < cool or redis.call('ZCARD',KEYS[1]) >= tonumber(ARGV[2]) then return 0 end
redis.call('ZADD',KEYS[1],now+tonumber(ARGV[3]),ARGV[1])
redis.call('PEXPIRE',KEYS[1],ARGV[3])
redis.call('SET',KEYS[2],now+tonumber(ARGV[4]),'PX',math.max(1,tonumber(ARGV[4])))
return 1
"""
_RENEW = """
local t=redis.call('TIME'); local now=t[1]*1000+t[2]/1000
local expiry=redis.call('ZSCORE',KEYS[1],ARGV[1])
if not expiry or tonumber(expiry)<=now then return 0 end
redis.call('ZADD',KEYS[1],now+tonumber(ARGV[2]),ARGV[1]); redis.call('PEXPIRE',KEYS[1],ARGV[2]); return 1
"""
_COOL = """
local t=redis.call('TIME'); local now=t[1]*1000+t[2]/1000
local until_time=now+tonumber(ARGV[1]); local old=tonumber(redis.call('GET',KEYS[1]) or '0')
if until_time>old then redis.call('SET',KEYS[1],until_time,'PX',ARGV[1]) end
return 1
"""


class ProviderGuard:
    def __init__(self):
        self._lock = threading.RLock()
        self._local = {}
        self._client = None
        self._url = None
        self._unavailable_until = 0

    def _redis(self):
        settings = get_settings()
        with self._lock:
            if self._url != settings.redis_url:
                self.close()
                import redis
                from redis.retry import Retry
                from redis.backoff import NoBackoff
                self._client = redis.Redis.from_url(settings.redis_url, decode_responses=True,
                    max_connections=16, socket_connect_timeout=.3, socket_timeout=.3,
                    retry=Retry(NoBackoff(), 0), retry_on_timeout=False)
                self._url = settings.redis_url
            return self._client

    def _execute(self, operation):
        if time.monotonic() < self._unavailable_until:
            raise ProviderBusyError('上游保护状态暂不可用，请稍后重试')
        try:
            return operation(self._redis())
        except Exception as exc:
            self._unavailable_until = time.monotonic()+2
            emit('provider_guard', 'backend', 'unavailable', error_type=type(exc).__name__)
            raise ProviderBusyError('上游保护状态暂不可用，未降级直连') from exc

    def scope(self, provider, credential, endpoint=''):
        # 模型共用账号配额，不保存原始密钥、服务地址或提示词。
        digest = hashlib.sha256(f'{provider}\0{endpoint.rstrip("/")}\0{credential}'.encode()).hexdigest()
        return f'tripmind:guard:v1:{{{digest}}}'

    def acquire(self, provider, credential, endpoint=''):
        settings = get_settings()
        scope = self.scope(provider, credential, endpoint)
        token = uuid4().hex
        limit = settings.provider_map_concurrency if provider == 'amap' else settings.provider_llm_concurrency
        interval = settings.provider_map_interval_seconds if provider == 'amap' else settings.provider_llm_interval_seconds
        deadline = time.monotonic()+settings.provider_wait_seconds
        started = time.monotonic()
        distributed = settings.redis_enabled
        while True:
            if distributed:
                granted = self._execute(lambda r: r.eval(_ACQUIRE, 3, scope+':active', scope+':next', scope+':cool',
                    token, limit, 60000, max(1, int(interval*1000))))
            else:
                with self._lock:
                    state = self._local.setdefault(scope, dict(active=set(), next=0., cool=0.))
                    granted = len(state['active']) < limit and time.monotonic() >= max(state['next'], state['cool'])
                    if granted:
                        state['active'].add(token); state['next'] = time.monotonic()+interval
            if granted:
                emit('provider_guard', provider, 'acquired', duration_ms=round((time.monotonic()-started)*1000))
                return scope, token, distributed
            if time.monotonic() >= deadline:
                emit('provider_guard', provider, 'rejected', reason='admission_timeout')
                raise ProviderBusyError('上游服务繁忙，请稍后重试')
            time.sleep(min(.1, max(0, deadline-time.monotonic())))

    def release(self, permit):
        scope, token, distributed = permit
        if distributed:
            self._execute(lambda r: r.zrem(scope+':active', token))
        else:
            with self._lock:
                self._local[scope]['active'].discard(token)

    def cooldown(self, permit, seconds):
        scope, _, distributed = permit
        seconds = max(1, min(300, seconds))
        if distributed:
            self._execute(lambda r: r.eval(_COOL, 1, scope+':cool', int(seconds*1000)))
        else:
            with self._lock:
                self._local[scope]['cool'] = max(self._local[scope]['cool'], time.monotonic()+seconds)
        emit('provider_guard', 'cooldown', 'started', duration_ms=int(seconds*1000))

    @contextmanager
    def slot(self, provider, credential, endpoint=''):
        permit = self.acquire(provider, credential, endpoint)
        stop, lost = threading.Event(), threading.Event()
        def renew():
            while not stop.wait(10):
                try:
                    if not self._execute(lambda r: r.eval(_RENEW, 1, permit[0]+':active', permit[1], 60000)):
                        lost.set(); return
                except ProviderBusyError:
                    lost.set(); return
        thread = threading.Thread(target=renew, daemon=True, name='provider-lease') if permit[2] else None
        if thread:
            thread.start()
        try:
            yield permit
            if lost.is_set():
                raise ProviderBusyError('上游并发许可失效，结果未确认')
        except BaseException as exc:
            status = getattr(exc, 'status_code', None)
            response = getattr(exc, 'response', None)
            status = status or getattr(response, 'status_code', None)
            emit('provider_guard', provider, 'failed', http_status=status, error_type=type(exc).__name__)
            if status == 429 or isinstance(exc, (TimeoutError,)) or 'Timeout' in type(exc).__name__:
                delay = 10 if status == 429 else 2
                try:
                    delay = float(response.headers.get('Retry-After', delay)) if response is not None else delay
                except (ValueError, TypeError):
                    pass
                try:
                    self.cooldown(permit, delay)
                except ProviderBusyError:
                    pass
            raise
        finally:
            stop.set()
            if thread:
                thread.join(timeout=.7)
            try:
                self.release(permit)
                emit('provider_guard', 'release', 'completed')
            except ProviderBusyError:
                pass  # 崩溃或不可达进程的许可会自动过期，不能删除其他持有者的令牌。

    @asynccontextmanager
    async def async_slot(self, provider, credential):
        context = self.slot(provider, credential)
        admission = asyncio.create_task(asyncio.to_thread(context.__enter__))
        try:
            permit = await asyncio.shield(admission)
        except asyncio.CancelledError:
            try:
                await admission
            except Exception:
                pass
            else:
                await asyncio.to_thread(context.__exit__, None, None, None)
            raise
        try:
            yield permit
        except BaseException as exc:
            await asyncio.to_thread(context.__exit__, type(exc), exc, exc.__traceback__)
            raise
        else:
            await asyncio.to_thread(context.__exit__, None, None, None)

    def inspect_map(self, permit, value):
        text = str(value).upper()
        if any(word in text for word in ('CUQPS_HAS_EXCEEDED', 'ACCESS_TOO_FREQUENT', 'DAILY_QUERY_OVER_LIMIT', 'RATE_LIMIT', 'TOO_MANY_REQUESTS')):
            self.cooldown(permit, 10)

    def close(self):
        with self._lock:
            if self._client is not None:
                self._client.close(); self._client.connection_pool.disconnect()
            self._client = None; self._url = None

    def status(self):
        settings = get_settings()
        return dict(mode='distributed' if settings.redis_enabled else 'local_process_only',
            map_concurrency=settings.provider_map_concurrency, llm_concurrency=settings.provider_llm_concurrency,
            wait_seconds=settings.provider_wait_seconds, lease_seconds=60,
            backend_cooldown=time.monotonic() < self._unavailable_until)


provider_guard = ProviderGuard()


def map_http(operation, credential=None):
    with provider_guard.slot('amap', get_settings().amap_api_key if credential is None else credential) as permit:
        response = operation()
        response.raise_for_status()
        provider_guard.inspect_map(permit, response.json())
        return response
