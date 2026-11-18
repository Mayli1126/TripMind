"""地图服务API路由"""

from fastapi import APIRouter, HTTPException, Query
from starlette.concurrency import run_in_threadpool
from ...models.schemas import POISearchResponse, RouteRequest, RouteResponse, WeatherResponse
from ...services.amap_service import get_amap_service

router = APIRouter(prefix="/map", tags=["地图服务"])


@router.get(
    "/poi",
    response_model=POISearchResponse,
    summary="搜索POI",
    description="根据关键词搜索POI(兴趣点)"
)
async def search_poi(
    keywords: str = Query(..., description="搜索关键词", examples=["故宫"]),
    city: str = Query(..., description="城市", examples=["北京"]),
    citylimit: bool = Query(True, description="是否限制在城市范围内")
):
    """
    搜索POI
    
    参数：
        keywords: 搜索关键词
        city: 城市
        citylimit: 是否限制在城市范围内
        
    返回：
        POI搜索结果
    """
    try:
        # 获取服务实例
        service = get_amap_service()
        
        # 搜索POI
        pois = await run_in_threadpool(service.search_poi, keywords, city, citylimit)
        
        return POISearchResponse(
            success=True,
            message="POI搜索成功",
            data=pois
        )
        
    except Exception as e:
        print(f"❌ POI搜索失败: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"POI搜索失败: {str(e)}"
        )


@router.get(
    "/weather",
    response_model=WeatherResponse,
    summary="查询天气",
    description="查询指定城市的天气信息"
)
async def get_weather(
    city: str = Query(..., description="城市名称", examples=["北京"])
):
    """
    查询天气
    
    参数：
        city: 城市名称
        
    返回：
        天气信息
    """
    try:
        # 获取服务实例
        service = get_amap_service()
        
        # 查询天气
        weather_info = await run_in_threadpool(service.get_weather, city)
        
        return WeatherResponse(
            success=True,
            message="天气查询成功",
            data=weather_info
        )
        
    except Exception as e:
        print(f"❌ 天气查询失败: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"天气查询失败: {str(e)}"
        )


@router.post(
    "/route",
    response_model=RouteResponse,
    summary="规划路线",
    description="规划两点之间的路线"
)
async def plan_route(request: RouteRequest):
    """
    规划路线
    
    参数：
        request: 路线规划请求
        
    返回：
        路线信息
    """
    try:
        # 获取服务实例
        service = get_amap_service()
        
        # 规划路线
        route_info = await run_in_threadpool(
            service.plan_route,
            request.origin_address,
            request.destination_address,
            request.origin_city,
            request.destination_city,
            request.route_type,
        )
        
        return RouteResponse(
            success=True,
            message="路线规划成功",
            data=route_info
        )
        
    except Exception as e:
        print(f"❌ 路线规划失败: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"路线规划失败: {str(e)}"
        )


@router.get(
    "/health",
    summary="健康检查",
    description="检查地图服务是否正常"
)
async def health_check():
    """健康检查"""
    try:
        # 检查服务是否可用
        service = get_amap_service()
        
        return {
            "status": "healthy",
            "service": "map-service",
            "mcp_tools_count": len(service.mcp_tool._available_tools)
        }
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"服务不可用: {str(e)}"
        )


# 四种方式只在用户展开时查询，不进入模型工具循环，也不生成虚假的可用路线。
from typing import Literal
from pydantic import BaseModel, Field
from ...models.schemas import Location, TripPlan, TripRequest


class TransportOptionsRequest(BaseModel):
    city: str = Field(min_length=1, max_length=50)
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    origin: Location
    destination: Location
    reference_time: Literal["10:00", "18:00"] = "10:00"


class TransportSelectionRequest(BaseModel):
    plan: TripPlan
    request: TripRequest
    segment_id: str
    mode: Literal["walking", "cycling", "driving", "transit"]


@router.post("/transport-options", summary="按需比较四种交通方式")
def transport_options(payload: TransportOptionsRequest):
    from ...services.road_route_service import RoadRouteResolver
    resolver = RoadRouteResolver(payload.city)
    options = []
    for mode in ("walking", "cycling", "driving", "transit"):
        leg = resolver.resolve(payload.origin, payload.destination, payload.date, payload.reference_time, mode)
        available = leg.source == "amap_route" and not leg.is_estimated
        options.append(dict(mode=mode, available=available, route=leg if available else None,
                            message="" if available else "暂无可用道路路线，请稍后重试"))
    return dict(options=options, note="按参考时段查询，实际耗时请以出行导航为准；驾车不含停车时间")


@router.post("/transport-selection", response_model=TripPlan, summary="切换一段交通并重算耗时与预算")
def transport_selection(payload: TransportSelectionRequest):
    from ...services.transport_selection_service import select_transport
    try:
        return select_transport(payload.plan, payload.request, payload.segment_id, payload.mode)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
