import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from app.services import amap_service as amap_module
from app.services.amap_service import AmapService, _decode_mcp_payload
from app.models.schemas import Location


class FakeTool:
    def __init__(self, payload):
        self.payload = payload

    def run(self, _request):
        return self.payload


def make_service(payload) -> AmapService:
    service = object.__new__(AmapService)
    service.mcp_tool = FakeTool(payload)
    return service


class AmapPayloadTests(TestCase):
    def setUp(self):
        with amap_module._POI_CACHE_LOCK:
            amap_module._POI_MEMORY_CACHE.clear()
            amap_module._POI_INFLIGHT.clear()

    def test_decodes_content_text_wrapper(self):
        raw = json.dumps({
            "content": [{"type": "text", "text": json.dumps({"pois": [{"name": "故宫"}]})}]
        })
        self.assertEqual(_decode_mcp_payload(raw)["pois"][0]["name"], "故宫")

    def test_parses_poi_result(self):
        service = make_service(json.dumps({
            "pois": [{
                "id": "B000A",
                "name": "故宫博物院",
                "type": "风景名胜",
                "address": "景山前街4号",
                "location": "116.397,39.916",
            }]
        }))
        result = service.search_poi("故宫", "北京")
        self.assertEqual(result[0].name, "故宫博物院")
        self.assertAlmostEqual(result[0].location.longitude, 116.397)

    def test_text_search_without_location_fetches_detail(self):
        # 高德 maps_text_search 常不带 location 坐标：应按 poi_id 自动调详情接口补齐
        class DetailFakeTool:
            def __init__(self):
                self.calls = []

            def run(self, request):
                self.calls.append(request["tool_name"])
                if request["tool_name"] == "maps_search_detail":
                    # 详情接口顶层就是 POI 对象（无 pois 列表包装）
                    return json.dumps({
                        "id": "B0FFH62KNA", "name": "古鸡鸣寺",
                        "type": "寺庙", "address": "鸡鸣寺路1号",
                        "location": "118.796,32.06",
                    })
                # text search：有名称无坐标
                return json.dumps({"pois": [
                    {"id": "B0FFH62KNA", "name": "古鸡鸣寺", "address": "鸡鸣寺路1号", "typecode": "110205"},
                    {"id": "BV10431631", "name": "鸡鸣寺(地铁站)", "address": "北京东路", "typecode": "150500"},
                ]})

        tool = DetailFakeTool()
        service = object.__new__(AmapService)
        service.mcp_tool = tool
        result = service.search_poi("鸡鸣寺", "南京")
        self.assertIn("maps_search_detail", tool.calls)  # 触发了详情补齐
        self.assertEqual(result[0].name, "古鸡鸣寺")  # 名称精确匹配优先于地铁站
        self.assertAlmostEqual(result[0].location.longitude, 118.796)

    def test_text_search_without_location_and_detail_fails_raises(self):
        service = make_service(json.dumps({
            "pois": [{"id": "B000X", "name": "不存在的店", "address": "某路"}]
        }))
        with self.assertRaises(RuntimeError):
            service.search_poi("不存在的店", "南京")

    def test_direct_search_keeps_location_and_uses_memory_cache(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "status": "1",
            "pois": [{
                "id": "B000A8UIN8",
                "name": "故宫博物院",
                "address": "景山前街4号",
                "type": "风景名胜",
                "location": "116.397029,39.917839",
            }],
        }
        service = object.__new__(AmapService)
        service._prefer_direct_poi_search = True

        settings = SimpleNamespace(redis_enabled=False, amap_api_key="test-key")
        with patch.object(amap_module, "get_settings", return_value=settings), patch.object(
            amap_module, "_wait_for_amap_poi_slot"
        ), patch.object(
            amap_module.requests, "get", return_value=response
        ) as request:
            first = service.search_poi("故宫", "北京")
            second = service.search_poi("故宫", "北京")

        self.assertEqual(request.call_count, 1)
        self.assertEqual(first[0].id, "B000A8UIN8")
        self.assertAlmostEqual(first[0].location.longitude, 116.397029)
        self.assertEqual(second[0].location, first[0].location)

    def test_direct_search_retries_qps_error_only_for_failed_request(self):
        limited = Mock()
        limited.raise_for_status.return_value = None
        limited.json.return_value = {
            "status": "0", "info": "CUQPS_HAS_EXCEEDED_THE_LIMIT"
        }
        success = Mock()
        success.raise_for_status.return_value = None
        success.json.return_value = {
            "status": "1",
            "pois": [{
                "id": "B000A7O1CU", "name": "颐和园",
                "address": "新建宫门路19号", "type": "风景名胜",
                "location": "116.275179,39.999617",
            }],
        }
        service = object.__new__(AmapService)
        service._prefer_direct_poi_search = True

        settings = SimpleNamespace(redis_enabled=False, amap_api_key="test-key")
        with patch.object(amap_module, "get_settings", return_value=settings), patch.object(
            amap_module, "_wait_for_amap_poi_slot"
        ), patch.object(
            amap_module.time, "sleep"
        ), patch.object(
            amap_module.requests, "get", side_effect=[limited, success]
        ) as request:
            result = service.search_poi("颐和园", "北京")

        self.assertEqual(request.call_count, 2)
        self.assertEqual(result[0].name, "颐和园")

    def test_nearby_restaurant_search_uses_one_direct_around_request(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "status": "1",
            "pois": [{
                "id": "B0FOOD", "name": "附近餐厅", "address": "测试路1号",
                "type": "餐饮服务", "location": "116.401,39.901",
            }],
        }
        service = object.__new__(AmapService)
        service._prefer_direct_poi_search = True
        settings = SimpleNamespace(redis_enabled=False, amap_api_key="test-key")

        with patch.object(amap_module, "get_settings", return_value=settings), patch.object(
            amap_module, "_wait_for_amap_poi_slot"
        ), patch.object(amap_module.requests, "get", return_value=response) as request:
            result = service.search_restaurants(
                "午餐", "北京", Location(longitude=116.4, latitude=39.9), 1500
            )

        self.assertEqual(request.call_count, 1)
        self.assertIn("/place/around", request.call_args.args[0])
        self.assertEqual(request.call_args.kwargs["params"]["radius"], "1500")
        self.assertEqual(result[0].name, "附近餐厅")

    def test_parses_weather_forecasts(self):
        service = make_service(json.dumps({
            "forecasts": [{
                "casts": [{
                    "date": "2026-08-01",
                    "dayweather": "晴",
                    "nightweather": "多云",
                    "daytemp": "31",
                    "nighttemp": "24",
                    "daywind": "南",
                    "daypower": "1-3",
                }]
            }]
        }))
        result = service.get_weather("北京")
        self.assertEqual(result[0].day_weather, "晴")
        self.assertEqual(result[0].day_temp, 31)

    def test_parses_route(self):
        service = make_service(json.dumps({
            "route": {"paths": [{"distance": "1200", "duration": "900", "strategy": "步行"}]}
        }))
        result = service.plan_route("A", "B")
        self.assertEqual(result["distance"], 1200)
        self.assertEqual(result["duration"], 900)

    def test_unknown_city_pair_does_not_fabricate_stations_or_times(self):
        service = object.__new__(AmapService)
        self.assertEqual(service.get_transportation_options("武汉", "威海"), [])

    def test_reverse_static_route_swaps_stations(self):
        service = object.__new__(AmapService)
        option = next(
            item for item in service.get_transportation_options("上海", "北京")
            if item.type == "high_speed_train"
        )
        self.assertEqual(option.departure_station, "上海虹桥站")
        self.assertEqual(option.arrival_station, "北京南站")
