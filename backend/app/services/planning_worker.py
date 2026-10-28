"""Redis 任务 Worker：消费 trip:jobs Stream，执行规划/修订图。

- 与 API 同进程时由 main.py startup 拉起守护线程（单实例部署）；
- 也可独立运行（python -m app.services.planning_worker）支持多实例；
- 失败任务退避重试（trip:retry ZSET），超过 3 次进死信 trip:jobs:dead；
- waiting_input / completed / failed 均 XACK（状态已持久化在 Redis/Checkpointer）。
"""

from __future__ import annotations

import json
import threading
from typing import Any, Optional

from ..infrastructure import redis_client
from ..models.schemas import TripPlan, TripRequest

_STOP = threading.Event()
_THREAD: Optional[threading.Thread] = None


def _execute(message_id: str, fields: dict[str, Any]) -> None:
    # 延迟导入，避免模块导入期拉起 MCP/Agent。
    from ..agents.trip_planner_agent import get_trip_planner_agent
    from ..graphs.planning_graph import PlanningWaitingInput, run_planning_graph
    from .planning_job_service import RedisJobRecorder

    # 坏消息防护：缺关键字段时直接 ACK 丢弃，否则会无限重投刷日志
    try:
        job_id = fields["job_id"]
        job_type = fields["type"]
        payload = json.loads(fields["payload"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"⚠️ 丢弃无法解析的任务消息 {message_id}: {exc}")
        redis_client.ack_job(message_id)
        return
    attempt = int(payload.get("attempt", 0))

    # resume 幂等保护：重复的 resume 消息（双击/多标签页）到达时任务可能已完成，
    # 对已终结的 thread 再 resume 必然异常并走进重试→failed，会把 completed 覆盖成失败
    if job_type == "resume":
        state = redis_client.get_job_state(job_id) or {}
        if state.get("status") in {"completed", "failed"}:
            print(f"⚠️ 任务 {job_id} 已 {state.get('status')}，忽略重复 resume 消息")
            redis_client.ack_job(message_id)
            return

    recorder = RedisJobRecorder(job_id)
    recorder.mark_started()
    try:
        # from_storage：任务可能在 waiting_input 停留过夜，重建时跳过
        # "出发日期不能早于今天"校验（否则次日 resume 必然 ValidationError 降级模板）
        request = TripRequest.model_validate(
            payload["request"], context={"from_storage": True}
        )
        if job_type == "plan":
            result = run_planning_graph(request, recorder.callback(), job_id=job_id)
        elif job_type == "resume":
            # 从 Checkpointer 断点恢复（interrupt 之后），不重算 research/plan
            result = run_planning_graph(
                request,
                recorder.callback(),
                job_id=job_id,
                resume_value=payload.get("resume_value"),
            )
        elif job_type == "revision":
            plan = TripPlan.model_validate_json(payload["plan"])
            # 阶段1批1：修订走 RevisionGraph（PlanPatch 局部修改），
            # 解析失败/超范围时图内部回退 legacy revise_trip。
            from ..graphs.revision_graph import run_revision_graph

            result = run_revision_graph(
                request, plan, payload["instruction"], recorder.callback(),
                base_version=payload.get("base_version"),
            )
        else:
            raise ValueError(f"未知任务类型: {job_type}")
        recorder.complete(result)
    except PlanningWaitingInput as wait:
        # 用户确认点：状态置 waiting_input，断点保留在 Checkpointer，XACK 等恢复
        recorder.waiting_input(wait.question)
    except Exception as exc:
        print(f"❌ 任务 {job_id} 第 {attempt + 1} 次执行失败: {exc}")
        will_retry = redis_client.schedule_retry(job_id, job_type, payload, attempt)
        if will_retry:
            recorder.update(0, "retrying", "执行失败，已加入重试队列", "retrying")
        else:
            recorder.fail(str(exc))
    finally:
        redis_client.ack_job(message_id)


def process_once(block_ms: int = 0) -> int:
    """处理一批到期任务（重试 + 新消息），返回处理条数。供测试与单步调试。"""
    handled = 0
    for item in redis_client.pop_due_retries():
        redis_client.enqueue_job(item["job_id"], item["type"], item["payload"])
    for message_id, fields in redis_client.dequeue_jobs(block_ms=block_ms):
        _execute(message_id, fields)
        handled += 1
    return handled


def _run_loop() -> None:
    while not _STOP.is_set():
        try:
            process_once(block_ms=2000)
        except Exception as exc:  # Redis 抖动时退避后继续
            print(f"⚠️ Worker 循环异常，1s 后重试: {exc}")
            _STOP.wait(1.0)


def start_worker() -> None:
    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        return
    redis_client.ensure_group()
    _STOP.clear()
    _THREAD = threading.Thread(target=_run_loop, daemon=True, name="trip-planning-worker")
    _THREAD.start()
    print("✅ Redis 规划 Worker 已启动")


def stop_worker() -> None:
    global _THREAD
    _STOP.set()
    if _THREAD is not None:
        _THREAD.join(timeout=5)
        _THREAD = None


if __name__ == "__main__":  # 独立多实例运行入口
    start_worker()
    try:
        while _THREAD and _THREAD.is_alive():
            _THREAD.join(timeout=1)
    except KeyboardInterrupt:
        stop_worker()
