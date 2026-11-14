"""规划用例入口：统一幂等提交、运行检查、配额和用户记忆。"""

from typing import Callable

from ..models.schemas import PlanningJobResponse, TripRequest, TripRevisionRequest
from .conversation_store import memory_context
from .conversation_summary_service import summary_for
from .job_submission_service import submit_once
from .planning_job_service import planning_job_service
from .quota_service import enforce_planning_quota
from .runtime_service import require_planning_runtime


def _prepare_request(raw_request, request: TripRequest, *, include_summary: bool = False) -> None:
    """只信任服务端账号记忆，不接受客户端伪造的偏好上下文。"""
    owner_id = raw_request.state.user["id"]
    require_planning_runtime()
    enforce_planning_quota(owner_id)
    request.memory_context = memory_context(owner_id, request.city)
    request.conversation_context = (
        summary_for(owner_id, request.conversation_id, "planner")[0][:4200]
        if include_summary else ""
    )


def submit_plan(raw_request, request: TripRequest) -> PlanningJobResponse:
    """普通任务与旧同步接口走同一条规划提交链。"""
    payload = request.model_dump(mode="json", exclude={"memory_context", "conversation_context"})

    def create(job_id):
        prepared = request.model_copy(deep=True)
        _prepare_request(raw_request, prepared, include_summary=True)
        return planning_job_service.create_plan(prepared, job_id=job_id)

    return submit_once(raw_request, "plan", payload, create)


def submit_revision(raw_request, request: TripRevisionRequest) -> PlanningJobResponse:
    """独立修改接口沿用任务服务的版本语义。"""
    payload = request.model_dump(mode="json")
    payload["trip_request"].pop("memory_context", None)
    payload["trip_request"].pop("conversation_context", None)

    def create(job_id):
        prepared = request.trip_request.model_copy(deep=True)
        _prepare_request(raw_request, prepared)
        return planning_job_service.create_revision(
            prepared, request.plan, request.instruction,
            base_version=request.base_version, job_id=job_id,
        )

    return submit_once(raw_request, "revision", payload, create)


def submit_saved_revision(raw_request, trip_id: str, payload, load_trip: Callable) -> PlanningJobResponse:
    """已保存行程在真正创建任务前再次检查版本，避免提交过期修改。"""
    load_trip(trip_id, payload.base_version)

    def create(job_id):
        trip = load_trip(trip_id, payload.base_version)
        prepared = trip.request.model_copy(deep=True)
        _prepare_request(raw_request, prepared)
        return planning_job_service.create_revision(
            prepared, trip.plan, payload.instruction,
            base_version=trip.current_version, job_id=job_id,
        )

    return submit_once(
        raw_request, "assistant_revision",
        {"trip_id": trip_id, **payload.model_dump(mode="json")}, create,
    )
