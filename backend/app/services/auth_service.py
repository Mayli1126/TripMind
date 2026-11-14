"""SQLite users, hashed sessions and ownership for private application resources."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time

from ..infrastructure.database import Database, application_database

from ..models.schemas import UserPreferences

COOKIE_NAME = "tripmind_session"
SESSION_SECONDS = 14 * 24 * 3600


class AuthService:
    def __init__(self, database_path=None):
        self._database = Database(database_path) if database_path is not None else application_database

    @property
    def database_path(self):
        return self._database.path

    def connection(self):
        # 保留已有调用入口；连接和事务规则统一由基础设施管理。
        return self._database.connection()

    def initialize(self):
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user',
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON user_sessions(expires_at);
                CREATE TABLE IF NOT EXISTS resource_owners (
                    kind TEXT NOT NULL, resource_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL, PRIMARY KEY(kind, resource_id)
                );
                CREATE INDEX IF NOT EXISTS idx_resource_owner ON resource_owners(owner_id, kind);
                CREATE TABLE IF NOT EXISTS personal_preferences (
                    user_id TEXT PRIMARY KEY, value_json TEXT NOT NULL
                );
            """)
            admin = db.execute("SELECT id FROM users WHERE role='admin' ORDER BY created_at LIMIT 1").fetchone()
            if admin:
                self._claim_legacy(db, admin["id"])
            db.execute("DELETE FROM user_sessions WHERE expires_at <= ?", (int(time.time()),))

    @staticmethod
    def _claim_legacy(db, admin_id):
        # Ownership lives in a separate table: existing itinerary JSON and version chains stay intact.
        for kind, table in (("trip", "trips"), ("guide", "guide_sources")):
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                db.execute(
                    f"INSERT OR IGNORE INTO resource_owners(kind, resource_id, owner_id, created_at) SELECT ?, id, ?, ? FROM {table}",
                    (kind, admin_id, int(time.time())),
                )
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_preferences'").fetchone():
            row = db.execute("SELECT value_json FROM user_preferences WHERE id=1").fetchone()
            if row:
                db.execute("INSERT OR IGNORE INTO personal_preferences VALUES (?, ?)", (admin_id, row["value_json"]))

    @staticmethod
    def hash_password(password, salt=None):
        salt = salt or secrets.token_hex(16)
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
        return f"scrypt${salt}${digest}"

    @staticmethod
    def public_user(row):
        return {"id": row["id"], "username": row["username"], "role": row["role"]}

    def has_admin(self):
        with self.connection() as db:
            return db.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone() is not None

    def create_user(self, username, password, role="user"):
        user_id = secrets.token_hex(16)
        with self.connection() as db:
            db.execute("INSERT INTO users VALUES (?, ?, ?, ?, ?)",
                       (user_id, username, self.hash_password(password), role, int(time.time())))
            if role == "admin":
                self._claim_legacy(db, user_id)
        return {"id": user_id, "username": username, "role": role}

    def login(self, username, password):
        with self.connection() as db:
            row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if row is None:
            return None
        _, salt, _ = row["password_hash"].split("$", 2)
        if not hmac.compare_digest(row["password_hash"], self.hash_password(password, salt)):
            return None
        return self.public_user(row)

    def new_session(self, user_id):
        token = secrets.token_urlsafe(40)
        with self.connection() as db:
            db.execute("DELETE FROM user_sessions WHERE expires_at <= ?", (int(time.time()),))
            db.execute("INSERT INTO user_sessions VALUES (?, ?, ?)",
                       (hashlib.sha256(token.encode()).hexdigest(), user_id, int(time.time()) + SESSION_SECONDS))
        return token

    def session_user(self, token):
        if not token:
            return None
        with self.connection() as db:
            row = db.execute("""SELECT u.* FROM users u JOIN user_sessions s ON s.user_id=u.id
                WHERE s.token_hash=? AND s.expires_at>?""",
                (hashlib.sha256(token.encode()).hexdigest(), int(time.time()))).fetchone()
        return self.public_user(row) if row else None

    def logout(self, token):
        if token:
            with self.connection() as db:
                db.execute("DELETE FROM user_sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))

    def claim(self, kind, resource_id, owner_id):
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO resource_owners VALUES (?, ?, ?, ?)",
                       (kind, resource_id, owner_id, int(time.time())))

    def can_access(self, user, kind, resource_id):
        if user["role"] == "admin":
            return True
        with self.connection() as db:
            return db.execute("SELECT 1 FROM resource_owners WHERE kind=? AND resource_id=? AND owner_id=?",
                              (kind, resource_id, user["id"])).fetchone() is not None

    def preferences(self, user_id):
        with self.connection() as db:
            row = db.execute("SELECT value_json FROM personal_preferences WHERE user_id=?", (user_id,)).fetchone()
        return UserPreferences.model_validate_json(row["value_json"]) if row else UserPreferences()

    def save_preferences(self, user_id, preferences):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT value_json FROM personal_preferences WHERE user_id=?', (user_id,)).fetchone()
            revision = UserPreferences.model_validate_json(row['value_json']).revision if row else 0
            if preferences.revision != revision:
                raise ValueError('常用偏好已在其他页面更新，请刷新后重试')
            preferences = preferences.model_copy(update={'revision': revision + 1})
            db.execute("INSERT INTO personal_preferences VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET value_json=excluded.value_json",
                       (user_id, preferences.model_dump_json()))
        return preferences

    def overview(self):
        with self.connection() as db:
            users = [dict(row) for row in db.execute("""SELECT u.id,u.username,u.role,u.created_at,
                (SELECT COUNT(*) FROM resource_owners r JOIN trips t ON t.id=r.resource_id WHERE r.kind='trip' AND r.owner_id=u.id) AS trip_count,
                (SELECT COUNT(*) FROM resource_owners r JOIN guide_sources g ON g.id=r.resource_id WHERE r.kind='guide' AND r.owner_id=u.id) AS guide_count
                FROM users u ORDER BY u.created_at DESC""")]
            trips = [dict(row) for row in db.execute("""SELECT t.id,t.title,t.updated_at,u.username,u.id AS owner_id
                FROM trips t LEFT JOIN resource_owners r ON r.kind='trip' AND r.resource_id=t.id
                LEFT JOIN users u ON u.id=r.owner_id ORDER BY t.updated_at DESC""")]
            guides = [dict(row) for row in db.execute("""SELECT g.id,g.title,g.parse_status,g.created_at,u.username,u.id AS owner_id
                FROM guide_sources g LEFT JOIN resource_owners r ON r.kind='guide' AND r.resource_id=g.id
                LEFT JOIN users u ON u.id=r.owner_id ORDER BY g.created_at DESC""")]
            jobs = [dict(row) for row in db.execute("""SELECT r.resource_id AS id,r.created_at,u.username,u.id AS owner_id
                FROM resource_owners r JOIN users u ON u.id=r.owner_id WHERE r.kind='job'
                ORDER BY r.created_at DESC LIMIT 100""")]
        return {"users": users, "trips": trips, "guides": guides, "jobs": jobs}


auth_service = AuthService()


if __name__ == "__main__":
    import argparse
    import getpass
    import re

    parser = argparse.ArgumentParser(description="Create the TripMind administrator locally, never through public registration.")
    parser.add_argument("command", choices=["create-admin"])
    parser.add_argument("--username", default="admin")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_\-]{3,32}", args.username):
        parser.error("Username must be 3-32 letters, numbers, underscores or hyphens.")
    auth_service.initialize()
    password = getpass.getpass("Administrator password (at least 8 characters): ")
    if not 8 <= len(password) <= 128 or password != getpass.getpass("Confirm password: "):
        parser.error("Password is too short or confirmation does not match.")
    try:
        auth_service.create_user(args.username, password, role="admin")
    except sqlite3.IntegrityError:
        parser.error("That username already exists.")
    print("Administrator created. Existing unowned trips and guides are assigned to this account.")
