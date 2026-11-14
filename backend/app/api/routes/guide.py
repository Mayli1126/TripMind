"""攻略解析 API（产品化计划 12.4 节）。

    POST /api/guide-sources            提交一篇攻略并同步解析
    GET  /api/guide-sources            已导入攻略列表
    GET  /api/guide-sources/{id}       单篇解析结果（含候选地点）
    POST /api/guide-sources/{id}/retry 失败单篇重试

同步解析说明：单篇解析 = 1 次 LLM 调用 + N 次高德检索，通常 1 分钟内；
路由用同步 def，FastAPI 自动放线程池执行，不阻塞事件循环。
一个多攻略融合任务将来可引用多个 source_id（7.2 节 fan_out 设计）。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ...config import get_settings
from ...graphs.guide_fusion_graph import run_guide_fusion
from ...models.candidate import CandidatePlace
from ...models.guide import GuideInput, GuideParseResult, GuideSourceRecord
from ...services.candidate_pool_service import CandidatePool
from ...services.guide_store_service import guide_store_service
from ..auth import current_user

router = APIRouter(prefix="/guide-sources", tags=["攻略解析"])


def _enforce_guide_quota(request: Request) -> None:
    """攻略解析配额（与规划配额独立计数）：每次解析 = 1 次 LLM + 多次高德查询，
    必须与规划提交一样限流，防成本滥用。redis_enabled 时生效。"""
    settings = get_settings()
    if not settings.redis_enabled or settings.guide_quota_per_day <= 0:
        return
    from ...infrastructure import redis_client

    client_id = getattr(request.state, "user", {}).get("id") or (
        request.client.host if request.client else "anonymous"
    )
    allowed, _ = redis_client.quota_check_and_incr(
        client_id, settings.guide_quota_per_day, scope="guide"
    )
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"今日攻略解析次数已达上限({settings.guide_quota_per_day})，请明日再试",
        )


def _parse_and_save(record: GuideSourceRecord, source: GuideInput) -> GuideSourceRecord:
    """执行解析并把结果回填存储；任何异常都转为 failed 结果而非 500。"""
    try:
        parse_token = guide_store_service.begin_parse(record.id, expected_status=record.parse_status)
    except KeyError:
        raise HTTPException(404, '攻略已删除') from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    try:
        pool, results = run_guide_fusion(
            [source],
            city_hint=source.city,
            source_ids=[record.id],
        )
        result: GuideParseResult = results[0]
        result.source_id = record.id
    except Exception as exc:  # 图执行本身异常：保留记录、标记失败
        result = GuideParseResult(
            source_id=record.id,
            parse_status="failed",
            error_message=f"攻略解析异常：{exc}",
            error_hint="请稍后重试，或改用粘贴正文方式导入",
        )
    try:
        return guide_store_service.save_result(record.id, result, parse_token=parse_token) or record
    except KeyError:
        raise HTTPException(404, '攻略已删除，解析结果未重新保存') from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("", response_model=GuideSourceRecord, status_code=201)
def create_guide_source(payload: GuideInput, raw_request: Request):
    """提交一篇攻略（正文/链接），同步解析并返回候选地点。"""
    _enforce_guide_quota(raw_request)
    record = guide_store_service.create_record(payload, owner_id=current_user(raw_request)["id"])
    return _parse_and_save(record, payload)


@router.get("", response_model=list[GuideSourceRecord])
def list_guide_sources(request: Request):
    """最近导入的攻略列表（候选确认界面回显用）。"""
    return guide_store_service.list_records(owner_id=current_user(request)["id"])


@router.get("/candidates", response_model=list[CandidatePlace])
def aggregate_candidates(request: Request, city: Optional[str] = None):
    """跨来源聚合候选池（候选云端化）。

    把所有解析成功攻略的候选经 CandidatePool 合并去重后返回；
    同 candidate_id 跨设备稳定（poi_id/名称哈希），前端可用本地
    user_choice 覆盖服务端默认值。city 参数按候选城市过滤。
    """
    pool = CandidatePool()
    for record in guide_store_service.list_records(limit=200, owner_id=current_user(request)["id"]):
        result = record.result
        if record.parse_status != "parsed" or result is None:
            continue
        for candidate in result.candidates:
            if city and candidate.city and candidate.city != city:
                continue
            pool.add_or_merge(candidate)
    return pool.to_list()


@router.get("/{source_id}", response_model=GuideSourceRecord)
def get_guide_source(source_id: str):
    record = guide_store_service.get_record(source_id)
    if record is None:
        raise HTTPException(status_code=404, detail="攻略来源不存在")
    return record


@router.patch("/{source_id}/candidates/choices")
def update_candidate_choices(source_id: str, payload: CandidateChoicesPayload):
    """用户确认板的四档选择回写（融合后半的 user_select 语义）。

    选择存进该篇候选，聚合候选池与后续规划即刻反映；跨设备一致。
    """
    record = guide_store_service.get_record(source_id)
    if record is None:
        raise HTTPException(status_code=404, detail="攻略来源不存在")
    updated = guide_store_service.update_candidate_choices(source_id, payload.choices)
    if updated is None:
        raise HTTPException(status_code=404, detail="攻略来源不存在")
    return {"updated": sum(1 for c in payload.choices.values())}


class CandidateChoicesPayload(BaseModel):
    """候选选择回写载荷：candidate_id -> user_choice。"""

    choices: dict[str, str] = Field(..., max_length=100, description="candidate_id -> required/preferred/optional/excluded")


@router.delete("/{source_id}", status_code=204)
def delete_guide_source(source_id: str):
    """删除攻略及其候选与事实（候选池聚合接口不再返回其内容）。"""
    if not guide_store_service.delete_record(source_id):
        raise HTTPException(status_code=404, detail="攻略来源不存在")
    from fastapi import Response

    return Response(status_code=204)


@router.post("/{source_id}/retry", response_model=GuideSourceRecord)
def retry_guide_source(source_id: str):
    """失败单篇重试：需要原始输入可重建（正文重新粘贴的场景除外）。"""
    record = guide_store_service.get_record(source_id)
    if record is None:
        raise HTTPException(status_code=404, detail="攻略来源不存在")
    if record.parse_status != "failed":
        raise HTTPException(status_code=400, detail="仅解析失败的攻略可以重试")
    # text 类正文不落库（大文本），重试要求请求方携带正文；链接类可由 URL 重建
    if record.input_type == "text":
        raise HTTPException(
            status_code=400,
            detail="粘贴的正文未保存，请重新以正文方式提交；链接类攻略可直接重试",
        )
    source = GuideInput(
        input_type="url",
        source_url=record.source_url,
        title=record.title,
    )
    return _parse_and_save(record, source)
