"""示例需符合真实协议，且不允许示例身份泄漏到实际行程。"""
import json
import unittest
from unittest.mock import Mock

from test_planning_feedback import fixture
from app.services.planner_examples import INITIAL_EXAMPLE, REPAIR_EXAMPLE
from app.services.planner_arrangement_service import Arrangement, invoke_planner, arrange_researched_plan
from app.services.planning_repair_service import RepairArrangement


class PlannerExamplesTests(unittest.TestCase):
    def test_thinking_options_only_target_supported_provider_without_switching_model(self):
        from types import SimpleNamespace
        from app.services.planner_arrangement_service import planner_call_options
        llm = SimpleNamespace(model='deepseek-v4.1-flash-1017', base_url='https://test.cn-beijing.maas.aliyuncs.com/compatible-mode/v1', max_tokens=8192)
        options = planner_call_options(llm)
        self.assertEqual(options['extra_body'], {'enable_thinking':True})
        self.assertEqual(options['response_format'], {'type':'json_object'})
        self.assertEqual(options['timeout'],180)
        llm.base_url='https://other-provider.example/v1'
        self.assertNotIn('extra_body',planner_call_options(llm))
        self.assertEqual(llm.model,'deepseek-v4.1-flash-1017')

    def test_task_budgets_are_independent_and_do_not_change_reasoning_strength(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from app.services.planner_arrangement_service import planner_call_options
        llm=SimpleNamespace(model='deepseek-v4.1-flash-1017',base_url='https://test.cn-beijing.maas.aliyuncs.com/v1',max_tokens=8192,timeout=180)
        with patch.dict('os.environ',{'LLM_LOCAL_REPAIR_MAX_TOKENS':'6000','LLM_LOCAL_REPAIR_TIMEOUT':'90',
                                    'LLM_SCHEMA_REPAIR_MAX_TOKENS':'7000'},clear=True):
            match=planner_call_options(llm,'local_repair')
            plan=planner_call_options(llm,'arrangement')
            repair=planner_call_options(llm,'schema_repair')
        self.assertEqual((match['max_tokens'],match['timeout']),(6000,90))
        self.assertEqual((plan['max_tokens'],plan['timeout']),(8192,180))
        self.assertEqual(repair['max_tokens'],7000)
        for options in [match,plan,repair]:
            self.assertEqual(options['extra_body'],{'enable_thinking':True})
            self.assertNotIn('reasoning_effort',options)
            self.assertNotIn('thinking_budget',options['extra_body'])

    def test_structured_timeout_does_not_retry_or_switch_provider(self):
        from contextlib import nullcontext
        from unittest.mock import patch
        from app.infrastructure.llm_client import LLMClient
        llm = LLMClient.__new__(LLMClient)
        llm.model='deepseek-v4.1-flash-1017'
        llm.base_url='https://test.cn-beijing.maas.aliyuncs.com/compatible-mode/v1'
        llm.api_key='test-only'
        llm.temperature=1
        llm._proxy_broken=False
        llm._client=Mock()
        llm._client.chat.completions.create.side_effect=TimeoutError('test timeout')
        with patch('app.infrastructure.provider_guard.provider_guard.slot',return_value=nullcontext()):
            with self.assertRaises(TimeoutError):
                llm.invoke_result_once([{'role':'user','content':'JSON'}],timeout=45,max_tokens=8192)
        llm._client.chat.completions.create.assert_called_once()
        self.assertFalse(llm._proxy_broken)

    def test_examples_match_schemas_and_repair_respects_frozen_day(self):
        generated = Arrangement.model_validate(INITIAL_EXAMPLE['output'])
        repaired = RepairArrangement.model_validate(REPAIR_EXAMPLE['output'])
        self.assertEqual(len(generated.days), 2)
        self.assertEqual([d.date for d in repaired.days], REPAIR_EXAMPLE['input']['editable_dates'])
        day = repaired.days[0]
        additions = {p.candidate_id for p in day.additional_places}
        self.assertTrue(additions.issubset(day.ordered_place_ids))
        self.assertIn('EXAMPLE_EXISTING_LIUYUAN', day.ordered_place_ids)
        self.assertEqual(repaired.duration_updates, [])

    def test_real_input_is_last_and_only_one_model_call_is_used(self):
        llm = Mock(max_tokens=8192)
        llm.invoke.return_value = '{}'
        payload = {'city':'杭州', 'candidates':[{'id':'actual-id'}]}
        invoke_planner(llm, '协议', payload, example=INITIAL_EXAMPLE)
        llm.invoke.assert_called_once()
        messages = llm.invoke.call_args.args[0]
        self.assertEqual([m['role'] for m in messages], ['system','user','assistant','user'])
        self.assertTrue(json.loads(messages[1]['content'])['example_only'])
        self.assertEqual(json.loads(messages[-1]['content']), payload)
        self.assertIn('严禁照抄', messages[0]['content'])

    def test_copying_example_output_is_rejected_for_real_request(self):
        _, request = fixture()
        llm = Mock(max_tokens=8192)
        llm.invoke.return_value = json.dumps(INITIAL_EXAMPLE['output'])
        with self.assertRaisesRegex(ValueError, '日期'):
            arrange_researched_plan(request, {}, llm)
        self.assertEqual(llm.invoke.call_count,2)


if __name__ == '__main__':
    unittest.main()
