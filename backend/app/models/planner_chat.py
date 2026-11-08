"""Conversational trip setup; final requests reuse the existing planner contract."""

from typing import Any, Literal

from pydantic import BaseModel, Field

from .schemas import TripRequest


class PlannerChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=6000)


class PlannerChatRequest(BaseModel):
    conversation_id: str | None = Field(default=None, max_length=100)
    messages: list[PlannerChatMessage] = Field(min_length=1, max_length=40)
    draft: dict[str, Any] = Field(default_factory=dict)


class PlannerChatResponse(BaseModel):
    conversation_summary: dict[str, Any] | None = None
    reply: str
    suggestions: list[str] = Field(default_factory=list)
    draft: dict[str, Any]
    ready: bool = False
    request: TripRequest | None = None
