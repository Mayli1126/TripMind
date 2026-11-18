"""针对对话格式恢复与就绪状态的测试，不调用外部服务。"""
import json
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException

from app.api.routes import planner_chat as route
from app.models.planner_chat import PlannerChatRequest
from app.services import planner_chat_service as service


class PlannerChatTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock(max_tokens=2200)
        self.client_patch = patch.object(service, '_client', return_value=self.client)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.today_patch = patch.object(service, 'get_app_today', return_value=date(2026, 11, 1))
        self.today_patch.start()
        self.addCleanup(self.today_patch.stop)
        # 请求校验必须与对话服务使用相同的固定日期。
        self.schema_today_patch = patch('app.models.schemas.get_app_today', return_value=date(2026, 11, 1))
        self.schema_today_patch.start()
        self.addCleanup(self.schema_today_patch.stop)
        self.draft = dict(city='青岛', origin_city='武汉', travel_days=4,
                          start_date='2026-11-02', end_date='2026-11-05')

    def payload(self, text='都行，你看着安排', draft=None):
        return PlannerChatRequest(messages=[dict(role='user', content=text)],
                                  draft=self.draft if draft is None else draft)

    def response(self, updates=None, **kwargs):
        return json.dumps(dict(reply='好的', updates=updates or {}, **kwargs), ensure_ascii=False)

    def test_empty_response_retries_once_without_losing_user_message(self):
        self.client.invoke.side_effect = ['', self.response({'preferences_confirmed': True})]
        result = service.chat_about_trip(self.payload())
        self.assertTrue(result.ready)
        self.assertEqual(self.client.invoke.call_count, 2)
        retry = self.client.invoke.call_args
        self.assertEqual(retry.args[0][1]['content'], '都行，你看着安排')
        self.assertGreaterEqual(retry.kwargs['max_tokens'], 8192)

    def test_destination_only_does_not_require_origin_and_discards_legacy_tickets(self):
        self.client.invoke.return_value = self.response({'preferences_confirmed': True, 'origin_city': '武汉',
                                                        'departure_time': '08:00', 'arrival_time': '12:00'})
        draft = {k:v for k,v in self.draft.items() if k != 'origin_city'}
        result = service.chat_about_trip(self.payload(draft=draft))
        self.assertTrue(result.ready)
        self.assertTrue(result.request.destination_only)
        self.assertEqual(result.request.origin_city, '')
        self.assertIsNone(result.request.departure_time)
        self.assertEqual(result.request.transportation, '公共交通')

    def test_destination_request_skips_intercity_budget_and_time(self):
        from tests.test_trip_quality import make_request, make_day
        from app.models.schemas import TripRequest, TripPlan
        from app.services.trip_quality_service import trip_quality_service
        from app.services.day_pipeline_service import build_day_capacities
        request = TripRequest.model_validate({**make_request().model_dump(), 'destination_only': True})
        plan = TripPlan(city=request.city, start_date=request.start_date, end_date=request.end_date,
                        days=[make_day(0, 10), make_day(1, 10)], overall_suggestions='fixture')
        self.assertIsNone(trip_quality_service._build_intercity_journey(request, None, None))
        self.assertIsNone(trip_quality_service._build_return_journey(request, None, None))
        self.assertEqual(trip_quality_service.calculate_budget(plan, request, 999).intercity_transportation, 0)
        self.assertFalse(any('抵达后' in c.note or '返程' in c.note for c in build_day_capacities(request, None)))

    def test_invalid_updates_retries_instead_of_attribute_error(self):
        self.client.invoke.side_effect = ['{"reply":"好","updates":[]}', self.response()]
        self.assertFalse(service.chat_about_trip(self.payload()).ready)
        self.assertEqual(self.client.invoke.call_count, 2)

    def test_format_retries_are_bounded(self):
        self.client.invoke.return_value = 'not json'
        with self.assertRaises(service.ChatResponseError):
            service.chat_about_trip(self.payload())
        self.assertEqual(self.client.invoke.call_count, 2)

    def test_code_fence_and_trailing_explanation_are_accepted(self):
        self.client.invoke.return_value = '```json\n' + self.response() + '\n```\n说明 {无需执行}'
        self.assertFalse(service.chat_about_trip(self.payload()).ready)
        self.assertEqual(self.client.invoke.call_count, 1)

    def test_ready_reply_does_not_ask_another_question(self):
        self.client.invoke.return_value = self.response({'preferences_confirmed': True})
        # 即使已收集字段有效，模型也可能多问一个可选问题。
        self.client.invoke.return_value = self.client.invoke.return_value.replace('好的', '这次几位出行？')
        result = service.chat_about_trip(self.payload())
        self.assertTrue(result.ready)
        self.assertNotIn('几位', result.reply)
        self.assertEqual(result.request.travelers, 1)

    def test_ambiguity_does_not_offer_generation(self):
        self.client.invoke.return_value = self.response({'preferences_confirmed': True}, needs_clarification=True)
        result = service.chat_about_trip(self.payload('人均两千'))
        self.assertFalse(result.ready)
        self.assertIsNone(result.request)

    def test_missing_required_field_remains_a_question(self):
        self.client.invoke.return_value = self.response({'preferences_confirmed': True})
        result = service.chat_about_trip(self.payload(draft={'city': '青岛'}))
        self.assertFalse(result.ready)
        self.assertIn('出发', result.reply)

    def test_informal_date_and_days_derive_end_date(self):
        self.client.invoke.return_value = self.response({'start_date': '2026-11-02'})
        result = service.chat_about_trip(self.payload('就明儿吧', dict(city='青岛', origin_city='武汉', travel_days=4)))
        self.assertEqual(result.draft['end_date'], '2026-11-05')

    def test_string_confirmation_is_not_truthy_consent(self):
        self.client.invoke.side_effect = [self.response({'preferences_confirmed': 'false'}), self.response()]
        self.assertFalse(service.chat_about_trip(self.payload()).ready)

    def test_provider_failure_is_not_retried_as_format_failure(self):
        self.client.invoke.side_effect = RuntimeError('private-provider-body')
        with self.assertRaises(RuntimeError):
            service.chat_about_trip(self.payload())
        self.assertEqual(self.client.invoke.call_count, 1)

    def test_route_logs_metadata_not_exception_or_conversation(self):
        request = SimpleNamespace(state=SimpleNamespace(user={'id': 'test'}))
        with patch.object(route, 'get_settings', return_value=SimpleNamespace(redis_enabled=False)), \
             patch('app.services.conversation_store.memory_context', return_value=''), \
             patch.object(route, 'chat_about_trip', side_effect=RuntimeError('secret-key-and-user-text')), \
             self.assertLogs(route.logger, level='ERROR') as logs:
            with self.assertRaises(HTTPException) as error:
                route.send_message(self.payload(), request)
        self.assertEqual(error.exception.status_code, 502)
        self.assertNotIn('secret-key-and-user-text', str(logs.output))
        self.assertIn('type=RuntimeError', str(logs.output))


if __name__ == '__main__':
    unittest.main()
