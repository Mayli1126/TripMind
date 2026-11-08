"""Version-bound itinerary conversation; answers never mutate a saved trip."""

from pydantic import BaseModel, Field
from .planner_chat import PlannerChatMessage


class AssistantRequest(BaseModel):
    conversation_id: str | None = Field(default=None, max_length=100)
    base_version: int = Field(ge=1)
    messages: list[PlannerChatMessage] = Field(min_length=1, max_length=16)


class AssistantEvidence(BaseModel):
    path: str
    value: str


class AssistantResponse(BaseModel):
    conversation_summary: dict | None = None
    answer: str = Field(min_length=1, max_length=6000)
    evidence: list[AssistantEvidence] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    proposed_instruction: str | None = None
    can_apply: bool = False
    base_version: int


class AssistantRevisionRequest(BaseModel):
    base_version: int = Field(ge=1)
    instruction: str = Field(min_length=2, max_length=1000)
