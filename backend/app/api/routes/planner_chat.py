"""Conversational planning entry point; no itinerary is created until confirmed."""

import logging

from fastapi import APIRouter, HTTPException, Request
from openai import APIConnectionError, APIStatusError, APITimeoutError

from ...config import get_settings
from ...models.planner_chat import PlannerChatRequest, PlannerChatResponse
from ...services.planner_chat_service import ChatResponseError, chat_about_trip

router = APIRouter(prefix="/planner-chat", tags=["对话规划"])
logger = logging.getLogger(__name__)


@router.post("/messages", response_model=PlannerChatResponse)
def send_message(payload: PlannerChatRequest, request: Request):
    settings = get_settings()
    if settings.redis_enabled and settings.chat_quota_per_day > 0:
        from ...infrastructure import redis_client

        client_id = request.state.user["id"]
        allowed, _ = redis_client.quota_check_and_incr(client_id, settings.chat_quota_per_day, scope="chat")
        if not allowed:
            raise HTTPException(status_code=429, detail="今日对话次数已达上限，请明日再试。")
    from ...services.conversation_summary_service import summary_for
    summary_context, summary = summary_for(request.state.user['id'], payload.conversation_id, 'planner')
    try:
        from ...services.conversation_store import memory_context
        result = chat_about_trip(payload, memory_context(request.state.user['id'], payload.draft.get('city', '')), summary_context)
        result.conversation_summary = summary
        if result.request:
            result.request.memory_context = memory_context(request.state.user['id'], result.request.city)
            result.request.conversation_id = payload.conversation_id
            result.request.conversation_context = summary_context[:4200]
        return result
    except Exception as exc:
        # Do not log exception text/traceback: provider errors may include keys or conversation text.
        category = 'internal'
        detail = '这次没能整理好你的需求，消息还在，请稍后点“再试一次”。'
        status = 502
        if isinstance(exc, ChatResponseError):
            category = 'invalid_model_response'
            detail = '模型暂时没返回可读取的内容，不是你的回答方式有问题。消息还在，请点“再试一次”。'
        elif isinstance(exc, APITimeoutError):
            category, status = 'model_timeout', 504
            detail = '模型响应超时，消息还在，请稍后点“再试一次”。'
        elif isinstance(exc, APIConnectionError):
            category = 'model_connection'
            detail = '暂时连接不上模型服务，消息还在，请稍后重试。'
        elif isinstance(exc, APIStatusError):
            category = 'model_http_error'
            detail = '模型服务暂时拒绝了请求，请检查服务额度、权限或稍后重试。'
        logger.error('planner_chat failed category=%s type=%s upstream_status=%s',
                     category, type(exc).__name__,
                     exc.status_code if isinstance(exc, APIStatusError) else None)
        raise HTTPException(status_code=status, detail=detail) from None
