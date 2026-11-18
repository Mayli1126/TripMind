"""原生 OpenAI SDK LLM 客户端（替代 HelloAgentsLLM）。

保留原 llm_service 已验证的行为：
- 从 LLM_* 环境变量读取配置；
- qwen 端点 temperature=1 约束；
- 探测客户端 max_retries=0（60s 超时立即切直连，不被 SDK 静默重试拖成 3 倍）；
- 直连强制不走系统代理，并用流式收集完整回复（读超时只作用于 chunk 间隔）；
- 代理被探测为不可用后全局直连，不再重复探测。
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
from openai import APIConnectionError, APITimeoutError, OpenAI

_RETRYABLE_CAUSES = (APITimeoutError, APIConnectionError)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, _RETRYABLE_CAUSES):
        return True
    text = str(exc).lower()
    return any(key in text for key in ("timeout", "timed out", "read timeout", "connect", "connection"))


@dataclass
class LLMResult:
    content: str
    raw: Any = field(default=None, repr=False)
    tool_calls: list[dict] = field(default_factory=list)
    finish_reason: Optional[str] = None
    reasoning_content: Optional[str] = field(default=None, repr=False)


class LLMClient:
    """OpenAI 兼容端点的同步客户端，带“代理超时后切直连”回退。"""

    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        temperature: float = 1.0,
        max_tokens: Optional[int] = None,
        timeout: Optional[int] = None,
    ) -> None:
        self.model = model or os.getenv("LLM_MODEL_ID", "")
        self.api_key = api_key or os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY", "")
        self.base_url = (
            base_url
            or os.getenv("LLM_BASE_URL")
            or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
        # 阿里云 MaaS 端点只允许 temperature=1。
        self.temperature = temperature
        self.max_tokens = max_tokens if max_tokens is not None else int(os.getenv("LLM_MAX_TOKENS", "8192"))
        full_timeout = int(timeout if timeout is not None else os.getenv("LLM_TIMEOUT", "180"))
        self.timeout = full_timeout
        probe_timeout = min(full_timeout, 60)

        # 探测客户端：跟随系统代理；max_retries=0，超时立即抛给回退逻辑。
        self._client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=probe_timeout,
            max_retries=0,
        )

        self.provider = "custom"
        self._direct_client: Optional[OpenAI] = None
        self._direct_lock = threading.Lock()
        self._proxy_broken = False

    # ------------------------------------------------------------------
    def _make_direct_client(self) -> OpenAI:
        """强制直连（忽略代理环境变量），流式收集大响应。"""
        http = httpx.Client(
            transport=httpx.HTTPTransport(proxy=None, retries=0),
            timeout=httpx.Timeout(self.timeout, connect=30, pool=30),
        )
        return OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            http_client=http,
            timeout=self.timeout,
            max_retries=0,
        )

    def _get_direct_client(self) -> OpenAI:
        with self._direct_lock:
            if self._direct_client is None:
                self._direct_client = self._make_direct_client()
            return self._direct_client

    def _direct_complete(self, messages: list[dict], **kwargs: Any) -> LLMResult:
        from .provider_guard import provider_guard
        with provider_guard.slot('llm', self.api_key, self.base_url):
            return self._direct_complete_guarded(messages, **kwargs)

    def _direct_complete_guarded(self, messages: list[dict], **kwargs: Any) -> LLMResult:
        client = self._get_direct_client()
        stream = client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=kwargs.get("temperature", self.temperature),
            max_tokens=kwargs.get("max_tokens", self.max_tokens),
            stream=True,
            **{k: v for k, v in kwargs.items() if k not in ("temperature", "max_tokens")},
        )
        parts: list[str] = []
        reasoning: list[str] = []
        calls: dict[int, dict] = {}
        finish_reason = None
        with stream:
            for chunk in stream:
                if chunk.choices:
                    choice = chunk.choices[0]
                    parts.append(choice.delta.content or "")
                    reasoning.append(getattr(choice.delta, "reasoning_content", None) or "")
                    finish_reason = choice.finish_reason or finish_reason
                    # 工具参数可能跨多个流片段，必须按 index 合并，不能只收集 content。
                    for delta in choice.delta.tool_calls or []:
                        call = calls.setdefault(delta.index, {"id": "", "type": "function",
                                                              "function": {"name": "", "arguments": ""}})
                        if delta.id:
                            # 调用 ID 是完整标识；兼容上游重复发送，但不能拼成另一个 ID。
                            if call["id"] and call["id"] != delta.id:
                                raise ValueError("流式工具调用标识发生冲突")
                            call["id"] = delta.id
                        if delta.function:
                            call["function"]["name"] += delta.function.name or ""
                            call["function"]["arguments"] += delta.function.arguments or ""
        return LLMResult(content="".join(parts), tool_calls=[calls[i] for i in sorted(calls)],
                         finish_reason=finish_reason, reasoning_content="".join(reasoning) or None)

    # ------------------------------------------------------------------
    def invoke(self, messages: list[dict], **kwargs: Any) -> str:
        """非流式调用；代理探测失败后自动切直连流式。"""
        return self.invoke_result(messages, **kwargs).content

    def invoke_result(self, messages: list[dict], **kwargs: Any) -> LLMResult:
        """需要判断截断或空回复的流程保留结束原因，普通调用仍兼容返回文本。"""
        from .task_trace import span
        with span('llm','completion',model=self.model[:100]):
            return self._invoke_untraced(messages,**kwargs)

    def invoke_result_once(self, messages: list[dict], **kwargs: Any) -> LLMResult:
        """有明确等待预算的结构化任务只请求一次，超时不再切直连重发。"""
        from .task_trace import span
        from .provider_guard import provider_guard
        client = self._get_direct_client() if self._proxy_broken else self._client
        with span('llm', 'structured_once', model=self.model[:100]):
            with provider_guard.slot('llm', self.api_key, self.base_url):
                response = client.chat.completions.create(model=self.model, messages=messages,
                    temperature=self.temperature, **kwargs)
        choice = response.choices[0]
        return LLMResult(content=choice.message.content or "", raw=response,
                         finish_reason=choice.finish_reason,
                         reasoning_content=getattr(choice.message, "reasoning_content", None))

    def invoke_tools(self, messages: list[dict], tools: list[dict], *, tool_choice="auto", **kwargs: Any) -> LLMResult:
        """原生工具调用入口，保留调用标识、参数及结束原因，不降级为文本协议。"""
        from .task_trace import span
        if not tools:
            raise ValueError("原生工具调用必须提供工具定义")
        if any(key in kwargs for key in ("tools", "stream", "n", "functions", "function_call")):
            raise ValueError("工具定义、流式模式和候选数量由工具执行器统一管理")
        with span('llm', 'function_calling', model=self.model[:100]):
            return self._invoke_untraced(messages, tools=tools, tool_choice=tool_choice, **kwargs)

    def _invoke_untraced(self,messages,**kwargs):
        if self._proxy_broken:
            return self._direct_complete(messages, **kwargs)
        try:
            from .provider_guard import provider_guard
            with provider_guard.slot('llm', self.api_key, self.base_url):
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=kwargs.get("temperature", self.temperature),
                    max_tokens=kwargs.get("max_tokens", self.max_tokens),
                    **{k: v for k, v in kwargs.items() if k not in ("temperature", "max_tokens")},
                )
                choice = response.choices[0]
                return LLMResult(content=choice.message.content or "", raw=response,
                                 tool_calls=[call.model_dump(exclude_none=True) for call in choice.message.tool_calls or []],
                                 finish_reason=choice.finish_reason,
                                 reasoning_content=getattr(choice.message, "reasoning_content", None))
        except Exception as exc:
            if _is_retryable(exc):
                from .task_trace import emit
                emit('llm','provider_retry','retrying',reason='switch_direct',error_type=type(exc).__name__)
                self._proxy_broken = True
                return self._direct_complete(messages, **kwargs)
            raise

    # 兼容旧调用名
    def complete(self, messages: list[dict], **kwargs: Any) -> str:
        return self.invoke(messages, **kwargs)

    def shutdown(self) -> None:  # 预留：统一资源清理入口
        with self._direct_lock:
            self._direct_client = None
            self._proxy_broken = False
