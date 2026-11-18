"""私有对话历史与用户自行管理的长期偏好。"""
import json
import time
from typing import Any, Literal
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from ...infrastructure.database import application_database
from ..auth import current_user
from ...services.auth_service import auth_service
from ...services.conversation_store import memories
from ...services.preference_memory_service import MemoryWrite, conflicts, render_memory, active

router = APIRouter(prefix='/library', tags=['会话与记忆'])


class ConversationWrite(BaseModel):
    kind: Literal['planner', 'trip']
    trip_id: str | None = None
    title: str = Field(default='旅行对话', max_length=100)
    snapshot: dict[str, Any] = Field(default_factory=dict)
    revision: int = Field(default=0, ge=0)


@router.get('/conversations')
def list_conversations(request: Request):
    with application_database.connection() as db:
        return [dict(row) for row in db.execute('SELECT id, kind, trip_id, title, revision, updated_at FROM conversations WHERE user_id=? ORDER BY updated_at DESC LIMIT 100', (current_user(request)['id'],))]


@router.get('/conversations/{conversation_id}')
def get_conversation(conversation_id: str, request: Request):
    with application_database.connection() as db:
        row = db.execute('SELECT * FROM conversations WHERE id=? AND user_id=?', (conversation_id, current_user(request)['id'])).fetchone()
    if not row:
        raise HTTPException(404, '会话不存在')
    if row['trip_id'] and not auth_service.can_access(current_user(request), 'trip', row['trip_id']):
        raise HTTPException(404, '行程已删除或无权访问')
    from ...services.conversation_summary_service import read_summary
    return {**dict(row), 'snapshot': json.loads(row['snapshot']), 'summary': read_summary(current_user(request)['id'], conversation_id)}


@router.put('/conversations/{conversation_id}')
def save_conversation(conversation_id: str, payload: ConversationWrite, request: Request):
    user = current_user(request)
    if len(conversation_id) > 100:
        raise HTTPException(400, '会话标识过长')
    if payload.kind == 'trip' and not payload.trip_id:
        raise HTTPException(400, '行程会话必须关联行程')
    if payload.trip_id and not auth_service.can_access(user, 'trip', payload.trip_id):
        raise HTTPException(404, '行程不存在或无权访问')
    raw = json.dumps(payload.snapshot, ensure_ascii=False)
    if len(raw.encode()) > 800000:
        raise HTTPException(413, '会话过长，请开始新对话')
    with application_database.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM conversations WHERE id=?', (conversation_id,)).fetchone()
        if row and row['user_id'] != user['id']:
            raise HTTPException(404, '会话不存在')
        if row and (row['kind'] != payload.kind or row['trip_id'] != payload.trip_id):
            raise HTTPException(409, '会话绑定不能改变')
        if (row['revision'] if row else 0) != payload.revision:
            raise HTTPException(409, '此会话已在其他页面更新，请重新打开，不会覆盖新消息')
        revision = payload.revision + 1
        db.execute('INSERT INTO conversations VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET title=excluded.title, snapshot=excluded.snapshot, revision=excluded.revision, updated_at=excluded.updated_at',
                   (conversation_id, user['id'], payload.kind, payload.trip_id, payload.title, raw, revision, int(time.time())))
        from ...services.conversation_summary_service import clean_messages, _hash
        summary = db.execute('SELECT * FROM conversation_summaries WHERE conversation_id=? AND user_id=?', (conversation_id, user['id'])).fetchone()
        if summary and summary['prefix_hash'] != _hash(clean_messages(payload.snapshot)[:summary['processed_count']]):
            db.execute('DELETE FROM conversation_summaries WHERE conversation_id=? AND user_id=?', (conversation_id, user['id']))
    return {'revision': revision}


