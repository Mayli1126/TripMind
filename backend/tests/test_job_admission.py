"""Offline policy checks, not Redis transaction or worker integration tests."""
from unittest import TestCase
from unittest.mock import patch, MagicMock
from fastapi import HTTPException
from app.config import get_settings
from app.services.job_admission import check_capacity, redis_records


class AdmissionChecks(TestCase):
    def setUp(self):
        settings=get_settings().model_copy(update=dict(job_user_active_limit=2,
            job_global_active_limit=4, job_queue_capacity=3))
        patcher=patch('app.services.job_admission.get_settings',return_value=settings)
        patcher.start(); self.addCleanup(patcher.stop)

    def test_user_limit(self):
        with self.assertRaises(HTTPException) as result:
            check_capacity(['u','u'],'u')
        self.assertEqual(result.exception.status_code,429)

    def test_global_queue_limit_and_retry_header(self):
        with self.assertRaises(HTTPException) as result:
            check_capacity(['a','b','c'],'d')
        self.assertEqual(result.exception.status_code,503)
        self.assertEqual(result.exception.headers['Retry-After'],'5')

    def test_different_user_and_released_slot(self):
        check_capacity(['a','a'],'b')
        check_capacity(['a'],'a')

    def test_expired_live_worker_not_reaped(self):
        pipe=MagicMock()
        pipe.hgetall.return_value={'alive':'{"owner":"u","until":1}', 'dead':'{"owner":"v","until":1}'}
        pipe.exists.side_effect=lambda key: 'alive' in key
        records, expired=redis_records(pipe,10)
        self.assertEqual(set(records),{'alive'})
        self.assertEqual(expired,['dead'])
