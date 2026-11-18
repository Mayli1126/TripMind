"""执行有时间限制的真实 MCP 握手检查，不下载依赖包或调用模型。"""
from app.infrastructure.mcp_runtime import amap_runtime
from app.infrastructure.mcp_client import MCPClientManager


def main():
    command, env = amap_runtime(allow_install=False)
    manager = MCPClientManager(command, env, pool_size=1, startup_timeout=20)
    try:
        manager.warmup()
        print('MCP_OFFLINE_HANDSHAKE_OK')
        return 0
    except Exception as error:
        print(f'MCP_OFFLINE_HANDSHAKE_FAILED: {type(error).__name__}')
        return 1
    finally:
        manager.stop()


if __name__ == '__main__':
    raise SystemExit(main())
