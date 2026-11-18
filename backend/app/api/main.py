"""FastAPI主应用"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from ..config import get_settings, validate_config, print_config
from .routes import trip, poi, library, map as map_routes, guide, planner_chat
from ..services.planning_job_service import planning_job_service
from ..services.auth_service import auth_service
from .auth import AuthMiddleware
from .routes import auth
from .routes import conversations
from ..services.runtime_service import runtime_status, warmup_planning_runtime

# 获取配置
settings = get_settings()

# 创建FastAPI应用
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="基于HelloAgents框架的智能旅行规划助手API",
    docs_url="/docs",
    redoc_url="/redoc"
)

# 配置CORS
app.add_middleware(AuthMiddleware)
from .request_trace import RequestTraceMiddleware
app.add_middleware(RequestTraceMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get_cors_origins_list(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=['X-Request-ID'],
)

# 注册路由
app.include_router(trip.router, prefix="/api")
app.include_router(poi.router, prefix="/api")
app.include_router(map_routes.router, prefix="/api")
app.include_router(library.router, prefix="/api")
app.include_router(guide.router, prefix="/api")
app.include_router(planner_chat.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(conversations.router, prefix="/api")
app.include_router(auth.admin_router, prefix="/api")


@app.on_event("startup")
async def startup_event():
    """应用启动事件"""
    auth_service.initialize()
    from ..services.job_submission_service import initialize as initialize_submissions
    initialize_submissions()
    from ..services.conversation_store import initialize
    initialize()
    from ..services.advisory_refresh_service import initialize as initialize_advisories
    initialize_advisories()
    if not auth_service.has_admin():
        print("账号系统已启用，请先运行 python -m app.services.auth_service create-admin --username admin 创建管理员。")
    print("\n" + "="*60)
    print(f"🚀 {settings.app_name} v{settings.app_version}")
    print("="*60)
    
    # 打印配置信息
    print_config()
    
    # 验证配置
    try:
        validate_config()
        print("\n✅ 配置验证通过")
    except ValueError as e:
        print(f"\n❌ 配置验证失败:\n{e}")
        print("\n请检查.env文件并确保所有必要的配置项都已设置")
        raise
    
    print("\n" + "="*60)
    print("📚 API文档: http://localhost:8000/docs")
    print("📖 ReDoc文档: http://localhost:8000/redoc")
    print("="*60 + "\n")

    maps_ready = True
    if settings.mcp_warmup_on_startup:
        print("正在离线预热地图工具；HTTP 进程启动不等于地图工具已就绪。")
        try:
            await run_in_threadpool(warmup_planning_runtime)
        except Exception:
            maps_ready = False
            print("地图预热未完成：账号和历史行程仍可访问，暂不启动规划 Worker。")
    # 启动准备完成后再启动任务执行器，避免排队任务与工具安装争抢资源。
    if settings.redis_enabled and maps_ready:
        from ..infrastructure import redis_client
        from ..services.planning_worker import start_worker
        if redis_client.redis_available():
            start_worker()
        else:
            print("⚠️ Redis 不可达，规划暂不可用；请恢复 Redis，或显式设置 REDIS_ENABLED=false 后重启。")


@app.on_event("shutdown")
async def shutdown_event():
    """应用关闭事件"""
    print("\n" + "="*60)
    print("👋 应用正在关闭...")
    print("="*60 + "\n")
    if settings.redis_enabled:
        from ..services.planning_worker import stop_worker
        stop_worker()
    planning_job_service.shutdown()
    from ..services.job_process import isolated_executor
    isolated_executor.stop()
    from ..infrastructure.shared_cache import shared_cache
    shared_cache.close()
    from ..infrastructure.provider_guard import provider_guard
    provider_guard.close()
    from ..infrastructure.mcp_client import shutdown_amap_manager
    await run_in_threadpool(shutdown_amap_manager)


@app.get("/")
async def root():
    """根路径"""
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "status": "running",
        "docs": "/docs",
        "redoc": "/redoc"
    }


@app.get("/health")
async def health():
    """健康检查"""
    return {
        "status": "healthy",
        "service": settings.app_name,
        "version": settings.app_version
    }


@app.get("/health/ready")
async def readiness():
    state = runtime_status()
    return JSONResponse({"ready": state["ready"], "maps_ready": state["mcp"]["ready"],
                         "worker_running": state["worker"]["running"]}, status_code=200 if state["ready"] else 503)


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "app.api.main:app",
        host=settings.host,
        port=settings.port,
        reload=True
    )
