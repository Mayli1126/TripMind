"""Travel advice is distinct from map identity verification."""
from typing import Literal
from pydantic import BaseModel, Field


class AttractionAdvisory(BaseModel):
    reservation_required: Literal['true', 'false', 'unknown'] = 'unknown'
    reservation_channel: str = ''
    reservation_url: str = ''
    advance_booking_days: int | None = None
    opening_hours_note: str = ''
    closure_rules: list[str] = Field(default_factory=list)
    last_entry_note: str = ''
    crowd_risk: Literal['low', 'medium', 'high', 'unknown'] = 'unknown'
    pitfall_tips: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    fact_status: Literal['verified', 'estimated', 'unverified'] = 'unverified'
    confidence: float = Field(default=0, ge=0, le=1)
    verified_at: str | None = None
    expires_at: str | None = None
    opening_checked_at: str | None = None
    opening_expires_at: str | None = None
    refresh_errors: list[str] = Field(default_factory=list)
