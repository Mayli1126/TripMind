"""优先使用国内可访问的高德地点实拍图，再以 Wikimedia 和 Unsplash 补充景点图片。"""

from __future__ import annotations

import asyncio
import re
from difflib import SequenceMatcher
from typing import Any, Optional

import aiohttp
import requests

from ..infrastructure.shared_cache import make_key, shared_cache
from .cache_policy import make_entry

def _image_cache_key(name: str, city: str):
    return make_key("attraction_image", {"name": name.strip(), "city": city.strip()})


def _image_cache_read(name: str, city: str) -> Optional[dict[str, Any]]:
    try:
        cached = shared_cache.read(_image_cache_key(name, city))
        if cached.hit:
            return cached.entry.data
    except Exception:
        pass
    return None


def _image_cache_write(name: str, city: str, result: Optional[dict[str, Any]]) -> None:
    # 只有真实图源结果才回写，占位失败(None)不缓存，避免负缓存锁死降级链
    if not result or not result.get("photo_url"):
        return
    try:
        shared_cache.write(
            _image_cache_key(name, city),
            make_entry("attraction_image", result, "attraction_image_sources"),
        )
    except Exception:
        pass


class AttractionImageService:
    """优先使用地点匹配准确且来源可追溯的景点图，避免采用无关素材图。"""

    headers = {
        "User-Agent": "TripMind/1.0 (personal travel planner; local development)"
    }

    def __init__(self):
        """初始化服务，加载Unsplash配置"""
        try:
            from ..config import get_settings
            settings = get_settings()
            self.unsplash_access_key = settings.unsplash_access_key
            self.amap_api_key = settings.amap_api_key
        except Exception:
            self.unsplash_access_key = None
            self.amap_api_key = None

        # 境外图源(中文维基百科/Wikimedia Commons)在国内网络通常不可达。
        # 一旦探测失败就置位，后续请求直接跳过，避免每个景点都白等8秒超时。
        self._cross_border_sources_disabled = False

    def get_attraction_image(self, name: str, city: str = "") -> Optional[dict[str, Any]]:
        """统一缓存负责内存/Redis 的期限，失败结果不在进程内永久保留。"""
        clean_name = name.strip()
        if not clean_name:
            return None
        # 缓存命中跳过图源请求；缓存关闭或故障时直接查询。
        cached = _image_cache_read(clean_name, city)
        if cached:
            return cached
        result = asyncio.run(self._get_attraction_image_parallel(clean_name, city))
        _image_cache_write(clean_name, city, result)
        return result

    async def _get_attraction_image_parallel(self, name: str, city: str = "") -> Optional[dict[str, Any]]:
        """按优先级获取景点图片。

        国内场景首选高德POI实拍图(直连、快、名称精确对应)，
        失败后再依次尝试维基百科/Wikimedia Commons/Unsplash兜底。
        """
        amap_result = await self._async_from_amap(name, city)
        if amap_result:
            return amap_result

        if self._cross_border_sources_disabled:
            return await self._async_from_unsplash(name, city)

        # 构建境外源与兜底源的请求任务
        tasks = [
            self._async_from_chinese_wikipedia(name),
            self._async_from_commons(name, city),
        ]
        if self.unsplash_access_key:
            tasks.append(self._async_from_unsplash(name, city))

        # 并行执行所有请求，设置总超时时间
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=15  # 总超时15秒
            )
        except asyncio.TimeoutError:
            # 境外源整体超时，基本可判定为网络不可达，之后不再尝试
            self._cross_border_sources_disabled = True
            print(f"⚠️ 图片获取超时({name})，境外图源将跳过")
            return None

        # 根据优先级筛选有效结果
        valid_results = [r for r in results if isinstance(r, dict)]

        for result in valid_results:
            # 优先选择精确匹配的结果
            if result.get("match_type") == "exact_page":
                return result

        for result in valid_results:
            # 其次选择标题匹配的结果
            if result.get("match_type") == "title_match":
                return result

        # 最后选择关键词搜索的结果
        for result in valid_results:
            if result.get("match_type") == "keyword_search":
                return result

        return None if not valid_results else valid_results[0]

    async def _async_from_amap(self, name: str, city: str = "") -> Optional[dict[str, Any]]:
        """从高德POI搜索2.0(v5)获取景点实拍图。

        高德 v5 place/text 通过 show_fields=photos 直接返回POI实拍图，
        国内可直连、延迟低，且图片与POI名称精确对应，是国内的理想首选。
        """
        if not self.amap_api_key:
            return None
        params: dict[str, Any] = {
            "key": self.amap_api_key,
            "keywords": name,
            "show_fields": "photos",
            "page_size": 3,
        }
        if city:
            params["city"] = city
        try:
            from ..infrastructure.provider_guard import provider_guard
            async with provider_guard.async_slot('amap', self.amap_api_key) as permit, aiohttp.ClientSession() as session:
                async with session.get(
                    "https://restapi.amap.com/v5/place/text",
                    params=params,
                    timeout=8,
                ) as response:
                    if response.status != 200:
                        if response.status == 429:
                            await asyncio.to_thread(provider_guard.cooldown, permit, 10)
                        return None
                    data = await response.json()
                    await asyncio.to_thread(provider_guard.inspect_map, permit, data)
        except Exception as exc:
            print(f"⚠️ 高德图片来源暂不可用: {exc}")
            return None

        if data.get("status") != "1":
            return None

        pois = data.get("pois") or []
        # 名称与景点越接近的POI，图片越可信
        ranked = sorted(
            pois,
            key=lambda p: self._title_score(name, p.get("name", "")),
            reverse=True,
        )
        for poi in ranked:
            score = self._title_score(name, poi.get("name", ""))
            if score < 0.45:
                continue
            for photo in poi.get("photos") or []:
                url = photo.get("url")
                if not url:
                    continue
                # 高德图床同时支持 https，统一升级避免混合内容告警
                url = url.replace("http://", "https://")
                return {
                    "photo_url": url,
                    "source": "高德地图",
                    "source_url": "",
                    "attribution": f"图片来自高德地图POI({poi.get('name', '')})",
                    "match_type": "title_match" if score >= 0.9 else "keyword_search",
                }
        return None

    async def _async_from_chinese_wikipedia(self, name: str) -> Optional[dict[str, Any]]:
        """异步从中文维基百科获取图片"""
        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(
                    "https://zh.wikipedia.org/w/api.php",
                    params={
                        "action": "query",
                        "format": "json",
                        "formatversion": 2,
                        "redirects": 1,
                        "prop": "pageimages|info",
                        "piprop": "thumbnail|original",
                        "pithumbsize": 1000,
                        "inprop": "url",
                        "titles": name,
                    },
                    timeout=8,
                ) as response:
                    if response.status != 200:
                        return None
                    data = await response.json()
        except Exception as exc:
            print(f"⚠️ 维基百科图片来源暂不可用: {exc}")
            self._cross_border_sources_disabled = True
            return None

        pages = data.get("query", {}).get("pages", []) if data else []
        for page in pages:
            if page.get("missing") is not None:
                continue
            image = page.get("original") or page.get("thumbnail")
            if image and image.get("source"):
                return {
                    "photo_url": image["source"],
                    "source": "Wikipedia",
                    "source_url": page.get("fullurl", ""),
                    "attribution": "图片来自中文维基百科，许可信息请查看原页面",
                    "match_type": "exact_page",
                }
        return None

    async def _async_from_commons(self, name: str, city: str) -> Optional[dict[str, Any]]:
        """异步从Wikimedia Commons获取图片"""
        query = f'intitle:"{name}" {city} filetype:bitmap'.strip()
        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(
                    "https://commons.wikimedia.org/w/api.php",
                    params={
                        "action": "query",
                        "format": "json",
                        "formatversion": 2,
                        "generator": "search",
                        "gsrsearch": query,
                        "gsrnamespace": 6,
                        "gsrlimit": 8,
                        "prop": "imageinfo|info",
                        "iiprop": "url|extmetadata",
                        "iiurlwidth": 1000,
                        "inprop": "url",
                    },
                    timeout=8,
                ) as response:
                    if response.status != 200:
                        return None
                    data = await response.json()
        except Exception as exc:
            print(f"⚠️ Wikimedia Commons图片来源暂不可用: {exc}")
            self._cross_border_sources_disabled = True
            return None

        pages = data.get("query", {}).get("pages", []) if data else []
        ranked = sorted(pages, key=lambda page: self._title_score(name, page.get("title", "")), reverse=True)
        for page in ranked:
            score = self._title_score(name, page.get("title", ""))
            if score < 0.68:
                continue
            image_info = (page.get("imageinfo") or [{}])[0]
            image_url = image_info.get("thumburl") or image_info.get("url")
            if not image_url:
                continue
            metadata = image_info.get("extmetadata") or {}
            license_name = self._metadata_value(metadata, "LicenseShortName")
            artist = self._strip_html(self._metadata_value(metadata, "Artist"))
            attribution_parts = [part for part in (artist, license_name) if part]
            return {
                "photo_url": image_url,
                "source": "Wikimedia Commons",
                "source_url": page.get("fullurl", ""),
                "attribution": " · ".join(attribution_parts) or "图片来自 Wikimedia Commons",
                "match_type": "title_match",
            }
        return None

    async def _async_from_unsplash(self, name: str, city: str = "") -> Optional[dict[str, Any]]:
        """异步从Unsplash获取备用图片"""
        if not self.unsplash_access_key:
            return None
        query = f"{name} {city}".strip() or name
        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(
                    "https://api.unsplash.com/search/photos",
                    params={
                        "query": query,
                        "per_page": 1,
                        "client_id": self.unsplash_access_key,
                        "orientation": "landscape",
                    },
                    timeout=10,
                ) as response:
                    if response.status != 200:
                        return None
                    data = await response.json()
        except Exception as exc:
            print(f"⚠️ Unsplash图片来源暂不可用: {exc}")
            return None

        results = data.get("results", [])
        if results:
            photo = results[0]
            return {
                "photo_url": photo.get("urls", {}).get("regular"),
                "source": "Unsplash",
                "source_url": photo.get("links", {}).get("html"),
                "attribution": f"Photo by {photo.get('user', {}).get('name')} on Unsplash",
                "match_type": "keyword_search",
            }
        return None

    def _from_chinese_wikipedia(self, name: str) -> Optional[dict[str, Any]]:
        """同步版本，用于向后兼容"""
        return asyncio.run(self._async_from_chinese_wikipedia(name))

    def _from_commons(self, name: str, city: str) -> Optional[dict[str, Any]]:
        """同步版本，用于向后兼容"""
        return asyncio.run(self._async_from_commons(name, city))

    def _from_unsplash(self, name: str, city: str = "") -> Optional[dict[str, Any]]:
        """同步版本，用于向后兼容"""
        return asyncio.run(self._async_from_unsplash(name, city))

    def _request_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        """同步请求JSON，用于向后兼容"""
        try:
            response = requests.get(url, params=params, headers=self.headers, timeout=8)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            print(f"⚠️ 图片来源暂不可用: {exc}")
            return {}

    @classmethod
    def _title_score(cls, name: str, title: str) -> float:
        normalized_name = cls._normalize(name)
        normalized_title = cls._normalize(title.replace("File:", "").replace("文件:", ""))
        if not normalized_name or not normalized_title:
            return 0
        if normalized_name in normalized_title:
            return 1.0
        return SequenceMatcher(None, normalized_name, normalized_title).ratio()

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"[^\w\u4e00-\u9fff]", "", value.lower())

    @staticmethod
    def _metadata_value(metadata: dict[str, Any], key: str) -> str:
        value = metadata.get(key, {})
        return str(value.get("value", "")) if isinstance(value, dict) else ""

    @staticmethod
    def _strip_html(value: str) -> str:
        return re.sub(r"<[^>]+>", "", value).strip()


attraction_image_service = AttractionImageService()
