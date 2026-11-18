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
from typing import Any, Dict, List, Optional

from ..config import get_settings
from .mcp_runtime import amap_runtime


class MCPToolExecutionError(ValueError):
    """工具已返回业务错误；不按会话断开自动重试，也不暴露上游正文。"""


# 当前研究流程只授权地图查询，不能仅凭上游自行声明就放行未来新增的写工具。
_READ_ONLY_MAP_TOOLS = frozenset({
    "maps_text_search", "maps_around_search", "maps_search_detail", "maps_weather",
    "maps_geo", "maps_regeocode", "maps_regeo", "maps_ip_location", "maps_distance",
    "maps_direction_walking", "maps_direction_driving", "maps_direction_bicycling",
    "maps_direction_transit_integrated", "maps_direction_walking_by_address",
    "maps_direction_driving_by_address", "maps_direction_transit_integrated_by_address",
    "maps_convert", "maps_schema_personal_map", "maps_schema_navi", "maps_schema_take_taxi",
})


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
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def submit(self, coro, timeout: float = 120.0) -> Any:
        if self._loop is None or self._loop.is_closed():
            raise RuntimeError("MCP 事件循环不可用")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            raise

    def stop(self) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
            if threading.current_thread() is not self._thread:
                self._thread.join(timeout=5)


