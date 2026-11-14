"""Redis 基础设施：连接、缓存、配额、Stream 队列帮助函数。

当前键空间与恢复边界见 docs/HANDOVER.md，多人缓存改造见 docs/backend-engineering-plan.md。
缓存读取失败可返回 miss；Redis任务模式不可用时拒绝新任务，不自动降级为内存执行。
"""

from __future__ import annotations

import json
import threading
import os
import socket
import time
from uuid import uuid4
from datetime import date, timedelta
from typing import Any, Optional

from ..config import get_settings

_STREAM = "trip:jobs"
_GROUP = "planner"
_CONSUMER = f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"
_DEAD = "trip:jobs:dead"
_RETRY = "trip:retry"
_JOB_TTL = 24 * 3600
_MAX_RETRIES = 3

_client = None
_client_lock = threading.Lock()
_group_ready = False


def _get_client():
    global _client
    if _client is None:
        import redis
        settings = get_settings()
        _client = redis.Redis.from_url(
            settings.redis_url, decode_responses=True, socket_timeout=5
        )
    return _client


def redis_available() -> bool:
    """探测 Redis 是否可用；不可用时调用方应走内存降级路径。"""
    try:
        _get_client().ping()
        return True
    except Exception as exc:
        from .task_trace import log
        log({'service':'redis','status':'unavailable','error_type':type(exc).__name__})
        return False


