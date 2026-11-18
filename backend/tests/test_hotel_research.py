"""快速酒店检索离线回归；不调用真实高德或模型。"""
import unittest
from unittest.mock import Mock, patch
from datetime import date, timedelta

from app.models.schemas import TripRequest, POIInfo, Location, DayPlan, TripPlan
from app.services.amap_service import AmapService
from app.services.hotel_research_service import research_hotels, bind_hotels_to_plan
from app.services.price_reference_service import enrich_amap_costs, enrich_llm_estimates
from app.graphs import planning_graph as pg


def request():
    day = (date.today() + timedelta(days=7)).isoformat()
    return TripRequest(city="北京", start_date=day, end_date=day, travel_days=1,
                       transportation="公共交通", accommodation="舒适型酒店")


def poi(index=1, **changes):
    values = dict(id=f"H{index}", name=f"酒店{index}", city_name="北京市", type="住宿服务;宾馆酒店",
                  type_code="100100", address="北京某街", location=Location(longitude=116.4, latitude=39.9))
    values.update(changes)
    return POIInfo(**values)


class HotelResearchTests(unittest.TestCase):
    def test_single_query_filters_wrong_city_duplicates_and_keeps_five(self):
        amap = Mock()
        amap.search_hotel_pois.return_value = [poi(city_name="天津市"), poi(type="停车场", type_code="150900"),
            poi(), poi(), *[poi(i) for i in range(2, 9)]]
        hotels, warning = research_hotels(request(), amap)
        amap.search_hotel_pois.assert_called_once_with("酒店", "北京")
        self.assertIsNone(warning)
        self.assertEqual(len(hotels), 5)
        self.assertTrue(all(h.poi_id and h.location and h.price_policy == "budget_only" for h in hotels))
        self.assertTrue(all(h.amap_average_cost is None for h in hotels))

    def test_hotel_http_path_is_bounded_to_one_attempt(self):
        import requests
        amap = AmapService()
        with patch("app.services.amap_service.requests.get", side_effect=requests.Timeout()) as get, \
             patch("app.services.amap_service._wait_for_amap_poi_slot"), \
             patch("app.infrastructure.provider_guard.map_http", side_effect=lambda call: call()):
            with self.assertRaises(RuntimeError):
                amap.search_hotel_pois("酒店", "北京")
        self.assertEqual(get.call_count, 1)
        self.assertEqual(get.call_args.kwargs["timeout"], 5)

    def test_query_failure_does_not_retry_or_call_agent(self):
        planner = Mock()
        with patch.object(pg, "_get_planner", return_value=planner), patch.object(pg, "research_hotels", return_value=([], "酒店待确认")):
            result = pg.hotel_research_node({"request": request()}, {})
        planner.run_agent_safely.assert_not_called()
        self.assertEqual(result["context"]["hotel_candidates"], [])
        self.assertTrue(result["warnings"])
        amap = Mock()
        amap.search_hotel_pois.side_effect = TimeoutError()
        hotels, warning = research_hotels(request(), amap)
        self.assertEqual(hotels, [])
        self.assertTrue(warning)
        self.assertEqual(amap.search_hotel_pois.call_count, 1)

    def test_real_hotel_binding_and_no_price_enrichment_requests(self):
        amap = Mock()
        amap.search_hotel_pois.return_value = [poi()]
        req = request()
        hotels, _ = research_hotels(req, amap)
        plan = TripPlan(city=req.city, start_date=req.start_date, end_date=req.end_date, days=[
            DayPlan(date=req.start_date, day_index=0, description="一天", transportation="公共交通",
                    accommodation=req.accommodation, attractions=[], meals=[])], overall_suggestions="建议")
        bind_hotels_to_plan(plan, req, [h.model_dump(mode="json") for h in hotels])
        self.assertEqual(plan.days[0].hotel.poi_id, "H1")
        self.assertEqual(plan.days[0].hotel.address, "北京某街")
        with patch("app.services.amap_service.AmapService._search_poi_cached_direct") as query, \
             patch("app.services.llm_service.get_llm") as llm:
            enrich_amap_costs(plan)
            enrich_llm_estimates(plan, req)
        query.assert_not_called()
        llm.assert_not_called()
        bind_hotels_to_plan(plan, req, [])
        self.assertIsNone(plan.days[0].hotel.location)
        self.assertGreater(plan.days[0].hotel.estimated_cost, 0)
        self.assertIn("待确认", plan.days[0].hotel.name)
