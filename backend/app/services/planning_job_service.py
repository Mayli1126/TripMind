"""规划任务队列：内存模式（默认）与 Redis 模式（redis_enabled=true）双轨。

Redis 模式对应方案 docs/langgraph-migration-plan.md 第八节：
- 提交：XADD trip:jobs + 状态写 trip:job:{id} Hash + 事件写 List；
- 执行：planning_worker 消费（见 planning_worker.py）；
- 读取：get/events_since 优先读 Redis，Hash 不存在时回退内存。
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, Callable, Optional
from uuid import uuid4

from ..agents.trip_planner_agent import get_trip_planner_agent
from ..config import get_settings
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


class RedisJobRecorder:
    """Redis 模式的任务状态记录器，字段语义与内存模式完全一致。"""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.created_at = _now()
        self.started_at: Optional[datetime] = None
        self._agents_started: dict[str, datetime] = {}

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
            metrics={
                "queue_wait_ms": 0,
                "total_duration_ms": None,
                "agent_duration_ms": {},
                "degraded_agents": [],
            },
            created_at=self.created_at.isoformat(),
            updated_at=self.created_at.isoformat(),
        )
        self._append(0, "queued", "任务已进入规划队列", "queued")

    def mark_started(self) -> None:
        from ..infrastructure import redis_client

        self.started_at = _now()
        queue_wait = _elapsed_ms(self.created_at, self.started_at) if self.created_at else 0
        state = redis_client.get_job_state(self.job_id) or {}
        metrics = json.loads(state.get("metrics") or "{}")
        metrics["queue_wait_ms"] = queue_wait
        redis_client.set_job_state(
            self.job_id,
            started_at=self.started_at.isoformat(),
            updated_at=self.started_at.isoformat(),
            metrics=metrics,
        )
        self.update(2, "starting", "Agent 正在启动", "running")

    # -- 进度回调 --------------------------------------------------------
    def callback(self) -> Callable[..., None]:
        return lambda progress, stage, message, agent=None, agent_status=None: self.update(
            progress, stage, message, "running", agent, agent_status
        )

    def update(
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

        if status in {"completed", "failed"}:
            created = datetime.fromisoformat(state.get("created_at")) if state.get("created_at") else now
            metrics["total_duration_ms"] = _elapsed_ms(created, now)
            if not agent:
                duration_ms = metrics["total_duration_ms"]
        metrics["degraded_agents"] = sorted(
            key for key, value in agents.items() if value["status"] == "degraded"
        )

        current_progress = int(state.get("progress") or 0)
        new_progress = max(current_progress, progress)
        redis_client.set_job_state(
            self.job_id,
            status=status,
            progress=new_progress,
            stage=stage,
            message=message,
            updated_at=now.isoformat(),
            agents=agents,
            metrics=metrics,
            agent_started_at={k: v.isoformat() for k, v in agent_started.items()},
        )
        self._append(new_progress, stage, message, status, agent, agent_status, duration_ms)

    def complete(self, result: TripPlan) -> None:
        from ..infrastructure import redis_client

        redis_client.set_job_state(self.job_id, data=result.model_dump_json(), waiting_input=None)
        self.update(100, "completed", "旅行计划已准备完成", "completed")

    def fail(self, error: str) -> None:
        from ..infrastructure import redis_client

        redis_client.set_job_state(self.job_id, error=error, waiting_input=None)
        self.update(100, "failed", "规划失败，请稍后重试", "failed")

    def waiting_input(self, question: dict) -> None:
        """interrupt 落地：任务暂停等待用户确认，断点保留在 Checkpointer。"""
        from ..infrastructure import redis_client

        question = question or {}
        redis_client.set_job_state(self.job_id, waiting_input=question)
        self.update(
            int(question.get("progress") or 90),
            str(question.get("type") or "waiting_input"),
            question.get("question") or "任务等待用户确认",
            "waiting_input",
        )

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
        if state.get("data"):
            data = TripPlan.model_validate_json(state["data"])
        agents = json.loads(state.get("agents") or "{}")
        return PlanningJobResponse(
            job_id=self.job_id,
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
            metrics=PlanningMetrics.model_validate(json.loads(state.get("metrics") or "{}")),
            waiting_input=json.loads(state["waiting_input"]) if state.get("waiting_input") else None,
        )


class PlanningJobService:
    """单工作线程保护共享 Agent/MCP 客户端，并保存短期任务状态。"""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="trip-planner")

    def _use_redis(self) -> bool:
        if not get_settings().redis_enabled:
            return False
        from ..infrastructure import redis_client
        return redis_client.redis_available()

    def create_plan(self, request: TripRequest) -> PlanningJobResponse:
        # 阶段 C：规划编排已迁入 LangGraph（行为等价）；修订仍走 revise_trip，
        # RevisionGraph 在下一阶段接入。
        if self._use_redis():
            return self._create_redis_job(
                "plan", {"request": request.model_dump(mode="json")}
            )
        return self._create_job(
            lambda callback: run_planning_graph(request, callback)
        )

    def create_revision(
        self,
        request: TripRequest,
        plan: TripPlan,
        instruction: str,
        base_version: Optional[int] = None,
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
                },
            )
        return self._create_job(
            lambda callback: run_revision_graph(
                request, plan, instruction, callback, base_version=base_version
            )
        )

    def _create_redis_job(self, job_type: str, payload: dict[str, Any]) -> PlanningJobResponse:
        from ..infrastructure import redis_client

        job_id = uuid4().hex
        recorder = RedisJobRecorder(job_id)
        recorder.init(job_type)
        # 原始请求存入状态 Hash，供 resume 接口重建任务载荷（Stream 消息已消费）
        redis_client.set_job_state(job_id, request=payload.get("request"))
        redis_client.enqueue_job(job_id, job_type, payload)
        return recorder.response()

    def get_stored_request(self, job_id: str) -> TripRequest:
        """读取创建任务时保存的原始请求（Redis 模式专用，供 resume 使用）。"""
        from ..infrastructure import redis_client

        state = redis_client.get_job_state(job_id)
        if not state or not state.get("request"):
            raise KeyError(job_id)
        return TripRequest.model_validate_json(state["request"])

    def mark_resumed(self, job_id: str) -> None:
        """用户已提交确认：清除 waiting_input 载荷，任务回到队列等 Worker 恢复。"""
        from ..infrastructure import redis_client

        redis_client.set_job_state(
            job_id,
            waiting_input=None,
            status="queued",
            stage="queued",
            message="已收到用户确认，任务重新入队",
        )
        redis_client.append_job_event(
            job_id,
            {
                "sequence": 0,
                "progress": 0,
                "stage": "queued",
                "message": "已收到用户确认，任务重新入队",
                "status": "queued",
                "timestamp": _now().isoformat(),
            },
        )

    def _create_job(
        self,
        operation: Callable[[Callable[[int, str, str], None]], TripPlan],
    ) -> PlanningJobResponse:
        self._cleanup_expired()
        job_id = uuid4().hex
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
            "metrics": {
                "queue_wait_ms": 0,
                "total_duration_ms": None,
                "agent_duration_ms": {},
                "degraded_agents": [],
            },
        }
        with self._lock:
            self._jobs[job_id] = job
            self._append_event(job, 0, "queued", "任务已进入规划队列", "queued")
        self._executor.submit(self._run, job_id, operation)
        return self.get(job_id)

    def _run(
        self,
        job_id: str,
        operation: Callable[[Callable[[int, str, str], None]], TripPlan],
    ) -> None:
        with self._lock:
            job = self._jobs[job_id]
            started_at = datetime.now(timezone.utc)
            job["started_at"] = started_at
            job["metrics"]["queue_wait_ms"] = self._elapsed_ms(
                job["created_at"], started_at
            )
        self._update(job_id, 2, "starting", "Agent 正在启动", "running")
        try:
            result = operation(
                lambda progress, stage, message, agent=None, agent_status=None: self._update(
                    job_id, progress, stage, message, "running", agent, agent_status
                )
            )
            with self._lock:
                job = self._jobs[job_id]
                job["data"] = result
            self._update(job_id, 100, "completed", "旅行计划已准备完成", "completed")
        except Exception as exc:
            with self._lock:
                self._jobs[job_id]["error"] = str(exc)
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
            now = datetime.now(timezone.utc)
            duration_ms = None
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

            if status in {"completed", "failed"}:
                job["metrics"]["total_duration_ms"] = self._elapsed_ms(
                    job["created_at"], now
                )
                if not agent:
                    duration_ms = job["metrics"]["total_duration_ms"]
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
                metrics=PlanningMetrics.model_validate(job["metrics"]),
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
                and job["status"] in {"completed", "failed"}
            ]
            for job_id in expired:
                self._jobs.pop(job_id, None)

    def shutdown(self) -> None:
        """应用退出时停止接收新任务并取消尚未执行的任务。"""
        self._executor.shutdown(wait=False, cancel_futures=True)


planning_job_service = PlanningJobService()
