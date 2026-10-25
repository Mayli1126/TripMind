"""多智能体旅行规划系统"""

import builtins
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import Dict, Any, Callable, List, Optional
from ..infrastructure.simple_agent import SimpleAgent
from ..infrastructure.llm_client import LLMClient
from ..services.prompt_templates import (
    PlannerPromptTemplate,
    ATTRACTION_AGENT_PROMPT_V2,
    WEATHER_AGENT_PROMPT_V2,
    HOTEL_AGENT_PROMPT_V2,
    TRANSPORT_AGENT_PROMPT_V2,
)
from ..services.mcp_tool_pool import MCPToolPool
from ..services.amap_service import AmapService
from ..services.trip_quality_service import trip_quality_service
from ..services.llm_service import get_llm, create_llm
from ..models.schemas import TripRequest, TripPlan, DayPlan, Attraction, Meal, WeatherInfo, Location, Hotel, Budget
from ..config import get_settings


def _safe_print(*values, **kwargs):
    """日志字符不受 Windows 控制台编码影响。"""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    safe_values = [
        value.encode(encoding, errors="replace").decode(encoding)
        if isinstance(value, str)
        else value
        for value in values
    ]
    builtins.print(*safe_values, **kwargs)


print = _safe_print

# ============ Agent提示词（V2 精简版） ============
# 资料型 Agent 提示词已大幅精简，静态内容剥离到模板模块；
# Planner Agent 的静态提示词改为 PlannerPromptTemplate.STATIC_SYSTEM_PROMPT。
ATTRACTION_AGENT_PROMPT = ATTRACTION_AGENT_PROMPT_V2
WEATHER_AGENT_PROMPT = WEATHER_AGENT_PROMPT_V2
HOTEL_AGENT_PROMPT = HOTEL_AGENT_PROMPT_V2
TRANSPORT_AGENT_PROMPT = TRANSPORT_AGENT_PROMPT_V2
PLANNER_AGENT_PROMPT = PlannerPromptTemplate.STATIC_SYSTEM_PROMPT


