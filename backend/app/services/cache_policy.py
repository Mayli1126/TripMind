"""Public fact cache policies. These windows are references, not freshness guarantees."""
from dataclasses import dataclass
import math
import random
import time
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, model_validator

CacheKind = Literal['poi', 'poi_detail', 'around', 'walking_route', 'transit_route',
                    'driving_route', 'weather', 'opening_hours', 'official_policy']


@dataclass(frozen=True)
class CachePolicy:
    fresh_seconds: int
    hard_seconds: int
    allow_stale: bool = False
    negative_seconds: int = 20


POLICIES: dict[str, CachePolicy] = {
    'poi': CachePolicy(86400, 604800, True),
    'poi_detail': CachePolicy(86400, 604800, True),
    'around': CachePolicy(86400, 604800, True),
    'walking_route': CachePolicy(3600, 21600, True),
    'transit_route': CachePolicy(300, 900),
    'driving_route': CachePolicy(300, 900),
    'weather': CachePolicy(600, 1800),
    'opening_hours': CachePolicy(3600, 21600, True),
    'official_policy': CachePolicy(3600, 21600),
}


class CacheEnvelope(BaseModel):
    model_config = ConfigDict(extra='forbid')
    schema_version: Literal[1] = 1
    outcome: Literal['success', 'empty']
    data: Any
    source: str
    fetched_at: float
    fresh_until: float
    hard_until: float

    @model_validator(mode='after')
    def validate_window(self):
        if not self.source.strip() or len(self.source) > 200:
            raise ValueError('Cache source must be a short provider identifier')
        if not all(math.isfinite(v) for v in (self.fetched_at, self.fresh_until, self.hard_until)):
            raise ValueError('Non-finite cache timestamp')
        if not self.fetched_at <= self.fresh_until <= self.hard_until:
            raise ValueError('Invalid absolute freshness window')
        if self.outcome == 'empty' and self.data not in (None, [], {}):
            raise ValueError('Negative cache must contain an actual empty result')
        return self


def make_entry(kind: CacheKind, data: Any, source: str, *, empty: bool = False,
               fetched_at: float | None = None, source_expires_at: float | None = None) -> CacheEnvelope:
    """Call only after a successful provider response; never pass transport failures here."""
    policy = POLICIES[kind]
    fetched = time.time() if fetched_at is None else fetched_at
    if kind == 'official_policy' and source_expires_at is None:
        raise ValueError('Official policy requires a provider expiry')
    if source_expires_at is not None and not math.isfinite(source_expires_at):
        raise ValueError('Invalid provider expiry')
    hard = fetched + (policy.negative_seconds if empty else policy.hard_seconds)
    if source_expires_at is not None:
        hard = min(hard, source_expires_at)
    if hard <= fetched:
        raise ValueError('Provider fact already expired')
    # Jitter only the soft refresh point, never extend a provider's hard expiry.
    fresh = hard if empty else min(hard, fetched + policy.fresh_seconds * random.uniform(.9, 1))
    return CacheEnvelope(outcome='empty' if empty else 'success', data=data, source=source,
                         fetched_at=fetched, fresh_until=fresh, hard_until=hard)
