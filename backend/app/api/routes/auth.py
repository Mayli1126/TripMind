"""提供公开登录注册、私有会话身份与管理员概览。"""

import sqlite3

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ...config import get_settings
from ...services.auth_service import COOKIE_NAME, SESSION_SECONDS, auth_service
from ..auth import current_user

router = APIRouter(prefix="/auth", tags=["用户账号"])
admin_router = APIRouter(prefix="/admin", tags=["管理员"])


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_\-]+$")
    password: str = Field(min_length=8, max_length=128)


def set_session(response, user):
    response.set_cookie(COOKIE_NAME, auth_service.new_session(user["id"]), max_age=SESSION_SECONDS,
                        httponly=True, secure=get_settings().auth_cookie_secure, samesite="lax", path="/")


@router.get("/status")
def status():
    return {"admin_ready": auth_service.has_admin()}


@router.post("/register", status_code=201)
def register(payload: Credentials, response: Response):
    if not auth_service.has_admin():
        raise HTTPException(status_code=503, detail="管理员尚未初始化，请先在服务器创建管理员账号。")
    try:
        user = auth_service.create_user(payload.username, payload.password)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="这个用户名已经被使用") from exc
    set_session(response, user)
    return user


@router.post("/login")
def login(payload: Credentials, response: Response):
    user = auth_service.login(payload.username, payload.password)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码不正确")
    set_session(response, user)
    return user


@router.get("/me")
def me(request: Request):
    return current_user(request)


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response):
    auth_service.logout(request.cookies.get(COOKIE_NAME))
    response.delete_cookie(COOKIE_NAME, path="/")


@admin_router.get("/overview")
def overview():
    return auth_service.overview()


@admin_router.get("/runtime")
def runtime():
    from ...services.runtime_service import installed_versions, runtime_status

    return {**runtime_status(), "installed_versions": installed_versions()}


@admin_router.post("/runtime/warmup")
def warmup():
    from ...services.runtime_service import runtime_status, warmup_planning_runtime

    try:
        warmup_planning_runtime()
        if get_settings().redis_enabled:
            from ...infrastructure.redis_client import redis_available
            from ...services.planning_worker import start_worker

            if redis_available():
                start_worker()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="地图工具仍未就绪，请先在后端环境执行 prepare，再重试预热。") from exc
    return runtime_status()


@admin_router.get('/runtime/diagnostics')
def runtime_diagnostics(request: Request):
    # 除现有管理员路由中间件外，此处再进行显式校验。
    if current_user(request)['role'] != 'admin':
        raise HTTPException(403, '需要管理员权限')
    from ...services.operations_service import operational_snapshot
    return operational_snapshot()


@admin_router.get('/search/quota')
def search_quota(request: Request):
    """管理员读取搜索额度；只读本地账本，不调用百度服务。"""
    if current_user(request)['role'] != 'admin':
        raise HTTPException(403, '需要管理员权限')
    from ...services.baidu_search_service import BaiduSearchService
    return BaiduSearchService().quota_status()
