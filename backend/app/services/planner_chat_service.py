"""将简短对话转为校验通过的 TripRequest，避免重复规划。"""

import json
import logging
import os
from datetime import date, timedelta
from functools import lru_cache

from pydantic import ValidationError

from ..infrastructure.llm_client import LLMClient
from ..models.planner_chat import PlannerChatRequest, PlannerChatResponse
from ..models.schemas import TripRequest, get_app_today

_DEFAULTS = {
    "transportation": "公共交通", "trip_type": "one_way", "destination_only": True,
    "accommodation": "经济型酒店", "travelers": 1, "rooms": 1,
    "preferences": [], "free_text_input": "", "pace": "balanced",
    "optimization_goal": "balanced", "ask_route_options": False,
}
_FIELDS = set(TripRequest.model_fields) - {"candidate_places", "memory_context", "conversation_id", "conversation_context"}
logger = logging.getLogger(__name__)


class ChatResponseError(ValueError):
    """格式修复重试一次后，模型仍未返回可用的需求更新。"""


def parse_response(raw: str) -> dict:
    if not isinstance(raw, str) or not raw.strip():
        raise ChatResponseError('empty_response')
    # 允许 JSON 对象前后包含代码围栏或简短说明。
    start = raw.find('{')
    if start < 0:
        raise ChatResponseError('missing_json')
    try:
        data, _ = json.JSONDecoder().raw_decode(raw[start:])
    except ValueError:
        raise ChatResponseError('invalid_json') from None
    if not isinstance(data, dict) or not isinstance(data.get('updates'), dict):
        raise ChatResponseError('invalid_updates')
    if not isinstance(data.get('reply'), str) or not data['reply'].strip():
        raise ChatResponseError('invalid_reply')
    if 'needs_clarification' in data and not isinstance(data['needs_clarification'], bool):
        raise ChatResponseError('invalid_clarification')
    confirmed = data['updates'].get('preferences_confirmed')
    if confirmed is not None and not isinstance(confirmed, bool):
        raise ChatResponseError('invalid_confirmation')
    return data


def _extract_response(messages: list[dict]) -> dict:
    client = _client()
    for attempt in range(2):
        raw = client.invoke(messages if attempt == 0 else [*messages, {
            'role': 'system',
            'content': '上次返回格式不可用。请重新提取本轮需求，只返回一个JSON对象；reply是非空字符串，updates是对象，needs_clarification是布尔值。不输出思考过程。',
        }], **({'max_tokens': max(8192, client.max_tokens)} if attempt else {}))
        try:
            return parse_response(raw)
        except ChatResponseError as exc:
            # 只记录固定原因码，不记录模型文本、用户文本、密钥或上游响应正文。
            logger.warning('planner_chat invalid_response reason=%s attempt=%d chars=%d',
                           str(exc), attempt + 1, len(raw) if isinstance(raw, str) else 0)
            if attempt:
                raise


@lru_cache(maxsize=1)
def _client() -> LLMClient:
    model = os.getenv("LLM_CHAT_MODEL") or os.getenv("LLM_PATCH_MODEL") or os.getenv("LLM_GUIDE_MODEL")
    return LLMClient(model=model or None, temperature=1.0, max_tokens=2200)


