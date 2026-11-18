from unittest import TestCase
from unittest.mock import patch
from app.models.schemas import TripPlan, Hotel
from app.services.attraction_hours_kb import AttractionHours
from app.services.price_reference_service import apply_price_references
from app.services.trip_quality_service import trip_quality_service
from tests.test_trip_quality import make_day, make_request


class PriceReferenceChecks(TestCase):
    def test_llm_missing_prices_one_batch_and_provenance(self):
        from app.services.price_reference_service import enrich_llm_estimates
        request, plan = self.plan()
        for a in plan.days[0].attractions:
            a.ticket_price = None
        plan.days[0].hotel = Hotel(name='fixture', estimated_cost=500)
        import json
        with patch('app.services.llm_service.get_llm') as model:
            count = len(plan.days[0].attractions)
            model.return_value.invoke.return_value = json.dumps({'prices': [
                *[{'id': i, 'amount': 80} for i in range(count)], {'id': count, 'amount': 360}]})
            enrich_llm_estimates(plan, request)
            self.assertEqual(model.return_value.invoke.call_count, 1)
        a = plan.days[0].attractions[0]
        self.assertEqual(a.ticket_price, 80)
        self.assertEqual(a.ticket_price_source, 'llm_estimate')
        self.assertEqual(plan.days[0].hotel.estimated_cost, 360)
        apply_price_references(plan)
        self.assertEqual(plan.days[0].hotel.price_source, 'llm_estimate')

    def test_llm_failure_does_not_fail_plan_or_invent_zero(self):
        from app.services.price_reference_service import enrich_llm_estimates
        request, plan = self.plan()
        for a in plan.days[0].attractions:
            a.ticket_price = None
        with patch('app.services.llm_service.get_llm', side_effect=TimeoutError()):
            result = enrich_llm_estimates(plan, request)
        self.assertIs(result, plan)
        self.assertIsNone(result.days[0].attractions[0].ticket_price)

    def test_amap_cost_parser_blank_zero_and_invalid(self):
        from app.services.amap_service import _parse_average_cost
        self.assertEqual(_parse_average_cost({'biz_ext': {'cost': '88.5'}}), 88.5)
        self.assertEqual(_parse_average_cost({'business': {'cost': '0'}}), 0)
        for value in ('', [], None, 'NaN', 'inf', '-1', '人均88元', True):
            self.assertIsNone(_parse_average_cost({'biz_ext': {'cost': value}}))

    def test_budget_prefers_amap_per_person_not_room_price(self):
        request, plan = self.plan()
        for a in plan.days[0].attractions:
            a.amap_average_cost = 25.5
        plan.days[0].hotel = Hotel(name='fixture', estimated_cost=500, amap_average_cost=120)
        budget = trip_quality_service.calculate_budget(plan, request)
        from math import ceil
        self.assertEqual(budget.total_attractions, ceil(25.5 * len(plan.days[0].attractions) * request.travelers))
        self.assertEqual(budget.total_hotels, 120 * request.travelers)
        self.assertEqual(budget.unknown_ticket_count, 0)

    def test_cost_enrichment_requires_exact_name_and_poi_identity(self):
        from types import SimpleNamespace
        from app.services.price_reference_service import enrich_amap_costs
        _, plan = self.plan()
        a = plan.days[0].attractions[0]
        a.poi_id = 'expected'
        with patch('app.services.amap_service.AmapService') as amap:
            amap.return_value._search_poi_cached_direct.return_value = [SimpleNamespace(name=a.name, id='wrong', amap_average_cost=90)]
            enrich_amap_costs(plan)
            self.assertIsNone(a.amap_average_cost)
            amap.return_value._search_poi_cached_direct.return_value = [SimpleNamespace(name=a.name, id='expected', amap_average_cost=90)]
            enrich_amap_costs(plan)
            self.assertEqual(a.amap_average_cost, 90)

    def plan(self):
        request = make_request()
        plan = TripPlan(city=request.city, start_date=request.start_date, end_date=request.end_date,
                        days=[make_day(0, 0)], overall_suggestions='fixture')
        return request, plan

    def test_unknown_zero_is_not_free_and_unpriced_budget_is_explicit(self):
        request, plan = self.plan()
        with patch('app.services.price_reference_service.ATTRACTION_HOURS_KB', {}):
            apply_price_references(plan)
        self.assertIsNone(plan.days[0].attractions[0].ticket_price)
        budget = trip_quality_service.calculate_budget(plan, request)
        self.assertGreater(budget.unknown_ticket_count, 0)
        self.assertIn('未计入', budget.calculation_note)

    def test_exact_reference_and_explicit_free_not_fuzzy_match(self):
        _, plan = self.plan()
        a = plan.days[0].attractions[0]
        with patch('app.services.price_reference_service.ATTRACTION_HOURS_KB',
                   {plan.city: {a.name: AttractionHours(reference_ticket_price=80)}}):
            apply_price_references(plan)
        self.assertEqual(a.ticket_price, 80)
        self.assertEqual(a.ticket_price_source, 'static_reference')
        a.ticket_price_source = 'unknown'
        with patch('app.services.price_reference_service.ATTRACTION_HOURS_KB',
                   {plan.city: {a.name: AttractionHours(reference_ticket_price=0)}}):
            apply_price_references(plan)
        self.assertEqual(a.ticket_price, 0)
        a.name += '附属展厅'; a.ticket_price_source = 'unknown'
        apply_price_references(plan)
        self.assertIsNone(a.ticket_price)

    def test_hotel_tier_reserve_not_presented_as_room_quote(self):
        _, plan = self.plan()
        plan.days[0].hotel = Hotel(name='fixture hotel', estimated_cost=500, price_range='500元/晚')
        apply_price_references(plan)
        hotel = plan.days[0].hotel
        self.assertEqual(hotel.price_range, '')
        self.assertEqual(hotel.estimated_cost, 500)
        self.assertIn('不是该酒店报价', hotel.price_note)
