"""LLM服务模块

LLM 客户端实现已迁移至 app/infrastructure/llm_client.py（原生 OpenAI SDK），
本模块只保留工厂与单例入口，供既有调用方无缝使用。
"""

import os

from ..infrastructure.llm_client import LLMClient

# 延迟初始化，确保 .env 已加载。
_llm_instance: LLMClient | None = None


def create_llm() -> LLMClient:
    """创建独立 LLM 客户端，供并行 Agent 隔离会话与客户端状态。"""
    # temperature 固定 1.0：阿里云 MaaS 端点只允许 temperature=1；
    # 代理探测/直连流式回退逻辑在 LLMClient 内部实现。
    return LLMClient(
        temperature=1.0,
        max_tokens=int(os.getenv("LLM_MAX_TOKENS", "8192")),
    )


def get_llm() -> LLMClient:
    """获取LLM实例(单例模式)"""
    global _llm_instance

    if _llm_instance is None:
        _llm_instance = create_llm()
        print(f"✅ LLM服务初始化成功")
        print(f"   模型: {_llm_instance.model}")
        print(f"   最大输出: {_llm_instance.max_tokens} tokens")

    return _llm_instance
