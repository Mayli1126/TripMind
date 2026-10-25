"""MCP 工具连接池 —— 基于官方 mcp SDK 的会话池（替代 hello_agents.tools.MCPTool）。

原 HelloAgents MCPTool 的职责已迁移至 app/infrastructure/mcp_client.py：
- MCPClientManager 管理长生命周期 stdio 会话池（默认 4 个）；
- MCPToolAdapter 复刻 .run({"action":"call_tool",...}) 接口与 auto_expand 子工具命名。
本模块保留原有类名与获取/归还 API，调用方无需改动。
"""

from __future__ import annotations

from ..infrastructure.mcp_client import MCPToolAdapter, get_amap_manager, shutdown_amap_manager

__all__ = ["MCPToolPool", "acquire_amap_tool", "release_amap_tool"]


class MCPToolPool:
    """管理一组可复用的 MCP 工具适配器（底层为会话池）。"""

    def get_tool(self) -> MCPToolAdapter:
        return MCPToolAdapter(get_amap_manager())

    def release_tool(self, tool: MCPToolAdapter) -> None:
        # 适配器无独立连接，会话由管理器统一持有，无需归还。
        return None

    def shutdown(self) -> None:
        shutdown_amap_manager()


def acquire_amap_tool() -> MCPToolAdapter:
    """获取一个 amap MCP 工具（从连接池）。"""
    return MCPToolPool().get_tool()


def release_amap_tool(tool: MCPToolAdapter) -> None:
    """归还 amap MCP 工具（实际为空操作，保持 API 兼容）。"""
    return None
