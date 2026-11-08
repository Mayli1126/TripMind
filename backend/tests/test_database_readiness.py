import json
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest import TestCase
from scripts.database_readiness import audit


class DatabaseReadinessChecks(TestCase):
    def test_snapshot_is_read_only_and_report_has_no_row_contents(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / 'source.db'
            with closing(sqlite3.connect(path)) as db:
                db.executescript('CREATE TABLE trips(id TEXT PRIMARY KEY,current_version INTEGER);'
                                 'CREATE TABLE trip_versions(trip_id TEXT,version INTEGER,plan_json TEXT);')
                db.execute('INSERT INTO trips VALUES (?,?)', ('private-name', 2))
                db.execute('INSERT INTO trip_versions VALUES (?,?,?)', ('private-name', 1, 'private-secret'))
                db.commit()
            before = path.read_bytes()
            report = audit(path)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(report['checks']['missing_current_versions'], 1)
            self.assertFalse(report['sqlite_checks_passed'])
            self.assertFalse(report['postgres_ready'])
            self.assertNotIn('private-', json.dumps(report))

    def test_missing_source_not_created(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / 'missing.db'
            with self.assertRaises(FileNotFoundError):
                audit(path)
            self.assertFalse(path.exists())
