"""Offline stage-C contract checks, not provider/Redis integration tests."""
from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import patch
from app.config import get_settings
from app.models.advisory import AttractionAdvisory
from app.services.advisory_refresh_service import OfficialPolicy, overlay
from app.services.cache_policy import make_entry
from app.services.public_advisory_cache import validate_policy


class PublicFactChecks(TestCase):
    def test_official_expiry_caps_envelope(self):
        now = datetime.now(timezone.utc)
        expiry = (now + timedelta(seconds=25)).timestamp()
        entry = make_entry('official_policy', {}, 'fixture', fetched_at=now.timestamp(), source_expires_at=expiry)
        self.assertEqual(entry.hard_until, expiry)
        self.assertLessEqual(entry.fresh_until, expiry)

    def test_expired_snapshot_not_verified(self):
        now = datetime.now(timezone.utc)
        snapshot = dict(opening=dict(note='old hours', checked_at=now.isoformat(),
            expires_at=(now-timedelta(seconds=1)).isoformat(), source='fixture'),
            policy=dict(poi_id='p', visit_date='2026-11-06', source_url='https://official.test/p',
            reservation_required=True, verified_at=(now-timedelta(hours=2)).isoformat(),
            expires_at=(now-timedelta(seconds=1)).isoformat()))
        with patch('app.services.advisory_refresh_service.cached', return_value=snapshot):
            result = overlay(AttractionAdvisory(fact_status='verified', reservation_required='true'), 'p', '2026-11-06')
        self.assertEqual(result.fact_status, 'unverified')
        self.assertEqual(result.reservation_required, 'unknown')
        self.assertEqual(result.opening_hours_note, '')

    def test_trust_change_rejects_policy(self):
        now = datetime.now(timezone.utc)
        policy = OfficialPolicy(poi_id='p', visit_date='2026-11-06', source_url='https://official.test/p',
            reservation_required=True, verified_at=now-timedelta(minutes=1), expires_at=now+timedelta(minutes=5))
        settings = get_settings().model_copy(update=dict(advisory_official_hosts='official.test'))
        self.assertIs(validate_policy(policy, 'p', '2026-11-06', settings), policy)
        settings.advisory_official_hosts = 'different.test'
        with self.assertRaises(ValueError):
            validate_policy(policy, 'p', '2026-11-06', settings)
