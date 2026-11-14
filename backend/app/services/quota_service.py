"""账号配额规则，与 HTTP 路由解耦。"""

from fastapi import HTTPException

from ..config import get_settings


def enforce_planning_quota(owner_id: str) -> None:
    """新建规划、修改和核验共享每日配额；复用已有任务不重复扣减。"""
    settings = get_settings()
    if not settings.redis_enabled or settings.plan_quota_per_day <= 0:
        return
    from ..infrastructure import redis_client

    allowed, _ = redis_client.quota_check_and_incr(owner_id, settings.plan_quota_per_day)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"今日提交次数已达上限({settings.plan_quota_per_day})，请明日再试",
        )
