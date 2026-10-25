"""系统提示词模板化模块 —— 拆分静态模板与动态变量，减少重复 Token 消耗。"""

from __future__ import annotations

from typing import List


class PlannerPromptTemplate:
    """Planner Agent 的静态系统提示词模板。

    将原先 200+ 行的完整提示词拆分为：
    1. 静态 JSON 格式规范（Agent 初始化时一次性传入）
    2. 动态编排规则（每次 query 按需组装）

    预期收益：每次 Planner 调用减少约 1500-2000 tokens 的重复系统提示。
    """

    # ── 静态部分：JSON Schema 与格式约束（只发一次）──
    STATIC_SYSTEM_PROMPT = """你是行程规划专家。请严格按下方 JSON Schema 输出旅行计划。

```json
{
  "city": "城市名称",
  "start_date": "YYYY-MM-DD",
  "end_date": "YYYY-MM-DD",
  "days": [
    {
      "date": "YYYY-MM-DD",
      "day_index": 0,
      "theme": "当日主题",
      "description": "每日概述（≤120字）",
      "transportation": "交通方式",
      "accommodation": "住宿类型",
      "hotel": {
        "name": "酒店名称",
        "address": "酒店地址",
        "location": {"longitude": 116.397, "latitude": 39.916},
        "price_range": "300-500元",
        "rating": "4.5",
        "distance": "距景点2公里",
        "type": "经济型酒店",
        "estimated_cost": 400
      },
      "attractions": [
        {
          "name": "景点名称",
          "address": "详细地址",
          "location": {"longitude": 116.397, "latitude": 39.916},
          "visit_duration": 90,
          "description": "景点描述（≤100字）",
          "category": "景点类别",
          "ticket_price": 60,
          "opening_time": "09:00",
          "closing_time": "17:00",
          "closed_weekdays": [0]
        }
      ],
      "meals": [
        {"type": "breakfast", "name": "早餐", "description": "", "estimated_cost": 30, "address": "", "location": {"longitude": 0, "latitude": 0}},
        {"type": "lunch",   "name": "午餐", "description": "", "estimated_cost": 50, "address": "", "location": {"longitude": 0, "latitude": 0}},
        {"type": "dinner",  "name": "晚餐", "description": "", "estimated_cost": 80, "address": "", "location": {"longitude": 0, "latitude": 0}}
      ]
    }
  ],
  "weather_info": [
    {"date": "YYYY-MM-DD", "day_weather": "晴", "night_weather": "多云", "day_temp": 25, "night_temp": 15, "wind_direction": "南风", "wind_power": "1-3级"}
  ],
  "overall_suggestions": "总体建议"
}
```

**格式铁律（违反会导致解析失败）：**
1. temperature / 金额 / 经纬度 必须是纯数字，不要带 °C / 元 等单位。
2. 不要输出 budget 汇总对象；系统后处理阶段确定性计算预算。
3. 每个景点描述 ≤100 字，每日概述 ≤120 字，防止多日 JSON 被截断。
4. 所有天的景点必须全局去重（POI ID 或名称相同视为重复）。
5. 每天必须有 breakfast / lunch / dinner 三餐。
6. 午餐和晚餐不要跨天重复同一家餐厅。
7. 每天设置不同 theme，避免内容雷同。
8. 尽量填写 opening_time / closing_time / closed_weekdays；无数据时按常识推断。
9. 为每个 meal 填写 address 和 location（尽量靠近对应景点）。
"""

    # ── 动态部分：编排规则模板（按请求动态填充）──
    DYNAMIC_RULES_TEMPLATE = """
**本次行程基础约束:**
- 目的地: {city}（{travel_days}天，{start_date} 至 {end_date}）
- 出发城市: {origin_city}
- 行程类型: {trip_type}
- 通勤交通: {transportation}
- 住宿偏好: {accommodation}
- 用户偏好: {preferences}
- 行程节奏: {pace} → 每天景点数 {min_attractions}-{max_attractions}
- 出行人数: {travelers}人 / {rooms}间房
- 预算上限: {budget_limit}
- 优化目标: {optimization_goal}

**景点数据（结构化摘要）:**
每行格式: 名称｜类别｜经度,纬度｜建议时长(分钟)｜门票(元)｜开放时间｜闭馆日
{attractions_summary}

**天气信息:**
{weather_summary}

**酒店信息:**
{hotels_summary}

**跨城交通信息:**
{transport_summary}

**编排规则（按优先级排序）:**
1. 景点游玩时长按规模设定：大型景区(故宫/长城等) 180-240min；中型(博物馆/主题公园) 90-120min；小型(公园/故居) 30-60min；特小型(打卡点) 15-30min。
2. 每日时间轴顺序：上午景点 → 午餐(11:30-13:30) → 下午景点 → 晚餐(17:30-19:30) → 夜景/自由活动。
3. 上午(08:30-12:00)安排 1-2 个活动；下午(13:30-17:30)安排 1-2 个活动；晚上安排轻松项目。
4. 天气适配：雨天优先室内景点；高温(≥35°C)减少正午户外；低温(≤0°C)缩短户外停留。
5. 体力曲线：
   - 每天前 2 个景点可安排强度较高的（如爬山、大面积步行）
   - 午餐后（13:30-15:00）安排室内或休闲景点作为体力恢复期
   - 连续高强度天后需穿插轻松日
   - 第一天和最后一天安排较轻松行程（适应旅途疲劳）
6. 第一天如含跨城抵达，景点只能用抵达后的剩余时间；最后一天活动必须在返程前结束。
7. 景点间预留 30-60 分钟交通时间；用餐地点优先安排在景点附近。
8. 历史文化类(博物馆/故宫等)通常 09:00-17:00 开放，周一闭馆；自然景观优先白天；夜景类安排 18:00 后。
{extra_rules}

请只返回完整 JSON，不要额外解释。
"""

    @classmethod
    def build_dynamic_query(
        cls,
        *,
        request,
        attractions_raw: str = "",
        weather_raw: str = "",
        hotels_raw: str = "",
        transport_raw: str = "",
    ) -> str:
        """将原始 Agent 返回的自然语言文本转换为结构化摘要，并填充动态模板。"""
        # 景点信息结构化压缩
        attractions_summary = cls._compress_attractions(attractions_raw)
        weather_summary = cls._compress_weather(weather_raw)
        hotels_summary = cls._compress_hotels(hotels_raw)
        transport_summary = transport_raw if transport_raw else "未提供"

        pace_limits = {
            "relaxed": (1, 2),
            "balanced": (2, 3),
            "intensive": (3, 5),
        }
        min_a, max_a = pace_limits.get(request.pace, (2, 3))

        return cls.DYNAMIC_RULES_TEMPLATE.format(
            city=request.city,
            origin_city=request.origin_city,
            start_date=request.start_date,
            end_date=request.end_date,
            travel_days=request.travel_days,
            trip_type="往返" if request.trip_type == "round_trip" else "单程",
            transportation=request.transportation,
            accommodation=request.accommodation,
            preferences=", ".join(request.preferences) if request.preferences else "无",
            pace=request.pace,
            min_attractions=min_a,
            max_attractions=max_a,
            travelers=request.travelers,
            rooms=request.rooms,
            budget_limit=f"{request.budget_limit}元" if request.budget_limit is not None else "未设置",
            optimization_goal=request.optimization_goal,
            attractions_summary=attractions_summary,
            weather_summary=weather_summary,
            hotels_summary=hotels_summary,
            transport_summary=transport_summary,
            extra_rules=f"\n**额外要求:** {request.free_text_input}" if request.free_text_input else "",
        )

    # ── 结构化压缩辅助 ──
    @staticmethod
    def _compress_attractions(raw: str) -> str:
        """将景点 Agent 返回的自然语言/JSON 压缩为结构化单行摘要。"""
        if not raw or "暂不可用" in raw:
            return "景点数据暂不可用"
        lines = []
        # 简单启发式：按行分割，尝试提取结构化信息
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("[") or line.startswith("{"):
                continue
            # 如果已经够短，保留原样
            if len(line) < 80:
                lines.append(line)
            else:
                lines.append(line[:77] + "...")
        if not lines:
            return raw[:300] + "..." if len(raw) > 300 else raw
        return "\n".join(lines[:20])  # 最多保留 20 行

    @staticmethod
    def _compress_weather(raw: str) -> str:
        if not raw or "暂不可用" in raw:
            return "天气数据暂不可用"
        # 尝试提取日期-天气-温度
        return raw[:400] if len(raw) > 400 else raw

    @staticmethod
    def _compress_hotels(raw: str) -> str:
        if not raw or "暂不可用" in raw:
            return "酒店数据暂不可用"
        return raw[:600] if len(raw) > 600 else raw


