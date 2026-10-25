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
        for chunk in stream:
            if chunk.choices:
                parts.append(chunk.choices[0].delta.content or "")
        return LLMResult(content="".join(parts))

    # ------------------------------------------------------------------
    def invoke(self, messages: list[dict], **kwargs: Any) -> str:
        """非流式调用；代理探测失败后自动切直连流式。"""
        if self._proxy_broken:
            return self._direct_complete(messages, **kwargs).content
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=kwargs.get("temperature", self.temperature),
                max_tokens=kwargs.get("max_tokens", self.max_tokens),
                **{k: v for k, v in kwargs.items() if k not in ("temperature", "max_tokens")},
            )
            return response.choices[0].message.content or ""
        except Exception as exc:
            if _is_retryable(exc):
                self._proxy_broken = True
                return self._direct_complete(messages, **kwargs).content
            raise

    # 兼容旧调用名
    def complete(self, messages: list[dict], **kwargs: Any) -> str:
        return self.invoke(messages, **kwargs)

    def shutdown(self) -> None:  # 预留：统一资源清理入口
        with self._direct_lock:
            self._direct_client = None
            self._proxy_broken = False
