"""只读检查 SQLite 迁移准备情况，不导入应用单例或输出数据行内容。"""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

TABLES = ('users', 'user_sessions', 'resource_owners', 'personal_preferences', 'trips',
          'trip_versions', 'trip_progress', 'guide_sources', 'guide_facts', 'conversations',
          'conversation_summaries', 'preference_memories', 'job_submissions', 'advisory_cache')


def audit(source):
    source = Path(source).resolve(strict=True)
    if not source.is_file():
        raise ValueError('Source must be an existing SQLite file')
    # 使用备份接口获取包含预写日志的一致快照，不以可写方式打开源数据库。
    with TemporaryDirectory(prefix='tripmind-db-audit-') as temp:
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=3)) as original:
            with closing(sqlite3.connect(Path(temp) / 'snapshot.db')) as snapshot:
                original.backup(snapshot)
                return inspect_snapshot(snapshot)


def inspect_snapshot(db):
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    counts = {t: db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in TABLES if t in names}
    schema = list(db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"))
    checks = {'integrity_ok': all(r[0] == 'ok' for r in db.execute('PRAGMA integrity_check')),
              'foreign_key_violations': sum(1 for _ in db.execute('PRAGMA foreign_key_check'))}
    queries = {
        'missing_current_versions': ({'trips', 'trip_versions'}, 'SELECT COUNT(*) FROM trips t LEFT JOIN trip_versions v ON v.trip_id=t.id AND v.version=t.current_version WHERE v.trip_id IS NULL'),
        'orphan_versions': ({'trips', 'trip_versions'}, 'SELECT COUNT(*) FROM trip_versions v LEFT JOIN trips t ON t.id=v.trip_id WHERE t.id IS NULL'),
        'orphan_progress': ({'trips', 'trip_progress'}, 'SELECT COUNT(*) FROM trip_progress p LEFT JOIN trips t ON t.id=p.trip_id WHERE t.id IS NULL'),
        'orphan_conversations': ({'users', 'conversations'}, 'SELECT COUNT(*) FROM conversations c LEFT JOIN users u ON u.id=c.user_id WHERE u.id IS NULL'),
        'orphan_memory_users': ({'users', 'preference_memories'}, 'SELECT COUNT(*) FROM preference_memories m LEFT JOIN users u ON u.id=m.user_id WHERE u.id IS NULL'),
        'orphan_owner_users': ({'users', 'resource_owners'}, 'SELECT COUNT(*) FROM resource_owners r LEFT JOIN users u ON u.id=r.owner_id WHERE u.id IS NULL'),
        'unowned_trips': ({'trips', 'resource_owners'}, "SELECT COUNT(*) FROM trips t LEFT JOIN resource_owners r ON r.kind='trip' AND r.resource_id=t.id WHERE r.resource_id IS NULL"),
        'unowned_guides': ({'guide_sources', 'resource_owners'}, "SELECT COUNT(*) FROM guide_sources g LEFT JOIN resource_owners r ON r.kind='guide' AND r.resource_id=g.id WHERE r.resource_id IS NULL"),
    }
    skipped = []
    for label, (required, sql) in queries.items():
        if required <= names:
            checks[label] = db.execute(sql).fetchone()[0]
        else:
            skipped.append(label)
    missing = [t for t in TABLES if t not in names]
    clean = checks['integrity_ok'] and all(v == 0 for k, v in checks.items() if k != 'integrity_ok')
    return dict(schema_version=1, source_mode='read_only_snapshot', rows=counts, checks=checks,
        missing_tables=missing, skipped_checks=skipped,
        schema_sha256=hashlib.sha256(json.dumps(schema, ensure_ascii=False).encode()).hexdigest(),
        sqlite_checks_passed=bool(clean and not skipped and not missing),
        postgres_ready=False, note='Schema/data audit only. No PostgreSQL import, type/JSON validation, or capacity verification.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, help='Explicit existing SQLite source; no default business path')
    args = parser.parse_args()
    try:
        report = audit(args.source)
    except Exception as exc:
        print(json.dumps({'audit_failed': True, 'error_type': type(exc).__name__}))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['sqlite_checks_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
