"""POI相关API路由"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional
from ...services.amap_service import get_amap_service
from ...services.attraction_image_service import attraction_image_service
from starlette.concurrency import run_in_threadpool

router = APIRouter(prefix="/poi", tags=["POI"])


class POIDetailResponse(BaseModel):
    """POI详情响应"""
    success: bool
    message: str
    data: Optional[dict] = None


@router.get(
    "/detail/{poi_id}",
    response_model=POIDetailResponse,
    summary="获取POI详情",
    description="根据POI ID获取详细信息,包括图片"
)
async def get_poi_detail(poi_id: str):
    """
    获取POI详情
    
    Args:
        poi_id: POI ID
        
    Returns:
        POI详情响应
    """
    try:
        amap_service = get_amap_service()
        
        # 调用高德地图POI详情API
        result = await run_in_threadpool(amap_service.get_poi_detail, poi_id)
        
        return POIDetailResponse(
            success=True,
            message="获取POI详情成功",
            data=result
        )
        
    except Exception as e:
        print(f"❌ 获取POI详情失败: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"获取POI详情失败: {str(e)}"
        )


@router.get(
    "/search",
    deprecated=True,
    summary="搜索POI",
    description="根据关键词搜索POI"
)
async def search_poi(keywords: str, city: str = "北京"):
    """
    搜索POI

    Args:
        keywords: 搜索关键词
        city: 城市名称

    Returns:
        搜索结果
    """
    try:
        amap_service = get_amap_service()
        result = await run_in_threadpool(amap_service.search_poi, keywords, city)

        return {
            "success": True,
            "message": "搜索成功",
            "data": result
        }

    except Exception as e:
        print(f"❌ 搜索POI失败: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"搜索POI失败: {str(e)}"
        )


@router.get(
    "/photo",
    summary="获取景点图片",
    description="根据景点名称获取实拍图：优先高德POI图片(国内直连)，维基/Unsplash兜底"
)
async def get_attraction_photo(name: str, city: str = ""):
    """
    获取景点图片

    Args:
        name: 景点名称

    Returns:
        图片URL
    """
    try:
        image = await run_in_threadpool(
            attraction_image_service.get_attraction_image, name, city
        )

        return {
            "success": True,
            "message": "获取图片成功",
            "data": {
                "name": name,
                "photo_url": image.get("photo_url") if image else None,
                "source": image.get("source") if image else None,
                "source_url": image.get("source_url") if image else None,
                "attribution": image.get("attribution") if image else None,
                "match_type": image.get("match_type") if image else None,
            }
        }

    except Exception as e:
        print(f"❌ 获取景点图片失败: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"获取景点图片失败: {str(e)}"
        )
