"""攻略来源 SQLite 存储（产品化计划 11.2 节 guide_sources / guide_facts 表）。

设计：
- guide_sources 保存原始输入与解析状态，解析结果整体存 result_json；
- guide_facts 保存结构化事实（推荐理由/警告/建议时长），
  解析完成后长期保留结构化事实即可；
- 复用 backend/data/tripmind.db，模式与 trip_store_service 一致。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Optional
from uuid import uuid4

from ..infrastructure.database import DEFAULT_DATABASE_PATH, connect, transaction

from ..models.guide import GuideInput, GuideParseResult, GuideSourceRecord


def guide_content_hash(source: GuideInput) -> str:
    """内容指纹：正文取正文，链接取链接，用于判断"同一篇攻略"重复提交。"""
    payload = source.content.strip() if source.input_type == "text" else source.source_url.strip()
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class GuideStoreService:
    """攻略来源本地持久化：无外部数据库依赖。"""

    def __init__(self, database_path: str | Path | None = None) -> None:
        self.database_path = Path(database_path or DEFAULT_DATABASE_PATH)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect(self.database_path)

    def _connection(self):
        """保留攻略事务边界，连接参数与行程、认证存储保持一致。"""
        return transaction(self._connect())

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS guide_parse_leases (
                source_id TEXT PRIMARY KEY, token TEXT NOT NULL, expires_at REAL NOT NULL)''')
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS guide_sources (
                    id TEXT PRIMARY KEY,
                    input_type TEXT NOT NULL,
                    source_url TEXT,
                    title TEXT,
                    content_hash TEXT NOT NULL,
                    parse_status TEXT NOT NULL,
                    error_message TEXT,
                    result_json TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS guide_facts (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    fact_type TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    claim_json TEXT NOT NULL,
                    confidence REAL,
                    FOREIGN KEY (source_id) REFERENCES guide_sources(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_guide_facts_source ON guide_facts(source_id);
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    # ------------------------------------------------------------------
    def create_record(self, source: GuideInput, source_id: Optional[str] = None, owner_id: str | None = None) -> GuideSourceRecord:
        """先落一条 pending 记录占住 source_id，解析结果稍后回填。"""
        record = GuideSourceRecord(
            id=source_id or f"guide-{uuid4().hex[:12]}",
            input_type=source.input_type,
            source_url=source.source_url.strip(),
            title=source.display_title(),
            content_hash=guide_content_hash(source),
            parse_status="pending",
            created_at=self._now(),
        )
        with self._lock, self._connection() as connection:
            connection.execute(
                """INSERT INTO guide_sources
                   (id, input_type, source_url, title, content_hash, parse_status, error_message, result_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)""",
                (record.id, record.input_type, record.source_url, record.title,
                 record.content_hash, record.parse_status, record.created_at),
            )
            if owner_id:
                connection.execute("INSERT INTO resource_owners VALUES ('guide', ?, ?, ?)",
                                   (record.id, owner_id, int(datetime.now(timezone.utc).timestamp())))
        return record

    def begin_parse(self, source_id: str, expected_status: str | None = None) -> str:
        """跨进程为每个来源保留一个解析器，上游调用期间不持有数据库锁。"""
        token = uuid4().hex
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            source = db.execute('SELECT parse_status FROM guide_sources WHERE id=?', (source_id,)).fetchone()
            if not source:
                raise KeyError(source_id)
            if expected_status is not None and source['parse_status'] != expected_status:
                raise ValueError('攻略状态已更新，请刷新后再决定是否重试')
            old = db.execute('SELECT expires_at FROM guide_parse_leases WHERE source_id=?', (source_id,)).fetchone()
            if old and old['expires_at'] > time.time():
                raise ValueError('这篇攻略正在解析，请等待完成后重试')
            db.execute('INSERT OR REPLACE INTO guide_parse_leases VALUES (?,?,?)', (source_id, token, time.time()+900))
        return token

    def save_result(self, source_id: str, result: GuideParseResult, parse_token: str | None = None) -> GuideSourceRecord:
        """回填解析结果；成功时同步写入 guide_facts（先清旧事实保证幂等）。"""
        with self._lock, self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            previous = connection.execute('SELECT * FROM guide_sources WHERE id=?', (source_id,)).fetchone()
            if previous is None:
                raise KeyError(source_id)
            lease = connection.execute('SELECT * FROM guide_parse_leases WHERE source_id=?', (source_id,)).fetchone()
            if parse_token is not None and (not lease or lease['token'] != parse_token or lease['expires_at'] <= time.time()):
                raise ValueError('解析已过期或被新任务接管，本次结果未覆盖现有攻略')
            if parse_token is None and lease:
                raise ValueError('已有解析任务，回填必须携带任务标识')
            result = result.model_copy(deep=True)
            # 保留用户最新选择，不能使用慢模型调用前的过期快照覆盖。
            old_record = self._record_from_row(previous)
            if old_record.result and result.parse_status != 'parsed':
                result = old_record.result.model_copy(deep=True)
                result.error_message = '重新解析未成功，已保留原解析结果和你的选择'
            if old_record.result and result.parse_status == 'parsed':
                by_poi = {c.poi_id: c.user_choice for c in old_record.result.candidates if c.poi_id}
                by_id = {c.candidate_id: c for c in old_record.result.candidates}
                for c in result.candidates:
                    if c.poi_id and c.poi_id in by_poi:
                        c.user_choice = by_poi[c.poi_id]
                    elif c.candidate_id in by_id:
                        old = by_id[c.candidate_id]
                        if not (old.poi_id and c.poi_id and old.poi_id != c.poi_id):
                            c.user_choice = old.user_choice
            cursor = connection.execute(
                """UPDATE guide_sources
                   SET parse_status=?, error_message=?, title=?, result_json=?
                   WHERE id=?""",
                (result.parse_status, result.error_message, result.title,
                 result.model_dump_json() if result.parse_status == "parsed" else None,
                 source_id),
            )
            if not cursor.rowcount:
                raise KeyError(source_id)
            connection.execute("DELETE FROM guide_facts WHERE source_id=?", (source_id,))
            if result.parse_status == "parsed":
                for fact in result.facts:
                    connection.execute(
                        """INSERT INTO guide_facts (id, source_id, fact_type, subject, claim_json, confidence)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (f"fact-{uuid4().hex[:16]}", source_id, fact.fact_type,
                         fact.subject, json.dumps({"claim": fact.claim}, ensure_ascii=False),
                         fact.confidence),
                    )
            if parse_token is not None:
                connection.execute('DELETE FROM guide_parse_leases WHERE source_id=? AND token=?', (source_id, parse_token))
        return self.get_record(source_id)  # type: ignore[return-value]

    def get_record(self, source_id: str) -> Optional[GuideSourceRecord]:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM guide_sources WHERE id=?", (source_id,)
            ).fetchone()
        return self._record_from_row(row) if row else None

    def update_candidate_choices(
        self, source_id: str, choices: dict[str, str]
    ) -> Optional[GuideSourceRecord]:
        """把用户在确认板做的选择回写到该篇候选（choices: candidate_id -> user_choice）。

        选择存进 result_json 的候选对象上，聚合接口即刻反映，跨设备一致。
        未知 candidate_id 静默跳过（候选可能已被合并/删除）。
        """
        with self._lock, self._connection() as connection:
            # 在数据库锁内读取、合并和写入，不能只依赖当前 Python 实例锁。
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM guide_sources WHERE id=?', (source_id,)).fetchone()
            record = self._record_from_row(row) if row else None
            if record is None or record.result is None:
                return None
            valid = {"required", "preferred", "optional", "excluded"}
            for candidate in record.result.candidates:
                choice = choices.get(candidate.candidate_id)
                if choice in valid:
                    candidate.user_choice = choice  # type: ignore[assignment]
            connection.execute(
                "UPDATE guide_sources SET result_json=? WHERE id=?",
                (record.result.model_dump_json(), source_id),
            )
        return self.get_record(source_id)

    def delete_record(self, source_id: str) -> bool:
        """删除攻略记录；事实显式删除（不依赖外键级联，幂等保险）。"""
        with self._lock, self._connection() as connection:
            connection.execute('DELETE FROM guide_parse_leases WHERE source_id=?', (source_id,))
            connection.execute("DELETE FROM guide_facts WHERE source_id=?", (source_id,))
            cursor = connection.execute("DELETE FROM guide_sources WHERE id=?", (source_id,))
        return cursor.rowcount > 0

    def list_records(self, limit: int = 50, owner_id: str | None = None) -> list[GuideSourceRecord]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM guide_sources"
                + (" WHERE id IN (SELECT resource_id FROM resource_owners WHERE kind='guide' AND owner_id=?)" if owner_id else "")
                + " ORDER BY created_at DESC, id DESC LIMIT ?",
                (owner_id, limit) if owner_id else (limit,),
            ).fetchall()
        return [self._record_from_row(row) for row in rows]

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> GuideSourceRecord:
        result = None
        if row["result_json"]:
            result = GuideParseResult.model_validate_json(row["result_json"])
        return GuideSourceRecord(
            id=row["id"],
            input_type=row["input_type"],  # type: ignore[arg-type]
            source_url=row["source_url"] or "",
            title=row["title"] or "",
            content_hash=row["content_hash"],
            parse_status=row["parse_status"],  # type: ignore[arg-type]
            error_message=row["error_message"] or "",
            created_at=row["created_at"],
            result=result,
        )


guide_store_service = GuideStoreService()
