"""高德地图MCP服务封装"""

from collections import OrderedDict
from typing import List, Dict, Any, Optional, TYPE_CHECKING
from ..config import get_settings
from ..models.schemas import Location, POIInfo, WeatherInfo, TransportationOption
import json
import random
import re
import threading
import time

import requests

if TYPE_CHECKING:
    from ..infrastructure.mcp_client import MCPToolAdapter

# 全局MCP工具实例
_amap_mcp_tool = None

# 地点坐标相对稳定，先使用快速的进程内缓存；
# 启用 Redis 后再使用共享二级缓存。第三方高德 MCP
# 封装会丢弃文本检索结果中的 location 字段，因此
# 正常运行时直接调用同一个官方接口，
# 避免为每个地点再请求一次详情。
_POI_CACHE_TTL_SECONDS = 7 * 24 * 3600
_POI_MEMORY_CACHE_SIZE = 2048
_POI_MEMORY_CACHE: "OrderedDict[str, tuple[float, list[dict[str, Any]]]]" = OrderedDict()
_POI_CACHE_LOCK = threading.Lock()
_POI_INFLIGHT: dict[str, threading.Event] = {}

# 实测当前开发者密钥的配额无法承受瞬间四个并发请求。
# 只对地点 HTTP 请求设置间隔来抑制突发流量，
# 规划图中的模型、天气和交通任务仍可并发执行。
_AMAP_POI_MIN_INTERVAL_SECONDS = 0.28
_AMAP_RATE_LOCK = threading.Lock()
_AMAP_NEXT_REQUEST_AT = 0.0


class AmapRateLimitError(RuntimeError):
    """高德每秒请求限流的本地短重试次数耗尽后抛出此异常。"""


def _parse_average_cost(item):
    """只读取高德结构化消费字段；空值或无效值表示未知，不代表免费。"""
    import math
    for field in ('biz_ext', 'business'):
        data = item.get(field)
        value = data.get('cost') if isinstance(data, dict) else None
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            continue
        try:
            amount = float(value)
            if math.isfinite(amount) and amount >= 0:
                return amount
        except (ValueError, TypeError):
            pass
    return None


def _normalise_cache_part(value: str) -> str:
    return re.sub(r"\s+", "", value.strip().lower())


def _poi_cache_key(keywords: str, city: str, citylimit: bool) -> str:
    return (
        f"amap:poi:v4:{_normalise_cache_part(city)}:"
        f"{_normalise_cache_part(keywords)}:{int(citylimit)}"
    )


def _poi_around_cache_key(
    keywords: str, location: Location, radius: int
) -> str:
    return (
        f"amap:around:v4:{location.longitude:.5f}:{location.latitude:.5f}:"
        f"{int(radius)}:{_normalise_cache_part(keywords)}"
    )


def _serialise_pois(pois: List[POIInfo]) -> list[dict[str, Any]]:
    return [poi.model_dump(mode="json") for poi in pois]


def _deserialise_pois(items: Any) -> List[POIInfo]:
    if not isinstance(items, list):
        return []
    pois: List[POIInfo] = []
    for item in items:
        try:
            pois.append(POIInfo.model_validate(item))
        except Exception:
            continue
    return pois


def _poi_cache_read(cache_key: str) -> List[POIInfo]:
    from ..infrastructure.task_trace import emit
    now = time.monotonic()
    with _POI_CACHE_LOCK:
        cached = _POI_MEMORY_CACHE.get(cache_key)
        if cached is not None:
            expires_at, items = cached
            if expires_at > now:
                _POI_MEMORY_CACHE.move_to_end(cache_key)
                emit('cache','poi_memory','lookup',hit=True)
                return _deserialise_pois(items)
            _POI_MEMORY_CACHE.pop(cache_key, None)

    emit('cache','poi_memory','lookup',hit=False)
    if not get_settings().redis_enabled:
        return []
    try:
        from ..infrastructure import redis_client

        raw = redis_client.cache_get(cache_key)
        if not raw:
            return []
        items = json.loads(raw)
        pois = _deserialise_pois(items)
        if pois:
            _poi_cache_write_memory(cache_key, pois)
        return pois
    except Exception:
        return []


def _poi_cache_write_memory(cache_key: str, pois: List[POIInfo]) -> None:
    items = _serialise_pois(pois)
    with _POI_CACHE_LOCK:
        _POI_MEMORY_CACHE[cache_key] = (
            time.monotonic() + _POI_CACHE_TTL_SECONDS,
            items,
        )
        _POI_MEMORY_CACHE.move_to_end(cache_key)
        while len(_POI_MEMORY_CACHE) > _POI_MEMORY_CACHE_SIZE:
            _POI_MEMORY_CACHE.popitem(last=False)


def _poi_cache_write(cache_key: str, pois: List[POIInfo]) -> None:
    _poi_cache_write_memory(cache_key, pois)
    if not get_settings().redis_enabled:
        return
    try:
        from ..infrastructure import redis_client

        redis_client.cache_set(
            cache_key,
            json.dumps(_serialise_pois(pois), ensure_ascii=False),
            _POI_CACHE_TTL_SECONDS,
        )
    except Exception:
        pass