def ensure_group() -> None:
    """创建 Stream 消费者组（幂等）。"""
    global _group_ready
    if _group_ready:
        return
    client = _get_client()
    try:
        client.xgroup_create(_STREAM, _GROUP, id="0", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise
    _group_ready = True


# ---------------------------------------------------------------------------
# 任务状态 Hash + 事件 List
# ---------------------------------------------------------------------------

def set_job_state(job_id: str, **fields: Any) -> None:
    client = _get_client()
    mapped = {
        key: json.dumps(value, ensure_ascii=False, default=str)
        if isinstance(value, (dict, list)) else ("" if value is None else str(value))
        for key, value in fields.items()
    }
    client.hset(f"trip:job:{job_id}", mapping=mapped)
    client.expire(f"trip:job:{job_id}", _JOB_TTL)


def get_job_state(job_id: str) -> Optional[dict[str, str]]:
    data = _get_client().hgetall(f"trip:job:{job_id}")
    return data or None


def append_job_event(job_id: str, event: dict[str, Any]) -> None:
    """追加进度事件；序列号用 INCR 原子生成。

    并行 research 分支同时写事件时，"读长度再写"的读-改-写会产生重复
    sequence（SSE 按 sequence>=last 过滤会漏发事件）。INCR 天然单调唯一。
    """
    key = f"trip:job:{job_id}:events"
    seq_key = f"trip:job:{job_id}:events:seq"
    client = _get_client()
    event = {**event, "sequence": int(client.incr(seq_key)) - 1}
    client.rpush(key, json.dumps(event, ensure_ascii=False, default=str))
    client.expire(key, _JOB_TTL)
    client.expire(seq_key, _JOB_TTL)


def read_job_events(job_id: str, since_sequence: int) -> list[dict[str, Any]]:
    raw = _get_client().lrange(f"trip:job:{job_id}:events", 0, -1)
    events = []
    for item in raw:
        event = json.loads(item)
        if event.get("sequence", 0) >= since_sequence:
            events.append(event)
    return events


# ---------------------------------------------------------------------------
# Stream 生产/消费/重试
# ---------------------------------------------------------------------------

def enqueue_job(job_id: str, job_type: str, payload: dict[str, Any]) -> None:
    raise RuntimeError('Legacy unreserved enqueue disabled; use publish_job/resume_job')


def dequeue_jobs(block_ms: int = 5000, count: int = 1) -> list[tuple[str, dict[str, Any]]]:
    """XREADGROUP 领取任务；返回 [(message_id, fields)]。"""
    ensure_group()
    result = _get_client().xreadgroup(
        _GROUP, _CONSUMER, {_STREAM: ">"}, block=block_ms if block_ms>0 else None, count=count
    )
    entries: list[tuple[str, dict[str, Any]]] = []
    for _stream, messages in result or []:
        for message_id, fields in messages:
            entries.append((message_id, dict(fields)))
    return entries


def ack_job(message_id: str) -> None:
    with _get_client().pipeline(transaction=True) as pipe:
        pipe.xack(_STREAM, _GROUP, message_id)
        pipe.xdel(_STREAM, message_id)
        pipe.execute()


def schedule_retry(job_id: str, job_type: str, payload: dict[str, Any],
                   attempt: int) -> bool:
    """退避重试：score=下次重试时间戳；超过上限进死信。返回是否还会重试。"""
    import time
    client = _get_client()
    if attempt >= _MAX_RETRIES:
        client.xadd(_DEAD, {
            "job_id": job_id,
            "type": job_type,
            "payload": json.dumps(payload, ensure_ascii=False, default=str),
            "failed_at": str(time.time()),
        }, maxlen=1000, approximate=False)
        return False
    delay = 60 * (2 ** attempt)
    member=json.dumps(
            {"job_id": job_id, "type": job_type, "payload": payload, "attempt": attempt + 1},
            ensure_ascii=False, default=str,
        )
    from redis.exceptions import WatchError
    key=f'trip:job:{job_id}'
    with client.pipeline() as pipe:
        try:
            pipe.watch(key)
            if pipe.hget(key,'status') not in {'running','retrying'}:
                return False
            previous=pipe.hget(key,'retry_member')
            pipe.multi()
            if previous:
                pipe.zrem(_RETRY, previous)
            pipe.zadd(_RETRY,{member:time.time()+delay})
            pipe.hset(key,'retry_member',member)
            pipe.execute()
        except WatchError:
            return False
    return True


def pop_due_retries() -> list[dict[str, Any]]:
    """取出到期重试任务。"""
    import time
    client = _get_client()
    items = client.zrangebyscore(_RETRY, 0, time.time())
    if not items:
        return []
    # Publish + remove in one transaction; never lose a retry between ZREM and XADD.
    moved=[]
    from redis.exceptions import WatchError
    for item in items:
        with client.pipeline() as pipe:
            try:
                data=json.loads(item)
                key=f"trip:job:{data['job_id']}"
                pipe.watch(_RETRY, _STREAM, key)
                score=pipe.zscore(_RETRY,item)
                if score is None or score>time.time():
                    pipe.unwatch(); continue
                if pipe.hget(key,'status') in {'completed','failed','cancelled','cancelling',None}:
                    pipe.multi(); pipe.zrem(_RETRY,item); pipe.execute(); continue
                if pipe.xlen(_STREAM) >= get_settings().job_queue_capacity:
                    pipe.unwatch(); continue
                pipe.multi()
                pipe.xadd(_STREAM,{'job_id':data['job_id'],'type':data['type'],
                    'payload':json.dumps({**data['payload'],'attempt':data['attempt']},ensure_ascii=False)})
                pipe.zrem(_RETRY,item)
                pipe.hdel(key,'retry_member')
                pipe.hset(key,'queued_at',str(time.time()))
                pipe.execute(); moved.append(data)
            except WatchError:
                continue
    return moved


def owns_job_lease(job_id, token):
    return _get_client().get(f'trip:job:{job_id}:lease') == token


def acquire_job_lease(job_id, token):
    return bool(_get_client().set(f'trip:job:{job_id}:lease',token,nx=True,ex=45))


def release_job_lease(job_id, token):
    client=_get_client()
    from redis.exceptions import WatchError
    with client.pipeline() as pipe:
        try:
            key=f'trip:job:{job_id}:lease'; pipe.watch(key)
            if pipe.get(key)!=token:
                pipe.unwatch(); return False
            pipe.multi(); pipe.delete(key); pipe.execute(); return True
        except WatchError:
            return False


def heartbeat_job(job_id, token, message_id):
    from redis.exceptions import WatchError
    client=_get_client(); key=f'trip:job:{job_id}:lease'
    with client.pipeline() as pipe:
        try:
            pipe.watch(key)
            if pipe.get(key)!=token:
                pipe.unwatch(); return False
            pipe.multi(); pipe.expire(key,45)
            pipe.hset(f'trip:job:{job_id}',mapping={'heartbeat_at':str(time.time()),'worker_id':_CONSUMER,'run_id':token})
            pipe.xclaim(_STREAM,_GROUP,_CONSUMER,min_idle_time=0,message_ids=[message_id],justid=True)
            pipe.execute(); return True
        except WatchError:
            return False


_RECLAIM_CURSOR='0-0'
def reclaim_jobs():
    global _RECLAIM_CURSOR
    ensure_group()
    result=_get_client().xautoclaim(_STREAM,_GROUP,_CONSUMER,min_idle_time=60000,
                                  start_id=_RECLAIM_CURSOR,count=10)
    _RECLAIM_CURSOR=result[0]
    return [(mid,dict(fields)) for mid,fields in result[1]]


def transition_job(job_id, allowed, **fields):
    """CAS for cancel/resume; terminal decisions cannot be undone by a late writer."""
    from redis.exceptions import WatchError
    key=f'trip:job:{job_id}'; client=_get_client()
    for _ in range(8):
        with client.pipeline() as pipe:
            try:
                pipe.watch(key)
                if pipe.hget(key,'status') not in allowed:
                    pipe.unwatch(); return False
                pipe.multi(); pipe.hset(key,mapping={k:'' if v is None else str(v) for k,v in fields.items()})
                pipe.expire(key,_JOB_TTL); pipe.execute(); return True
            except WatchError:
                continue
    return False


def guarded_job_write(job_id, token, fields, allowed=None):
    """
    守卫式写入任务字段：仅当「任务存在 + 租约令牌匹配 + 状态合法」时才写入。

    - token: 租约令牌。持锁 worker 写状态时传入自己的 token 验明正身；
             None 表示无租约要求（管理面直接写）
    - fields: 要写入的字段字典（值可为任意类型，统一规整为字符串）
    - allowed: 允许的状态白名单（如只许 RUNNING→SUCCEEDED），None 不限

    返回 True=写入成功；False=任务消失/令牌不符/状态不允许/重试 8 轮全撞车
    """
    from redis.exceptions import WatchError
    key=f'trip:job:{job_id}'; lease=f'{key}:lease'; client=_get_client()
    for _ in range(8):
        with client.pipeline() as pipe:
            try:
                pipe.watch(key,lease)
                state=pipe.hgetall(key)
                if not state or (token and pipe.get(lease)!=token) or (allowed is not None and state.get('status') not in allowed):
                    pipe.unwatch(); return False
                values={k:json.dumps(v,ensure_ascii=False,default=str) if isinstance(v,(dict,list)) else '' if v is None else str(v) for k,v in fields.items()}
                pipe.multi(); pipe.hset(key,mapping=values); pipe.expire(key,_JOB_TTL)
                from ..services.job_admission import TERMINAL, ADMISSION_KEY
                if fields.get('status') in TERMINAL:
                    pipe.hdel(ADMISSION_KEY, job_id)
                    if state.get('retry_member'):
                        pipe.zrem(_RETRY,state['retry_member'])
                        pipe.hdel(key,'retry_member')
                pipe.execute(); return True
            except WatchError:
                continue
    return False


def publish_job(job_id, job_type, payload, initial):
    """Initial state and queue entry commit together, including a deduplicated job ID."""
    from redis.exceptions import WatchError
    from ..services.job_admission import ADMISSION_KEY, check_capacity, redis_records, expire_records, submission_owner
    ensure_group(); key=f'trip:job:{job_id}'; client=_get_client()
    for _ in range(8):
        with client.pipeline() as pipe:
            try:
                pipe.watch(key, ADMISSION_KEY, _STREAM)
                if pipe.exists(key):
                    pipe.unwatch(); return False
                server_time=client.time(); now=server_time[0]+server_time[1]/1e6
                records, expired=redis_records(pipe, now)
                owner=submission_owner.get()
                check_capacity([record['owner'] for record in records.values()], owner)
                if pipe.xlen(_STREAM) >= get_settings().job_queue_capacity:
                    from fastapi import HTTPException
                    raise HTTPException(503,'规划消息队列已满，请稍后重试',headers={'Retry-After':'5'})
                initial={**initial, 'owner_id':owner, 'queued_at':now, 'admission_until':now+get_settings().job_admission_max_age_seconds}
                mapped={k:json.dumps(v,ensure_ascii=False,default=str) if isinstance(v,(dict,list)) else str(v or '') for k,v in initial.items()}
                pipe.multi(); pipe.hset(key,mapping=mapped); pipe.expire(key,_JOB_TTL)
                expire_records(pipe, expired)
                pipe.hset(ADMISSION_KEY, job_id, json.dumps(dict(owner=owner, until=initial['admission_until'])))
                pipe.xadd(_STREAM,{'job_id':job_id,'type':job_type,'payload':json.dumps(payload,ensure_ascii=False,default=str)})
                pipe.execute(); return True
            except WatchError:
                continue
    raise RuntimeError('job_submission_contention')


def resume_job(job_id,value):
    from redis.exceptions import WatchError
    key=f'trip:job:{job_id}'; client=_get_client()
    for _ in range(8):
        with client.pipeline() as pipe:
            try:
                from ..services.job_admission import ADMISSION_KEY
                pipe.watch(key, ADMISSION_KEY, _STREAM)
                state=pipe.hgetall(key)
                if state.get('status')!='waiting_input':
                    pipe.unwatch(); return False
                server_time=client.time(); now_seconds=server_time[0]+server_time[1]/1e6
                if float(state.get('admission_until') or 0) <= now_seconds:
                    from fastapi import HTTPException
                    raise HTTPException(409,'任务确认时间已过期，请重新提交')
                if not pipe.hexists(ADMISSION_KEY, job_id):
                    from fastapi import HTTPException
                    raise HTTPException(409,'任务名额已过期，请重新提交')
                if pipe.xlen(_STREAM) >= get_settings().job_queue_capacity:
                    from fastapi import HTTPException
                    raise HTTPException(503,'规划消息队列已满，请稍后重试',headers={'Retry-After':'5'})
                payload={'request':json.loads(state['request']),'resume_value':value,
                         'expected_question':json.loads(state.get('waiting_input') or '{}')}
                from datetime import datetime, timezone
                from ..services.planning_metrics_service import advance_stage
                now=datetime.now(timezone.utc)
                metrics=json.loads(state.get('metrics') or '{}')
                advance_stage(metrics,'queued','queued',now,job_id)
                pipe.multi(); pipe.hset(key,mapping={'status':'queued','stage':'queued','waiting_input':'','message':'已确认，等待继续执行','queued_at':str(now_seconds),
                    'updated_at':now.isoformat(),'metrics':json.dumps(metrics,ensure_ascii=False)})
                pipe.xadd(_STREAM,{'job_id':job_id,'type':'resume','payload':json.dumps(payload,ensure_ascii=False)})
                pipe.execute(); return True
            except WatchError:
                continue
    return False


# ---------------------------------------------------------------------------
# 配额（WATCH 事务保证"检查+递增"原子性）
# ---------------------------------------------------------------------------

def quota_check_and_incr(client_id: str, limit: int, scope: str = "plan") -> tuple[bool, int]:
    """返回 (是否放行, 当前已用次数)。limit<=0 表示不限。

    scope 用于区分业务配额（plan/guide），互不挤占。
    """
    if limit <= 0:
        return True, 0
    client = _get_client()
    key = f"quota:{scope}:{client_id}:{date.today().isoformat()}"
    ttl = int(timedelta(days=2).total_seconds())
    for _ in range(5):
        pipe = client.pipeline()
        try:
            pipe.watch(key)
            current = int(pipe.get(key) or 0)
            if current >= limit:
                pipe.unwatch()
                return False, current
            pipe.multi()
            pipe.incr(key)
            pipe.expire(key, ttl)
            pipe.execute()
            return True, current + 1
        except Exception:
            continue  # WATCH 冲突，重试
    return False, limit


# ---------------------------------------------------------------------------
# 通用缓存
# ---------------------------------------------------------------------------

def cache_get(key: str) -> Optional[str]:
    try:
        value=_get_client().get(f"cache:{key}")
        from .task_trace import emit
        emit('cache','redis_lookup','lookup',hit=value is not None)
        return value
    except Exception:
        return None


def cache_set(key: str, value: str, ttl_seconds: int) -> None:
    try:
        _get_client().setex(f"cache:{key}", ttl_seconds, value)
    except Exception:
        pass
