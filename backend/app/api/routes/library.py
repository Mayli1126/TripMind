"""Personal trip library API."""

from fastapi import APIRouter, HTTPException, Request, Response, status
from ..auth import current_user
from ...services.auth_service import auth_service

from ...models.schemas import (
    SavedTripCreate,
    SavedTripDetail,
    SavedTripSummary,
    SavedTripUpdate,
    TripProgressResponse,
    TripProgressUpdate,
    UserPreferences,
)
from ...services.trip_store_service import trip_store_service
from ...models.trip_assistant import AssistantRequest, AssistantResponse, AssistantRevisionRequest
from ...models.schemas import PlanningJobResponse
from ...services.trip_assistant_service import answer_trip

router = APIRouter(prefix="/library", tags=["个人行程库"])


@router.post("/trips", response_model=SavedTripDetail, status_code=status.HTTP_201_CREATED)
def create_trip(payload: SavedTripCreate, request: Request):
    return trip_store_service.create_trip(payload, owner_id=current_user(request)["id"])


@router.get("/trips", response_model=list[SavedTripSummary])
def list_trips(request: Request):
    return trip_store_service.list_trips(owner_id=current_user(request)["id"])


@router.get("/trips/{trip_id}", response_model=SavedTripDetail)
def get_trip(trip_id: str):
    try:
        return trip_store_service.get_trip(trip_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="行程不存在") from exc


@router.post("/trips/{trip_id}/versions", response_model=SavedTripDetail)
def add_version(trip_id: str, payload: SavedTripUpdate):
    if payload.base_version is None:
        raise HTTPException(428, '缺少行程基础版本，请刷新页面后重试')
    try:
        return trip_store_service.add_version(trip_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="行程不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _assistant_trip(trip_id: str, base_version: int):
    try:
        trip = trip_store_service.get_trip(trip_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="行程不存在") from exc
    if trip.current_version != base_version:
        raise HTTPException(status_code=409, detail="行程已更新，请刷新对话并基于最新版本重新提问")
    return trip


@router.post("/trips/{trip_id}/assistant/messages", response_model=AssistantResponse)
def assistant_message(trip_id: str, payload: AssistantRequest, request: Request):
    from ...config import get_settings
    settings = get_settings()
    trip = _assistant_trip(trip_id, payload.base_version)
    if settings.redis_enabled and settings.chat_quota_per_day > 0:
        from ...infrastructure import redis_client
        allowed, _ = redis_client.quota_check_and_incr(current_user(request)["id"], settings.chat_quota_per_day, scope="chat")
        if not allowed:
            raise HTTPException(status_code=429, detail="今日对话次数已达上限")
    from ...services.conversation_summary_service import summary_for
    from ...services.conversation_store import memory_context
    summary_context, summary = summary_for(current_user(request)['id'], payload.conversation_id, 'trip', trip_id)
    try:
        result = answer_trip(trip, payload, summary_context, memory_context(current_user(request)['id'], trip.city))
        result.conversation_summary = summary
    except Exception as exc:
        raise HTTPException(status_code=502, detail="暂时未能回答，请稍后重试；行程没有变化") from exc
    _assistant_trip(trip_id, payload.base_version)
    return result


@router.post("/trips/{trip_id}/assistant/revision-jobs", response_model=PlanningJobResponse, status_code=202)
def assistant_revision(trip_id: str, payload: AssistantRevisionRequest, request: Request):
    from ...services.runtime_service import require_planning_runtime
    from ...services.planning_job_service import planning_job_service
    from .trip import _enforce_quota
    trip = _assistant_trip(trip_id, payload.base_version)
    from ...services.job_submission_service import submit_once
    def create(job_id):
        require_planning_runtime(); _enforce_quota(request)
        from ...services.conversation_store import memory_context
        trip.request.memory_context=memory_context(current_user(request)['id'],trip.city)
        return planning_job_service.create_revision(trip.request,trip.plan,payload.instruction,
                                                   base_version=trip.current_version,job_id=job_id)
    return submit_once(request,'assistant_revision',{'trip_id':trip_id,**payload.model_dump(mode='json')},create)


@router.post("/trips/{trip_id}/versions/{version}/restore", response_model=SavedTripDetail)
def restore_version(trip_id: str, version: int, base_version: int):
    try:
        return trip_store_service.restore_version(trip_id, version, base_version)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="行程或历史版本不存在") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post('/trips/{trip_id}/refresh-advisories')
def refresh_advisories(trip_id: str, request: Request):
    from ...services.advisory_refresh_service import refresh_trip
    try:
        trip = trip_store_service.get_trip(trip_id)
    except KeyError as exc:
        raise HTTPException(404, '行程不存在') from exc
    from .trip import _enforce_quota
    _enforce_quota(request)
    result = refresh_trip(trip)
    return {**result, 'trip': trip_store_service.get_trip(trip_id)}


@router.delete("/trips/{trip_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_trip(trip_id: str):
    try:
        trip_store_service.delete_trip(trip_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="行程不存在") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/preferences", response_model=UserPreferences)
def get_preferences(request: Request):
    return auth_service.preferences(current_user(request)["id"])


@router.put("/preferences", response_model=UserPreferences)
def save_preferences(preferences: UserPreferences, request: Request):
    try:
        return auth_service.save_preferences(current_user(request)["id"], preferences)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/trips/{trip_id}/progress", response_model=TripProgressResponse)
def get_trip_progress(trip_id: str):
    try:
        return trip_store_service.get_progress(trip_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="行程不存在") from exc


@router.put("/trips/{trip_id}/progress/{item_key}", response_model=TripProgressResponse)
def update_trip_progress(trip_id: str, item_key: str, payload: TripProgressUpdate):
    if payload.base_version is None or 'expected_revision' not in payload.model_fields_set:
        raise HTTPException(428, '缺少状态版本，请刷新页面后重试')
    try:
        return trip_store_service.set_progress(trip_id, item_key, payload.completed,
                                               payload.expected_revision, payload.base_version)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="行程不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
