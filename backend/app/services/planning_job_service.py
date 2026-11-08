"""规划任务队列：内存模式（默认）与 Redis 模式（redis_enabled=true）双轨。

Redis 模式当前实现见 docs/HANDOVER.md：
- 提交：XADD trip:jobs + 状态写 trip:job:{id} Hash + 事件写 List；
- 执行：planning_worker 消费（见 planning_worker.py）；
- 读取：get/events_since 优先读 Redis，Hash 不存在时回退内存。
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Lock, RLock, Event
from typing import Any, Callable, Optional
from uuid import uuid4

from ..agents.trip_planner_agent import get_trip_planner_agent
from ..config import get_settings
from .planning_metrics_service import advance_stage, new_metrics, project_metrics
from ..graphs.planning_graph import run_planning_graph
from ..models.schemas import (
    AgentExecutionState,
    PlanningJobResponse,
    PlanningMetrics,
    TripPlan,
    TripRequest,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _elapsed_ms(start: datetime, end: datetime) -> int:
    return max(0, round((end - start).total_seconds() * 1000))


def merge_diagnostic(data,event):
    from .diagnostic_summary import summarize_event
    summarize_event(data, event)
    data.setdefault('events',[]).append(event)
    data['events']=data['events'][-100:]
    calls=data.setdefault('active_calls',{})
    key=event.get('call_id')
    if key and event['status']=='started':
        calls[key]=event
    elif key:
        calls.pop(key,None)
    service=event.get('service','runtime')
    counts=data.setdefault('services',{}).setdefault(service,{'started':0,'completed':0,'failed':0,'duration_ms':0,'hits':0,'misses':0})
    if event['status'] in ('started','completed','failed','cancelled','interrupted','retrying','degraded',
                            'waiting','timeout','rejected','acquired','fresh','stale','negative','unavailable'):
        counts[event['status']]=counts.get(event['status'],0)+1
    counts['duration_ms']+=event.get('duration_ms',0)
    if 'hit' in event:
        counts['hits' if event['hit'] else 'misses']+=1


class RedisJobRecorder:
    """Redis 模式的任务状态记录器，字段语义与内存模式完全一致。"""

    def __init__(self, job_id: str, lease_token: str = '') -> None:
        self.job_id = job_id
        self.lease_token = lease_token
        self.created_at = _now()
        self.started_at: Optional[datetime] = None
        self._agents_started: dict[str, datetime] = {}
        self._update_lock = RLock()

    # -- 生命周期 --------------------------------------------------------
    def init(self, job_type: str) -> None:
        from ..infrastructure import redis_client

        redis_client.set_job_state(
            self.job_id,
            job_type=job_type,
            status="queued",
            progress=0,
            stage="queued",
            message="任务已进入规划队列",
            error="",
            data="",
            agents={},
            metrics=new_metrics(self.created_at),
            created_at=self.created_at.isoformat(),
            updated_at=self.created_at.isoformat(),
        )
        self._append(0, "queued", "任务已进入规划队列", "queued")

    def mark_started(self, attempt: int = 0) -> None:
        from ..infrastructure import redis_client

        self.started_at = _now()
        state = redis_client.get_job_state(self.job_id) or {}
        self.created_at = datetime.fromisoformat(state["created_at"]) if state.get("created_at") else self.created_at
        metrics = json.loads(state.get("metrics") or "{}")
        metrics["retry_count"] = max(metrics.get("retry_count", 0), attempt)
        if attempt:
            metrics["agent_duration_ms"] = {}
            metrics["degraded_agents"] = []
            redis_client.set_job_state(self.job_id, agents={}, agent_started_at={})
        redis_client.set_job_state(
            self.job_id,
            started_at=state.get("started_at") or self.started_at.isoformat(),
            attempt=attempt,
            updated_at=self.started_at.isoformat(),
            metrics=metrics,
        )
        self.update(2, "starting", "正在准备地图工具与规划服务", "running")

    # -- 进度回调 --------------------------------------------------------
    def callback(self) -> Callable[..., None]:
        return lambda progress, stage, message, agent=None, agent_status=None: self.update(
            progress, stage, message, "running", agent, agent_status
        )

    def update(
        self, progress: int, stage: str, message: str, status: str,
        agent: Optional[str] = None, agent_status: Optional[str] = None,
    ) -> None:
        # All four graph branches use this recorder: merge their state under one lock.
        with self._update_lock:
            self._update_locked(progress, stage, message, status, agent, agent_status)

    def _update_locked(
        self,
        progress: int,
        stage: str,
        message: str,
        status: str,
        agent: Optional[str] = None,
        agent_status: Optional[str] = None,
    ) -> None:
        from ..infrastructure import redis_client

        state = redis_client.get_job_state(self.job_id) or {}
        now = _now()
        agents = json.loads(state.get("agents") or "{}")
        metrics = json.loads(state.get("metrics") or "{}")
        agent_started = {
            key: datetime.fromisoformat(value)
            for key, value in json.loads(state.get("agent_started_at") or "{}").items()
        }
        duration_ms = None
        advance_stage(metrics, stage, status, now, self.job_id, agent=agent)

        if agent and agent_status:
            if agent_status == "running":
                agent_started.setdefault(agent, now)
            else:
                started = agent_started.get(agent, now)
                duration_ms = _elapsed_ms(started, now)
                metrics.setdefault("agent_duration_ms", {})[agent] = duration_ms
            agents[agent] = {
                "status": agent_status,
                "message": message,
                "duration_ms": duration_ms,
            }

        if status in {"completed", "failed", "cancelled"}:
            created = datetime.fromisoformat(state.get("created_at")) if state.get("created_at") else now
            metrics["total_duration_ms"] = _elapsed_ms(created, now)
            print(f"[planning:{self.job_id}] {status} total={metrics['total_duration_ms']}ms stages={json.dumps(metrics.get('stage_duration_ms', {}))}")
            if not agent:
                duration_ms = metrics["total_duration_ms"]
            diagnostics=json.loads(state.get('diagnostics') or '{}')
            for call in list(diagnostics.get('active_calls',{}).values()):
                merge_diagnostic(diagnostics,{**call,'status':'cancelled' if status=='cancelled' else 'interrupted',
                    'timestamp':now.isoformat(),'duration_ms':_elapsed_ms(datetime.fromisoformat(call['timestamp']),now) if call.get('timestamp') else 0})
            redis_client.guarded_job_write(self.job_id,self.lease_token,{'diagnostics':diagnostics},
                                          allowed={'queued','running','retrying','waiting_input','cancelling'})
        metrics["degraded_agents"] = sorted(
            key for key, value in agents.items() if value["status"] == "degraded"
        )

        current_progress = int(state.get("progress") or 0)
        new_progress = max(current_progress, progress)
        allowed={'queued','running','retrying','waiting_input'}
        if status == 'cancelled':
            allowed.add('cancelling')
        written=redis_client.guarded_job_write(
            self.job_id, self.lease_token, dict(
            status=status,
            progress=new_progress,
            stage=stage,
            message=message,
            updated_at=now.isoformat(),
            agents=agents,
            metrics=metrics,
            agent_started_at={k: v.isoformat() for k, v in agent_started.items()},
        ),allowed=allowed)
        if written:
            self._append(new_progress, stage, message, status, agent, agent_status, duration_ms)

    def complete(self, result: TripPlan) -> None:
        from ..infrastructure import redis_client

        redis_client.guarded_job_write(self.job_id,self.lease_token,dict(data=result.model_dump_json(),waiting_input=None),allowed={'running'})
        self.update(100, "completed", "旅行计划已准备完成", "completed")

    def fail(self, error: str) -> None:
        from ..infrastructure import redis_client

        redis_client.guarded_job_write(self.job_id,self.lease_token,dict(error=error,waiting_input=None),allowed={'queued','running','retrying'})
        self.update(100, "failed", "规划失败，请稍后重试", "failed")

    def waiting_input(self, question: dict) -> None:
        """interrupt 落地：任务暂停等待用户确认，断点保留在 Checkpointer。"""
        from ..infrastructure import redis_client

        question = question or {}
        redis_client.guarded_job_write(self.job_id,self.lease_token,dict(waiting_input=question),allowed={'running'})
        self.update(
            int(question.get("progress") or 90),
            str(question.get("type") or "waiting_input"),
            question.get("question") or "任务等待用户确认",
            "waiting_input",
        )

    def cancel(self):
        from ..infrastructure import redis_client
        redis_client.guarded_job_write(self.job_id,self.lease_token,dict(data='',error='',waiting_input=None),
                                      allowed={'queued','running','retrying','waiting_input','cancelling'})
        self.update(0,'cancelled','任务已取消，后续步骤不会继续执行','cancelled')

    def diagnostic(self,event):
        from ..infrastructure import redis_client, task_trace
        with self._update_lock:
            state=redis_client.get_job_state(self.job_id) or {}
            diagnostics=json.loads(state.get('diagnostics') or '{}')
            merge_diagnostic(diagnostics,event)
            if redis_client.guarded_job_write(self.job_id,self.lease_token,{'diagnostics':diagnostics},allowed={'running','waiting_input'}):
                task_trace.log(event)

    # -- 事件与读取 ------------------------------------------------------
    def _append(
        self,
        progress: int,
        stage: str,
        message: str,
        status: str,
        agent: Optional[str] = None,
        agent_status: Optional[str] = None,
        duration_ms: Optional[int] = None,
    ) -> None:
        from ..infrastructure import redis_client

        # sequence 由 append_job_event 内部 INCR 原子生成（并行分支安全）
        event = {
            "sequence": 0,
            "progress": progress,
            "stage": stage,
            "message": message,
            "status": status,
            "timestamp": _now().isoformat(),
        }
        if agent:
            event["agent"] = agent
        if agent_status:
            event["agent_status"] = agent_status
        if duration_ms is not None:
            event["duration_ms"] = duration_ms
        redis_client.append_job_event(self.job_id, event)

    def response(self) -> PlanningJobResponse:
        from ..infrastructure import redis_client

        state = redis_client.get_job_state(self.job_id) or {}
        data = None
        if state.get("data") and state.get('status')=='completed':
            data = TripPlan.model_validate_json(state["data"])
        agents = json.loads(state.get("agents") or "{}")
        return PlanningJobResponse(
            job_id=self.job_id,
            trace_id=self.job_id,worker_id=state.get('worker_id',''),run_id=state.get('run_id',''),
            heartbeat_at=state.get('heartbeat_at') or None,recovery_count=int(state.get('recovery_count') or 0),
            diagnostics=json.loads(state.get('diagnostics') or '{}'),
            status=state.get("status", "queued"),
            progress=int(state.get("progress") or 0),
            stage=state.get("stage", "queued"),
            message=state.get("message", ""),
            data=data,
            error=state.get("error") or None,
            agents={
                key: AgentExecutionState.model_validate(value)
                for key, value in agents.items()
            },
            metrics=PlanningMetrics.model_validate(project_metrics(
                json.loads(state.get("metrics") or "{}"),
                state.get("created_at") or self.created_at,
                state.get("updated_at") or self.created_at,
                state.get("status", "queued"), agents, _now(),
            )),
            created_at=state.get("created_at"),
            started_at=state.get("started_at"),
            updated_at=state.get("updated_at"),
            waiting_input=json.loads(state["waiting_input"]) if state.get("waiting_input") else None,
        )


class PlanningJobService:
    """单工作线程保护共享 Agent/MCP 客户端，并保存短期任务状态。"""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = Lock()
        self._stopping = Event()
        self._pending = 0
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="trip-planner")

    def _use_redis(self) -> bool:
        if not get_settings().redis_enabled:
            return False
        from ..infrastructure import redis_client
        if not redis_client.redis_available():
            from fastapi import HTTPException
            raise HTTPException(503,'Redis规划队列不可用，请恢复队列后重试')
        return True

    def create_plan(self, request: TripRequest, job_id: str = '') -> PlanningJobResponse:
        # 阶段 C：规划编排已迁入 LangGraph（行为等价）；修订仍走 revise_trip，
        # RevisionGraph 在下一阶段接入。
        if self._use_redis():
            return self._create_redis_job(
                "plan", {"request": request.model_dump(mode="json")}, job_id=job_id
            )
        return self._create_job(
            lambda callback: run_planning_graph(request, callback), job_id=job_id,
            spec={'type':'plan','payload':{'request':request.model_dump(mode='json')}}
        )

    def create_revision(
        self,
        request: TripRequest,
        plan: TripPlan,
        instruction: str,
        base_version: Optional[int] = None,
        job_id: str = '',
    ) -> PlanningJobResponse:
        # 阶段1批1：修订走 RevisionGraph（LLM 翻译 PlanPatch + 确定性应用），
        # 解析失败/超范围时图内部回退 legacy revise_trip，E2E 不回退。
        from ..graphs.revision_graph import run_revision_graph  # 局部导入避免环

        if self._use_redis():
            return self._create_redis_job(
                "revision",
                {
                    "request": request.model_dump(mode="json"),
                    "plan": plan.model_dump_json(),
                    "instruction": instruction,
                    "base_version": base_version,
                }, job_id=job_id,
            )
        return self._create_job(
            lambda callback: run_revision_graph(
                request, plan, instruction, callback, base_version=base_version
            ),job_id=job_id,spec={'type':'revision','payload':{'request':request.model_dump(mode='json'),
                'plan':plan.model_dump_json(),'instruction':instruction,'base_version':base_version}}
        )

    def _create_redis_job(self, job_type: str, payload: dict[str, Any], job_id: str = '') -> PlanningJobResponse:
        from ..infrastructure import redis_client

        job_id = job_id or uuid4().hex
        recorder = RedisJobRecorder(job_id)
        now=_now().isoformat()
        created=redis_client.publish_job(job_id,job_type,payload,dict(job_type=job_type,status='queued',progress=0,
            stage='queued',message='任务已进入规划队列',error='',data='',agents={},metrics=new_metrics(recorder.created_at),
            created_at=now,updated_at=now,request=payload.get('request'),diagnostics={}))
        if created:
            recorder._append(0,'queued','任务已进入规划队列','queued')
        return recorder.response()

    def get_stored_request(self, job_id: str) -> TripRequest:
        """读取创建任务时保存的原始请求（Redis 模式专用，供 resume 使用）。"""
        from ..infrastructure import redis_client

        state = redis_client.get_job_state(job_id)
        if not state or not state.get("request"):
            raise KeyError(job_id)
        return TripRequest.model_validate_json(state["request"],context={'from_storage':True})

    def mark_resumed(self, job_id: str) -> None:
        """用户已提交确认：清除 waiting_input 载荷，任务回到队列等 Worker 恢复。"""
        from ..infrastructure import redis_client

        redis_client.set_job_state(
            job_id,
            waiting_input=None,
        )
        RedisJobRecorder(job_id).update(0, "queued", "已收到用户确认，任务重新入队", "queued")

    def _create_job(
        self,
        operation: Callable[[Callable[[int, str, str], None]], TripPlan],
        job_id: str = '', spec: dict | None = None,
    ) -> PlanningJobResponse:
        self._cleanup_expired()
        job_id = job_id or uuid4().hex
        now = datetime.now(timezone.utc)
        job = {
            "job_id": job_id,
            "status": "queued",
            "progress": 0,
            "stage": "queued",
            "message": "任务已进入规划队列",
            "data": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "events": [],
            "agents": {},
            "agent_started_at": {},
            "metrics": new_metrics(now),
            "diagnostics":{},'run_id':uuid4().hex,
        }
        with self._lock:
            created=job_id not in self._jobs
            if created:
                from .job_admission import check_capacity, submission_owner, TERMINAL
                owner=submission_owner.get()
                active=[item.get('owner_id','internal') for item in self._jobs.values() if item['status'] not in TERMINAL]
                check_capacity(active, owner)
                if self._pending >= get_settings().job_queue_capacity:
                    from fastapi import HTTPException
                    raise HTTPException(503,'规划队列已满，请稍后重试',headers={'Retry-After':'5'})
                job['owner_id']=owner
                self._pending += 1
                self._jobs[job_id] = job
                self._append_event(job, 0, "queued", "任务已进入规划队列", "queued")
        if created:
            try:
                self._executor.submit(self._run, job_id, operation, spec)
            except Exception:
                with self._lock:
                    self._pending -= 1
                    self._jobs.pop(job_id, None)
                raise
        return self.get(job_id)

    def _run(
        self,
        job_id: str,
        operation: Callable[[Callable[[int, str, str], None]], TripPlan],
        spec: dict | None = None,
    ) -> None:
        with self._lock:
            self._pending -= 1
            job = self._jobs[job_id]
            if job['status']=='cancelled':
                return
            if (_now()-job['created_at']).total_seconds() >= get_settings().job_max_queue_wait_seconds:
                job.update(status='failed',stage='expired',error='任务等待过久，请重新提交')
                return
            started_at = datetime.now(timezone.utc)
            job["started_at"] = started_at
            job["metrics"]["queue_wait_ms"] = self._elapsed_ms(
                job["created_at"], started_at
            )
        self._update(job_id, 2, "starting", "正在准备地图工具与规划服务", "running")
        try:
            callback=lambda progress, stage, message, agent=None, agent_status=None: self._update(
                    job_id, progress, stage, message, "running", agent, agent_status
                )
            if spec:
                from .job_process import isolated_executor
                value=isolated_executor.run({**spec,'job_id':job_id,'run_id':job['run_id']},callback,
                    lambda event:self._diagnostic(job_id,event),lambda:self._cancelled(job_id),stopping=self._stopping.is_set)
                result=TripPlan.model_validate(value)
            else:
                result=operation(callback)
            with self._lock:
                job = self._jobs[job_id]
                cancelled=job['status'] in {'cancelling','cancelled'}
                if not cancelled:
                    job["data"] = result
            if cancelled:
                self._update(job_id,0,'cancelled','任务已取消，后续步骤不会继续执行','cancelled')
            else:
                self._update(job_id, 100, "completed", "旅行计划已准备完成", "completed")
        except Exception as exc:
            from .job_process import JobCancelled
            if isinstance(exc,JobCancelled) or self._cancelled(job_id):
                self._update(job_id,0,'cancelled','任务已取消，后续步骤不会继续执行','cancelled')
                return
            with self._lock:
                self._jobs[job_id]["error"] = f'执行中断（{type(exc).__name__}），请重试；任务编号 {job_id}'
            self._update(job_id, 100, "failed", "规划失败，请稍后重试", "failed")

    def _update(
        self,
        job_id: str,
        progress: int,
        stage: str,
        message: str,
        status: str,
        agent: str | None = None,
        agent_status: str | None = None,
    ) -> None:
        with self._lock:
            job = self._jobs[job_id]
            if job['status'] in {'completed','failed','cancelled'}:
                return
            if job['status']=='cancelling' and status!='cancelled':
                return
            now = datetime.now(timezone.utc)
            duration_ms = None
            advance_stage(job["metrics"], stage, status, now, job_id, agent=agent)
            if agent and agent_status:
                if agent_status == "running":
                    job["agent_started_at"].setdefault(agent, now)
                else:
                    started_at = job["agent_started_at"].get(agent, now)
                    duration_ms = self._elapsed_ms(started_at, now)
                    job["metrics"]["agent_duration_ms"][agent] = duration_ms
                job["agents"][agent] = {
                    "status": agent_status,
                    "message": message,
                    "duration_ms": duration_ms,
                }

            if status in {"completed", "failed", "cancelled"}:
                job["metrics"]["total_duration_ms"] = self._elapsed_ms(
                    job["created_at"], now
                )
                print(f"[planning:{job_id}] {status} total={job['metrics']['total_duration_ms']}ms stages={json.dumps(job['metrics'].get('stage_duration_ms', {}))}")
                if not agent:
                    duration_ms = job["metrics"]["total_duration_ms"]
                for call in list(job.get('diagnostics',{}).get('active_calls',{}).values()):
                    merge_diagnostic(job['diagnostics'],{**call,'status':'cancelled' if status=='cancelled' else 'interrupted',
                        'timestamp':now.isoformat(),'duration_ms':_elapsed_ms(datetime.fromisoformat(call['timestamp']),now) if call.get('timestamp') else 0})
            job["metrics"]["degraded_agents"] = sorted(
                key for key, value in job["agents"].items()
                if value["status"] == "degraded"
            )
            job.update({
                "status": status,
                "progress": max(job["progress"], progress),
                "stage": stage,
                "message": message,
                "updated_at": now,
            })
            self._append_event(
                job, job["progress"], stage, message, status,
                agent, agent_status, duration_ms,
            )

    @staticmethod
    def _append_event(
        job: dict[str, Any],
        progress: int,
        stage: str,
        message: str,
        status: str,
        agent: str | None = None,
        agent_status: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        event = {
            "sequence": len(job["events"]),
            "progress": progress,
            "stage": stage,
            "message": message,
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if agent:
            event["agent"] = agent
        if agent_status:
            event["agent_status"] = agent_status
        if duration_ms is not None:
            event["duration_ms"] = duration_ms
        job["events"].append(event)

    @staticmethod
    def _elapsed_ms(start: datetime, end: datetime) -> int:
        return max(0, round((end - start).total_seconds() * 1000))

    def get(self, job_id: str) -> PlanningJobResponse:
        if get_settings().redis_enabled:
            from ..infrastructure import redis_client

            if redis_client.get_job_state(job_id):
                return RedisJobRecorder(job_id).response()
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            return PlanningJobResponse(
                job_id=job_id,
                trace_id=job_id,run_id=job.get('run_id',''),diagnostics=job.get('diagnostics',{}),
                status=job["status"],
                progress=job["progress"],
                stage=job["stage"],
                message=job["message"],
                data=job["data"],
                error=job["error"],
                agents={
                    key: AgentExecutionState.model_validate(value)
                    for key, value in job["agents"].items()
                },
                metrics=PlanningMetrics.model_validate(project_metrics(
                    job["metrics"], job["created_at"], job["updated_at"], job["status"], job["agents"], _now(),
                )),
                created_at=job["created_at"].isoformat(),
                started_at=job["started_at"].isoformat() if job.get("started_at") else None,
                updated_at=job["updated_at"].isoformat(),
            )

    def events_since(self, job_id: str, sequence: int) -> list[dict[str, Any]]:
        if get_settings().redis_enabled:
            from ..infrastructure import redis_client

            if redis_client.get_job_state(job_id):
                return redis_client.read_job_events(job_id, sequence)
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            return [dict(event) for event in job["events"] if event["sequence"] >= sequence]

    def _cleanup_expired(self) -> None:
        threshold = datetime.now(timezone.utc) - timedelta(hours=1)
        with self._lock:
            expired = [
                job_id
                for job_id, job in self._jobs.items()
                if job["updated_at"] < threshold
                and job["status"] in {"completed", "failed", "cancelled"}
            ]
            for job_id in expired:
                self._jobs.pop(job_id, None)

    def shutdown(self) -> None:
        """应用退出时停止接收新任务并取消尚未执行的任务。"""
        self._stopping.set()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _cancelled(self,job_id):
        with self._lock:
            return self._jobs[job_id]['status'] in {'cancelling','cancelled'}

    def _diagnostic(self,job_id,event):
        from ..infrastructure import task_trace
        with self._lock:
            merge_diagnostic(self._jobs[job_id]['diagnostics'],event)
        task_trace.log(event)

    def cancel(self,job_id):
        if get_settings().redis_enabled:
            from ..infrastructure import redis_client
            state=redis_client.get_job_state(job_id)
            if state:
                status=state.get('status')
                if status not in {'completed','failed','cancelled','cancelling'}:
                    redis_client.transition_job(job_id,{'queued','running','retrying','waiting_input'},
                        status='cancelling',stage='cancelling',message='正在停止任务执行',waiting_input='',updated_at=_now().isoformat())
                    if not redis_client._get_client().exists(f'trip:job:{job_id}:lease'):
                        RedisJobRecorder(job_id).cancel()
                return RedisJobRecorder(job_id).response()
        with self._lock:
            job=self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job['status'] not in {'completed','failed','cancelled'}:
                job.update(status='cancelled' if job['status']=='queued' else 'cancelling',data=None,
                           stage='cancelled' if job['status']=='queued' else 'cancelling',message='已请求取消任务')
                self._append_event(job,job['progress'],job['stage'],job['message'],job['status'])
        return self.get(job_id)


planning_job_service = PlanningJobService()
