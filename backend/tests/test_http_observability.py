"""验证本地 HTTP 并发与失败契约，不使用业务数据库或付费服务。"""
import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware
from app.api.request_trace import RequestTraceMiddleware
from app.infrastructure.task_trace import request_id
from app.services.http_metrics import HttpMetrics


class HttpChecks(IsolatedAsyncioTestCase):
    async def test_concurrent_contexts_and_template_buckets(self):
        metrics, events = HttpMetrics(), []
        async def endpoint(scope, receive, send):
            before = request_id.get()
            await asyncio.sleep(0.001)
            self.assertEqual(before, request_id.get())
            scope['route'] = SimpleNamespace(path='/items/{id}')
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})
        middleware = RequestTraceMiddleware(endpoint)
        async def one(i):
            messages = []
            async def send(message):
                messages.append(message)
            async def receive():
                return {'type': 'http.request', 'body': b''}
            await middleware({'type': 'http', 'method': 'GET', 'path': f'/items/private-{i}',
                              'headers': [(b'x-request-id', f'request-{i}'.encode())]}, receive, send)
            self.assertEqual(dict(messages[0]['headers'])[b'x-request-id'], f'request-{i}'.encode())
        with patch('app.api.request_trace.http_metrics', metrics), patch('app.api.request_trace.log', events.append):
            await asyncio.gather(*(one(i) for i in range(40)))
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot['requests'], 40)
        self.assertEqual(snapshot['active'], 0)
        self.assertGreater(snapshot['peak_active'], 1)
        self.assertEqual(list(snapshot['routes']), ['GET /items/{id}'])
        self.assertNotIn('private-', json.dumps([snapshot, events]))
        self.assertEqual(request_id.get(), '')

    async def test_stream_failure_not_rewritten_as_second_response(self):
        metrics, events, messages = HttpMetrics(), [], []
        async def endpoint(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'data: hello\n\n', 'more_body': True})
            raise ValueError('private-provider-secret')
        async def send(message):
            messages.append(message)
        with patch('app.api.request_trace.http_metrics', metrics), patch('app.api.request_trace.log', events.append):
            with self.assertRaises(ValueError):
                await RequestTraceMiddleware(endpoint)({'type': 'http', 'method': 'GET', 'headers': []}, None, send)
        self.assertEqual(len([m for m in messages if m['type'] == 'http.response.start']), 1)
        self.assertEqual(metrics.snapshot()['interrupted'], 1)
        self.assertEqual(metrics.snapshot()['active'], 0)
        self.assertNotIn('private-provider-secret', json.dumps(events))


class ErrorChecks(TestCase):
    def test_error_headers_cors_busy_and_existing_http_errors(self):
        app = FastAPI()
        app.add_middleware(RequestTraceMiddleware)
        app.add_middleware(CORSMiddleware, allow_origins=['http://fixture'], expose_headers=['X-Request-ID'])
        @app.get('/failure')
        def failure():
            raise ValueError('private-provider-secret')
        @app.get('/busy')
        def busy():
            exc = sqlite3.OperationalError('private-db-path')
            exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise exc
        @app.get('/conflict')
        def conflict():
            raise HTTPException(409, '版本冲突')
        events = []
        with TestClient(app) as client, patch('app.api.request_trace.log', events.append):
            failed = client.get('/failure', headers={'Origin': 'http://fixture', 'X-Request-ID': 'fixture-error'})
            self.assertEqual(failed.status_code, 500)
            self.assertEqual(failed.json()['request_id'], failed.headers['x-request-id'])
            self.assertEqual(failed.headers['access-control-allow-origin'], 'http://fixture')
            busy_response = client.get('/busy')
            self.assertEqual(busy_response.status_code, 503)
            self.assertEqual(busy_response.json()['code'], 'database_busy')
            self.assertEqual(busy_response.headers['retry-after'], '1')
            conflict_response = client.get('/conflict')
            self.assertEqual(conflict_response.status_code, 409)
            self.assertEqual(conflict_response.json()['detail'], '版本冲突')
        self.assertNotIn('private-', json.dumps([failed.json(), busy_response.json(), events]))

    def test_metric_cardinality_and_snapshot_isolation(self):
        metrics = HttpMetrics()
        for i in range(200):
            metrics.begin()
            metrics.finish('GET', f'/route-{i}', 503, 200, 'completed')
        snapshot = metrics.snapshot()
        self.assertLessEqual(len(snapshot['routes']), 129)
        self.assertEqual(snapshot['server_errors'], 200)
        snapshot['routes'].clear()
        self.assertTrue(metrics.snapshot()['routes'])
