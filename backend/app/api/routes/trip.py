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
from ...config import get_settings
from ...services.amap_service import get_amap_service
from ...services.runtime_service import require_planning_runtime
from ...services import planning_application_service
from ...services.planning_job_service import planning_job_service

router = APIRouter(prefix="/trip", tags=["旅行规划"])


@router.post(
    "/jobs",
    response_model=PlanningJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="创建异步旅行规划任务",
)
async def create_planning_job(request: TripRequest, raw_request: Request):
    return await run_in_threadpool(planning_application_service.submit_plan, raw_request, request)


@router.post(
    "/revision-jobs",
    response_model=PlanningJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="创建自然语言行程修改任务",
)
async def create_revision_job(request: TripRevisionRequest, raw_request: Request):
    return await run_in_threadpool(planning_application_service.submit_revision, raw_request, request)


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
    except Exception:
        raise HTTPException(503,'暂时无法读取任务队列，请稍后重试') from None


@router.post('/jobs/{job_id}/cancel',response_model=PlanningJobResponse)
def cancel_planning_job(job_id: str):
    try:
        return planning_job_service.cancel(job_id)
    except KeyError:
        raise HTTPException(404,'任务不存在或已过期') from None


@router.get('/jobs/{job_id}/diagnostics')
def job_diagnostics(job_id: str):
    try:
        job=planning_job_service.get(job_id)
    except KeyError:
        raise HTTPException(404,'任务不存在或已过期') from None
    return {'trace_id':job_id,'status':job.status,'stage':job.stage,'metrics':job.metrics,
            'worker_id':job.worker_id,'run_id':job.run_id,'heartbeat_at':job.heartbeat_at,
            'recovery_count':job.recovery_count,**job.diagnostics}


@router.post(
    "/jobs/{job_id}/resume",
    response_model=PlanningJobResponse,
    summary="恢复等待用户确认的规划任务（interrupt）",
)
async def resume_planning_job(job_id: str, body: JobResumeRequest):
    """对 waiting_input 状态的任务提交用户选择，Worker 从 Checkpointer 断点继续。"""
    require_planning_runtime()
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
        planning_job_service.get_stored_request(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务缺少原始请求，无法恢复") from exc

    if body.resume_value not in {str(item.get('value')) for item in (current.waiting_input or {}).get('options',[])}:
        raise HTTPException(422,'请选择当前确认卡片中的有效选项')
    redis_client.resume_job(job_id,body.resume_value)
    return planning_job_service.get(job_id)


@router.get("/jobs/{job_id}/events", summary="订阅规划任务进度")
async def stream_planning_events(job_id: str, request: Request):
    try:
        planning_job_service.get(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="规划任务不存在或已过期") from exc

    async def event_generator():
        last_snapshot_at = 0.0
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
            # 快照携带实时计时信息，不编造进度，也不扩展持久化事件历史。
            tick = asyncio.get_running_loop().time()
            if tick - last_snapshot_at >= 5 or current.status in {"completed", "failed", "cancelled"}:
                snapshot = current.model_dump(mode="json", exclude={"data"})
                yield f'event: snapshot\ndata: {json.dumps(snapshot, ensure_ascii=False)}\n\n'
                last_snapshot_at = tick
            if current.status in {"completed", "failed", "cancelled"} and not events:
                break
            await asyncio.sleep(0.5)

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
    deprecated=True,
    response_model=TripPlanResponse,
    summary="生成旅行计划",
    description="根据用户输入的旅行需求,生成详细的旅行计划"
)
async def plan_trip(request: TripRequest, raw_request: Request):
    """兼容旧客户端的同步响应；执行仍使用统一任务队列。

    需要用户确认或超过等待时间时返回任务编号，客户端可继续查询，
    不再另起一条不受配额和并发限制的规划流程。
    """
    job = await run_in_threadpool(planning_application_service.submit_plan, raw_request, request)
    settings = get_settings()
    deadline = asyncio.get_running_loop().time() + settings.planning_job_timeout_seconds
    while True:
        if job.status == "completed" and job.data is not None:
            return TripPlanResponse(
                success=True,
                message="已生成参考计划" if job.data.is_fallback else "旅行计划生成成功",
                data=job.data,
            )
        if job.status == "waiting_input":
            raise HTTPException(409, detail={
                "message": "需要确认后才能继续，请使用任务接口提交选择",
                "job_id": job.job_id, "waiting_input": job.waiting_input,
            })
        if job.status in {"failed", "cancelled"}:
            raise HTTPException(502, detail={
                "message": job.error or "任务未完成，请通过任务接口查看详情",
                "job_id": job.job_id,
            })
        if asyncio.get_running_loop().time() >= deadline:
            raise HTTPException(504, detail={
                "message": "同步等待已结束，任务未被取消，请通过任务接口继续查询",
                "job_id": job.job_id,
            })
        # 等待让出事件循环；状态读取可能访问 Redis，放在线程池执行。
        await asyncio.sleep(0.5)
        job = await run_in_threadpool(planning_job_service.get, job.job_id)


@router.get(
    "/health",
    summary="健康检查",
    description="检查旅行规划服务是否正常"
)
async def health_check():
    """仅查看运行状态，探针不创建 Agent 或启动地图子进程。"""
    from ...services.runtime_service import runtime_status
    state = await run_in_threadpool(runtime_status)
    if not state["ready"]:
        raise HTTPException(503, detail={"message": "规划服务尚未就绪", "runtime": state})
    return {
        "status": "healthy",
        "service": "trip-planner",
        "runtime": state,
    }


@router.post(
    "/transportation",
    response_model=TransportationResponse,
    summary="查询通勤费用",
    description="查询从出发城市到目的城市的通勤费用和交通方式（火车、高铁、飞机、长途汽车）"
)
async def query_transportation(request: TransportationRequest):
    """
    查询通勤费用

    参数：
        request: 通勤查询请求参数

    返回：
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
