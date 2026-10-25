"""轻量 SimpleAgent 替代品（替代 hello_agents.agents.SimpleAgent）。

保持与原实现一致的对外行为：
- run(query) -> 最终文本回复；
- [TOOL_CALL:{tool_name}:{parameters}] 文本标记触发工具调用，最多 max_tool_iterations 轮；
- 参数解析：JSON 优先，其次 key=value,key=value，再做 schema 类型转换；
- add_tool / clear_history / list_tools 接口兼容。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from .llm_client import LLMClient
from .mcp_client import MCPToolAdapter

_TOOL_CALL_PATTERN = re.compile(
    r"\[TOOL_CALLS?\s*:?\s*([A-Za-z0-9_]+)[\s:]+([^\]]+)\]"
)

_TOOL_PROMPT_SECTION = """

## 可用工具
你可以使用以下工具来帮助回答问题：
{tools_description}

## 工具调用格式
当需要使用工具时，请使用以下格式：
`[TOOL_CALL:{{tool_name}}:{{parameters}}]`

### 参数格式说明
1. **多个参数**：使用 `key=value` 格式，用逗号分隔
2. **单个参数**：直接使用 `key=value`
3. **简单查询**：可以直接传入文本

### 重要提示
- 参数名必须与工具定义的参数名完全匹配
- 数字参数直接写数字，不需要引号
- 工具调用结果会自动插入到对话中，然后你可以基于结果继续回答
"""


class SimpleAgent:
    """基于文本标记工具循环的极简 Agent。"""

    def __init__(
        self,
        name: str,
        llm: LLMClient,
        system_prompt: Optional[str] = None,
    ) -> None:
        self.name = name
        self.llm = llm
        self.system_prompt = system_prompt or "你是一个有用的AI助手。"
        self.tools: List[MCPToolAdapter] = []
        self._history: List[Dict[str, str]] = []

    # ------------------------------------------------------------------
    def add_tool(self, tool: MCPToolAdapter, auto_expand: bool = True) -> None:
        if tool not in self.tools:
            self.tools.append(tool)

    def clear_history(self) -> None:
        self._history.clear()

    def list_tools(self) -> List[str]:
        names: List[str] = []
        for adapter in self.tools:
            names.extend(tool["name"] for tool in adapter.expanded_tools())
        return names

    # ------------------------------------------------------------------
    def _enhanced_system_prompt(self) -> str:
        if not self.tools:
            return self.system_prompt
        lines: List[str] = []
        for adapter in self.tools:
            for tool in adapter.expanded_tools():
                desc = tool["description"].splitlines()[0] if tool["description"] else ""
                # 参数签名直接写入清单，避免模型猜参数名。
                props = (tool.get("schema") or {}).get("properties", {})
                sig = ", ".join(f"{key}:{info.get('type', 'string')}" for key, info in props.items())
                lines.append(f"- {tool['name']}({sig}): {desc}")
        return self.system_prompt + _TOOL_PROMPT_SECTION.format(tools_description="\n".join(lines))

    @staticmethod
    def _parse_tool_calls(text: str) -> List[Dict[str, str]]:
        calls = []
        for match in _TOOL_CALL_PATTERN.finditer(text or ""):
            tool_name, parameters = match.group(1), match.group(2)
            calls.append({
                "tool_name": tool_name.strip(),
                "parameters": parameters.strip(),
                "original": match.group(0),
            })
        return calls

    def _convert_param_types(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        types: Dict[str, str] = {}
        for adapter in self.tools:
            found = adapter.param_types(tool_name)
            if found:
                types = found
                break
        if not types:
            return params
        converted: Dict[str, Any] = {}
        for key, value in params.items():
            expected = types.get(key, "string")
            try:
                if expected in ("number", "integer") and isinstance(value, str):
                    converted[key] = float(value) if expected == "number" else int(float(value))
                elif expected == "boolean" and isinstance(value, str):
                    converted[key] = value.lower() in ("true", "1", "yes")
                else:
                    converted[key] = value
            except (TypeError, ValueError):
                converted[key] = value
        return converted

    @staticmethod
    def _parse_parameters(parameters: str) -> Dict[str, Any]:
        text = (parameters or "").strip()
        if not text:
            return {}
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
                return parsed if isinstance(parsed, dict) else {"input": parsed}
            except json.JSONDecodeError:
                pass
        params: Dict[str, Any] = {}
        if "=" in text:
            for pair in text.split(","):
                if "=" in pair:
                    key, value = pair.split("=", 1)
                    params[key.strip()] = value.strip()
        else:
            params = {"keywords": text}
        return params

    def _execute_tool_call(self, tool_name: str, parameters: str) -> str:
        for adapter in self.tools:
            server_name = adapter.get_server_tool_name(tool_name)
            if f"{adapter.name}_{server_name}" == tool_name:
                params = self._parse_parameters(parameters)
                params = self._convert_param_types(tool_name, params)
                result = adapter.call_expanded(tool_name, params)
                return f"🔧 工具 {tool_name} 执行结果：\n{result}"
        return f"❌ 错误：未找到工具 '{tool_name}'"

    # ------------------------------------------------------------------
    def run(self, input_text: str, max_tool_iterations: int = 3, **kwargs: Any) -> str:
        messages: List[Dict[str, str]] = [{"role": "system", "content": self._enhanced_system_prompt()}]
        messages.extend(self._history)
        messages.append({"role": "user", "content": input_text})

        final_response = ""
        for iteration in range(max_tool_iterations):
            response = self.llm.invoke(messages, **kwargs)
            tool_calls = self._parse_tool_calls(response) if self.tools else []
            if not tool_calls:
                final_response = response
                break

            results: List[str] = []
            clean_response = response
            for call in tool_calls:
                results.append(self._execute_tool_call(call["tool_name"], call["parameters"]))
                clean_response = clean_response.replace(call["original"], "")
            messages.append({"role": "assistant", "content": clean_response})
            messages.append({
                "role": "user",
                "content": "工具执行结果：\n" + "\n\n".join(results) + "\n\n请基于这些结果给出完整的回答。",
            })
        else:
            final_response = self.llm.invoke(messages, **kwargs)

        self._history.append({"role": "user", "content": input_text})
        self._history.append({"role": "assistant", "content": final_response})
        return final_response