class MultiAgentTripPlanner:
    """多智能体旅行规划系统"""

    def __init__(self):
        """初始化多智能体系统"""
        print("🔄 开始初始化多智能体旅行规划系统...")

        try:
            settings = get_settings()
            self.llm = get_llm()
            # 同一个 Planner 实例不会同时服务两份旅行，避免 Agent 历史和 MCP 会话串线。
            self._request_lock = Lock()

            # 使用 MCP 连接池复用连接，减少进程启动开销。
            print("  - 从连接池获取 MCP 工具...")
            pool = MCPToolPool()
            self.attraction_amap_tool = pool.get_tool()
            self.weather_amap_tool = pool.get_tool()
            self.hotel_amap_tool = pool.get_tool()
            self.transport_amap_tool = pool.get_tool()


            # 创建景点搜索Agent
            print("  - 创建景点搜索Agent...")
            self.attraction_agent = SimpleAgent(
                name="景点搜索专家",
                llm=create_llm(),
                system_prompt=ATTRACTION_AGENT_PROMPT
            )
            self.attraction_agent.add_tool(self.attraction_amap_tool)

            # 创建天气查询Agent
            print("  - 创建天气查询Agent...")
            self.weather_agent = SimpleAgent(
                name="天气查询专家",
                llm=create_llm(),
                system_prompt=WEATHER_AGENT_PROMPT
            )
            self.weather_agent.add_tool(self.weather_amap_tool)

            # 创建酒店推荐Agent
            print("  - 创建酒店推荐Agent...")
            self.hotel_agent = SimpleAgent(
                name="酒店推荐专家",
                llm=create_llm(),
                system_prompt=HOTEL_AGENT_PROMPT
            )
            self.hotel_agent.add_tool(self.hotel_amap_tool)

            # 创建通勤查询Agent
            print("  - 创建通勤查询Agent...")
            self.transport_agent = SimpleAgent(
                name="通勤查询专家",
                llm=create_llm(),
                system_prompt=TRANSPORT_AGENT_PROMPT
            )
            self.transport_agent.add_tool(self.transport_amap_tool)

            # 创建行程规划Agent(不需要工具)
            print("  - 创建行程规划Agent...")
            # 规划模型按模块预留接线：LLM_PLANNER_MODEL 配置了就用（如 deepseek-v4.1-flash），
            # 未配置回退主模型。规划是纯文本生成（无工具），换模型不影响工具链路。
            planner_model = (os.getenv("LLM_PLANNER_MODEL") or "").strip()
            planner_llm = (
                LLMClient(
                    model=planner_model,
                    temperature=1.0,
                    max_tokens=int(os.getenv("LLM_MAX_TOKENS", "8192")),
                )
                if planner_model
                else self.llm
            )
            if planner_model:
                print(f"   规划模型: {planner_model}")
            self.planner_agent = SimpleAgent(
                name="行程规划专家",
                llm=planner_llm,
                system_prompt=PLANNER_AGENT_PROMPT
            )

            print(f"✅ 多智能体系统初始化成功")
            print(f"   景点搜索Agent: {len(self.attraction_agent.list_tools())} 个工具")
            print(f"   天气查询Agent: {len(self.weather_agent.list_tools())} 个工具")
            print(f"   酒店推荐Agent: {len(self.hotel_agent.list_tools())} 个工具")
            print(f"   通勤查询Agent: {len(self.transport_agent.list_tools())} 个工具")

        except Exception as e:
            print(f"❌ 多智能体系统初始化失败: {str(e)}")
            import traceback
            traceback.print_exc()
            raise
    
    def plan_trip(
        self,
        request: TripRequest,
        progress_callback: Optional[Callable[[int, str, str], None]] = None,
    ) -> TripPlan:
        """
        使用多智能体协作生成旅行计划

        Args:
            request: 旅行请求

        Returns:
            旅行计划
        """
        request_lock = getattr(self, "_request_lock", None)
        if request_lock:
            request_lock.acquire()
        active_stage = "初始化"
        degraded_warnings: list[str] = []
        try:
            self._report_progress(progress_callback, 5, "initializing", "正在理解旅行需求与约束")
            print(f"\n{'='*60}")
            print(f"🚀 开始多智能体协作规划旅行...")
            print(f"目的地: {request.city}")
            print(f"日期: {request.start_date} 至 {request.end_date}")
            print(f"天数: {request.travel_days}天")
            print(f"偏好: {', '.join(request.preferences) if request.preferences else '无'}")
            print(f"{'='*60}\n")

            # 四个资料型分支互不依赖，并行执行后再交给 Planner 汇总。
            active_stage = "并行资料检索"
            context = self._collect_parallel_context(request, progress_callback)
            attraction_response = context["attractions"]
            weather_response = context["weather"]
            hotel_response = context["hotels"]
            transport_response = context["transportation"]
            intercity_cost_per_person = context["intercity_cost_per_person"]
            transportation_option = context["transportation_option"]
            degraded_warnings.extend(context["warnings"])

            # 步骤5: 行程规划Agent整合信息生成计划
            print("📋 步骤5: 生成行程计划...")
            self._report_progress(progress_callback, 76, "planning", "Planner 正在编排每日行程")
            planner_query = self._build_planner_query(request, attraction_response, weather_response, hotel_response, transport_response)
            active_stage = "行程生成"
            planner_response = self._run_agent_fresh(self.planner_agent, planner_query)
            print(f"行程规划结果: {planner_response[:300]}...\n")
            self._report_progress(progress_callback, 86, "validating", "正在校验计划结构与可执行性")

            # 解析最终计划
            trip_plan = self._parse_response(planner_response, request)
            if self._needs_repair(trip_plan, request):
                print("🔁 Validator发现结构性问题，执行一次受限重规划...")
                self._report_progress(progress_callback, 89, "replanning", "发现结构问题，正在自动修复")
                repaired_plan = self._repair_plan(request, planner_response)
                if (
                    not self._needs_repair(repaired_plan, request)
                    and self._plan_score(repaired_plan, request) > self._plan_score(trip_plan, request)
                ):
                    trip_plan = repaired_plan
                    trip_plan.planning_iterations = 2
                    trip_plan.warnings.append("初始计划未通过结构校验，系统已自动修复一次。")
                else:
                    trip_plan.warnings.append("自动修复未产生更好的结果，已保留原计划。")
            trip_plan.warnings.extend(degraded_warnings)
            trip_plan = trip_quality_service.finalize(
                trip_plan,
                request,
                intercity_cost_per_person=intercity_cost_per_person,
                transportation_option=transportation_option,
            )
            if trip_plan.is_fallback:
                self._report_progress(
                    progress_callback, 97, "degraded", "生成结果不完整，已切换为参考计划并标注原因"
                )
            else:
                self._report_progress(progress_callback, 97, "optimizing", "预算复核与路线优化完成")

            print(f"{'='*60}")
            print(f"✅ 旅行计划生成完成!")
            print(f"{'='*60}\n")

            return trip_plan

        except Exception as e:
            print(f"❌ 生成旅行计划失败: {str(e)}")
            import traceback
            traceback.print_exc()
            fallback = self._create_fallback_plan(
                request,
                reason=f"{active_stage}失败：{self._friendly_error(e)}",
            )
            fallback.warnings.extend(degraded_warnings)
            self._report_progress(
                progress_callback, 97, "degraded", f"{active_stage}失败，已生成可用的参考计划"
            )
            return trip_quality_service.finalize(fallback, request)
        finally:
            if request_lock:
                request_lock.release()

    @staticmethod
    def _report_progress(
        callback: Optional[Callable[..., None]],
        progress: int,
        stage: str,
        message: str,
        agent: Optional[str] = None,
        agent_status: Optional[str] = None,
    ) -> None:
        if callback:
            if agent or agent_status:
                callback(progress, stage, message, agent, agent_status)
            else:
                callback(progress, stage, message)

    def _collect_parallel_context(
        self,
        request: TripRequest,
        progress_callback: Optional[Callable[..., None]],
    ) -> dict[str, Any]:
        """并行收集 Planner 所需上下文，并对单个分支做独立降级。"""
        branches: dict[str, tuple[str, Callable[[], tuple[Any, Optional[str]]]]] = {
            "attractions": (
                "景点 Agent",
                lambda: self._run_agent_safely(
                    self.attraction_agent,
                    self._build_attraction_query(request),
                    "景点检索",
                ),
            ),
            "weather": (
                "天气 Agent",
                lambda: self._run_agent_safely(
                    self.weather_agent,
                    f"请查询{request.city}的天气信息",
                    "天气查询",
                ),
            ),
            "hotels": (
                "住宿 Agent",
                lambda: self._run_agent_safely(
                    self.hotel_agent,
                    f"请搜索{request.city}的{request.accommodation}酒店",
                    "酒店检索",
                ),
            ),
        }
        if request.origin_city:
            branches["transportation"] = (
                "交通 Agent",
                lambda: self._query_transportation_context(request),
            )

        results: dict[str, Any] = {
            "attractions": "景点检索暂不可用",
            "weather": "天气查询暂不可用",
            "hotels": "酒店检索暂不可用",
            "transportation": "",
            "intercity_cost_per_person": 0,
            "transportation_option": None,
            "warnings": [],
        }
        for agent_key, (label, _) in branches.items():
            self._report_progress(
                progress_callback, 12, "parallel_research", f"{label} 已开始工作",
                agent_key, "running",
            )

        completed = 0
        with ThreadPoolExecutor(
            max_workers=len(branches), thread_name_prefix="trip-research"
        ) as executor:
            future_to_agent = {
                executor.submit(operation): (agent_key, label)
                for agent_key, (label, operation) in branches.items()
            }
            for future in as_completed(future_to_agent):
                agent_key, label = future_to_agent[future]
                warning = None
                try:
                    value, warning = future.result()
                    if agent_key == "transportation":
                        results["transportation"] = value[0]
                        results["intercity_cost_per_person"] = value[1]
                        # 兼容测试替身及旧的自定义交通分支返回值。
                        results["transportation_option"] = value[2] if len(value) > 2 else None
                    else:
                        results[agent_key] = value
                except Exception as exc:
                    warning = f"{label}未完成：{self._friendly_error(exc)}。"
                if warning:
                    results["warnings"].append(warning)
                completed += 1
                progress = 12 + round(58 * completed / len(branches))
                self._report_progress(
                    progress_callback, progress, "parallel_research",
                    f"{label}{'已降级' if warning else '已完成'}",
                    agent_key, "degraded" if warning else "completed",
                )

        self._report_progress(
            progress_callback, 72, "research_joined", "并行资料已汇合，准备生成行程"
        )
        return results

    def _query_transportation_context(
        self, request: TripRequest
    ) -> tuple[tuple[str, int], Optional[str]]:
        """查询跨城交通，返回可供 Planner 使用的摘要和人均费用。"""
        try:
            options = AmapService(
                mcp_tool=getattr(self, "transport_amap_tool", None)
            ).get_transportation_options(
                request.origin_city, request.city, request.start_date
            )
            if not options:
                return ("未查询到通勤信息", 0, None), None

            transport_type_map = {"自驾": None, "公共交通": "high_speed_train"}
            target_type = transport_type_map.get(
                request.transportation, request.transportation
            )
            selected = [
                option for option in options
                if target_type is None or option.type == target_type
            ]
            if request.transportation == "自驾":
                return (("自驾油费、过路费和停车费需按实际车型与路线另行核算", 0, None), None)
            if not selected:
                selected = options[:2]
            summary = "\n".join(
                f"{option.name}: 约{option.price}元, {option.duration}, "
                f"{option.departure_time or '待确认'}—{option.arrival_time or '待确认'}, "
                f"{option.departure_station or '出发站'}→{option.arrival_station or '到达站'}"
                for option in selected
            )
            return (
                summary,
                selected[0].price if selected else 0,
                selected[0] if selected else None,
            ), None
        except Exception as exc:
            reason = self._friendly_error(exc)
            return (
                ("通勤查询失败", 0, None),
                f"跨城交通查询未完成：{reason}。预算中的交通费用仅供参考。",
            )

    @classmethod
    def _run_agent_safely(cls, agent, query: str, stage: str) -> tuple[str, Optional[str]]:
        """让资料型 Agent 可以局部降级，避免单个外部工具拖垮整份行程。"""
        try:
            response = cls._run_agent_fresh(agent, query)
            if response is None or not str(response).strip():
                raise ValueError("服务返回空结果")
            return str(response), None
        except Exception as exc:
            reason = cls._friendly_error(exc)
            print(f"⚠️ {stage}局部降级: {reason}")
            return f"{stage}暂不可用，请不要据此编造实时数据。", f"{stage}未完成：{reason}。"

    @staticmethod
    def _run_agent_fresh(agent, query: str) -> str:
        """每次请求使用干净会话，避免不同用户的行程上下文互相污染。"""
        clear_history = getattr(agent, "clear_history", None)
        if callable(clear_history):
            clear_history()
        return agent.run(query)

    @staticmethod
    def _friendly_error(exc: Exception | None) -> str:
        """把底层异常转换成可展示且不泄露密钥的诊断信息。"""
        if exc is None:
            return "未知错误"
        raw = str(exc).strip()
        lowered = raw.lower()
        if "timeout" in lowered or "timed out" in lowered or "超时" in raw:
            return "服务请求超时"
        if any(token in lowered for token in ("401", "403", "unauthorized", "forbidden", "api key")):
            return "服务鉴权失败，请检查 API Key"
        if "429" in lowered or "rate limit" in lowered:
            return "服务请求过于频繁或额度不足"
        if any(token in lowered for token in ("connection", "connecterror", "network", "dns")):
            return "无法连接外部服务"
        if not raw:
            return exc.__class__.__name__
        # 仅保留简短摘要，避免第三方异常中夹带请求头或密钥。
        return raw.splitlines()[0][:160]

    def revise_trip(
        self,
        request: TripRequest,
        current_plan: TripPlan,
        instruction: str,
        progress_callback: Optional[Callable[[int, str, str], None]] = None,
    ) -> TripPlan:
        """按自然语言指令局部修改计划，并重新执行质量与预算校验。"""
        self._report_progress(progress_callback, 10, "understanding", "正在理解修改要求")
        current_json = current_plan.model_dump_json(exclude_none=True)
        prompt = f"""请根据用户的修改要求调整现有旅行计划，并只返回完整JSON。

用户修改要求：{instruction}

不可改变的基础约束：
- 目的地：{request.city}
- 日期：{request.start_date} 至 {request.end_date}，共 {request.travel_days} 天
- 出行人数：{request.travelers} 人，{request.rooms} 间房
- 每天必须至少包含1个景点和 breakfast、lunch、dinner 三餐
- 不要编造新的实时价格、评分、开放时间或交通班次
- 只修改与用户要求相关的部分，其他内容尽量保持不变

当前计划：
{current_json[:14000]}
"""
        try:
            self._report_progress(progress_callback, 35, "revising", "Planner 正在调整相关行程")
            response = self._run_agent_fresh(self.planner_agent, prompt)
            self._report_progress(progress_callback, 75, "validating", "正在校验修改后的计划")
            revised = self._parse_response(response, request)
            if revised.is_fallback or self._needs_repair(revised, request):
                preserved = current_plan.model_copy(deep=True)
                preserved.warnings.append("本次自然语言修改未通过结构校验，已保留原计划。")
                return preserved

            intercity_cost = 0
            if current_plan.budget and request.travelers:
                intercity_cost = round(
                    current_plan.budget.intercity_transportation / request.travelers
                )
            revised.planning_iterations = 2
            revised.warnings.append(f"已根据修改要求更新：{instruction}")
            self._report_progress(progress_callback, 92, "optimizing", "正在重新计算预算与路线")
            return trip_quality_service.finalize(
                revised,
                request,
                intercity_cost_per_person=intercity_cost,
            )
        except Exception as exc:
            preserved = current_plan.model_copy(deep=True)
            preserved.warnings.append(f"修改失败，已保留原计划：{exc}")
            return preserved
    
    def query_transportation(self, origin_city: str, destination_city: str, date: str = None) -> list:
        """
        查询通勤费用和交通方式

        Args:
            origin_city: 出发城市
            destination_city: 目的城市
            date: 出发日期

        Returns:
            交通方式选项列表
        """
        try:
            print(f"\n{'='*60}")
            print(f"🚆 开始查询通勤费用...")
            print(f"出发城市: {origin_city}")
            print(f"目的城市: {destination_city}")
            print(f"出发日期: {date if date else '近期'}")
            print(f"{'='*60}\n")

            transport_query = f"请查询从{origin_city}到{destination_city}的交通方式和费用信息。"
            if date:
                transport_query += f"出发日期: {date}"
            
            # 每次请求干净会话：全局单例 agent 的历史不清会让所有用户的
            # 通勤查询互相污染，累积到上下文超限后该接口对所有人持续失败
            transport_response = self._run_agent_fresh(self.transport_agent, transport_query)
            print(f"通勤查询结果: {transport_response[:300]}...\n")

            print(f"{'='*60}")
            print(f"✅ 通勤费用查询完成!")
            print(f"{'='*60}\n")

            return transport_response

        except Exception as e:
            print(f"❌ 查询通勤费用失败: {str(e)}")
            import traceback
            traceback.print_exc()
            return []
    
    def _build_attraction_query(self, request: TripRequest) -> str:
        """构建景点搜索查询 - 支持多关键词批量搜索。

        当用户有多个偏好时，为每个偏好生成独立的工具调用，
        让 Agent 并行搜索后在 Python 层去重合并，提升景点覆盖度。
        """
        base_keywords = ["景点", "旅游"]
        if request.preferences:
            # 每个偏好作为一个独立搜索关键词
            keywords_list = list(request.preferences)
        else:
            keywords_list = base_keywords

        tool_calls = []
        for kw in keywords_list[:3]:  # 最多 3 个关键词，避免过度搜索
            tool_calls.append(
                f"[TOOL_CALL:amap_maps_text_search:keywords={kw},city={request.city}]"
            )

        query = (
            f"请使用amap_maps_text_search工具搜索{request.city}的相关景点。"
            f"需要搜索以下关键词，请依次调用工具：{', '.join(keywords_list[:3])}\n"
            + "\n".join(tool_calls)
        )
        return query
        """构建景点搜索查询 - 直接包含工具调用"""
        keywords = ""
        if request.preferences:
            keywords = " ".join(request.preferences)
        else:
            keywords = "景点"

        # 直接返回工具调用格式
        query = f"请使用amap_maps_text_search工具搜索{request.city}的{keywords}相关景点。\n[TOOL_CALL:amap_maps_text_search:keywords={keywords},city={request.city}]"
        return query

    def _build_planner_query(self, request: TripRequest, attractions: str, weather: str, hotels: str = "", transportation_info: str = "") -> str:
        """构建行程规划查询"""
        attractions = attractions[:1500] if len(attractions) > 1500 else attractions
        weather = weather[:500] if len(weather) > 500 else weather
        hotels = hotels[:1000] if len(hotels) > 1000 else hotels
        
        transport_text = ""
        if transportation_info:
            transport_text = f"""

**通勤交通信息:**
{transportation_info}"""
        
        query = f"""请根据以下信息生成从{request.origin_city}到{request.city}的{request.travel_days}天旅行计划:

**基本信息:**
- 出发城市: {request.origin_city}
- 目的地城市: {request.city}
- 日期: {request.start_date} 至 {request.end_date}
- 天数: {request.travel_days}天
- 通勤交通: {request.transportation}
- 行程类型: {'往返' if request.trip_type == 'round_trip' else '单程'}
- 已确认交通时刻: {f'{request.departure_time}—{request.arrival_time}' if request.departure_time else '未填写，必须将查询到的时刻视为参考值'}
- 已确认返程时刻: {f'{request.return_departure_time}—{request.return_arrival_time}' if request.return_departure_time else '未填写'}
- 住宿: {request.accommodation}
- 偏好: {', '.join(request.preferences) if request.preferences else '无'}
- 出行人数: {request.travelers}人，{request.rooms}间房
- 预算上限: {f'{request.budget_limit}元' if request.budget_limit is not None else '未设置'}
- 优化目标: {request.optimization_goal}
- 行程节奏: {request.pace}

**景点信息:**
{attractions}

**天气信息:**
{weather}

**酒店信息:**
{hotels}{transport_text}

**要求:**
1. 每天景点数量遵循行程节奏：轻松1-2个、适中2-3个、紧凑3-5个；当前为{request.pace}
2. 每天必须包含早中晚三餐
3. 每天推荐一个具体的酒店(从酒店信息中选择)
4. 第一天必须先安排跨城交通、进站/值机缓冲和抵达接驳，景点只能使用抵达后的剩余时间
5. 如提供返程时刻，最后一天的所有活动必须在前往返程车站/机场之前结束
6. 如果有通勤交通信息,请在预算中包含通勤费用
7. 返回完整的JSON格式数据
8. 景点的经纬度坐标要真实准确
"""
        if request.free_text_input:
            query += f"\n**额外要求:** {request.free_text_input}"

        return query

    def _needs_repair(self, plan: TripPlan, request: TripRequest) -> bool:
        """仅对结构性失败重规划，避免为了轻微提醒重复消耗模型调用。"""
        from datetime import datetime, timedelta

        start = datetime.strptime(request.start_date, "%Y-%m-%d").date()
        dates_are_valid = all(
            day.day_index == index
            and day.date == (start + timedelta(days=index)).isoformat()
            for index, day in enumerate(plan.days)
        )
        meals_are_complete = all(
            {"breakfast", "lunch", "dinner"}.issubset({meal.type for meal in day.meals})
            for day in plan.days
        )
        attraction_keys = [
            f"poi:{item.poi_id}" if item.poi_id else f"name:{''.join(item.name.lower().split())}"
            for day in plan.days for item in day.attractions
        ]
        categories = {
            (item.category or "").strip().lower()
            for day in plan.days for item in day.attractions
            if (item.category or "").strip()
        }
        main_meals = [
            meal.name.strip().lower()
            for day in plan.days for meal in day.meals
            if meal.type in {"lunch", "dinner"} and meal.name.strip()
        ]
        minimum_categories = min(3, len(attraction_keys))
        return (
            plan.is_fallback
            or len(plan.days) != request.travel_days
            or not dates_are_valid
            or not meals_are_complete
            or any(not day.attractions for day in plan.days)
            or any(len(day.attractions) < self._minimum_attractions(request.pace) for day in plan.days)
            or len(attraction_keys) != len(set(attraction_keys))
            or len(categories) < minimum_categories
            or len(main_meals) != len(set(main_meals))
        )

    @staticmethod
    def _minimum_attractions(pace: str) -> int:
        return {"relaxed": 1, "balanced": 2, "intensive": 3}[pace]

    def _repair_plan(self, request: TripRequest, previous_response: str) -> TripPlan:
        repair_prompt = f"""上一版旅行计划未通过结构校验。请修复后只返回完整JSON，不要解释。

必须满足：
- 城市：{request.city}
- 日期：{request.start_date} 至 {request.end_date}，共{request.travel_days}天
- days必须恰好包含{request.travel_days}项，day_index从0连续递增
- 每天至少1个景点，并包含breakfast、lunch、dinner三餐
- 所有日期的景点全局去重，同一POI或同名景点只能出现一次
- 每天使用不同主题，至少覆盖历史文化、自然/城市风光、休闲体验或当地美食中的多个类别
- 午餐和晚餐不要跨天重复同一家餐厅
- 景点数量和游览时长必须能在08:30至21:30之间合理完成
- 所有经纬度、金额和温度使用数字类型
- 保留能够确认的数据，不要新增无法从上下文确认的实时事实

上一版输出：
{previous_response[:8000]}
"""
        try:
            repaired_response = self._run_agent_fresh(self.planner_agent, repair_prompt)
            return self._parse_response(repaired_response, request)
        except Exception as exc:
            print(f"⚠️ 自动修复失败: {exc}")
            return self._create_fallback_plan(
                request, reason=f"自动修复失败：{self._friendly_error(exc)}"
            )

    @staticmethod
    def _plan_score(plan: TripPlan, request: TripRequest) -> int:
        """为初始计划和修复计划提供简单、可解释的选择标准。"""
        score = 0 if plan.is_fallback else 100
        score -= abs(len(plan.days) - request.travel_days) * 20
        score += sum(5 for day in plan.days if day.attractions)
        score += sum(3 for day in plan.days if len(day.meals) >= 3)
        return score
    
    def _extract_json(self, response: str) -> str:
        """
        从LLM响应中提取JSON字符串,支持多种格式

        优先级:
        1. ```json ... ``` 代码块(支持多个,取最长的)
        2. ``` ... ``` 代码块
        3. 裸JSON对象 { ... }
        """
        import re

        # 策略1: 提取所有 ```json ... ``` 代码块,取最长的(避免截断)
        json_blocks = re.findall(r'```json\s*\n?(.*?)```', response, re.DOTALL)
        if json_blocks:
            return max(json_blocks, key=len).strip()

        # 策略2: 提取所有 ``` ... ``` 代码块
        code_blocks = re.findall(r'```\s*\n?(.*?)```', response, re.DOTALL)
        if code_blocks:
            return max(code_blocks, key=len).strip()

        # 策略3: 裸JSON - 使用括号匹配找到完整的JSON对象
        brace_count = 0
        start = -1
        for i, char in enumerate(response):
            if char == '{':
                if brace_count == 0:
                    start = i
                brace_count += 1
            elif char == '}':
                brace_count -= 1
                if brace_count == 0 and start != -1:
                    return response[start:i+1]

        raise ValueError("响应中未找到有效的JSON数据")

    def _clean_json_string(self, json_str: str) -> str:
        """
        清理JSON字符串中的常见LLM输出错误

        处理:
        - 尾部逗号: [1, 2,] -> [1, 2]
        - 单引号: {'key': 'value'} -> {"key": "value"}
        - JavaScript注释: // ... 和 /* ... */
        - Markdown加粗符号: **text**
        - 多余的换行和空格
        - 字符串中的转义问题
        - 省略号或不完整值
        """
        import re

        # 移除JavaScript风格注释：字符串感知版——只剥字符串字面量之外的注释，
        # 否则值里的 URL（https://…）会被削成 "https:"（踩过）
        json_str = self._strip_comments_outside_strings(json_str)

        # 移除Markdown加粗符号 **
        json_str = re.sub(r'\*\*', '', json_str)

        # 移除对象/数组末尾的逗号 (尾部逗号)
        json_str = re.sub(r',\s*([}\]])', r'\1', json_str)

        # 将单引号替换为双引号(仅在键名和简单值中,避免影响字符串内容)
        # 注意: 这是一个简化处理,复杂情况可能失效
        json_str = re.sub(r"'([^']*)'\s*:", r'"\1":', json_str)  # 键名
        json_str = re.sub(r":\s*'([^']*)'", r': "\1"', json_str)  # 字符串值

        return json_str

    @staticmethod
    def _strip_comments_outside_strings(json_str: str) -> str:
        """剥除 JSON 字符串字面量之外的 JS 注释（// 行注释与 /* */ 块注释）。

        逐字符扫描并跟踪是否处于字符串内（处理 \\" 转义），
        保证 {"url": "https://a.com"} 中的 // 不被误删。
        """
        out: list[str] = []
        i, n = 0, len(json_str)
        in_string = False
        while i < n:
            ch = json_str[i]
            if in_string:
                out.append(ch)
                if ch == "\\":  # 转义：连同下一个字符一起原样保留
                    if i + 1 < n:
                        out.append(json_str[i + 1])
                        i += 1
                elif ch == '"':
                    in_string = False
                i += 1
                continue
            if ch == '"':
                in_string = True
                out.append(ch)
                i += 1
                continue
            # 字符串外：识别注释起点
            if ch == "/" and i + 1 < n:
                nxt = json_str[i + 1]
                if nxt == "/":  # 行注释：跳到行尾
                    while i < n and json_str[i] != "\n":
                        i += 1
                    continue
                if nxt == "*":  # 块注释：跳到 */
                    end = json_str.find("*/", i + 2)
                    i = n if end == -1 else end + 2
                    continue
            out.append(ch)
            i += 1
        return "".join(out)

    def _normalize_data(self, data: dict, request: TripRequest) -> dict:
        """
        清洗并规范化LLM返回的字典数据,使其符合TripPlan Pydantic模型

        处理:
        - 补全缺失的必填字段
        - 类型转换(如ticket_price "60元" -> 60)
        - location字符串 "116.3,39.9" -> {"longitude": 116.3, "latitude": 39.9}
        """
        import re
        from datetime import datetime, timedelta

        # 确保顶层必填字段存在
        data.setdefault('city', request.city)
        data.setdefault('start_date', request.start_date)
        data.setdefault('end_date', request.end_date)
        data.setdefault('overall_suggestions', f"这是为您规划的{request.city}{request.travel_days}日游行程。")

        # 规范化days
        days = data.get('days', [])
        if not isinstance(days, list):
            days = []
        for i, day in enumerate(days):
            if not isinstance(day, dict):
                continue
            expected_date = (
                datetime.strptime(request.start_date, "%Y-%m-%d") + timedelta(days=i)
            ).strftime("%Y-%m-%d")
            day.setdefault('date', expected_date)
            day.setdefault('day_index', i)
            day.setdefault('description', f"第{i+1}天行程")
            day.setdefault('transportation', request.transportation)
            day.setdefault('accommodation', request.accommodation)
            day.setdefault('attractions', [])
            day.setdefault('meals', [])
            day.pop('schedule', None)

            # 规范化attractions中的字段
            for attr in day.get('attractions', []):
                if not isinstance(attr, dict):
                    continue
                attr.setdefault('name', '未知景点')
                attr.setdefault('address', request.city)
                attr.setdefault('visit_duration', 120)
                attr.setdefault('description', '')
                attr.setdefault('category', '景点')
                attr.setdefault('ticket_price', 0)

                # 处理location: 字符串 -> 字典
                loc = attr.get('location')
                if isinstance(loc, str):
                    # 尝试解析 "116.397128, 39.916527" 格式
                    parts = re.findall(r'[\d.]+', loc)
                    if len(parts) >= 2:
                        attr['location'] = {
                            'longitude': float(parts[0]),
                            'latitude': float(parts[1])
                        }
                    else:
                        attr['location'] = {'longitude': 116.397128, 'latitude': 39.916527}
                elif isinstance(loc, dict):
                    # 确保键名正确
                    if 'lng' in loc and 'longitude' not in loc:
                        loc['longitude'] = float(loc.pop('lng'))
                    if 'lat' in loc and 'latitude' not in loc:
                        loc['latitude'] = float(loc.pop('lat'))
                    # 确保经纬度是数字
                    if 'longitude' in loc and isinstance(loc['longitude'], str):
                        nums = re.findall(r'[\d.]+', loc['longitude'])
                        loc['longitude'] = float(nums[0]) if nums else 116.397128
                    if 'latitude' in loc and isinstance(loc['latitude'], str):
                        nums = re.findall(r'[\d.]+', loc['latitude'])
                        loc['latitude'] = float(nums[0]) if nums else 39.916527
                elif loc is None or not isinstance(loc, dict):
                    attr['location'] = {'longitude': 116.397128, 'latitude': 39.916527}

                # 处理ticket_price: 字符串 "60元" -> int
                price = attr.get('ticket_price')
                if isinstance(price, str):
                    nums = re.findall(r'\d+', price)
                    attr['ticket_price'] = int(nums[0]) if nums else 0
                elif price is None:
                    attr['ticket_price'] = 0
                elif isinstance(price, (int, float)):
                    attr['ticket_price'] = int(price)

                # 处理visit_duration: 字符串/None -> int
                duration = attr.get('visit_duration')
                if isinstance(duration, str):
                    nums = re.findall(r'\d+', duration)
                    attr['visit_duration'] = max(15, int(nums[0])) if nums else 120
                elif duration is None or not isinstance(duration, (int, float)):
                    attr['visit_duration'] = 120
                else:
                    attr['visit_duration'] = max(15, min(1440, int(duration)))

                # rating 容错：LLM 常回 "4.7分"/"4.5/5"/"暂无"；非法或超 0-5 置 None，
                # 避免单字段校验失败把整份计划打成 fallback 模板
                rating = attr.get('rating')
                if isinstance(rating, str):
                    nums = re.findall(r'\d+(?:\.\d+)?', rating)
                    attr['rating'] = min(5.0, float(nums[0])) if nums else None
                elif isinstance(rating, (int, float)) and not 0 <= rating <= 5:
                    attr['rating'] = None
                elif rating is not None and not isinstance(rating, (int, float)):
                    attr['rating'] = None

                # photos 容错：要求字符串列表；非列表（字符串/null/对象）一律丢弃
                photos = attr.get('photos')
                if not isinstance(photos, list) or any(not isinstance(p, str) for p in photos):
                    attr['photos'] = []

                # closed_weekdays 容错：要求 int 列表（周一为 0）；LLM 回星期文字时丢弃
                closed = attr.get('closed_weekdays')
                if not isinstance(closed, list) or any(
                    not isinstance(c, int) or not 0 <= c <= 6 for c in closed
                ):
                    attr['closed_weekdays'] = []

                # 门票非负兜底
                if isinstance(attr.get('ticket_price'), (int, float)) and attr['ticket_price'] < 0:
                    attr['ticket_price'] = 0

            # 规范化meals
            for meal in day.get('meals', []):
                if not isinstance(meal, dict):
                    continue
                meal.setdefault('type', 'snack')
                meal.setdefault('name', '餐饮')
                meal.setdefault('description', '')
                meal.setdefault('estimated_cost', 0)
                cost = meal.get('estimated_cost')
                if isinstance(cost, str):
                    nums = re.findall(r'\d+', cost)
                    meal['estimated_cost'] = int(nums[0]) if nums else 0
                elif cost is None or not isinstance(cost, (int, float)):
                    meal['estimated_cost'] = 0
                else:
                    meal['estimated_cost'] = int(cost)

            # 规范化hotel
            hotel = day.get('hotel')
            if isinstance(hotel, dict):
                hotel.setdefault('name', '推荐酒店')
                hotel.setdefault('address', request.city)
                hotel.setdefault('price_range', '')
                hotel.setdefault('rating', '')
                hotel.setdefault('distance', '')
                hotel.setdefault('type', '')
                hotel.setdefault('estimated_cost', 0)
                h_loc = hotel.get('location')
                if isinstance(h_loc, str):
                    parts = re.findall(r'[\d.]+', h_loc)
                    if len(parts) >= 2:
                        hotel['location'] = {
                            'longitude': float(parts[0]),
                            'latitude': float(parts[1])
                        }
                elif isinstance(h_loc, dict):
                    if 'lng' in h_loc and 'longitude' not in h_loc:
                        h_loc['longitude'] = float(h_loc.pop('lng'))
                    if 'lat' in h_loc and 'latitude' not in h_loc:
                        h_loc['latitude'] = float(h_loc.pop('lat'))
                hotel_cost = hotel.get('estimated_cost')
                if isinstance(hotel_cost, str):
                    nums = re.findall(r'\d+', hotel_cost)
                    hotel['estimated_cost'] = int(nums[0]) if nums else 0
                elif hotel_cost is None or not isinstance(hotel_cost, (int, float)):
                    hotel['estimated_cost'] = 0
                else:
                    hotel['estimated_cost'] = int(hotel_cost)

        data['days'] = days

        # 规范化weather_info
        weather = data.get('weather_info', [])
        if not isinstance(weather, list):
            weather = []
        for i, w in enumerate(weather):
            if not isinstance(w, dict):
                continue
            expected_date = (
                datetime.strptime(request.start_date, "%Y-%m-%d") + timedelta(days=i)
            ).strftime("%Y-%m-%d")
            w.setdefault('date', expected_date)
            w.setdefault('day_weather', '晴')
            w.setdefault('night_weather', '多云')
            w.setdefault('day_temp', 25)
            w.setdefault('night_temp', 15)
            w.setdefault('wind_direction', '南风')
            w.setdefault('wind_power', '1-3级')
            # 确保温度是数字
            for temp_key in ('day_temp', 'night_temp'):
                tv = w.get(temp_key)
                if isinstance(tv, str):
                    nums = re.findall(r'-?\d+', tv)
                    w[temp_key] = int(nums[0]) if nums else 20
                elif tv is None or not isinstance(tv, (int, float)):
                    w[temp_key] = 20
                else:
                    w[temp_key] = int(tv)

        data['weather_info'] = weather

        # 汇总预算由确定性质量服务重新计算，丢弃模型中常见的“¥200”等非结构化汇总值。
        data.pop('budget', None)

        return data

    def _parse_response(self, response: str, request: TripRequest) -> TripPlan:
        """
        解析Agent响应

        采用三层容错策略:
        1. 提取JSON字符串(支持代码块和裸JSON)
        2. 清理常见LLM输出错误(尾部逗号、注释等)
        3. 规范化数据(类型转换、字段补全)

        若全部失败则使用增强版备份计划

        Args:
            response: Agent响应文本
            request: 原始请求

        Returns:
            旅行计划
        """
        last_error = None

        # 尝试1: 原始提取 + 清理 + 解析
        for attempt, do_clean in enumerate([False, True]):
            try:
                json_str = self._extract_json(response)
                if do_clean:
                    json_str = self._clean_json_string(json_str)

                data = json.loads(json_str)
                data = self._normalize_data(data, request)
                trip_plan = TripPlan(**data)

                if do_clean:
                    print(f"✅ 解析成功(经过JSON清理)")
                else:
                    print(f"✅ 解析成功")
                return trip_plan

            except Exception as e:
                last_error = e
                if not do_clean:
                    print(f"⚠️  首次解析失败({str(e)}),尝试清理JSON后重试...")
                continue

        # 尝试2: 使用正则提取最外层JSON对象(更暴力)
        try:
            import re
            # 尝试匹配整个最大的JSON对象
            match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', response, re.DOTALL)
            if match:
                json_str = self._clean_json_string(match.group())
                data = json.loads(json_str)
                data = self._normalize_data(data, request)
                trip_plan = TripPlan(**data)
                print(f"✅ 使用正则兜底解析成功")
                return trip_plan
        except Exception as e:
            last_error = e

        # 全部失败,使用增强版备份计划
        print(f"❌ 解析响应最终失败: {str(last_error)}")
        print(f"   将使用增强版备用方案生成计划")
        appears_truncated = response.count("{") > response.count("}") or response.count("[") > response.count("]")
        if appears_truncated:
            reason = "模型输出疑似被长度限制截断，JSON未完整闭合"
        else:
            reason = f"模型输出无法通过结构校验：{self._friendly_error(last_error)}"
        return self._create_fallback_plan(request, reason=reason)
    
    def _create_fallback_plan(self, request: TripRequest, reason: str = "外部工具或模型输出不可用") -> TripPlan:
        """
        创建增强版备用计划(当Agent完全失败时)

        与旧版不同,此版本包含:
        - 每天一个具体酒店(hotel字段)
        - 完整预算信息(budget字段)
        - 模拟天气信息(weather_info字段)
        - 合理的门票价格和经纬度
        确保前端能正常渲染所有卡片
        """
        from datetime import datetime, timedelta

        # 解析日期
        start_date = datetime.strptime(request.start_date, "%Y-%m-%d")

        # 城市经纬度映射(常见城市)
        city_coords = {
            "北京": (116.407396, 39.904200),
            "上海": (121.473701, 31.230416),
            "广州": (113.264385, 23.129112),
            "深圳": (114.057865, 22.543096),
            "成都": (104.066541, 30.572268),
            "杭州": (120.155070, 30.274086),
            "西安": (108.939621, 34.343147),
            "重庆": (106.551556, 29.563009),
            "武汉": (114.305393, 30.593099),
            "南京": (118.796877, 32.060255),
            "长沙": (112.938814, 28.228209),
            "苏州": (120.585316, 31.298886),
            "天津": (117.190686, 39.125596),
            "青岛": (120.382639, 36.067082),
            "厦门": (118.089425, 24.479834),
            "昆明": (102.832899, 25.038898),
            "大连": (121.614687, 38.914003),
            "哈尔滨": (126.534967, 45.803775),
        }
        base_lng, base_lat = city_coords.get(request.city, (116.397128, 39.916527))

        # 景点模板(按城市提供)
        attraction_templates = {
            "北京": [
                ("故宫博物院", "北京市东城区景山前街4号", 60, "历史文化", "中国明清两代的皇家宫殿,世界上现存规模最大的木质结构古建筑群,占地72万平方米。"),
                ("颐和园", "北京市海淀区新建宫门路19号", 30, "历史文化", "中国古典园林之首,清朝帝王避暑行宫,以昆明湖、万寿山为基址,以杭州西湖为蓝本建造。"),
                ("天坛公园", "北京市东城区天坛内东里7号", 15, "历史文化", "明清两代皇帝祭天祈谷的场所,是中国古代建筑艺术的精华。"),
                ("八达岭长城", "北京市延庆区G6京藏高速62号出口", 40, "历史文化", "万里长城的精华段落,地势险要,城关坚固。"),
                ("圆明园遗址公园", "北京市海淀区清华西路28号", 10, "历史文化", "清代大型皇家园林,有'万园之园'之称。"),
            ],
            "上海": [
                ("外滩", "上海市黄浦区中山东一路", 0, "城市风光", "上海标志性景观,全长1.5公里,东侧有哥特式、罗马式等52幢风格迥异的古典复兴大楼。"),
                ("东方明珠", "上海市浦东新区世纪大道1号", 180, "现代建筑", "上海地标建筑之一,高约468米,集都市观光、时尚餐饮、购物娱乐于一体。"),
                ("豫园", "上海市黄浦区安仁街137号", 40, "历史文化", "始建于明代的古典园林,有'东南名园冠'之美誉。"),
                ("迪士尼乐园", "上海市浦东新区川沙镇", 475, "主题乐园", "中国大陆首个迪士尼主题乐园,拥有众多独特的游乐项目和精彩演出。"),
                ("朱家角古镇", "上海市青浦区朱家角镇", 0, "历史文化", "上海四大历史文化名镇之一,有'上海威尼斯'之称。"),
            ],
        }

        # 获取城市景点模板,无则使用通用模板
        city_attractions = attraction_templates.get(request.city)
        if not city_attractions:
            city_attractions = [
                (f"{request.city}博物馆", f"{request.city}市中心", 0, "历史文化", f"展示{request.city}历史文化的综合性博物馆。"),
                (f"{request.city}古城", f"{request.city}老城区", 30, "历史文化", f"{request.city}保存完好的历史古城区域。"),
                (f"{request.city}公园", f"{request.city}市区", 0, "自然风光", f"{request.city}市民休闲的大型城市公园。"),
                (f"{request.city}商业街", f"{request.city}繁华地段", 0, "城市风光", f"{request.city}最具特色的商业街区。"),
                (f"{request.city}美食街", f"{request.city}市区", 0, "美食", f"品尝{request.city}地道美食的热门街区。"),
            ]

        # 天气模拟(根据月份推测)
        month = start_date.month
        if month in [12, 1, 2]:
            weathers = [("多云", "晴", 5, -3, "北风", "3-4级"), ("晴", "晴", 6, -2, "西北风", "2-3级")]
        elif month in [3, 4, 5]:
            weathers = [("多云", "多云", 18, 10, "南风", "2-3级"), ("晴", "多云", 20, 12, "东南风", "1-2级")]
        elif month in [6, 7, 8]:
            weathers = [("晴", "晴", 32, 25, "南风", "1-2级"), ("多云", "雷阵雨", 30, 24, "东南风", "3-4级")]
        else:
            weathers = [("多云", "多云", 22, 14, "东风", "2-3级"), ("晴", "晴", 24, 16, "南风", "1-2级")]

        # 住宿价格映射
        hotel_prices = {
            "经济型酒店": (200, 350),
            "舒适型酒店": (350, 600),
            "高档型酒店": (600, 1200),
            "豪华型酒店": (1200, 3000),
            "豪华酒店": (1200, 3000),
            "民宿": (250, 600),
        }
        cost_range = hotel_prices.get(request.accommodation, (300, 500))

        days = []
        total_attractions_cost = 0
        total_meals_cost = 0
        total_hotel_cost = 0
        weather_info_list = []

        for i in range(request.travel_days):
            current_date = start_date + timedelta(days=i)
            date_str = current_date.strftime("%Y-%m-%d")

            # 降级模板也遵循用户选择的行程节奏。
            pace_counts = {"relaxed": 2, "balanced": 3, "intensive": 4}
            num_attractions = min(pace_counts[request.pace], len(city_attractions))
            # 优先保证跨天不重复。候选不足时宁可减少每日安排，也不循环复制景点。
            start_index = round(i * len(city_attractions) / request.travel_days)
            end_index = round((i + 1) * len(city_attractions) / request.travel_days)
            day_attractions_templates = city_attractions[start_index:end_index][:num_attractions]
            if not day_attractions_templates:
                day_attractions_templates = [(
                    f"{request.city}自由探索区域（第{i + 1}天）",
                    f"{request.city}市区",
                    0,
                    "休闲",
                    "预留为自由活动，请结合实时地图选择附近已开放的场所。",
                )]

            attractions = []
            for j, (name, address, price, category, desc) in enumerate(day_attractions_templates):
                offset_lng = (i * 0.02 + j * 0.008) % 0.1
                offset_lat = (i * 0.015 + j * 0.01) % 0.08
                attractions.append(Attraction(
                    name=name,
                    address=address,
                    location=Location(longitude=round(base_lng + offset_lng, 6), latitude=round(base_lat + offset_lat, 6)),
                    visit_duration=120,
                    description=desc,
                    category=category,
                    ticket_price=price
                ))
                total_attractions_cost += price

            # 餐饮(每天早中晚)
            meals = [
                Meal(type="breakfast", name=f"{request.city}特色早餐", description=f"第{i+1}天早晨品尝当地特色早点", estimated_cost=25),
                Meal(type="lunch", name=f"当地特色午餐", description=f"景点附近{request.city}地道美食", estimated_cost=60),
                Meal(type="dinner", name=f"精选晚餐", description=f"品尝{request.city}特色美食,感受当地饮食文化", estimated_cost=80),
            ]
            total_meals_cost += 25 + 60 + 80

            # 酒店(每天推荐一个)
            hotel_cost = cost_range[0] + (i * 37) % (cost_range[1] - cost_range[0])
            total_hotel_cost += hotel_cost
            hotel = Hotel(
                name=f"{request.city}{request.accommodation}第{i+1}晚",
                address=f"{request.city}市中心区域",
                location=Location(longitude=round(base_lng + i * 0.01, 6), latitude=round(base_lat + i * 0.005, 6)),
                price_range=f"{cost_range[0]}-{cost_range[1]}元/晚",
                rating=str(round(4.3 + (i % 3) * 0.2, 1)),
                distance="距景点约1-3公里",
                type=request.accommodation,
                estimated_cost=hotel_cost,
            )

            # 天气信息
            weather_tuple = weathers[i % len(weathers)]
            weather_info_list.append(WeatherInfo(
                date=date_str,
                day_weather=weather_tuple[0],
                night_weather=weather_tuple[1],
                day_temp=weather_tuple[2],
                night_temp=weather_tuple[3],
                wind_direction=weather_tuple[4],
                wind_power=weather_tuple[5],
            ))

            day_plan = DayPlan(
                date=date_str,
                day_index=i,
                description=f"第{i+1}天: {request.city}精华景点游览,感受{request.city}独特的城市魅力。",
                transportation=request.transportation,
                accommodation=request.accommodation,
                hotel=hotel,
                attractions=attractions,
                meals=meals,
            )
            days.append(day_plan)

        total_transportation = request.travel_days * 60  # 每天约60元交通费
        total_budget = total_attractions_cost + total_hotel_cost + total_meals_cost + total_transportation

        return TripPlan(
            city=request.city,
            start_date=request.start_date,
            end_date=request.end_date,
            days=days,
            weather_info=weather_info_list,
            overall_suggestions=f"这是系统降级生成的{request.city}{request.travel_days}日参考模板。景点、天气、酒店、票价和坐标并非实时查询结果，请在出行前逐项核实。",
            budget = Budget(
                total_attractions=total_attractions_cost,
                total_hotels=total_hotel_cost,
                total_meals=total_meals_cost,
                total_transportation=total_transportation,
                total=total_budget,
            ),
            data_quality="fallback",
            is_fallback=True,
            warnings=[
                f"降级原因：{reason}。当前展示的是内置参考模板。",
                "天气、价格、开放时间、酒店和交通班次均不是实时数据。",
            ],
            data_sources=trip_quality_service.default_sources(is_fallback=True),
        )


# 全局多智能体系统实例
_multi_agent_planner = None


def get_trip_planner_agent() -> MultiAgentTripPlanner:
    """获取多智能体旅行规划系统实例(单例模式)"""
    global _multi_agent_planner

    if _multi_agent_planner is None:
        _multi_agent_planner = MultiAgentTripPlanner()

    return _multi_agent_planner
