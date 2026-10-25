"""官方 mcp SDK 的 stdio 会话管理（替代 hello_agents.tools.MCPTool）。

设计：
- 后台事件循环线程承载所有异步 MCP 调用，业务线程用同步 API；
- 会话池复用 stdio 子进程（uvx amap-mcp-server），默认 4 个，匹配现有并行度；
- 每个会话同一时刻只执行一个调用（stdio 消息按请求 id 配对，串行最稳妥）；
- MCPToolAdapter 复刻 HelloAgents MCPTool 的对外接口：
  .run({"action":"call_tool","tool_name":...,"arguments":...}) 返回文本；
  子工具命名为 `{name}_{server_tool}`（与原 auto_expand 行为一致，如 amap_maps_text_search）。
"""

from __future__ import annotations

import asyncio
import itertools
import threading
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional

from ..config import get_settings


class _LoopThread:
    """承载 MCP 异步调用的后台事件循环。"""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._started = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="mcp-loop")
        self._thread.start()
        self._started.wait(timeout=10)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._started.set()
        self._loop.run_forever()

    def submit(self, coro, timeout: float = 120.0) -> Any:
        if self._loop is None or self._loop.is_closed():
            raise RuntimeError("MCP 事件循环不可用")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def stop(self) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)


class _StdioSession:
    """单个长生命周期 stdio MCP 会话。"""

    def __init__(self, command: List[str], env: Dict[str, str]) -> None:
        self._command = command
        self._env = env
        self._lock = asyncio.Lock()
        self._contexts: tuple = ()
        self.session = None
        self.tools: List[Dict[str, Any]] = []

    async def ensure_started(self) -> None:
        if self.session is not None:
            return
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(command=self._command[0], args=self._command[1:], env=self._env)
        # 上下文必须保持打开状态，退出时逆序关闭。
        stack = AsyncExitStack()
        read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()
        self._contexts = (stack,)
        self.session = session
        await self._refresh_tools()

    async def _refresh_tools(self) -> None:
        result = await self.session.list_tools()
        self.tools = [
            {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.inputSchema or {},
            }
            for tool in result.tools
        ]

    async def call(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        async with self._lock:
            await self.ensure_started()
            try:
                result = await self.session.call_tool(tool_name, arguments)
            except Exception:
                # 会话可能已失效：关闭并重建一次。
                await self.close()
                await self.ensure_started()
                result = await self.session.call_tool(tool_name, arguments)
        parts: list[str] = []
        for item in result.content or []:
            text = getattr(item, "text", None)
            if text:
                parts.append(text)
        return "\n".join(parts) if parts else ""

    async def close(self) -> None:
        if self.session is None:
            return
        self.session = None
        self.tools = []
        stack, self._contexts = self._contexts, ()
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:
                pass


class MCPClientManager:
    """管理一组 stdio MCP 会话，提供线程安全的同步调用。"""

    def __init__(self, command: List[str], env: Dict[str, str], pool_size: int = 4) -> None:
        self._command = command
        self._env = env
        self._pool_size = max(pool_size, 1)
        self._loop = _LoopThread()
        self._sessions: List[_StdioSession] = []
        self._lock = threading.Lock()
        self._counter = itertools.count()
        self._closed = False

    def _get_session(self, index: int) -> _StdioSession:
        with self._lock:
            if self._closed:
                raise RuntimeError("MCP 管理器已关闭")
            while len(self._sessions) <= index:
                self._sessions.append(_StdioSession(self._command, self._env))
            return self._sessions[index]

    def call_tool(self, tool_name: str, arguments: Dict[str, Any], session_hint: int = 0) -> str:
        index = next(self._counter) % self._pool_size if session_hint <= 0 else session_hint - 1
        session = self._get_session(index)
        return self._loop.submit(session.call(tool_name, arguments), timeout=180.0)

    def list_tools(self, session_hint: int = 1) -> List[Dict[str, Any]]:
        session = self._get_session(max(session_hint, 1) - 1)
        self._loop.submit(session.ensure_started(), timeout=120.0)
        return session.tools

    def stop(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            sessions, self._sessions = self._sessions, []
        for session in sessions:
            try:
                self._loop.submit(session.close(), timeout=15.0)
            except Exception:
                pass
        self._loop.stop()


_manager: Optional[MCPClientManager] = None
_manager_lock = threading.Lock()


def get_amap_manager() -> MCPClientManager:
    """获取 amap-mcp-server 会话管理器单例。"""
    global _manager
    with _manager_lock:
        if _manager is None:
            settings = get_settings()
            if not settings.amap_api_key:
                raise ValueError("高德地图API Key未配置,请在.env文件中设置AMAP_API_KEY")
            # 锁版本原因见原 mcp_tool_pool 注释：
            # mcp<2（FastMCP 改名）、pydantic 2.13.x（eval_type_backport 被删）。
            _manager = MCPClientManager(
                command=[
                    "uvx",
                    "--with", "mcp>=1.28,<2",
                    "--with", "pydantic>=2.13,<2.14",
                    "amap-mcp-server",
                ],
                env={"AMAP_MAPS_API_KEY": settings.amap_api_key},
                pool_size=4,
            )
        return _manager


def shutdown_amap_manager() -> None:
    global _manager
    with _manager_lock:
        if _manager is not None:
            _manager.stop()
            _manager = None


class MCPToolAdapter:
    """复刻 HelloAgents MCPTool 的对外接口（供 amap_service 与 SimpleAgent 使用）。"""

    def __init__(self, manager: MCPClientManager, name: str = "amap") -> None:
        self.manager = manager
        self.name = name
        self.description = "高德地图服务,支持POI搜索、路线规划、天气查询等功能"
        self.expandable = True
        self._tools_cache: Optional[List[Dict[str, Any]]] = None

    # -- 原 MCPTool.run 兼容入口 ----------------------------------------
    def run(self, payload: Dict[str, Any]) -> str:
        action = payload.get("action", "call_tool")
        if action != "call_tool":
            raise ValueError(f"不支持的 MCP action: {action}")
        tool_name = payload.get("tool_name")
        if not tool_name:
            raise ValueError("缺少 tool_name")
        arguments = payload.get("arguments") or {}
        if isinstance(arguments, str):
            arguments = {"keywords": arguments}
        return self.manager.call_tool(tool_name, arguments)

    # -- 展开子工具（与原 auto_expand 行为一致） --------------------------
    def _ensure_tools(self) -> List[Dict[str, Any]]:
        if self._tools_cache is None:
            self._tools_cache = self.manager.list_tools()
        return self._tools_cache

    def expanded_tools(self) -> List[Dict[str, Any]]:
        """返回展开后的子工具描述：{name, description, schema}。"""
        tools = []
        for tool in self._ensure_tools():
            tools.append({
                "name": f"{self.name}_{tool['name']}",
                "server_tool": tool["name"],
                "description": tool["description"],
                "schema": tool["input_schema"],
            })
        return tools

    def get_server_tool_name(self, expanded_name: str) -> str:
        prefix = f"{self.name}_"
        return expanded_name[len(prefix):] if expanded_name.startswith(prefix) else expanded_name

    def param_types(self, expanded_name: str) -> Dict[str, str]:
        """从 inputSchema 提取参数类型，供 [TOOL_CALL] 参数做类型转换。"""
        server_tool = self.get_server_tool_name(expanded_name)
        for tool in self._ensure_tools():
            if tool["name"] == server_tool:
                properties = (tool.get("input_schema") or {}).get("properties", {})
                return {key: str(info.get("type", "string")) for key, info in properties.items()}
        return {}

    def call_expanded(self, expanded_name: str, arguments: Dict[str, Any]) -> str:
        return self.manager.call_tool(self.get_server_tool_name(expanded_name), arguments)
