"""共用任务准入策略；Redis 在发布时原子预留，本地模式在任务锁内预留。"""
from contextvars import ContextVar
from fastapi import HTTPException
from ..config import get_settings

submission_owner = ContextVar('submission_owner', default='internal')
TERMINAL = {'completed', 'failed', 'cancelled'}
ADMISSION_KEY = 'trip:admission:v1'


def check_capacity(owners, owner):
    settings = get_settings()
    if sum(value == owner for value in owners) >= settings.job_user_active_limit:
        from ..infrastructure.task_trace import emit
        emit('admission', 'submit', 'rejected', reason='user_limit')
        raise HTTPException(429, '你已有较多未结束的任务，请完成或取消后再提交', headers={'Retry-After': '5'})
    # 保守估算队列容量时，同时计算运行中、重试中和等待输入的任务。
    if len(owners) >= min(settings.job_global_active_limit, settings.job_queue_capacity):
        from ..infrastructure.task_trace import emit
        emit('admission', 'submit', 'rejected', reason='global_capacity')
        raise HTTPException(503, '规划队列已满，请稍后重试', headers={'Retry-After': '5'})


def redis_records(pipe, now):
    """调用方对准入哈希执行 WATCH，仍存活的执行器即使租约过期也保留预留名额。"""
    import json
    records = {k: json.loads(v) for k, v in pipe.hgetall(ADMISSION_KEY).items()}
    expired = []
    for job_id, record in list(records.items()):
        if record['until'] <= now:
            key = f'trip:job:{job_id}'
            pipe.watch(key, key+':lease')
            if not pipe.exists(key+':lease'):
                expired.append(job_id)
                del records[job_id]
    return records, expired


def expire_records(pipe, expired):
    for job_id in expired:
        pipe.hdel(ADMISSION_KEY, job_id)
        pipe.hset(f'trip:job:{job_id}', mapping=dict(status='failed', stage='expired',
            message='任务等待或确认时间过长，请重新提交', error='admission_expired'))
        pipe.expire(f'trip:job:{job_id}', 86400)
