"""Explicit deployment commands: prepare tool cache, or diagnose without downloading."""

import argparse
import json
import sys

from ..config import get_settings
from ..infrastructure.mcp_client import MCPClientManager
from ..infrastructure.mcp_runtime import amap_runtime, MCP_VERSION, PYDANTIC_VERSION
from .runtime_service import installed_versions


def prepare(timeout):
    settings = get_settings()
    if not settings.amap_api_key or settings.amap_api_key.startswith("your_"):
        raise RuntimeError("请先在 backend/.env 配置高德 Web 服务 Key。")
    # Download with a single process, then verify the same command works offline.
    for allow_install, pool_size in ((True, 1), (False, settings.mcp_pool_size)):
        print("准备固定版本地图工具（此步骤允许下载）" if allow_install else "确认离线缓存与所有地图会话")
        command, env = amap_runtime(allow_install=allow_install)
        manager = MCPClientManager(command, env, pool_size=pool_size, startup_timeout=timeout)
        try:
            manager.warmup()
        finally:
            manager.stop()
    print("地图工具缓存准备完成。请用相同用户、Python 环境和 UV_CACHE_DIR 启动后端。")


def doctor():
    versions = installed_versions()
    print(json.dumps({"python": sys.version.split()[0], "versions": versions,
                      "expected_mcp": MCP_VERSION, "expected_pydantic": PYDANTIC_VERSION}, ensure_ascii=False, indent=2))
    ok = versions["mcp"] == MCP_VERSION and versions["pydantic"] == PYDANTIC_VERSION and bool(versions["uv"])
    settings = get_settings()
    key_configured = bool(settings.amap_api_key and not settings.amap_api_key.startswith("your_") )
    print("高德 Key：已配置" if key_configured else "高德 Key：未配置")
    ok = ok and key_configured
    if settings.redis_enabled:
        from ..infrastructure.redis_client import _get_client

        try:
            client = _get_client()
            client.ping()
            modules = {str(item.get("name", "")) for item in client.module_list()}
            redis_ok = {"search", "ReJSON"}.issubset(modules)
            print("Redis Stack：模块齐全" if redis_ok else "Redis 可连接，但缺少 Search/JSON 模块")
            ok = ok and redis_ok
        except Exception:
            print("Redis：连接失败，请检查服务与 REDIS_URL（此命令不打印连接凭证）")
            ok = False
    print("doctor 不会下载包、启动地图工具或调用模型；地图握手请使用 prepare。")
    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "doctor"])
    parser.add_argument("--timeout", type=int, default=600, help="每个地图会话的安装/握手等待秒数")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare(args.timeout)
        elif not doctor():
            return 1
    except Exception as exc:
        print(f"准备未完成（{type(exc).__name__}）。请查看上方安装日志，确认镜像和网络后重试。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
