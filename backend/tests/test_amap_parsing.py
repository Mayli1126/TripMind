import json
from unittest import TestCase

from app.services.amap_service import AmapService, _decode_mcp_payload


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