@router.delete('/conversations/{conversation_id}')
def delete_conversation(conversation_id: str, request: Request):
    with application_database.connection() as db:
        db.execute('DELETE FROM conversation_summaries WHERE conversation_id=? AND user_id=?', (conversation_id, current_user(request)['id']))
        db.execute('DELETE FROM conversations WHERE id=? AND user_id=?', (conversation_id, current_user(request)['id']))
    return {'deleted': True}


@router.get('/memories')
def list_memories(request: Request):
    return [{**r, 'expired': r.get('expires_at') is not None and r['expires_at'] <= time.time(),
             'active': active(r)} for r in memories(current_user(request)['id'])]


@router.put('/memories/{memory_id}')
def save_memory(memory_id: str, payload: MemoryWrite, request: Request):
    with application_database.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM preference_memories WHERE id=?', (memory_id,)).fetchone()
        if row and row['user_id'] != current_user(request)['id']:
            raise HTTPException(404, '偏好不存在')
        if row and 'revision' not in payload.model_fields_set:
            raise HTTPException(428, '缺少偏好版本，请刷新后重试')
        if (row['revision'] if row else 0) != payload.revision:
            raise HTTPException(409, '偏好已更新或删除，请刷新比较后重试；本次内容未覆盖')
        if not row and db.execute('SELECT COUNT(*) FROM preference_memories WHERE user_id=?', (current_user(request)['id'],)).fetchone()[0] >= 100:
            raise HTTPException(400, '最多保存100条偏好，请先整理已有偏好')
        # 旧客户端切换结构化偏好状态时，不能重置新增字段。
        data = {**(dict(row) if row else {}), **payload.model_dump(exclude_unset=True)}
        try:
            preference = MemoryWrite.model_validate(data)
        except ValueError:
            raise HTTPException(422, '偏好分类、内容或有效期不正确，请重新检查') from None
        data = preference.model_dump()
        data['content'] = render_memory(data)
        rows = [dict(r) for r in db.execute('SELECT * FROM preference_memories WHERE user_id=?', (current_user(request)['id'],))]
        matched = conflicts(rows, data, memory_id)
        if matched and (not payload.replace_conflicts or {r['id'] for r in matched} != set(payload.conflict_ids)
                        or {r['id']: r['revision'] for r in matched} != payload.conflict_revisions):
            raise HTTPException(409, {'message': '同一范围已有相同或冲突偏好，替换会暂停旧记录',
                                      'conflicts': [{'id': r['id'], 'content': r['content'], 'revision': r['revision']} for r in matched]})
        for previous in matched:
            db.execute('UPDATE preference_memories SET enabled=0, revision=revision+1 WHERE id=? AND user_id=?', (previous['id'], current_user(request)['id']))
        db.execute('''INSERT INTO preference_memories
            (id,user_id,content,city,enabled,updated_at,category,value,polarity,expires_at,revision)
            VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            content=excluded.content,city=excluded.city,enabled=excluded.enabled,updated_at=excluded.updated_at,
            category=excluded.category,value=excluded.value,polarity=excluded.polarity,expires_at=excluded.expires_at,revision=excluded.revision''',
            (memory_id, current_user(request)['id'], data['content'], data['city'], int(data['enabled']), int(time.time()),
             data['category'], data['value'], data['polarity'], data['expires_at'], payload.revision+1))
    return {'saved': True, 'revision': payload.revision+1, 'replaced': [r['id'] for r in matched]}


@router.delete('/memories/{memory_id}')
def delete_memory(memory_id: str, request: Request, revision: int):
    with application_database.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT revision FROM preference_memories WHERE id=? AND user_id=?',
                         (memory_id, current_user(request)['id'])).fetchone()
        if not row:
            raise HTTPException(404, '偏好不存在')
        if row['revision'] != revision:
            raise HTTPException(409, '偏好已更新，请刷新后确认删除')
        db.execute('DELETE FROM preference_memories WHERE id=? AND user_id=?', (memory_id, current_user(request)['id']))
    return {'deleted': True}
