"""追踪完整 HTTP 请求生命周期，不记录私密请求内容或异常正文。"""
import re
import sqlite3
import time
from uuid import uuid4
from pathlib import Path
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from ..infrastructure.task_trace import request_id, log
from ..services.http_metrics import http_metrics


class RequestTraceMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        supplied = Headers(scope=scope).get('x-request-id', '')
        trace = supplied if re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', supplied) else uuid4().hex
        token = request_id.set(trace)
        scope.setdefault('state', {})['request_id'] = trace
        started = time.monotonic()
        status, headers_sent, complete, error_type = 500, False, False, None
        error_location = None
        http_metrics.begin()

        async def traced_send(message):
            nonlocal status, headers_sent, complete
            if message['type'] == 'http.response.start':
                status = message['status']
                MutableHeaders(scope=message)['X-Request-ID'] = trace
                headers_sent = True
            await send(message)
            if message['type'] == 'http.response.body' and not message.get('more_body', False):
                complete = True

        try:
            await self.app(scope, receive, traced_send)
        except Exception as exc:
            error_type = type(exc).__name__
            frame = exc.__traceback__
            while frame and frame.tb_next:
                frame = frame.tb_next
            if frame:
                error_location = f'{Path(frame.tb_frame.f_code.co_filename).name}:{frame.tb_lineno}'
            if headers_sent:
                # SSE 流已经开始后不能再次发送响应，否则会破坏数据流。
                raise
            busy = (isinstance(exc, sqlite3.OperationalError) and
                    (getattr(exc, 'sqlite_errorcode', 0) & 255) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})
            response = JSONResponse(
                {'detail': '数据正在更新，请稍后重试' if busy else '服务暂时出现问题，请凭请求编号联系管理员',
                 'code': 'database_busy' if busy else 'internal_error', 'request_id': trace},
                status_code=503 if busy else 500, headers={'Retry-After': '1'} if busy else None)
            await response(scope, receive, traced_send)
        finally:
            elapsed = round((time.monotonic() - started) * 1000)
            outcome = 'completed' if complete and not error_type else 'interrupted'
            route = getattr(scope.get('route'), 'path', 'unmatched')
            method = scope.get('method', 'OTHER')
            if method not in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'}:
                method = 'OTHER'
            http_metrics.finish(method, route, status, elapsed, outcome)
            log({'service': 'http', 'request_id': trace, 'method': method,
                 'route': route, 'http_status': status, 'duration_ms': elapsed, 'outcome': outcome,
                 'error_type': error_type, 'error_location': error_location})
            request_id.reset(token)
