"""Planning readiness is distinct from HTTP process liveness."""

import os
import time
from importlib import metadata
from threading import Lock

from fastapi import HTTPException

from ..config import get_settings
from ..infrastructure.mcp_client import amap_manager_status, get_amap_manager
from ..infrastructure.mcp_runtime import AMAP_SERVER_VERSION, MCP_VERSION, PYDANTIC_VERSION

_warmup_lock = Lock()
_state = {"phase": "not_started", "duration_ms": None, "error": ""}


def warmup_planning_runtime():
    if not _warmup_lock.acquire(blocking=False):
        raise RuntimeError("规划服务正在准备中")
    started = time.monotonic()
    _state.update(phase="preparing", error="")
    try:
        get_amap_manager().warmup()
        _state.update(phase="ready", error="")
    except Exception as exc:
        _state.update(phase="failed", error="地图工具尚未就绪，请在同一运行环境执行 python -m app.services.mcp_setup prepare 后重新预热。")
        print(_state["error"])
        print(f"地图预热失败类型：{type(exc).__name__}（详细安装/启动日志见上方子进程输出）")
        raise
    finally:
        _state["duration_ms"] = round((time.monotonic() - started) * 1000)
        _warmup_lock.release()
    return runtime_status()


def runtime_status():
    settings = get_settings()
    mcp = amap_manager_status()
    from .planning_worker import worker_status

    worker = worker_status()
    from ..infrastructure.shared_cache import shared_cache
    from ..infrastructure.provider_guard import provider_guard
    llm_configured = bool((os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY"))
                          and (os.getenv("LLM_PLANNER_MODEL") or os.getenv("LLM_MODEL_ID") or os.getenv("OPENAI_MODEL")))
    ready = mcp["ready"] and llm_configured and (not settings.redis_enabled or worker["running"])
    return {
        "ready": ready, "phase": _state["phase"], "duration_ms": _state["duration_ms"],
        "error": _state["error"], "mcp": mcp, "llm_configured": llm_configured,
        "redis_enabled": settings.redis_enabled, "worker": worker,
        "shared_cache": shared_cache.status(),
        "provider_guard": provider_guard.status(),
        "job_admission": dict(user_limit=settings.job_user_active_limit,
            global_limit=settings.job_global_active_limit, capacity=settings.job_queue_capacity,
            max_queue_wait_seconds=settings.job_max_queue_wait_seconds,
            max_age_seconds=settings.job_admission_max_age_seconds,
            scope='distributed' if settings.redis_enabled else 'local_process_only'),
        "mcp_offline": True,
        "tool_versions": {"amap-mcp-server": AMAP_SERVER_VERSION, "mcp": MCP_VERSION, "pydantic": PYDANTIC_VERSION},
    }


def require_planning_runtime():
    if get_settings().mcp_warmup_on_startup and not runtime_status()["ready"]:
        raise HTTPException(status_code=503, detail="规划服务尚未就绪，请稍后重试或联系管理员检查服务状态。")


def installed_versions():
    names = ("langgraph", "langgraph-checkpoint-redis", "redisvl", "redis", "mcp", "pydantic", "uv")
    result = {}
    for name in names:
        try:
            result[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            result[name] = None
    return result