def _wait_for_amap_poi_slot() -> None:
    """对地点查询统一设置调用间隔，不串行阻塞其他规划任务。"""
    global _AMAP_NEXT_REQUEST_AT
    with _AMAP_RATE_LOCK:
        now = time.monotonic()
        delay = max(0.0, _AMAP_NEXT_REQUEST_AT - now)
        _AMAP_NEXT_REQUEST_AT = max(now, _AMAP_NEXT_REQUEST_AT) + _AMAP_POI_MIN_INTERVAL_SECONDS
    if delay:
        time.sleep(delay)


def _decode_mcp_payload(raw: Any) -> Any:
    """解开 MCP 常见的 JSON、content/text 和 result 包装。"""
    value = raw
    for _ in range(5):
        if isinstance(value, str):
            text = value.strip()
            try:
                value = json.loads(text)
                continue
            except json.JSONDecodeError:
                match = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
                if not match:
                    return text
                try:
                    value = json.loads(match.group(1))
                    continue
                except json.JSONDecodeError:
                    return text
        if isinstance(value, dict) and "content" in value:
            content = value["content"]
            if isinstance(content, list) and content:
                first = content[0]
                value = first.get("text", first) if isinstance(first, dict) else first
                continue
        if isinstance(value, dict) and set(value).intersection({"result", "data"}):
            key = "result" if "result" in value else "data"
            nested = value[key]
            if isinstance(nested, (dict, list, str)):
                value = nested
                continue
        break
    return value


def _parse_location(value: Any) -> Optional[Location]:
    if isinstance(value, dict):
        longitude = value.get("longitude", value.get("lng"))
        latitude = value.get("latitude", value.get("lat"))
    elif isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
        if len(parts) != 2:
            return None
        longitude, latitude = parts
    else:
        return None
    try:
        return Location(longitude=float(longitude), latitude=float(latitude))
    except (TypeError, ValueError):
        return None


def _find_list(payload: Any, *keys: str) -> list:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    for value in payload.values():
        if isinstance(value, dict):
            found = _find_list(value, *keys)
            if found:
                return found
    return []


def get_amap_mcp_tool() -> "MCPToolAdapter":
    """
    获取高德地图MCP工具实例(单例模式)

    返回：
        MCPToolAdapter 实例（底层为官方 mcp SDK 会话池）
    """
    global _amap_mcp_tool

    if _amap_mcp_tool is None:
        from ..infrastructure.mcp_client import MCPToolAdapter, get_amap_manager

        _amap_mcp_tool = MCPToolAdapter(get_amap_manager())

        print(f"✅ 高德地图MCP工具初始化成功")

    return _amap_mcp_tool


