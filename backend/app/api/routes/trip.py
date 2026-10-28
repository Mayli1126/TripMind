"""旅行规划API路由"""

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool
from ...models.schemas import (
    TripRequest,
    TripPlanResponse,
    TransportationRequest,
    TransportationResponse,
    PlanningJobResponse,
    TripRevisionRequest,
    JobResumeRequest,
)
from ...agents.trip_planner_agent import get_trip_planner_agent
from ...config import get_settings
from ...services.amap_service import get_amap_service


def _enforce_quota(request: Request) -> None:
    """设备级每日提交配额（redis_enabled 时生效）。超限返回 429。"""
    settings = get_settings()
    if not settings.redis_enabled or settings.plan_quota_per_day <= 0:
        return
    from ...infrastructure import redis_client

    client_id = request.headers.get("X-Client-Id") or (
        request.client.host if request.client else "anonymous"
    )
    allowed, used = redis_client.quota_check_and_incr(
        client_id, settings.plan_quota_per_day
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"今日提交次数已达上限({settings.plan_quota_per_day})，请明日再试",
        )
from ...services.planning_job_service import planning_job_service

router = APIRouter(prefix="/trip", tags=["旅行规划"])


@router.post(
    "/jobs",
    response_model=PlanningJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="创建异步旅行规划任务",
)
async def create_planning_job(request: TripRequest, raw_request: Request):
    _enforce_quota(raw_request)
    return planning_job_service.create_plan(request)


@router.post(
    "/revision-jobs",
    response_model=PlanningJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="创建自然语言行程修改任务",
)
async def create_revision_job(request: TripRevisionRequest, raw_request: Request):
    _enforce_quota(raw_request)
    return planning_job_service.create_revision(
        request.trip_request,
        request.plan,
        request.instruction,
        base_version=request.base_version,
    )


@router.get(
    "/jobs/{job_id}",
    response_model=PlanningJobResponse,
    summary="查询规划任务状态",
)
async def get_planning_job(job_id: str):
    try:
        return planning_job_service.get(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="规划任务不存在或已过期") from exc


@router.post(
    "/jobs/{job_id}/resume",
    response_model=PlanningJobResponse,
    summary="恢复等待用户确认的规划任务（interrupt）",
)
async def resume_planning_job(job_id: str, body: JobResumeRequest):
    """对 waiting_input 状态的任务提交用户选择，Worker 从 Checkpointer 断点继续。"""
    settings = get_settings()
    if not settings.redis_enabled:
        raise HTTPException(status_code=400, detail="恢复功能需要 Redis 队列启用")
    from ...infrastructure import redis_client

    if not redis_client.redis_available():
        raise HTTPException(status_code=503, detail="Redis 不可用，恢复功能暂不可用")
    try:
        current = planning_job_service.get(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="规划任务不存在或已过期") from exc
    if current.status != "waiting_input":
        raise HTTPException(
            status_code=409,
            detail=f"任务当前状态为 {current.status}，仅 waiting_input 状态可恢复",
        )
    try:
        stored_request = planning_job_service.get_stored_request(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务缺少原始请求，无法恢复") from exc

    planning_job_service.mark_resumed(job_id)
    redis_client.enqueue_job(
        job_id,
        "resume",
        {
            "request": stored_request.model_dump(mode="json"),
            "resume_value": body.resume_value,
        },
    )
    return planning_job_service.get(job_id)


@router.get("/jobs/{job_id}/events", summary="订阅规划任务进度")
async def stream_planning_events(job_id: str, request: Request):
    try:
        planning_job_service.get(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="规划任务不存在或已过期") from exc

    async def event_generator():
        last_event_id = request.headers.get("last-event-id", "").strip()
        try:
            sequence = max(0, int(last_event_id) + 1) if last_event_id else 0
        except ValueError:
            sequence = 0
        yield "retry: 1000\n\n"
        while True:
            if await request.is_disconnected():
                break
            try:
                events = planning_job_service.events_since(job_id, sequence)
                current = planning_job_service.get(job_id)
            except KeyError:
                break
            for event in events:
                sequence = event["sequence"] + 1
                payload = json.dumps(event, ensure_ascii=False)
                yield f'id: {event["sequence"]}\nevent: progress\ndata: {payload}\n\n'
            if current.status in {"completed", "failed"} and not events:
                break
            await asyncio.sleep(0.25)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/plan",
    response_model=TripPlanResponse,
    summary="生成旅行计划",
    description="根据用户输入的旅行需求,生成详细的旅行计划"
)
async def plan_trip(request: TripRequest):
    """
    生成旅行计划

    Args:
        request: 旅行请求参数

    Returns:
        旅行计划响应
    """
    try:
        print(f"\n{'='*60}")
        print(f"📥 收到旅行规划请求:")
        print(f"   城市: {request.city}")
        print(f"   日期: {request.start_date} - {request.end_date}")
        print(f"   天数: {request.travel_days}")
        print(f"{'='*60}\n")

        # 获取Agent实例
        print("🔄 获取多智能体系统实例...")
        agent = get_trip_planner_agent()

        # 生成旅行计划
        print("🚀 开始生成旅行计划...")
        # HelloAgents/MCP 当前提供同步调用；放入线程池避免阻塞 FastAPI 事件循环。
        trip_plan = await run_in_threadpool(agent.plan_trip, request)

        print("✅ 旅行计划生成成功,准备返回响应\n")

        return TripPlanResponse(
            success=True,
            message=(
                "外部服务不可用，已返回带明确标记的参考计划"
                if trip_plan.is_fallback
                else "旅行计划生成成功"
            ),
            data=trip_plan
        )

    except Exception as e:
        print(f"❌ 生成旅行计划失败: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=f"生成旅行计划失败: {str(e)}"
        )


@router.get(
    "/health",
    summary="健康检查",
    description="检查旅行规划服务是否正常"
)
async def health_check():
    """健康检查"""
    try:
        # 检查Agent是否可用
        agent = get_trip_planner_agent()
        
        return {
            "status": "healthy",
            "service": "trip-planner",
            "agent_name": agent.planner_agent.name,
            "tools_count": sum(
                len(item.list_tools())
                for item in (
                    agent.attraction_agent,
                    agent.weather_agent,
                    agent.hotel_agent,
                    agent.transport_agent,
                )
            )
        }
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"服务不可用: {str(e)}"
        )


@router.post(
    "/transportation",
    response_model=TransportationResponse,
    summary="查询通勤费用",
    description="查询从出发城市到目的城市的通勤费用和交通方式（火车、高铁、飞机、长途汽车）"
)
async def query_transportation(request: TransportationRequest):
    """
    查询通勤费用

    Args:
        request: 通勤查询请求参数

    Returns:
        通勤费用响应
    """
    try:
        print(f"\n{'='*60}")
        print(f"📥 收到通勤费用查询请求:")
        print(f"   出发城市: {request.origin_city}")
        print(f"   目的城市: {request.destination_city}")
        print(f"   出发日期: {request.date}")
        print(f"{'='*60}\n")

        amap_service = get_amap_service()
        options = await run_in_threadpool(
            amap_service.get_transportation_options,
            request.origin_city,
            request.destination_city,
            request.date,
        )

        print(f"✅ 通勤费用查询成功, 返回{len(options)}种交通方式\n")

        return TransportationResponse(
            success=True,
            message=f"成功查询到从{request.origin_city}到{request.destination_city}的交通方式",
            data=options
        )

    except Exception as e:
        print(f"❌ 查询通勤费用失败: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=f"查询通勤费用失败: {str(e)}"
        )
