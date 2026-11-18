"""原生工具调用的离线协议与执行边界检查，不使用真实密钥或上游服务。"""
import copy
import json
from types import SimpleNamespace
from unittest import TestCase, mock

from openai.types.chat import ChatCompletion, ChatCompletionChunk

from app.infrastructure.llm_client import LLMClient, LLMResult
from app.infrastructure.simple_agent import SimpleAgent, ToolCallingError
from app.infrastructure.mcp_client import MCPToolAdapter, MCPToolExecutionError


def call(identity="c1", name="amap_maps_weather", arguments='{"city":"北京"}'):
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": arguments}}


class FakeTool:
    def __init__(self):
        self.call_expanded = mock.Mock(return_value='{"city":"北京","weather":"晴"}')

    def expanded_tools(self):
        return [{"name": "amap_maps_weather", "description": "查询天气", "read_only": True,
                 "schema": {"type": "object", "properties": {"city": {"type": "string", "minLength": 1}},
                            "required": ["city"]}}]


class FakeLLM:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def invoke_tools(self, messages, tools, **kwargs):
        self.requests.append((copy.deepcopy(messages), copy.deepcopy(tools), kwargs))
        return next(self.responses)


def ready(calls):
    return LLMResult(content="", tool_calls=calls, finish_reason="tool_calls")


def final():
    return LLMResult(content="查询完成", finish_reason="stop")


class FunctionCallingTests(TestCase):
    def agent(self, responses):
        llm, tool = FakeLLM(responses), FakeTool()
        agent = SimpleAgent("天气", llm, require_tool=True, allowed_tools={"amap_maps_weather"})
        agent.add_tool(tool)
        return agent, llm, tool

    def test_native_protocol_pairs_results_and_uses_schema(self):
        agent, llm, tool = self.agent([ready([call()]), final()])
        self.assertEqual(agent.run("北京天气"), "查询完成")
        tool.call_expanded.assert_called_once_with("amap_maps_weather", {"city": "北京"})
        messages, definitions, options = llm.requests[1]
        self.assertEqual(messages[-1]["role"], "tool")
        self.assertEqual(messages[-1]["tool_call_id"], "c1")
        self.assertTrue(json.loads(messages[-1]["content"])["ok"])
        self.assertEqual(definitions[0]["function"]["parameters"]["required"], ["city"])
        self.assertFalse(definitions[0]["function"]["parameters"]["additionalProperties"])
        self.assertEqual(llm.requests[0][2]["tool_choice"], "required")
        self.assertEqual(options["tool_choice"], "auto")
        self.assertNotIn("TOOL_CALL:", messages[0]["content"])

    def test_invalid_parameters_and_unknown_tools_never_execute(self):
        cases = [call(arguments='not json'), call(arguments='[]'), call(arguments='{}'),
                 call(arguments='{"city":123}'), call(arguments='{"city":"北京","extra":1}'),
                 call(arguments='{"city":NaN}'), call(arguments='{"city":"北京","city":"上海"}'),
                 call(name="delete_trip")]
        for invalid in cases:
            with self.subTest(invalid=invalid):
                agent, llm, tool = self.agent([ready([invalid]), final()])
                with self.assertRaises(ToolCallingError):
                    agent.run("查询")
                tool.call_expanded.assert_not_called()
                self.assertFalse(json.loads(llm.requests[1][0][-1]["content"])["ok"])
                self.assertEqual(agent._history, [])

    def test_parameter_repair_then_success(self):
        agent, llm, tool = self.agent([ready([call(arguments='{}')]), ready([call("c2")]), final()])
        self.assertEqual(agent.run("查询"), "查询完成")
        self.assertEqual(tool.call_expanded.call_count, 1)
        self.assertEqual(llm.requests[1][2]["tool_choice"], "required")

    def test_same_query_with_different_ids_reuses_read_only_result(self):
        agent, llm, tool = self.agent([ready([call(), call("c2")]), final()])
        agent.run("查询")
        self.assertEqual(tool.call_expanded.call_count, 1)
        replies = [m for m in llm.requests[1][0] if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in replies], ["c1", "c2"])
        self.assertEqual(replies[0]["content"], replies[1]["content"])

    def test_unknown_result_is_not_retried_or_reported_success(self):
        agent, llm, tool = self.agent([ready([call(), call("c2")]), final()])
        tool.call_expanded.side_effect = TimeoutError("private-key-must-not-leak")
        with self.assertRaises(ToolCallingError):
            agent.run("查询")
        self.assertEqual(tool.call_expanded.call_count, 1)
        payload = llm.requests[1][0][-1]["content"]
        self.assertIn("tool_failed", payload)
        self.assertNotIn("private-key", payload)

    def test_duplicate_ids_truncation_and_budget_stop_before_execution(self):
        cases = [ready([call(), call()]), LLMResult("", tool_calls=[call()], finish_reason="length"),
                 ready([call(str(i)) for i in range(13)])]
        for response in cases:
            agent, _, tool = self.agent([response])
            with self.assertRaises(ToolCallingError):
                agent.run("查询")
            tool.call_expanded.assert_not_called()
        agent, llm, tool = self.agent([ready([call()]), ready([call("c2")])])
        with self.assertRaises(ToolCallingError):
            agent.run("查询", max_tool_iterations=1)
        self.assertEqual(llm.requests[-1][2]["tool_choice"], "none")
        self.assertEqual(tool.call_expanded.call_count, 1)

    def test_adapter_blocks_write_tools_and_business_errors(self):
        manager = mock.Mock()
        manager.list_tools.return_value = [dict(name="maps_weather", description="天气", input_schema={}),
                                          dict(name="delete_trip", description="删除", input_schema={})]
        adapter = MCPToolAdapter(manager)
        agent = SimpleAgent("天气", mock.Mock())
        agent.add_tool(adapter)
        self.assertEqual(agent.list_tools(), ["amap_maps_weather"])
        manager.call_tool.return_value = '{"status":"0","info":"private"}'
        with self.assertRaises(MCPToolExecutionError):
            adapter.call_expanded("amap_maps_weather", {})

    def test_plain_agent_keeps_text_interface(self):
        llm = mock.Mock()
        llm.invoke.return_value = "普通回答"
        self.assertEqual(SimpleAgent("规划", llm).run("规划"), "普通回答")
        llm.invoke_tools.assert_not_called()
        from app.agents.trip_planner_agent import MultiAgentTripPlanner
        planner = object.__new__(MultiAgentTripPlanner)
        query = planner.build_attraction_query(SimpleNamespace(city="北京", preferences=["历史", "公园"]))
        self.assertNotIn("TOOL_CALL", query)
        self.assertIn("历史, 公园", query)

    def test_external_schema_reference_is_rejected_before_execution(self):
        agent, llm, tool = self.agent([])
        definitions = tool.expanded_tools()
        definitions[0]["schema"]["properties"]["city"] = {"$ref": "https://offline.invalid/schema"}
        tool.expanded_tools = mock.Mock(return_value=definitions)
        with self.assertRaises(ToolCallingError):
            agent.run("查询")
        self.assertEqual(llm.requests, [])
        tool.call_expanded.assert_not_called()