class _StdioSession:
    """由同一个所有者任务打开和关闭 MCP 上下文，并串行执行工具调用。"""

    def __init__(self, command: List[str], env: Dict[str, str], startup_timeout=120) -> None:
        self._command, self._env = command, env
        self._startup_timeout = startup_timeout
        self._startup_lock = asyncio.Lock()
        self._owner = None
        self._ready = None
        self._requests = asyncio.Queue()
        self.session = None
        self.tools: List[Dict[str, Any]] = []

    @property
    def ready(self):
        return bool(self.tools and self._owner and not self._owner.done())

    async def ensure_started(self) -> None:
        async with self._startup_lock:
            if not self._owner or self._owner.done():
                self._ready = asyncio.get_running_loop().create_future()
                # 启动最终失败时，之前超时的等待者可能已经退出。
                self._ready.add_done_callback(lambda future: future.exception() if not future.cancelled() else None)
                self._owner = asyncio.create_task(self._serve())
            await asyncio.shield(self._ready)

    async def _serve(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        pending = None
        params = StdioServerParameters(command=self._command[0], args=self._command[1:], env=self._env)
        try:
            # 上下文必须在同一个任务中进入和退出，这是 AnyIO 的要求。
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    async with asyncio.timeout(self._startup_timeout):
                        await session.initialize()
                        result = await session.list_tools()
                    tools = [{"name": tool.name, "description": tool.description or "",
                              "input_schema": tool.inputSchema or {}} for tool in result.tools]
                    if not tools:
                        raise RuntimeError("地图服务未返回可用工具")
                    self.session, self.tools = session, tools
                    self._ready.set_result(None)
                    while True:
                        tool_name, arguments, pending = await self._requests.get()
                        if pending.cancelled():
                            continue
                        from .provider_guard import provider_guard, ProviderBusyError
                        try:
                            async with provider_guard.async_slot('amap', self._env.get('AMAP_MAPS_API_KEY', '')) as permit:
                                result = await asyncio.wait_for(session.call_tool(tool_name, arguments), 180)
                                await asyncio.to_thread(provider_guard.inspect_map, permit, result)
                        except ProviderBusyError as exc:
                            if not pending.done():
                                pending.set_exception(exc)
                            pending = None
                            continue
                        parts = [item.text for item in result.content or [] if getattr(item, "text", None)]
                        # MCP 协议里的 isError 不等于网络异常，应明确交回业务层处理。
                        if getattr(result, "isError", False):
                            if not pending.done():
                                pending.set_exception(MCPToolExecutionError("地图工具返回业务错误"))
                            pending = None
                            continue
                        if not pending.done():
                            pending.set_result("\n".join(parts))
                        pending = None
        except BaseException as exc:
            error = RuntimeError(f"地图工具会话未就绪或已中断（{type(exc).__name__}）")
            if not self._ready.done():
                self._ready.set_exception(error)
            if pending is not None and not pending.done():
                pending.set_exception(error)
            while not self._requests.empty():
                _, _, response = self._requests.get_nowait()
                if not response.done():
                    response.set_exception(error)
        finally:
            self.session = None
            self.tools = []

    async def call(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        for attempt in range(2):
            await self.ensure_started()
            response = asyncio.get_running_loop().create_future()
            self._requests.put_nowait((tool_name, arguments, response))
            try:
                return await response
            except RuntimeError as exc:
                from .provider_guard import ProviderBusyError
                if isinstance(exc, ProviderBusyError):
                    raise
                if attempt:
                    raise
                await self.close()
        raise RuntimeError("地图工具会话不可用")

    async def close(self) -> None:
        if self._owner and not self._owner.done():
            self._owner.cancel()
        if self._owner:
            await self._owner


class MCPClientManager:
    """管理一组 stdio MCP 会话，提供线程安全的同步调用。"""

    def __init__(self, command: List[str], env: Dict[str, str], pool_size: int = 4, startup_timeout: int = 120) -> None:
        self._command = command
        self._env = env
        self._pool_size = max(pool_size, 1)
        self._loop = _LoopThread()
        self._sessions: List[_StdioSession] = []
        self._lock = threading.Lock()
        self._counter = itertools.count()
        self._closed = False
        self._startup_timeout = startup_timeout

    def _get_session(self, index: int) -> _StdioSession:
        with self._lock:
            if self._closed:
                raise RuntimeError("MCP 管理器已关闭")
            while len(self._sessions) <= index:
                self._sessions.append(_StdioSession(self._command, self._env, self._startup_timeout))
            return self._sessions[index]

    def call_tool(self, tool_name: str, arguments: Dict[str, Any], session_hint: int = 0) -> str:
        index = next(self._counter) % self._pool_size if session_hint <= 0 else session_hint - 1
        session = self._get_session(index)
        from .task_trace import span
        with span('map_mcp','tool',reason=tool_name):
            return self._loop.submit(session.call(tool_name, arguments), timeout=self._startup_timeout + 185.0)

    def list_tools(self, session_hint: int = 1) -> List[Dict[str, Any]]:
        session = self._get_session(max(session_hint, 1) - 1)
        from .task_trace import span
        with span('map_mcp','initialize'):
            self._loop.submit(session.ensure_started(), timeout=self._startup_timeout + 5.0)
        return session.tools

    def warmup(self):
        # 冷启动的 uv 进程会争抢缓存锁，因此按顺序预热。
        for index in range(self._pool_size):
            tools = self.list_tools(session_hint=index + 1)
            print(f"地图工具已就绪：会话 {index + 1}/{self._pool_size}，{len(tools)} 个工具")

    def status(self):
        with self._lock:
            count = sum(session.ready for session in self._sessions)
        return {"ready": count == self._pool_size, "ready_sessions": count, "pool_size": self._pool_size}

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
            command, env = amap_runtime()
            _manager = MCPClientManager(command, env, pool_size=settings.mcp_pool_size,
                                       startup_timeout=settings.mcp_startup_timeout)
        return _manager


def amap_manager_status():
    """读取状态不会启动子进程或下载依赖包。"""
    if _manager is None:
        return {"ready": False, "ready_sessions": 0, "pool_size": get_settings().mcp_pool_size}
    return _manager.status()


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
                "read_only": tool["name"] in _READ_ONLY_MAP_TOOLS,
            })
        return tools

    def get_server_tool_name(self, expanded_name: str) -> str:
        prefix = f"{self.name}_"
        return expanded_name[len(prefix):] if expanded_name.startswith(prefix) else expanded_name

    def call_expanded(self, expanded_name: str, arguments: Dict[str, Any]) -> str:
        result = self.manager.call_tool(self.get_server_tool_name(expanded_name), arguments)
        # 部分 MCP 封装未设置 isError，而是在 JSON 正文返回失败，仍不能计作查询成功。
        import json
        try:
            payload = json.loads(result)
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict) and (str(payload.get("status")) == "0"
                                         or payload.get("success") is False or payload.get("error")):
            raise MCPToolExecutionError("地图服务未返回成功查询结果")
        return result
