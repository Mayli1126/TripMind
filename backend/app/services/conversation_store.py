"""Account-scoped conversation snapshots and explicit preference memories."""
import json
import time
from uuid import uuid4
from .auth_service import auth_service


def initialize():
    with auth_service.connection() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS conversations (
          id TEXT PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL,
          trip_id TEXT, title TEXT NOT NULL, snapshot TEXT NOT NULL,
          revision INTEGER NOT NULL DEFAULT 1, updated_at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_conversation_user ON conversations(user_id, updated_at);
        CREATE TABLE IF NOT EXISTS preference_memories (
          id TEXT PRIMARY KEY, user_id TEXT NOT NULL, content TEXT NOT NULL,
          city TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
          updated_at INTEGER NOT NULL);
        ''')
        db.execute('BEGIN IMMEDIATE')
        columns = {r['name'] for r in db.execute('PRAGMA table_info(preference_memories)')}
        for name, declaration in {
            'category': "TEXT NOT NULL DEFAULT 'general'",
            'value': "TEXT NOT NULL DEFAULT ''",
            'polarity': "TEXT NOT NULL DEFAULT 'prefer'",
            'expires_at': 'INTEGER',
            'revision': 'INTEGER NOT NULL DEFAULT 0',
        }.items():
            if name not in columns:
                db.execute(f'ALTER TABLE preference_memories ADD COLUMN {name} {declaration}')
        db.execute('''CREATE TABLE IF NOT EXISTS conversation_summaries (
            conversation_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
            prefix_hash TEXT NOT NULL, processed_count INTEGER NOT NULL,
            summary_json TEXT NOT NULL, updated_at INTEGER NOT NULL)''')


def memories(user_id):
    with auth_service.connection() as db:
        return [dict(row) for row in db.execute('SELECT * FROM preference_memories WHERE user_id=? ORDER BY updated_at DESC', (user_id,))]


def memory_context(user_id, city=''):
    from .preference_memory_service import effective_memories, render_memory
    rows = effective_memories(memories(user_id), city)
    rows.sort(key=lambda r: (bool(r['city']), r['updated_at'], r['id']), reverse=True)
    return '\n'.join(render_memory(r) for r in rows[:8])[:1600]