class NativeClientTests(TestCase):
    def client(self):
        client = object.__new__(LLMClient)
        client._proxy_broken = False
        client.model, client.api_key, client.base_url = "offline", "fake", "https://offline.invalid"
        client.temperature, client.max_tokens = 1.0, 200
        client._client = mock.Mock()
        return client

    def test_non_streaming_preserves_tool_calls_and_plain_text(self):
        client = self.client()
        client._client.chat.completions.create.return_value = ChatCompletion.model_validate({
            "id": "r1", "created": 0, "model": "offline", "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [call()]}}]})
        result = client.invoke_tools([], [{"type": "function", "function": {"name": "test", "parameters": {}}}])
        self.assertEqual(result.tool_calls, [call()])
        self.assertEqual(result.finish_reason, "tool_calls")
        self.assertEqual(client.invoke([]), "")

    def test_direct_stream_assembles_multiple_call_fragments(self):
        client = self.client()
        def chunk(delta, finish=None):
            return ChatCompletionChunk.model_validate({"id": "r", "created": 0, "model": "offline",
                "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})
        pieces = [chunk({"tool_calls": [{"index": 1, "id": "c2", "type": "function", "function": {
                   "name": "second", "arguments": "{}"}}, {"index": 0, "id": "c1", "type": "function",
                   "function": {"name": "weather", "arguments": '{"city":'}}]}),
                  chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"北京"}'}}]}, "tool_calls")]
        stream = mock.MagicMock()
        stream.__iter__.return_value = iter(pieces)
        direct = mock.Mock()
        direct.chat.completions.create.return_value = stream
        with mock.patch.object(client, "_get_direct_client", return_value=direct):
            result = client._direct_complete_guarded([], tools=[{"type": "function"}])
        self.assertEqual([c["id"] for c in result.tool_calls], ["c1", "c2"])
        self.assertEqual(json.loads(result.tool_calls[0]["function"]["arguments"]), {"city": "北京"})
        self.assertEqual(result.finish_reason, "tool_calls")

    def test_proxy_failure_uses_native_result_from_fallback(self):
        client = self.client()
        client._client.chat.completions.create.side_effect = TimeoutError("timeout")
        expected = ready([call()])
        with mock.patch.object(client, "_direct_complete", return_value=expected) as fallback:
            result = client.invoke_tools([], [{"type": "function"}])
        self.assertEqual(result.tool_calls, [call()])
        self.assertIn("tools", fallback.call_args.kwargs)
        self.assertTrue(client._proxy_broken)
