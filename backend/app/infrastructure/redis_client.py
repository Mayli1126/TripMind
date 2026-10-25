"""Redis 基础设施：连接、缓存、配额、Stream 队列帮助函数。

对应方案 docs/langgraph-migration-plan.md 第八节键空间设计。
Redis 不可用时缓存/配额直接穿透（返回 miss/放行），不影响核心规划。
"""

from __future__ import annotations

import json
import threading
from datetime import date, timedelta
from typing import Any, Optional

from ..config import get_settings

_STREAM = "trip:jobs"
_GROUP = "planner"
_CONSUMER = "tripmind-worker"
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
        print(f"⚠️ Redis 不可用，使用内存队列降级: {exc}")
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
    ensure_group()
    _get_client().xadd(_STREAM, {
        "job_id": job_id,
        "type": job_type,
        "payload": json.dumps(payload, ensure_ascii=False, default=str),
    })


def dequeue_jobs(block_ms: int = 5000, count: int = 1) -> list[tuple[str, dict[str, Any]]]:
    """XREADGROUP 领取任务；返回 [(message_id, fields)]。"""
    ensure_group()
    result = _get_client().xreadgroup(
        _GROUP, _CONSUMER, {_STREAM: ">"}, block=block_ms, count=count
    )
    entries: list[tuple[str, dict[str, Any]]] = []
    for _stream, messages in result or []:
        for message_id, fields in messages:
            entries.append((message_id, dict(fields)))
    return entries


def ack_job(message_id: str) -> None:
    _get_client().xack(_STREAM, _GROUP, message_id)


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
        })
        return False
    delay = 60 * (2 ** attempt)
    client.zadd(_RETRY, {
        json.dumps(
            {"job_id": job_id, "type": job_type, "payload": payload, "attempt": attempt + 1},
            ensure_ascii=False, default=str,
        ): time.time() + delay
    })
    return True


def pop_due_retries() -> list[dict[str, Any]]:
    """取出到期重试任务。"""
    import time
    client = _get_client()
    items = client.zrangebyscore(_RETRY, 0, time.time())
    if not items:
        return []
    client.zremrangebyscore(_RETRY, 0, time.time())
    return [json.loads(item) for item in items]


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
        return _get_client().get(f"cache:{key}")
    except Exception:
        return None


def cache_set(key: str, value: str, ttl_seconds: int) -> None:
    try:
        _get_client().setex(f"cache:{key}", ttl_seconds, value)
    except Exception:
        pass
