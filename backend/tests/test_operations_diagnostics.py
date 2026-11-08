"""Offline diagnostics contracts, no live Redis/provider probes."""
import json
from unittest import TestCase
from unittest.mock import patch
from app.config import get_settings
from app.services.diagnostic_summary import summarize_event
from app.services.operations_service import queue_snapshot, deployment_warnings


class DiagnosticChecks(TestCase):
    def test_distinct_operations_and_statuses(self):
        data = {}
        for operation, status in [('shared_l1','fresh'),('shared_l2','negative'),('rebuild_lock','timeout')]:
            summarize_event(data,dict(service='cache',operation=operation,status=status))
        self.assertEqual(len(data['operations']),3)
        self.assertEqual(data['operations']['cache:rebuild_lock']['timeouts'],1)

    def test_summary_bounded_and_no_payload(self):
        data = {}
        for i in range(200):
            summarize_event(data,dict(service='fixture',operation=str(i),status='completed',prompt='secret-fixture'))
        self.assertLessEqual(len(data['operations']),129)
        self.assertNotIn('secret-fixture',json.dumps(data))

    def test_backend_errors_redacted(self):
        settings=get_settings().model_copy(update=dict(redis_enabled=True))
        with patch('app.services.operations_service.get_settings',return_value=settings), \
             patch('app.infrastructure.provider_guard.provider_guard._redis',side_effect=ConnectionError('credential-fixture')):
            result=queue_snapshot()
        self.assertFalse(result['available'])
        self.assertEqual(result['error_type'],'ConnectionError')
        self.assertNotIn('credential-fixture',json.dumps(result))

    def test_same_endpoint_not_reported_isolated(self):
        warnings=deployment_warnings(dict(redis_enabled=True,
            shared_cache=dict(enabled=True,configured=True,resource_isolation='same_endpoint')))
        self.assertTrue(any('尚未资源隔离' in text for text in warnings))
