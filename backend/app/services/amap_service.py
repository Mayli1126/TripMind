"""高德地图MCP服务封装"""

from typing import List, Dict, Any, Optional, TYPE_CHECKING
from ..config import get_settings
from ..models.schemas import Location, POIInfo, WeatherInfo, TransportationOption
import json
import re

if TYPE_CHECKING:
    from ..infrastructure.mcp_client import MCPToolAdapter

# 全局MCP工具实例
_amap_mcp_tool = None


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

    Returns:
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
        self.mcp_tool = mcp_tool or get_amap_mcp_tool()
    
    def search_poi(self, keywords: str, city: str, citylimit: bool = True) -> List[POIInfo]:
        """
        搜索POI
        
        Args:
            keywords: 搜索关键词
            city: 城市
            citylimit: 是否限制在城市范围内
            
        Returns:
            POI信息列表
        """
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
                    type=str(best.get("type", best.get("typecode", ""))),
                    address=str(best.get("address", "")),
                    location=best_location,
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
                    type=str(item.get("type", item.get("category", ""))),
                    address=str(item.get("address", "")),
                    location=location,
                    tel=str(item["tel"]) if item.get("tel") else None,
                ))
            if not pois:
                raise ValueError("高德 MCP 返回的 POI 均无法解析出坐标")
            return pois

        except Exception as e:
            print(f"❌ POI搜索失败: {str(e)}")
            raise RuntimeError(f"POI搜索失败: {e}") from e

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
            # 顶层直接是 POI 对象
            if isinstance(detail_, dict) and detail_.get("name"):
                return detail_
            items = [
                item for item in _find_list(detail_, "pois", "results")
                if isinstance(item, dict)
            ]
            return items[0] if items else None
        except Exception as exc:
            print(f"⚠️ POI 详情获取失败（poi_id={poi_id}）: {exc}")
            return None
    
    def get_weather(self, city: str) -> List[WeatherInfo]:
        """
        查询天气
        
        Args:
            city: 城市名称
            
        Returns:
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
        
        Args:
            origin_address: 起点地址
            destination_address: 终点地址
            origin_city: 起点城市
            destination_city: 终点城市
            route_type: 路线类型 (walking/driving/transit)
            
        Returns:
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

        Args:
            address: 地址
            city: 城市

        Returns:
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
        
        Args:
            keywords: 搜索关键词（如"美食"、"餐厅"、"火锅"等）
            city: 城市
            location: 中心点坐标（可选，用于附近搜索）
            radius: 搜索半径（米）
            
        Returns:
            POI信息列表
        """
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

        Args:
            poi_id: POI ID

        Returns:
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

        Args:
            origin_city: 出发城市
            destination_city: 目的城市
            date: 出发日期

        Returns:
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

        Args:
            origin_city: 出发城市
            destination_city: 目的城市

        Returns:
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
