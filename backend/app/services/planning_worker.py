"""Redis 任务监管器，提供租约所有权校验、心跳、待确认消息恢复和隔离执行。"""
from __future__ import annotations
import json
import threading
import time
from uuid import uuid4
from ..infrastructure import redis_client
from ..models.schemas import TripPlan

_STOP=threading.Event()
_THREAD=None
_LAST_RECLAIM=0.0


def worker_status():
    return {"running":_THREAD is not None and _THREAD.is_alive() and not _STOP.is_set(),
            "consumer":redis_client._CONSUMER,"lease_seconds":45,"reclaim_idle_seconds":60}


def _execute(message_id, fields, recovered=False):
    from ..graphs.planning_graph import PlanningWaitingInput
    from .planning_job_service import RedisJobRecorder
    from .job_process import isolated_executor, JobCancelled, ExecutorStopped
    try:
        job_id=fields["job_id"]; job_type=fields["type"]; payload=json.loads(fields["payload"])
        attempt=int(payload.get("attempt",0))
    except (KeyError,ValueError,TypeError):
        redis_client.ack_job(message_id)
        return
    state=redis_client.get_job_state(job_id) or {}
    if not state or state.get("status") in {"completed","failed","cancelled"}:
        redis_client.ack_job(message_id); return
    if state.get("status")=="waiting_input" and job_type!="resume":
        redis_client.ack_job(message_id); return
    if state.get("status")=="retrying" and attempt<=int(state.get('attempt') or 0):
        # 安排重试后即使崩溃，也不能导致原任务与重试副本同时执行。
        redis_client.ack_job(message_id); return
    token=uuid4().hex
    if not redis_client.acquire_job_lease(job_id,token):
        return  # 保留待确认队列记录，直到当前存活持有者完成任务或租约过期。
    recorder=RedisJobRecorder(job_id,token)
    acknowledge=False
    try:
        from ..config import get_settings
        # 旧版未完成任务没有准入记录，上线前必须先清空存量任务。
        if float(state.get('admission_until') or 0) <= time.time():
            recorder.fail('任务等待或确认时间已过期，请重新提交')
            acknowledge=True; return
        if not recovered and time.time()-float(state.get('queued_at') or 0) > get_settings().job_max_queue_wait_seconds:
            recorder.fail('任务排队超时，请稍后重新提交')
            acknowledge=True; return
        if state.get("status")=="cancelling":
            recorder.cancel(); acknowledge=True; return
        recovery_count=int(state.get("recovery_count") or 0)+(1 if recovered else 0)
        if recovery_count>3:
            recorder.fail("任务多次异常退出，请使用任务编号联系管理员。")
            acknowledge=True; return
        redis_client.set_job_state(job_id,recovery_count=recovery_count)
        if not redis_client.heartbeat_job(job_id,token,message_id):
            return
        recorder.mark_started(attempt)
        if recovered:
            recorder.diagnostic(dict(trace_id=job_id,run_id=token,service="runtime",operation="recovery",
                status="started",reason="pending_message_reclaimed",attempt=recovery_count))
        def cancelled():
            return (redis_client.get_job_state(job_id) or {}).get("status") in {"cancelling","cancelled"}
        def heartbeat():
            return redis_client.heartbeat_job(job_id,token,message_id)
        value=isolated_executor.run(dict(job_id=job_id,run_id=token,type=job_type,payload=payload,
            redis=True,recover=recovered),recorder.callback(),recorder.diagnostic,cancelled,heartbeat,_STOP.is_set)
        if cancelled():
            recorder.cancel()
        else:
            recorder.complete(TripPlan.model_validate(value))
        acknowledge=True
    except PlanningWaitingInput as wait:
        recorder.waiting_input(wait.question); acknowledge=True
    except JobCancelled:
        recorder.cancel(); acknowledge=True
    except ExecutorStopped:
        # 关闭或失去租约时不确认消息，由其他监管进程接管待确认记录。
        pass
    except Exception as exc:
        if redis_client.owns_job_lease(job_id,token):
            if (redis_client.get_job_state(job_id) or {}).get("status")=="cancelling":
                recorder.cancel()
            else:
                recorder.diagnostic(dict(trace_id=job_id,run_id=token,service="runtime",operation="execute",
                    status="failed",error_type=getattr(exc,'error_type',type(exc).__name__)))
                if getattr(exc,'error_type','') == 'TourismResearchError':
                    # 依据不足、额度、权限等已明确失败，不自动重跑付费搜索。
                    recorder.fail(getattr(exc,'public_message',None) or '联网选点未通过核验，请调整需求后重试')
                elif redis_client.schedule_retry(job_id,job_type,payload,attempt):
                    recorder.update(0,"retrying","执行中断，已安排自动重试","retrying")
                else:
                    recorder.fail(f"执行失败（{type(exc).__name__}），任务编号 {job_id}")
            acknowledge=True
    finally:
        # 只有所有权校验通过时才能确认消息，旧执行器不能消费替代执行器的消息。
        if acknowledge and redis_client.owns_job_lease(job_id,token):
            if (redis_client.get_job_state(job_id) or {}).get('status')=='cancelling':
                recorder.cancel()
            redis_client.ack_job(message_id)
        redis_client.release_job_lease(job_id,token)


def process_once(block_ms=0):
    global _LAST_RECLAIM
    redis_client.pop_due_retries()  # 原子转移到消息流，不进行第二次入队。
    entries=[]
    if time.monotonic()-_LAST_RECLAIM>=5:
        _LAST_RECLAIM=time.monotonic()
        entries=redis_client.reclaim_jobs()
    handled=0
    for mid,fields in entries:
        _execute(mid,fields,recovered=True); handled+=1
        if _STOP.is_set():
            return handled
    for mid,fields in redis_client.dequeue_jobs(block_ms=block_ms):
        _execute(mid,fields); handled+=1
    return handled


def _run_loop():
    from ..infrastructure.task_trace import log
    while not _STOP.is_set():
        try:
            process_once(block_ms=2000)
        except Exception as exc:
            log({"service":"worker","status":"failed","error_type":type(exc).__name__})
            _STOP.wait(1)


def start_worker():
    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        return
    redis_client.ensure_group(); _STOP.clear()
    _THREAD=threading.Thread(target=_run_loop,daemon=True,name="trip-planning-worker")
    _THREAD.start()


def stop_worker():
    global _THREAD
    _STOP.set()
    if _THREAD is not None:
        _THREAD.join(timeout=15)
        if not _THREAD.is_alive():
            _THREAD=None


if __name__=="__main__":
    start_worker()
    try:
        while _THREAD and _THREAD.is_alive():
            _THREAD.join(timeout=1)
    except KeyboardInterrupt:
        stop_worker()