class AmapService:
    """高德地图服务封装类"""

    def __init__(self, mcp_tool: Optional["MCPToolAdapter"] = None):
        """初始化服务；并行任务可注入独占 MCP 连接。"""
        # 测试和特殊调用方会注入工具，因此保留它们的
        # MCP 解析路径；正常应用实例使用下方的
        # 单次地点查询路径。
        self._prefer_direct_poi_search = mcp_tool is None
        self._mcp_tool = mcp_tool

    @property
    def mcp_tool(self) -> "MCPToolAdapter":
        """只有非地点查询操作确实需要时，才初始化 MCP 管理器。"""
        if self._mcp_tool is None:
            self._mcp_tool = get_amap_mcp_tool()
        return self._mcp_tool

    @mcp_tool.setter
    def mcp_tool(self, value: "MCPToolAdapter") -> None:
        # 保留此适配入口，供测试替身和已有调用方替换实现。
        self._mcp_tool = value
    
    def search_poi(self, keywords: str, city: str, citylimit: bool = True) -> List[POIInfo]:
        """
        搜索POI
        
        参数：
            keywords: 搜索关键词
            city: 城市
            citylimit: 是否限制在城市范围内
            
        返回：
            POI信息列表
        """
        if getattr(self, "_prefer_direct_poi_search", False):
            return self._search_poi_cached_direct(keywords, city, citylimit)

        try:
            # 调用MCP工具
            result = self.mcp_tool.run({
                "action": "call_tool",
                "tool_name": "maps_text_search",
                "arguments": {
                    "keywords": keywords,
                    "city": city,
                    "citylimit": str(citylimit).lower()
                }
            })

            # 解析结果
            # 注意: MCP工具返回的是字符串,需要解析
            result_ = _decode_mcp_payload(result)
            result_preview = json.dumps(
                result_,
                ensure_ascii=False,
                default=str
            )
            print(f"POI搜索结果: {result_preview[:200]}...")

            items = [
                item for item in _find_list(result_, "pois", "results")
                if isinstance(item, dict) and item.get("name")
            ]
            if not items:
                raise ValueError("高德 MCP 返回中未找到可解析的 POI")

            # 名称精确匹配优先，其次取第一个结果；
            # text search 的返回常不带 location 坐标，需按 poi_id 调详情接口补齐
            best = next(
                (item for item in items if str(item.get("name")) == keywords), items[0]
            )
            if _parse_location(best.get("location")) is None:
                detail = self._fetch_poi_detail(str(best.get("id", "")))
                if detail:
                    best = {**best, **detail}

            pois = []
            best_location = _parse_location(best.get("location"))
            if best_location is not None:
                pois.append(POIInfo(
                    id=str(best.get("id", "")),
                    name=str(best["name"]),
                    city_name=str(best.get("cityname") or ""),
                    type_code=str(best.get("typecode") or ""),
                    parent_poi_id=best.get("parent") if isinstance(best.get("parent"), str) else "",
                    type=str(best.get("type", best.get("typecode", ""))),
                    address=str(best.get("address", "")),
                    location=best_location,
                    amap_average_cost=_parse_average_cost(best),
                    tel=str(best["tel"]) if best.get("tel") else None,
                ))
            # 其余项若自带坐标也一并返回（供调用方按名称挑选）
            for item in items:
                if item is best:
                    continue
                location = _parse_location(item.get("location"))
                if not location:
                    continue
                pois.append(POIInfo(
                    id=str(item.get("id", "")),
                    name=str(item["name"]),
                    city_name=str(item.get("cityname") or ""),
                    type_code=str(item.get("typecode") or ""),
                    parent_poi_id=item.get("parent") if isinstance(item.get("parent"), str) else "",
                    type=str(item.get("type", item.get("category", ""))),
                    address=str(item.get("address", "")),
                    location=location,
                    amap_average_cost=_parse_average_cost(item),
                    tel=str(item["tel"]) if item.get("tel") else None,
                ))
            if not pois:
                raise ValueError("高德 MCP 返回的 POI 均无法解析出坐标")
            return pois

        except Exception as e:
            print(f"❌ POI搜索失败: {str(e)}")
            raise RuntimeError(f"POI搜索失败: {e}") from e

    def _search_poi_cached_direct(
        self,
        keywords: str,
        city: str,
        citylimit: bool,
        location: Optional[Location] = None,
        radius: int = 2000,
        limit: int = 20,
    ) -> List[POIInfo]:
        """单次官方地点检索，使用两级缓存，并合并相同查询的并发请求。"""
        if get_settings().cache_enabled:
            from ..infrastructure.shared_cache import make_key
            from ..infrastructure.cache_rebuild import cache_rebuilder
            from .cache_policy import make_entry
            kind = 'around' if location is not None else 'poi'
            parameters = dict(provider='amap', api='v3/place-city-cost-v2', keywords=keywords.strip(),
                              offset=limit, page=1, extensions='all')
            if location is not None:
                # 保留请求坐标的原始精度，不为缓存命中而对地理位置取整。
                parameters.update(location=f'{location.longitude},{location.latitude}', radius=int(radius))
            else:
                parameters.update(city=city.strip(), citylimit=bool(citylimit))
            def load():
                pois = self._search_poi_direct(keywords, city, citylimit, location=location,
                                               radius=radius, allow_empty=True, limit=limit)
                return make_entry(kind, _serialise_pois(pois), 'amap_v3_place', empty=not pois)
            entry = cache_rebuilder.get(make_key(kind, parameters), load)
            return _deserialise_pois(entry.data)
        cache_key = (
            _poi_around_cache_key(keywords, location, radius)
            if location is not None
            else _poi_cache_key(keywords, city, citylimit)
        ) + f":limit:{limit}"
        cached = _poi_cache_read(cache_key)
        if cached:
            return cached

        owner = False
        with _POI_CACHE_LOCK:
            event = _POI_INFLIGHT.get(cache_key)
            if event is None:
                event = threading.Event()
                _POI_INFLIGHT[cache_key] = event
                owner = True

        if not owner:
            # 重复查询共享首个调用者的结果，
            # 避免再次消耗高德调用配额。
            event.wait(timeout=20.0)
            cached = _poi_cache_read(cache_key)
            if cached:
                return cached

        try:
            pois = self._search_poi_direct(
                keywords, city, citylimit, location=location, radius=radius, limit=limit
            )
            _poi_cache_write(cache_key, pois)
            return pois
        finally:
            if owner:
                with _POI_CACHE_LOCK:
                    finished = _POI_INFLIGHT.pop(cache_key, None)
                if finished is not None:
                    finished.set()

    def search_tourism_pois(self, keywords: str, city: str) -> List[POIInfo]:
        """规划专用：只取高德排序第一条并保留坐标，其他搜索入口不受影响。"""
        return self._search_poi_cached_direct(keywords, city, True, limit=1)[:1]

    def search_hotel_pois(self, keywords: str, city: str) -> List[POIInfo]:
        """酒店快速路径：单页、一次尝试、五秒网络超时，不查详情。"""
        return self._search_poi_direct(keywords, city, True, allow_empty=True,
                                       max_attempts=1, request_timeout=5)

    def search_poi_once(self, keywords: str, city: str, timeout: float = 3) -> List[POIInfo]:
        """补搜与路线锚点的有界核验，不进入多次重试或工具自由循环。"""
        return self._search_poi_direct(keywords, city, True, allow_empty=True,
                                      max_attempts=1, request_timeout=timeout)

    def _search_poi_direct(
        self,
        keywords: str,
        city: str,
        citylimit: bool,
        location: Optional[Location] = None,
        radius: int = 2000,
        allow_empty: bool = False,
        max_attempts: int = 3,
        request_timeout: float = 12,
        limit: int = 20,
    ) -> List[POIInfo]:
        """调用高德官方文本检索接口，保留 location 坐标字段。

        当前 amap-mcp-server 会移除此字段，导致
        不得不再次请求详情，使每秒请求压力翻倍。
        """
        settings = get_settings()
        last_limit_message = "CUQPS_HAS_EXCEEDED_THE_LIMIT"
        if location is not None:
            endpoint = "https://restapi.amap.com/v3/place/around"
            params = {
                "key": settings.amap_api_key,
                "keywords": keywords,
                "location": f"{location.longitude},{location.latitude}",
                "radius": str(radius),
                "offset": limit,
                "page": 1,
                "extensions": "all",
            }
        else:
            endpoint = "https://restapi.amap.com/v3/place/text"
            params = {
                "key": settings.amap_api_key,
                "keywords": keywords,
                "city": city,
                "citylimit": str(citylimit).lower(),
                "offset": limit,
                "page": 1,
                "extensions": "all",
            }
        for attempt in range(max_attempts):
            from ..infrastructure.task_trace import span
            with span('map_http','rate_limit_wait'):
                _wait_for_amap_poi_slot()
            try:
                from ..infrastructure.task_trace import span
                with span('map_http','poi_search',attempt=attempt):
                    from ..infrastructure.provider_guard import map_http
                    response = map_http(lambda: requests.get(endpoint,params=params,timeout=request_timeout))
                    response.raise_for_status()
                    payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                if attempt < max_attempts - 1:
                    from ..infrastructure.task_trace import emit
                    emit('map_http','poi_retry','retrying',attempt=attempt+1,error_type=type(exc).__name__)
                    time.sleep((0.18 * (2 ** attempt)) + random.uniform(0.02, 0.08))
                    continue
                raise RuntimeError(f"高德 POI 请求失败: {exc}") from exc

            if str(payload.get("status")) != "1":
                message = str(payload.get("info") or payload.get("infocode") or "未知错误")
                if "QPS" in message.upper() or "LIMIT" in message.upper():
                    last_limit_message = message
                    if attempt < max_attempts - 1:
                        time.sleep((0.2 * (2 ** attempt)) + random.uniform(0.03, 0.1))
                        continue
                    raise AmapRateLimitError(
                        f"高德 POI 查询触发频率限制: {message}"
                    )
                raise RuntimeError(f"高德 POI 查询失败: {message}")

            items = payload.get('pois')
            if allow_empty and isinstance(items, list) and not items:
                return []
            if not isinstance(items, list):
                raise ValueError('高德 POI 响应缺少列表，不能缓存为无结果')
            pois: List[POIInfo] = []
            for item in payload.get("pois") or []:
                if not isinstance(item, dict) or not item.get("name"):
                    continue
                location = _parse_location(item.get("location"))
                if location is None:
                    continue
                pois.append(POIInfo(
                    id=str(item.get("id", "")),
                    name=str(item["name"]),
                    city_name=str(item.get("cityname") or ""),
                    type_code=str(item.get("typecode") or ""),
                    parent_poi_id=item.get("parent") if isinstance(item.get("parent"), str) else "",
                    type=str(item.get("type", item.get("typecode", ""))),
                    address=str(item.get("address", "")),
                    location=location,
                    amap_average_cost=_parse_average_cost(item),
                    tel=str(item["tel"]) if item.get("tel") else None,
                ))
            if not pois:
                raise ValueError(
                    f"高德未返回带坐标的 POI（关键词={keywords}，城市={city}）"
                )
            return pois

        raise AmapRateLimitError(f"高德 POI 查询触发频率限制: {last_limit_message}")

    def _fetch_poi_detail(self, poi_id: str) -> Optional[dict]:
        """maps_text_search 不返回坐标时，按 poi_id 调详情接口补 location 等字段。

        详情接口返回顶层就是 POI 对象（非 pois 列表包装），两种结构都兼容。
        """
        if not poi_id:
            return None
        try:
            result = self.mcp_tool.run({
                "action": "call_tool",
                "tool_name": "maps_search_detail",
                "arguments": {"id": poi_id},
            })
            detail_ = _decode_mcp_payload(result)
            if isinstance(detail_, dict) and detail_.get("error"):
                message = str(detail_["error"])
                if "QPS" in message.upper() or "LIMIT" in message.upper():
                    raise AmapRateLimitError(
                        f"高德 POI 详情查询触发频率限制: {message}"
                    )
                print(f"⚠️ 高德 POI 详情返回错误（poi_id={poi_id}）: {message}")
                return None
            # 顶层直接是 POI 对象
            if isinstance(detail_, dict) and detail_.get("name"):
                return detail_
            items = [
                item for item in _find_list(detail_, "pois", "results")
                if isinstance(item, dict)
            ]
            return items[0] if items else None
        except AmapRateLimitError:
            raise
        except Exception as exc:
            print(f"⚠️ POI 详情获取失败（poi_id={poi_id}）: {exc}")
            return None
    
    def get_weather(self, city: str) -> List[WeatherInfo]:
        if get_settings().cache_enabled:
            from ..infrastructure.shared_cache import make_key
            from ..infrastructure.cache_rebuild import cache_rebuilder
            from .cache_policy import make_entry
            def load():
                forecasts = self._get_weather_uncached(city)
                return make_entry('weather', [w.model_dump(mode='json') for w in forecasts], 'amap_mcp_weather')
            entry = cache_rebuilder.get(make_key('weather', dict(provider='amap_mcp', api='maps_weather_v1',
                city=city.strip(), forecast_type='forecast')), load)
            return [WeatherInfo.model_validate(w) for w in entry.data]
        return self._get_weather_uncached(city)

    def _get_weather_uncached(self, city: str) -> List[WeatherInfo]:
        """
        查询天气
        
        参数：
            city: 城市名称
            
        返回：
            天气信息列表
        """
        try:
            # 调用MCP工具
            result = self.mcp_tool.run({
                "action": "call_tool",
                "tool_name": "maps_weather",
                "arguments": {
                    "city": city
                }
            })
            result_ = _decode_mcp_payload(result)
            result_preview = json.dumps(
                result_,
                ensure_ascii=False,
                default=str
            )
            print(f"天气查询结果: {result_preview[:200]}...")
            
            casts = _find_list(result_, "casts", "forecasts", "weather")
            if casts and isinstance(casts[0], dict) and isinstance(casts[0].get("casts"), list):
                casts = casts[0]["casts"]
            weather = []
            for item in casts:
                if not isinstance(item, dict):
                    continue
                weather.append(WeatherInfo(
                    date=str(item.get("date", "")),
                    day_weather=str(item.get("day_weather", item.get("dayweather", ""))),
                    night_weather=str(item.get("night_weather", item.get("nightweather", ""))),
                    day_temp=item.get("day_temp", item.get("daytemp", 0)),
                    night_temp=item.get("night_temp", item.get("nighttemp", 0)),
                    wind_direction=str(item.get("wind_direction", item.get("daywind", ""))),
                    wind_power=str(item.get("wind_power", item.get("daypower", ""))),
                ))
            if not weather:
                raise ValueError("高德 MCP 返回中未找到可解析的天气预报")
            return weather
            
        except Exception as e:
            print(f"❌ 天气查询失败: {str(e)}")
            raise RuntimeError(f"天气查询失败: {e}") from e
    
    def plan_route(
        self,
        origin_address: str,
        destination_address: str,
        origin_city: Optional[str] = None,
        destination_city: Optional[str] = None,
        route_type: str = "walking"
    ) -> Dict[str, Any]:
        """
        规划路线
        
        参数：
            origin_address: 起点地址
            destination_address: 终点地址
            origin_city: 起点城市
            destination_city: 终点城市
            route_type: 路线类型 (walking/driving/transit)
            
        返回：
            路线信息
        """
        try:
            # 根据路线类型选择工具
            tool_map = {
                "walking": "maps_direction_walking_by_address",
                "driving": "maps_direction_driving_by_address",
                "transit": "maps_direction_transit_integrated_by_address"
            }
            
            tool_name = tool_map.get(route_type, "maps_direction_walking_by_address")
            
            # 构建参数
            arguments = {
                "origin_address": origin_address,
                "destination_address": destination_address
            }
            
            # 公共交通需要城市参数
            if route_type == "transit":
                if origin_city:
                    arguments["origin_city"] = origin_city
                if destination_city:
                    arguments["destination_city"] = destination_city
            else:
                # 其他路线类型也可以提供城市参数提高准确性
                if origin_city:
                    arguments["origin_city"] = origin_city
                if destination_city:
                    arguments["destination_city"] = destination_city
            
            # 调用MCP工具
            result = self.mcp_tool.run({
                "action": "call_tool",
                "tool_name": tool_name,
                "arguments": arguments
            })
            result_ = _decode_mcp_payload(result)
            result_preview = json.dumps(
                result_,
                ensure_ascii=False,
                default=str
            )
            print(f"路线规划结果: {result_preview[:200]}...")
            
            candidates = _find_list(result_, "paths", "transits", "routes")
            if not candidates or not isinstance(candidates[0], dict):
                raise ValueError("高德 MCP 返回中未找到可解析的路线")
            route = candidates[0]
            return {
                "distance": float(route.get("distance", 0)),
                "duration": int(float(route.get("duration", 0))),
                "route_type": route_type,
                "description": str(
                    route.get("description", route.get("strategy", f"{route_type} 路线"))
                ),
            }
            
        except Exception as e:
            print(f"❌ 路线规划失败: {str(e)}")
            raise RuntimeError(f"路线规划失败: {e}") from e
    
    def geocode(self, address: str, city: Optional[str] = None) -> Optional[Location]:
        """
        地理编码(地址转坐标)

        参数：
            address: 地址
            city: 城市

        返回：
            经纬度坐标
        """
        try:
            arguments = {"address": address}
            if city:
                arguments["city"] = city

            result = self.mcp_tool.run({
                "action": "call_tool",
                "tool_name": "maps_geo",
                "arguments": arguments
            })
            result_ = _decode_mcp_payload(result)
            result_preview = json.dumps(
                result_,
                ensure_ascii=False,
                default=str
            )
            print(f"地理编码结果: {result_preview[:200]}...")

            geocodes = _find_list(result_, "geocodes", "results")
            location = _parse_location(geocodes[0].get("location")) if geocodes and isinstance(geocodes[0], dict) else None
            if not location and isinstance(result_, dict):
                location = _parse_location(result_.get("location"))
            if not location:
                raise ValueError("高德 MCP 返回中未找到可解析的坐标")
            return location

        except Exception as e:
            print(f"❌ 地理编码失败: {str(e)}")
            raise RuntimeError(f"地理编码失败: {e}") from e

    def search_restaurants(self, keywords: str, city: str, location: Optional[Location] = None, radius: int = 2000) -> List[POIInfo]:
        """
        搜索餐厅/美食POI
        
        参数：
            keywords: 搜索关键词（如"美食"、"餐厅"、"火锅"等）
            city: 城市
            location: 中心点坐标（可选，用于附近搜索）
            radius: 搜索半径（米）
            
        返回：
            POI信息列表
        """
        if getattr(self, "_prefer_direct_poi_search", False):
            try:
                return self._search_poi_cached_direct(
                    keywords,
                    city,
                    True,
                    location=location,
                    radius=radius,
                )
            except Exception as exc:
                print(f"❌ 餐厅搜索失败: {exc}")
                return []

        try:
            arguments = {
                "keywords": keywords,
                "city": city,
                "citylimit": str(True).lower()
            }
            if location:
                arguments["location"] = f"{location.longitude},{location.latitude}"
                arguments["radius"] = str(radius)
            
            result = self.mcp_tool.run({
                "action": "call_tool",
                "tool_name": "maps_text_search",
                "arguments": arguments
            })
            
            result_ = _decode_mcp_payload(result)
            pois = []
            for item in _find_list(result_, "pois", "results"):
                if not isinstance(item, dict):
                    continue
                loc = _parse_location(item.get("location"))
                if not loc or not item.get("name"):
                    continue
                pois.append(POIInfo(
                    id=str(item.get("id", "")),
                    name=str(item["name"]),
                    city_name=str(item.get("cityname") or ""),
                    type_code=str(item.get("typecode") or ""),
                    parent_poi_id=item.get("parent") if isinstance(item.get("parent"), str) else "",
                    type=str(item.get("type", item.get("category", "餐饮服务"))),
                    address=str(item.get("address", "")),
                    location=loc,
                    tel=str(item["tel"]) if item.get("tel") else None,
                ))
            if not pois:
                print(f"⚠️ 未搜索到{keywords}相关餐厅，尝试扩大范围")
                return []
            return pois
            
        except Exception as e:
            print(f"❌ 餐厅搜索失败: {str(e)}")
            return []

    def get_poi_detail(self, poi_id: str) -> Dict[str, Any]:
        """
        获取POI详情

        参数：
            poi_id: 地点标识

        返回：
            POI详情信息
        """
        try:
            result = self.mcp_tool.run({
                "action": "call_tool",
                "tool_name": "maps_search_detail",
                "arguments": {
                    "id": poi_id
                }
            })
            result_ = json.loads(result)
            result_preview = json.dumps(
                result_,
                ensure_ascii=False,
                default=str
            )
            print(f"POI详情结果: {result_preview[:200]}...")



            # 尝试从结果中提取JSON
            json_match = re.search(r'\{.*\}', result, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                return data

            return {"raw": result}

        except Exception as e:
            print(f"❌ 获取POI详情失败: {str(e)}")
            return {}

    def get_transportation_options(
        self,
        origin_city: str,
        destination_city: str,
        date: Optional[str] = None
    ) -> List[TransportationOption]:
        """
        查询通勤费用和交通方式

        参数：
            origin_city: 出发城市
            destination_city: 目的城市
            date: 出发日期

        返回：
            交通方式选项列表
        """
        try:
            print(f"查询通勤费用: {origin_city} -> {destination_city}")
            return self._get_fallback_transportation_options(origin_city, destination_city)
        except Exception as e:
            print(f"通勤费用查询失败: {str(e)}")
            return self._get_fallback_transportation_options(origin_city, destination_city)

    def _get_fallback_transportation_options(
        self,
        origin_city: str,
        destination_city: str
    ) -> List[TransportationOption]:
        """
        获取备用通勤费用数据（当LLM查询失败时使用）

        参数：
            origin_city: 出发城市
            destination_city: 目的城市

        返回：
            交通方式选项列表
        """
        city_pairs = {
            ("北京", "上海"): {
                "distance": 1318,
                "plane": {"price": 550, "duration": "2小时30分钟", "dep_time": "08:30", "arr_time": "11:00", "dep_station": "北京首都国际机场", "arr_station": "上海浦东国际机场"},
                "high_speed_train": {"price": 576, "duration": "4小时30分钟", "dep_time": "07:00", "arr_time": "11:30", "dep_station": "北京南站", "arr_station": "上海虹桥站"},
                "train": {"price": 177, "duration": "14小时", "dep_time": "10:00", "arr_time": "22:00", "dep_station": "北京站", "arr_station": "上海站"},
                "bus": {"price": 350, "duration": "18小时", "dep_time": "18:00", "arr_time": "次日12:00", "dep_station": "北京六里桥汽车站", "arr_station": "上海长途汽车总站"},
            },
            ("北京", "广州"): {
                "distance": 2120,
                "plane": {"price": 800, "duration": "3小时", "dep_time": "09:00", "arr_time": "12:00", "dep_station": "北京首都国际机场", "arr_station": "广州白云国际机场"},
                "high_speed_train": {"price": 1023, "duration": "8小时30分钟", "dep_time": "07:00", "arr_time": "15:30", "dep_station": "北京西站", "arr_station": "广州南站"},
                "train": {"price": 251, "duration": "22小时", "dep_time": "10:00", "arr_time": "次日08:00", "dep_station": "北京站", "arr_station": "广州站"},
                "bus": {"price": 550, "duration": "28小时", "dep_time": "16:00", "arr_time": "次日20:00", "dep_station": "北京丽泽桥汽车站", "arr_station": "广州天河客运站"},
            },
            ("北京", "深圳"): {
                "distance": 2160,
                "plane": {"price": 850, "duration": "3小时20分钟", "dep_time": "08:00", "arr_time": "11:20", "dep_station": "北京首都国际机场", "arr_station": "深圳宝安国际机场"},
                "high_speed_train": {"price": 1077, "duration": "9小时", "dep_time": "07:00", "arr_time": "16:00", "dep_station": "北京西站", "arr_station": "深圳北站"},
                "train": {"price": 257, "duration": "23小时", "dep_time": "10:00", "arr_time": "次日09:00", "dep_station": "北京站", "arr_station": "深圳站"},
                "bus": {"price": 600, "duration": "30小时", "dep_time": "15:00", "arr_time": "次日21:00", "dep_station": "北京八王坟汽车站", "arr_station": "深圳福田汽车站"},
            },
            ("北京", "杭州"): {
                "distance": 1380,
                "plane": {"price": 500, "duration": "2小时20分钟", "dep_time": "09:00", "arr_time": "11:20", "dep_station": "北京首都国际机场", "arr_station": "杭州萧山国际机场"},
                "high_speed_train": {"price": 604, "duration": "5小时", "dep_time": "07:00", "arr_time": "12:00", "dep_station": "北京南站", "arr_station": "杭州东站"},
                "train": {"price": 189, "duration": "15小时", "dep_time": "10:00", "arr_time": "23:00", "dep_station": "北京站", "arr_station": "杭州站"},
                "bus": {"price": 380, "duration": "19小时", "dep_time": "17:00", "arr_time": "次日12:00", "dep_station": "北京四惠汽车站", "arr_station": "杭州汽车北站"},
            },
            ("北京", "成都"): {
                "distance": 1800,
                "plane": {"price": 700, "duration": "2小时50分钟", "dep_time": "08:30", "arr_time": "11:20", "dep_station": "北京首都国际机场", "arr_station": "成都天府国际机场"},
                "high_speed_train": {"price": 864, "duration": "7小时", "dep_time": "07:00", "arr_time": "14:00", "dep_station": "北京西", "arr_station": "成都东"},
                "train": {"price": 220, "duration": "20小时", "dep_time": "10:00", "arr_time": "次日06:00", "dep_station": "北京站", "arr_station": "成都站"},
                "bus": {"price": 480, "duration": "26小时", "dep_time": "15:00", "arr_time": "次日17:00", "dep_station": "北京六里桥汽车站", "arr_station": "成都五块石汽车站"},
            },
            ("上海", "广州"): {
                "distance": 1432,
                "plane": {"price": 600, "duration": "2小时10分钟", "dep_time": "09:00", "arr_time": "11:10", "dep_station": "上海浦东国际机场", "arr_station": "广州白云国际机场"},
                "high_speed_train": {"price": 858, "duration": "6小时30分钟", "dep_time": "07:00", "arr_time": "13:30", "dep_station": "上海虹桥站", "arr_station": "广州南站"},
                "train": {"price": 199, "duration": "16小时", "dep_time": "10:00", "arr_time": "次日02:00", "dep_station": "上海站", "arr_station": "广州站"},
                "bus": {"price": 400, "duration": "20小时", "dep_time": "16:00", "arr_time": "次日12:00", "dep_station": "上海长途汽车总站", "arr_station": "广州天河客运站"},
            },
            ("上海", "深圳"): {
                "distance": 1462,
                "plane": {"price": 650, "duration": "2小时20分钟", "dep_time": "08:00", "arr_time": "10:20", "dep_station": "上海浦东国际机场", "arr_station": "深圳宝安国际机场"},
                "high_speed_train": {"price": 898, "duration": "7小时", "dep_time": "07:00", "arr_time": "14:00", "dep_station": "上海虹桥站", "arr_station": "深圳北站"},
                "train": {"price": 203, "duration": "17小时", "dep_time": "10:00", "arr_time": "次日03:00", "dep_station": "上海站", "arr_station": "深圳站"},
                "bus": {"price": 420, "duration": "21小时", "dep_time": "15:00", "arr_time": "次日12:00", "dep_station": "上海南站", "arr_station": "深圳福田汽车站"},
            },
            ("上海", "杭州"): {
                "distance": 176,
                "plane": {"price": -1, "duration": "", "dep_time": "", "arr_time": "", "dep_station": "", "arr_station": ""},
                "high_speed_train": {"price": 73, "duration": "1小时15分钟", "dep_time": "07:00", "arr_time": "08:15", "dep_station": "上海虹桥站", "arr_station": "杭州东站"},
                "train": {"price": 23, "duration": "2小时", "dep_time": "07:30", "arr_time": "09:30", "dep_station": "上海站", "arr_station": "杭州站"},
                "bus": {"price": 65, "duration": "3小时", "dep_time": "07:00", "arr_time": "10:00", "dep_station": "上海汽车南站", "arr_station": "杭州汽车北站"},
            },
            ("广州", "深圳"): {
                "distance": 102,
                "plane": {"price": -1, "duration": "", "dep_time": "", "arr_time": "", "dep_station": "", "arr_station": ""},
                "high_speed_train": {"price": 74, "duration": "36分钟", "dep_time": "07:00", "arr_time": "07:36", "dep_station": "广州南站", "arr_station": "深圳北站"},
                "train": {"price": 24, "duration": "1小时10分钟", "dep_time": "07:30", "arr_time": "08:40", "dep_station": "广州站", "arr_station": "深圳站"},
                "bus": {"price": 55, "duration": "2小时", "dep_time": "07:00", "arr_time": "09:00", "dep_station": "广州天河客运站", "arr_station": "深圳福田汽车站"},
            },
            ("广州", "成都"): {
                "distance": 1620,
                "plane": {"price": 750, "duration": "2小时40分钟", "dep_time": "09:00", "arr_time": "11:40", "dep_station": "广州白云国际机场", "arr_station": "成都天府国际机场"},
                "high_speed_train": {"price": 720, "duration": "6小时30分钟", "dep_time": "07:00", "arr_time": "13:30", "dep_station": "广州南站", "arr_station": "成都东站"},
                "train": {"price": 200, "duration": "18小时", "dep_time": "10:00", "arr_time": "次日04:00", "dep_station": "广州站", "arr_station": "成都站"},
                "bus": {"price": 450, "duration": "24小时", "dep_time": "15:00", "arr_time": "次日15:00", "dep_station": "广州天河客运站", "arr_station": "成都五块石汽车站"},
            },
            ("成都", "重庆"): {
                "distance": 340,
                "plane": {"price": -1, "duration": "", "dep_time": "", "arr_time": "", "dep_station": "", "arr_station": ""},
                "high_speed_train": {"price": 96, "duration": "1小时50分钟", "dep_time": "07:00", "arr_time": "08:50", "dep_station": "成都东站", "arr_station": "重庆北站"},
                "train": {"price": 50, "duration": "3小时", "dep_time": "07:30", "arr_time": "10:30", "dep_station": "成都站", "arr_station": "重庆站"},
                "bus": {"price": 110, "duration": "4小时30分钟", "dep_time": "07:00", "arr_time": "11:30", "dep_station": "成都五块石汽车站", "arr_station": "重庆菜园坝汽车站"},
            },
        }

        key = (origin_city, destination_city)
        reversed_route = False
        if key not in city_pairs:
            key = (destination_city, origin_city)
            reversed_route = key in city_pairs
        
        data = city_pairs.get(key)
        
        if data:
            options = []
            
            if data["plane"]["price"] > 0:
                options.append(TransportationOption(
                    type="plane",
                    name="飞机",
                    price=data["plane"]["price"],
                    duration=data["plane"]["duration"],
                    departure_time=data["plane"]["dep_time"],
                    arrival_time=data["plane"]["arr_time"],
                    departure_station=data["plane"]["dep_station"],
                    arrival_station=data["plane"]["arr_station"],
                    description="直达航班,快捷舒适"
                ))
            
            options.append(TransportationOption(
                type="high_speed_train",
                name="高铁",
                price=data["high_speed_train"]["price"],
                duration=data["high_speed_train"]["duration"],
                departure_time=data["high_speed_train"]["dep_time"],
                arrival_time=data["high_speed_train"]["arr_time"],
                departure_station=data["high_speed_train"]["dep_station"],
                arrival_station=data["high_speed_train"]["arr_station"],
                description="高铁直达,舒适快捷"
            ))
            
            options.append(TransportationOption(
                type="train",
                name="火车",
                price=data["train"]["price"],
                duration=data["train"]["duration"],
                departure_time=data["train"]["dep_time"],
                arrival_time=data["train"]["arr_time"],
                departure_station=data["train"]["dep_station"],
                arrival_station=data["train"]["arr_station"],
                description="普通列车,经济实惠"
            ))
            
            options.append(TransportationOption(
                type="bus",
                name="长途汽车",
                price=data["bus"]["price"],
                duration=data["bus"]["duration"],
                departure_time=data["bus"]["dep_time"],
                arrival_time=data["bus"]["arr_time"],
                departure_station=data["bus"]["dep_station"],
                arrival_station=data["bus"]["arr_station"],
                description="长途汽车,沿途可欣赏风景"
            ))
            
            if reversed_route:
                for option in options:
                    option.departure_station, option.arrival_station = (
                        option.arrival_station, option.departure_station
                    )
                    option.description = f"反向线路静态参考；{option.description or ''}"
            for option in options:
                option.source = "static"
                option.is_estimated = True
            return options

        # 未知城市对不能用固定1000公里和拼接站名冒充班次。
        # 用户可填写已购票时刻，或由后续接入的官方票务数据源提供结构化班次。
        return []


# 创建全局服务实例
_amap_service = None


def get_amap_service() -> AmapService:
    """获取高德地图服务实例(单例模式)"""
    global _amap_service
    
    if _amap_service is None:
        _amap_service = AmapService()
    
    return _amap_service
