"""Personal trip library API."""

from fastapi import APIRouter, HTTPException, Response, status

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

router = APIRouter(prefix="/library", tags=["个人行程库"])


@router.post("/trips", response_model=SavedTripDetail, status_code=status.HTTP_201_CREATED)
def create_trip(payload: SavedTripCreate):
    return trip_store_service.create_trip(payload)


@router.get("/trips", response_model=list[SavedTripSummary])
def list_trips():
    return trip_store_service.list_trips()


@router.get("/trips/{trip_id}", response_model=SavedTripDetail)
def get_trip(trip_id: str):
    try:
        return trip_store_service.get_trip(trip_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="行程不存在") from exc


@router.post("/trips/{trip_id}/versions", response_model=SavedTripDetail)
def add_version(trip_id: str, payload: SavedTripUpdate):
    try:
        return trip_store_service.add_version(trip_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="行程不存在") from exc


@router.post("/trips/{trip_id}/versions/{version}/restore", response_model=SavedTripDetail)
def restore_version(trip_id: str, version: int):
    try:
        return trip_store_service.restore_version(trip_id, version)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="行程或历史版本不存在") from exc


@router.delete("/trips/{trip_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_trip(trip_id: str):
    try:
        trip_store_service.delete_trip(trip_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="行程不存在") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/preferences", response_model=UserPreferences)
def get_preferences():
    return trip_store_service.get_preferences()


@router.put("/preferences", response_model=UserPreferences)
def save_preferences(preferences: UserPreferences):
    return trip_store_service.save_preferences(preferences)


@router.get("/trips/{trip_id}/progress", response_model=TripProgressResponse)
def get_trip_progress(trip_id: str):
    try:
        return trip_store_service.get_progress(trip_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="行程不存在") from exc


@router.put("/trips/{trip_id}/progress/{item_key}", response_model=TripProgressResponse)
def update_trip_progress(trip_id: str, item_key: str, payload: TripProgressUpdate):
    try:
        return trip_store_service.set_progress(trip_id, item_key, payload.completed)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="行程不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
