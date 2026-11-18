"""仅管理员可主动执行的有界只读诊断，不自动修复或探测上游。"""
import time
from ..config import get_settings


def queue_snapshot():
    settings = get_settings()
    if not settings.redis_enabled:
        from .planning_job_service import planning_job_service
        with planning_job_service._lock:
            jobs = list(planning_job_service._jobs.values())
            statuses = {}
            for job in jobs:
                statuses[job['status']] = statuses.get(job['status'], 0)+1
            return dict(available=True, scope='local_process_only', statuses=statuses,
                        pending_executor_messages=planning_job_service._pending)
    try:
        from ..infrastructure.provider_guard import provider_guard
        from .job_admission import ADMISSION_KEY
        client = provider_guard._redis()  # 使用独立且超时较短的可靠状态连接池，不回退到缓存。
        with client.pipeline(transaction=False) as pipe:
            pipe.xlen('trip:jobs'); pipe.zcard('trip:retry'); pipe.xlen('trip:jobs:dead')
            pipe.hlen(ADMISSION_KEY); pipe.xpending('trip:jobs', 'planner')
            values = pipe.execute(raise_on_error=False)
        pending = values[4]
        for value in values[:4]:
            if isinstance(value, BaseException):
                raise value
        cursor, records = client.hscan(ADMISSION_KEY, cursor=0, count=100)
        ids = list(records)[:100]
        with client.pipeline(transaction=False) as pipe:
            for job_id in ids:
                pipe.hmget(f'trip:job:{job_id}', 'status', 'queued_at')
            states = pipe.execute()
        statuses = {}; oldest = 0
        for status, queued_at in states:
            status = status if status in {'queued','running','retrying','waiting_input','cancelling',
                                         'completed','failed','cancelled'} else 'unknown'
            statuses[status] = statuses.get(status, 0)+1
            if status == 'queued':
                oldest = max(oldest, max(0, time.time()-float(queued_at or time.time())))
        return dict(available=True, scope='redis_read_snapshot_not_atomic',
            stream_messages=values[0], delayed_retries=values[1], dead_letters=values[2], reservations=values[3],
            pending_ack=pending.get('pending') if isinstance(pending, dict) else None,
            pending_ack_available=isinstance(pending,dict), sampled_reservations=len(ids),
            sample_truncated=bool(cursor or len(records)>100), sampled_statuses=statuses,
            oldest_sampled_queue_wait_seconds=round(oldest, 1))
    except Exception as exc:
        return dict(available=False, scope='redis', error_type=type(exc).__name__)


def deployment_warnings(runtime):
    warnings = []
    if not runtime['redis_enabled']:
        warnings.append('任务队列和上游许可仅限当前进程，不是多进程保护。')
    cache = runtime['shared_cache']
    if cache['enabled'] and not cache['configured']:
        warnings.append('新缓存已启用但未配置连接，无新鲜值时会拒绝重建或明确估算。')
    if cache['enabled'] and cache['resource_isolation'] == 'same_endpoint':
        warnings.append('缓存与可靠状态使用相同端点，尚未资源隔离；不同DB也不能隔离淘汰。')
    if cache['enabled'] and cache['resource_isolation'] == 'different_endpoint_unverified':
        warnings.append('缓存端点不同，但仍需人工核实独立实例、内存与淘汰策略。')
    warnings.append('ready仅代表配置/会话/Worker条件，不证明余额、上游可达或容量验收。')
    return warnings


def operational_snapshot():
    from .runtime_service import runtime_status
    from ..infrastructure.task_trace import process_metrics
    from .http_metrics import http_metrics
    runtime = runtime_status()
    from .baidu_search_service import BaiduSearchService
    try:
        search_quota = BaiduSearchService().quota_status()
    except Exception as exc:
        search_quota = {"available": False, "error_type": type(exc).__name__}
    return dict(runtime=runtime, queues=queue_snapshot(), process_metrics=process_metrics(),
        search_quota=search_quota,
        warnings=deployment_warnings(runtime),
        http=http_metrics.snapshot(),
        validation='A-F local acceptance passed; paid providers and production capacity not verified')
