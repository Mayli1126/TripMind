"""使用 SQLite 保存个人行程库与偏好设置。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4

from ..infrastructure.database import DEFAULT_DATABASE_PATH, connect, transaction

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
from .trip_projection_service import project_stored_plan
from .trip_quality_service import trip_quality_service


class TripStoreService:
    """将行程持久化到本地，无需外部数据库服务。"""

    def __init__(self, database_path: str | Path | None = None) -> None:
        self.database_path = Path(database_path or DEFAULT_DATABASE_PATH)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect(self.database_path)

    def _connection(self):
        """保留存储层事务边界，复用统一的连接生命周期。"""
        return transaction(self._connect())

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

            connection.execute('BEGIN IMMEDIATE')
            columns = {r['name'] for r in connection.execute('PRAGMA table_info(trip_progress)')}
            if 'revision' not in columns:
                connection.execute("ALTER TABLE trip_progress ADD COLUMN revision TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def create_trip(self, payload: SavedTripCreate, owner_id: str | None = None) -> SavedTripDetail:
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
            if owner_id:
                connection.execute("INSERT INTO resource_owners VALUES ('trip', ?, ?, ?)",
                                   (trip_id, owner_id, int(datetime.now(timezone.utc).timestamp())))
        return self.get_trip(trip_id)

    def list_trips(self, owner_id: str | None = None) -> list[SavedTripSummary]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT t.*, v.plan_json FROM trips t
                JOIN trip_versions v ON v.trip_id=t.id AND v.version=t.current_version
                """ + (" WHERE t.id IN (SELECT resource_id FROM resource_owners WHERE kind='trip' AND owner_id=?)" if owner_id else "")
                + " ORDER BY t.updated_at DESC",
                (owner_id,) if owner_id else (),
            ).fetchall()
        return [self._summary(row) for row in rows]

    def get_trip(self, trip_id: str) -> SavedTripDetail:
        with self._connection() as connection:
            connection.execute('BEGIN')
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
        request, plan = project_stored_plan(row["plan_json"], row["request_json"])
        return SavedTripDetail.model_construct(
            **summary.model_dump(),
            request=request,
            plan=plan,
            versions=[TripVersionSummary(**dict(item)) for item in version_rows],
        )

    def add_version(self, trip_id: str, payload: SavedTripUpdate) -> SavedTripDetail:
        now = self._now()
        updated_plan = payload.plan.model_copy(deep=True)
        if updated_plan.revision_status in {"rejected", "unchanged", "clarify_needed"}:
            raise ValueError(updated_plan.revision_message or "本次修改未生效，不能保存为新版本")
        # 先读取基础快照，地图调用期间不持有数据库写锁。
        with self._connection() as connection:
            row = connection.execute(
                """SELECT t.current_version, t.request_json, v.plan_json
                FROM trips t JOIN trip_versions v
                  ON v.trip_id=t.id AND v.version=t.current_version
                WHERE t.id=?""", (trip_id,)
            ).fetchone()
            if row is None:
                raise KeyError(trip_id)
            if payload.base_version is not None and payload.base_version != int(row["current_version"]):
                raise ValueError("行程已更新，请基于最新版本重新生成修改建议")
            # 使用model_construct跳过验证，避免历史行程日期过期导致验证失败
            import json
            request_data = json.loads(row["request_json"])
            request_data.setdefault('destination_only', False)
            request = TripRequest.model_construct(**request_data)
            current_plan = TripPlan.model_validate_json(row["plan_json"])
            if updated_plan.revision_status in {"draft_ready", "applied"}:
                from .revision_outcome import has_plan_changes
                if not has_plan_changes(current_plan, updated_plan):
                    raise ValueError("本次修改未产生实际变化，不能保存为新版本")
            journey_count = 2 if request.trip_type == "round_trip" else 1
            intercity_per_person = 0
            if current_plan.budget and request.travelers and journey_count:
                intercity_per_person = round(
                    current_plan.budget.intercity_transportation
                    / request.travelers / journey_count
                )
            changed_days = {
                index for index, day in enumerate(updated_plan.days)
                if index >= len(current_plan.days) or day != current_plan.days[index] or not day.schedule
            }
            from .revision_intent import dependent_days
            changed_days = dependent_days(current_plan, updated_plan, changed_days)
            requested_orders = [[a.stable_id or a.name for a in day.attractions] for day in updated_plan.days]
            updated_plan = trip_quality_service.finalize_revision(
                updated_plan,
                request,
                changed_days=changed_days,
                intercity_cost_per_person=intercity_per_person,
            )
            if [[a.stable_id or a.name for a in day.attractions] for day in updated_plan.days] != requested_orders:
                raise ValueError("保存时景点顺序发生了意外变化，已取消保存，请重新生成修改建议")
            base = int(row["current_version"])
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            latest = connection.execute("SELECT current_version FROM trips WHERE id=?", (trip_id,)).fetchone()
            if latest is None:
                raise KeyError(trip_id)
            if int(latest['current_version']) != base:
                raise ValueError("行程已更新，请刷新后基于最新版本重新修改")
            version = base + 1
            if updated_plan.revision_status in {"draft_ready", "applied"}:
                updated_plan.revision_status = "applied"
                updated_plan.revision_message = f"修改已保存为 v{version}。"
            connection.execute(
                "INSERT INTO trip_versions VALUES (?, ?, ?, ?, ?)",
                (trip_id, version, updated_plan.model_dump_json(), payload.change_note, now),
            )
            connection.execute(
                "UPDATE trips SET current_version=?, updated_at=? WHERE id=?",
                (version, now, trip_id),
            )
        return self.get_trip(trip_id)

    def restore_version(self, trip_id: str, version: int, base_version: int | None = None) -> SavedTripDetail:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT v.plan_json, t.current_version FROM trip_versions v JOIN trips t ON t.id=v.trip_id WHERE v.trip_id=? AND v.version=?",
                (trip_id, version),
            ).fetchone()
        if row is None:
            raise KeyError(f"{trip_id}:{version}")
        return self.add_version(
            trip_id,
            SavedTripUpdate(
                plan=TripPlan.model_validate_json(row["plan_json"]),
                change_note=f"恢复到版本 v{version}",
                base_version=base_version if base_version is not None else row['current_version'],
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
            connection.execute('BEGIN')
            exists = connection.execute("SELECT 1 FROM trips WHERE id=?", (trip_id,)).fetchone()
            if exists is None:
                raise KeyError(trip_id)
            rows = connection.execute(
                "SELECT item_key, completed, completed_at, revision FROM trip_progress WHERE trip_id=? ORDER BY item_key",
                (trip_id,),
            ).fetchall()
        return TripProgressResponse(
            trip_id=trip_id,
            items=[
                TripProgressItem(
                    item_key=row["item_key"],
                    completed=bool(row["completed"]),
                    completed_at=row["completed_at"],
                    revision=row['revision'] or None,
                )
                for row in rows
            ],
        )

    def set_progress(self, trip_id: str, item_key: str, completed: bool,
                     expected_revision: str | None = None, base_version: int | None = None) -> TripProgressResponse:
        if not item_key or len(item_key) > 200:
            raise ValueError("无效的计划步骤标识")
        now = self._now()
        completed_at = now if completed else None
        with self._lock, self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            exists = connection.execute("SELECT current_version FROM trips WHERE id=?", (trip_id,)).fetchone()
            if exists is None:
                raise KeyError(trip_id)
            if base_version is not None:
                old = connection.execute('SELECT revision FROM trip_progress WHERE trip_id=? AND item_key=?',
                                         (trip_id, item_key)).fetchone()
                if exists['current_version'] != base_version or ((old['revision'] or None) if old else None) != expected_revision:
                    raise ValueError('完成状态或行程已更新，请刷新后重试')
            connection.execute(
                """INSERT INTO trip_progress(trip_id, item_key, completed, completed_at, updated_at, revision)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(trip_id, item_key) DO UPDATE SET
                    completed=excluded.completed,
                    completed_at=excluded.completed_at,
                    updated_at=excluded.updated_at, revision=excluded.revision""",
                (trip_id, item_key, int(completed), completed_at, now, uuid4().hex),
            )
        return self.get_progress(trip_id)

    @staticmethod
    def _summary(row: sqlite3.Row) -> SavedTripSummary:
        plan = TripPlan.model_validate_json(row["plan_json"])
        # 封面：按天序取第一个有图片的景点，让列表卡片直接展示真实目的地照片
        cover = None
        for day in plan.days:
            for attraction in day.attractions:
                if attraction.image_url or attraction.photos:
                    cover = attraction.image_url or attraction.photos[0]
                    break
            if cover:
                break
        return SavedTripSummary(
            id=row["id"], title=row["title"], city=plan.city,
            start_date=plan.start_date, end_date=plan.end_date, days=len(plan.days),
            estimated_budget=plan.budget.total if plan.budget else None,
            data_quality=plan.data_quality, current_version=row["current_version"],
            cover_image_url=cover,
            created_at=row["created_at"], updated_at=row["updated_at"],
        )


trip_store_service = TripStoreService()