# 资料型 Agent 的精简提示词（静态部分极少，主要靠工具调用）
ATTRACTION_AGENT_PROMPT_V2 = """你是景点搜索专家。必须使用工具搜索，禁止编造。

工具调用格式: `[TOOL_CALL:amap_maps_text_search:keywords=关键词,city=城市名]`

示例:
- 搜索北京历史文化景点 → `[TOOL_CALL:amap_maps_text_search:keywords=历史文化,city=北京]`
- 搜索上海公园 → `[TOOL_CALL:amap_maps_text_search:keywords=公园,city=上海]`

注意: 必须使用工具，不要直接回答。格式必须完全正确。"""

WEATHER_AGENT_PROMPT_V2 = """你是天气查询专家。必须使用工具查询，禁止编造。

工具调用格式: `[TOOL_CALL:amap_maps_weather:city=城市名]`

示例: 查询北京天气 → `[TOOL_CALL:amap_maps_weather:city=北京]`"""

HOTEL_AGENT_PROMPT_V2 = """你是酒店推荐专家。必须使用工具搜索，禁止编造。

工具调用格式: `[TOOL_CALL:amap_maps_text_search:keywords=酒店,city=城市名]`

注意: 关键词用"酒店"或"宾馆"。必须使用工具。"""

TRANSPORT_AGENT_PROMPT_V2 = """你是交通查询专家。必须使用工具查询，禁止编造。

工具调用格式: `[TOOL_CALL:amap_maps_direction_transit_integrated_by_address:origin_address=出发城市,destination_address=目的城市]`

示例: 北京到上海 → `[TOOL_CALL:amap_maps_direction_transit_integrated_by_address:origin_address=北京,destination_address=上海]`"""