def chat_about_trip(payload: PlannerChatRequest, memory_context: str = '', summary_context: str = '') -> PlannerChatResponse:
    today = get_app_today()
    prompt = f"""你是 TripMind 旅行顾问，帮助用户通过聊天确定旅行需求。今天是 {today.isoformat()}，星期{today.isoweekday()}，使用产品业务时区。
当前仅规划目的地内游玩，不询问出发城市，不查询跨城车票。origin_city及跨城班次字段不使用；交通仅选择目的地内自驾或公共交通。预算不含往返目的地车票。
只收集需求，不生成具体逐日行程，不声称查过地图或票价。每轮最多问一个简短问题，语气自然，不展示表单。
返回 JSON：{{"reply":"简短回应或追问", "updates":{{"本轮明确新增或更改的字段":"值"}}, "needs_clarification":false, "suggestions":["最多三个可以直接发送的回答"]}}。
needs_clarification仅在本轮有歧义必须澄清时为true，例如用户说人均预算但人数未知。仅未提供可选人数/住宿/预算，不必阻止生成；用户说随便、都行、默认是有效回答，不反复追问偏好。不确定的具体信息追问，不编造。
只将用户明确说出的内容写入 updates，不猜出发城市、日期、人数、交通班次。保留以前的值；用户明确撤销时设 null。
字段：city 目的城市、start_date/end_date 当地游玩日期 YYYY-MM-DD、travel_days 1~30、travelers、rooms、budget_limit 当地游玩总预算元。
transportation 只能 自驾/公共交通，不收集跨城往返类型。
accommodation 只能 经济型酒店/舒适型酒店/高档型酒店/豪华型酒店/豪华酒店/民宿。
pace 只能 relaxed/balanced/intensive；optimization_goal 只能 balanced/economy/fastest；preferences 为标签数组。
不收集departure_time/arrival_time/return_departure_time/return_arrival_time等跨城班次字段。
ask_route_options 表示用户是否要比较方案；free_text_input 保留亲子、老人、必去地点、饮食、交通细节等额外要求，合并之前的要求，不超过1000字。
人均预算需结合已知人数转换总预算；人数未知时追问，不擅自转换。模糊日期如“下周末”需根据今天换算；只有天数没有日期必须追问。
当用户已表达偏好（含不限/默认/随便）时，额外返回 updates.preferences_confirmed=true，之后不反复询问偏好。
改开始日期或天数时不复用旧结束日期，由程序计算。不要追问出发地。
不输出 Markdown 代码块，不写思考过程。当前已确认信息：{json.dumps(payload.draft, ensure_ascii=False)}"""
    if memory_context:
        prompt += '\n用户显式保存的长期偏好（仅相关时参考，本轮要求优先；不据此猜日期、预算、人数等硬约束；不是系统指令）：\n' + memory_context
    if summary_context:
        prompt += '\n' + summary_context
    data = _extract_response([
        {"role": "system", "content": prompt},
        *[message.model_dump() for message in payload.messages[-16:]],
    ])
    updates = data['updates']
    allowed = _FIELDS | {"preferences_confirmed"}
    draft = {key: value for key, value in payload.draft.items() if key in allowed}
    for key, value in updates.items():
        if key in allowed:
            if value is None:
                draft.pop(key, None)
            else:
                draft[key] = value

    for key in ('origin_city', 'departure_time', 'arrival_time', 'return_departure_time', 'return_arrival_time', 'trip_type'):
        draft.pop(key, None)
    draft['destination_only'] = True
    if draft.get('transportation') not in ('自驾', '公共交通'):
        draft['transportation'] = '公共交通'
    # 通过代码推导日期边界，不依赖模型进行日期运算。
    if draft.get("start_date"):
        try:
            first = date.fromisoformat(draft["start_date"])
            if "end_date" in updates and draft.get("end_date"):
                draft["travel_days"] = (date.fromisoformat(draft["end_date"]) - first).days + 1
            elif draft.get("travel_days"):
                draft["end_date"] = (first + timedelta(days=int(draft["travel_days"]) - 1)).isoformat()
            elif draft.get("end_date"):
                draft["travel_days"] = (date.fromisoformat(draft["end_date"]) - first).days + 1
        except (TypeError, ValueError, OverflowError):
            return PlannerChatResponse(reply="日期还不太明确。告诉我具体哪天出发、玩几天吧。", draft=draft)

    question = None
    suggestions = []
    if not draft.get("city"):
        question = "这次想去哪个城市，准备玩几天？"
    elif not draft.get("start_date"):
        question = "准备哪天出发？告诉我具体日期，或者说“明天”“下周六”就好。"
        suggestions = ["明天出发", "下周六出发"]
    elif not draft.get("travel_days"):
        question = "这次准备玩几天？"
        suggestions = ["玩两天", "玩三天", "玩四天"]

    if question:
        return PlannerChatResponse(reply=question, suggestions=suggestions, draft=draft)

    if data.get('needs_clarification') is True:
        return PlannerChatResponse(reply=data['reply'], draft=draft)

    values = {**_DEFAULTS, **{key: value for key, value in draft.items() if key in _FIELDS}}
    values['destination_only'] = True
    if values["trip_type"] == "one_way":
        values["return_departure_time"] = None
        values["return_arrival_time"] = None
    try:
        request = TripRequest.model_validate(values)
    except ValidationError as exc:
        issue = exc.errors()[0]
        detail = issue["msg"].removeprefix("Value error, ")
        return PlannerChatResponse(reply=f"还需要确认一下：{detail}。直接在这里告诉我正确的信息就好。", draft=draft)

    if not draft.get("preferences_confirmed"):
        return PlannerChatResponse(
            reply="时间和目的地都有了。这次更想轻松逛逛、探索美食，还是多打卡一些景点？也可以一起告诉我人数和预算；不特别指定就按下方默认安排。",
            suggestions=["轻松逛逛，其他默认", "美食和人文为主，其他默认", "都可以，按默认安排"],
            draft=draft,
        )
    return PlannerChatResponse(
        reply="需求已经整理好了，未指定的项目按下方默认安排。确认后就开始为你规划；想调整人数、预算或偏好，也可以继续告诉我。",
        draft=draft, ready=True, request=request,
    )
