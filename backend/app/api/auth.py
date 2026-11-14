"""Central authentication and object authorization, including native SSE requests."""

import re

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from ..config import get_settings
from ..services.auth_service import COOKIE_NAME, auth_service

_PUBLIC = {"/api/auth/status", "/api/auth/login", "/api/auth/register"}
# 地图查询同样消耗服务端配额，不作为匿名代理公开。
_PRIVATE = ("/api/library", "/api/guide-sources", "/api/trip", "/api/planner-chat", "/api/admin", "/api/auth", "/api/map", "/api/poi")
_RESOURCES = (
    (re.compile(r"^/api/library/trips/([^/]+)"), "trip"),
    (re.compile(r"^/api/guide-sources/([^/]+)"), "guide"),
    (re.compile(r"^/api/trip/jobs/([^/]+)"), "job"),
)


def current_user(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path.rstrip("/")
        if request.method == "OPTIONS" or not path.startswith(_PRIVATE):
            return await call_next(request)
        if request.method not in {"GET", "HEAD"}:
            origin = request.headers.get("origin")
            if origin and origin not in get_settings().get_cors_origins_list():
                return JSONResponse({"detail": "请求来源不被允许，请检查前端访问地址配置"}, status_code=403)
        if path in _PUBLIC:
            return await call_next(request)
        user = auth_service.session_user(request.cookies.get(COOKIE_NAME))
        if not user:
            return JSONResponse({"detail": "请先登录，或重新登录已过期的会话"}, status_code=401)
        request.state.user = user
        if path.startswith("/api/admin") and user["role"] != "admin":
            return JSONResponse({"detail": "仅管理员可以访问"}, status_code=403)
        for pattern, kind in _RESOURCES:
            match = pattern.match(path)
            if match and not (kind == "guide" and match[1] == "candidates"):
                if not auth_service.can_access(user, kind, match[1]):
                    return JSONResponse({"detail": "记录不存在或无权访问"}, status_code=404)
        return await call_next(request)
