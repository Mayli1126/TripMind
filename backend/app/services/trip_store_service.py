"""SQLite-backed personal trip library and preference store."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4

from ..models.schemas import (
    SavedTripCreate,
    SavedTripDetail,
    SavedTripSummary,
    SavedTripUpdate,
    TripPlan,
    TripRequest,
    TripVersionSummary,
    TripProgressItem,
    TripProgressResponse,
    UserPreferences,
)
from .itinerary_schedule_service import itinerary_schedule_service
from .trip_quality_service import trip_quality_service


class TripStoreService:
    """Persist trips locally without requiring an external database service."""

    def __init__(self, database_path: str | Path | None = None) -> None:
        default_path = Path(__file__).resolve().parents[2] / "data" / "tripmind.db"
        self.database_path = Path(database_path or default_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self):
        """提交或回滚事务，并确保 SQLite 文件句柄及时释放。"""
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS trips (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    current_version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trip_versions (
                    trip_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    plan_json TEXT NOT NULL,
                    change_note TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (trip_id, version),
                    FOREIGN KEY (trip_id) REFERENCES trips(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS user_preferences (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trip_progress (
                    trip_id TEXT NOT NULL,
                    item_key TEXT NOT NULL,
                    completed INTEGER NOT NULL DEFAULT 0,
                    completed_at TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (trip_id, item_key),
                    FOREIGN KEY (trip_id) REFERENCES trips(id) ON DELETE CASCADE
                );
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def create_trip(self, payload: SavedTripCreate) -> SavedTripDetail:
        trip_id = uuid4().hex
        now = self._now()
        title = (payload.title or f"{payload.plan.city} · {payload.plan.start_date}").strip()
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT INTO trips VALUES (?, ?, ?, 1, ?, ?)",
                (trip_id, title, payload.request.model_dump_json(), now, now),
            )
            connection.execute(
                "INSERT INTO trip_versions VALUES (?, 1, ?, ?, ?)",
                (trip_id, payload.plan.model_dump_json(), "AI 生成初始行程", now),
            )
        return self.get_trip(trip_id)

    def list_trips(self) -> list[SavedTripSummary]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT t.*, v.plan_json FROM trips t
                JOIN trip_versions v ON v.trip_id=t.id AND v.version=t.current_version
                ORDER BY t.updated_at DESC"""
            ).fetchall()
        return [self._summary(row) for row in rows]

    def get_trip(self, trip_id: str) -> SavedTripDetail:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT t.*, v.plan_json FROM trips t
                JOIN trip_versions v ON v.trip_id=t.id AND v.version=t.current_version
                WHERE t.id=?""",
                (trip_id,),
            ).fetchone()
            if row is None:
                raise KeyError(trip_id)
            version_rows = connection.execute(
                "SELECT version, change_note, created_at FROM trip_versions WHERE trip_id=? ORDER BY version DESC",
                (trip_id,),
            ).fetchall()
        summary = self._summary(row)
        plan = TripPlan.model_validate_json(row["plan_json"])
        duplicate_warnings = trip_quality_service.remove_duplicate_attractions(plan)
        if duplicate_warnings:
            plan.warnings = [
                warning for warning in plan.warnings
                if "在多天行程中重复出现" not in warning
            ]
            plan.warnings.extend(duplicate_warnings)
        if any(not day.schedule for day in plan.days):
            plan = itinerary_schedule_service.build(plan)
        # 使用model_construct跳过验证，避免历史行程日期过期导致验证失败
        import json
        request_data = json.loads(row["request_json"])
        request = TripRequest.model_construct(**request_data)
        return SavedTripDetail.model_construct(
            **summary.model_dump(),
            request=request,
            plan=plan,
            versions=[TripVersionSummary(**dict(item)) for item in version_rows],
        )

    def add_version(self, trip_id: str, payload: SavedTripUpdate) -> SavedTripDetail:
        now = self._now()
        updated_plan = payload.plan.model_copy(deep=True)
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """SELECT t.current_version, t.request_json, v.plan_json
                FROM trips t JOIN trip_versions v
                  ON v.trip_id=t.id AND v.version=t.current_version
                WHERE t.id=?""", (trip_id,)
            ).fetchone()
            if row is None:
                raise KeyError(trip_id)
            # 使用model_construct跳过验证，避免历史行程日期过期导致验证失败
            import json
            request_data = json.loads(row["request_json"])
            request = TripRequest.model_construct(**request_data)
            current_plan = TripPlan.model_validate_json(row["plan_json"])
            journey_count = 2 if request.trip_type == "round_trip" else 1
            intercity_per_person = 0
            if current_plan.budget and request.travelers and journey_count:
                intercity_per_person = round(
                    current_plan.budget.intercity_transportation
                    / request.travelers / journey_count
                )
            updated_plan = trip_quality_service.finalize(
                updated_plan,
                request,
                intercity_cost_per_person=intercity_per_person,
            )
            version = int(row["current_version"]) + 1
            connection.execute(
                "INSERT INTO trip_versions VALUES (?, ?, ?, ?, ?)",
                (trip_id, version, updated_plan.model_dump_json(), payload.change_note, now),
            )
            connection.execute(
                "UPDATE trips SET current_version=?, updated_at=? WHERE id=?",
                (version, now, trip_id),
            )
        return self.get_trip(trip_id)

    def restore_version(self, trip_id: str, version: int) -> SavedTripDetail:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT plan_json FROM trip_versions WHERE trip_id=? AND version=?",
                (trip_id, version),
            ).fetchone()
        if row is None:
            raise KeyError(f"{trip_id}:{version}")
        return self.add_version(
            trip_id,
            SavedTripUpdate(
                plan=TripPlan.model_validate_json(row["plan_json"]),
                change_note=f"恢复到版本 v{version}",
            ),
        )

    def delete_trip(self, trip_id: str) -> None:
        with self._lock, self._connection() as connection:
            cursor = connection.execute("DELETE FROM trips WHERE id=?", (trip_id,))
            if cursor.rowcount == 0:
                raise KeyError(trip_id)

    def get_preferences(self) -> UserPreferences:
        with self._connection() as connection:
            row = connection.execute("SELECT value_json FROM user_preferences WHERE id=1").fetchone()
        return UserPreferences.model_validate_json(row["value_json"]) if row else UserPreferences()

    def save_preferences(self, preferences: UserPreferences) -> UserPreferences:
        with self._lock, self._connection() as connection:
            connection.execute(
                """INSERT INTO user_preferences(id, value_json, updated_at) VALUES(1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at""",
                (preferences.model_dump_json(), self._now()),
            )
        return preferences

    def get_progress(self, trip_id: str) -> TripProgressResponse:
        with self._connection() as connection:
            exists = connection.execute("SELECT 1 FROM trips WHERE id=?", (trip_id,)).fetchone()
            if exists is None:
                raise KeyError(trip_id)
            rows = connection.execute(
                "SELECT item_key, completed, completed_at FROM trip_progress WHERE trip_id=? ORDER BY item_key",
                (trip_id,),
            ).fetchall()
        return TripProgressResponse(
            trip_id=trip_id,
            items=[
                TripProgressItem(
                    item_key=row["item_key"],
                    completed=bool(row["completed"]),
                    completed_at=row["completed_at"],
                )
                for row in rows
            ],
        )

    def set_progress(self, trip_id: str, item_key: str, completed: bool) -> TripProgressResponse:
        if not item_key or len(item_key) > 200:
            raise ValueError("无效的计划步骤标识")
        now = self._now()
        completed_at = now if completed else None
        with self._lock, self._connection() as connection:
            exists = connection.execute("SELECT 1 FROM trips WHERE id=?", (trip_id,)).fetchone()
            if exists is None:
                raise KeyError(trip_id)
            connection.execute(
                """INSERT INTO trip_progress(trip_id, item_key, completed, completed_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(trip_id, item_key) DO UPDATE SET
                    completed=excluded.completed,
                    completed_at=excluded.completed_at,
                    updated_at=excluded.updated_at""",
                (trip_id, item_key, int(completed), completed_at, now),
            )
        return self.get_progress(trip_id)

    @staticmethod
    def _summary(row: sqlite3.Row) -> SavedTripSummary:
        plan = TripPlan.model_validate_json(row["plan_json"])
        return SavedTripSummary(
            id=row["id"], title=row["title"], city=plan.city,
            start_date=plan.start_date, end_date=plan.end_date, days=len(plan.days),
            estimated_budget=plan.budget.total if plan.budget else None,
            data_quality=plan.data_quality, current_version=row["current_version"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )


trip_store_service = TripStoreService()
