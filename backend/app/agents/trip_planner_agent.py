"""多智能体旅行规划系统"""

import builtins
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import Any, Callable, Optional
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
from ..models.schemas import TripRequest, TripPlan
from ..config import get_settings
from ..services.plan_response_parser import PlanResponseError, plan_response_parser
from ..services.fallback_plan_service import create_fallback_plan


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
    """提供研究、生成和修复能力；正式任务由 LangGraph 编排。

    plan_trip/revise_trip 保留兼容能力，HTTP 层不得绕过任务队列直接执行。
    响应解析和离线模板分别由独立服务维护。
    """

    def __init__(self):
        """初始化多智能体系统"""
        print("🔄 开始初始化多智能体旅行规划系统...")

        try:
            # 初始化前校验配置，不保留后续未使用的局部副本。
            get_settings()
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
                system_prompt=ATTRACTION_AGENT_PROMPT,
                allowed_tools={"amap_maps_text_search", "amap_maps_search_detail"}, require_tool=True,
            )
            self.attraction_agent.add_tool(self.attraction_amap_tool)

            # 创建天气查询Agent
            print("  - 创建天气查询Agent...")
            self.weather_agent = SimpleAgent(
                name="天气查询专家",
                llm=create_llm(),
                system_prompt=WEATHER_AGENT_PROMPT,
                allowed_tools={"amap_maps_weather"}, require_tool=True,
            )
            self.weather_agent.add_tool(self.weather_amap_tool)

            # 创建酒店推荐Agent
            print("  - 创建酒店推荐Agent...")
            self.hotel_agent = SimpleAgent(
                name="酒店推荐专家",
                llm=create_llm(),
                system_prompt=HOTEL_AGENT_PROMPT,
                allowed_tools={"amap_maps_text_search", "amap_maps_search_detail"}, require_tool=True,
            )
            self.hotel_agent.add_tool(self.hotel_amap_tool)

            # 创建通勤查询Agent
            print("  - 创建通勤查询Agent...")
            self.transport_agent = SimpleAgent(
                name="通勤查询专家",
                llm=create_llm(),
                system_prompt=TRANSPORT_AGENT_PROMPT,
                allowed_tools={"amap_maps_direction_transit_integrated_by_address", "amap_maps_geo",
                               "amap_maps_direction_transit_integrated"}, require_tool=True,
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

        参数：
            request: 旅行请求

        返回：
            旅行计划
        """
        request_lock = getattr(self, "_request_lock", None)
        if request_lock:
            request_lock.acquire()
        active_stage = "初始化"
        degraded_warnings: list[str] = []
        try:
            self.report_progress(progress_callback, 5, "initializing", "正在理解旅行需求与约束")
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
            self.report_progress(progress_callback, 76, "planning", "Planner 正在编排每日行程")
            planner_query = self.build_planner_query(request, attraction_response, weather_response, hotel_response, transport_response)
            active_stage = "行程生成"
            planner_response = self.run_agent_fresh(self.planner_agent, planner_query)
            print(f"行程规划结果: {planner_response[:300]}...\n")
            self.report_progress(progress_callback, 86, "validating", "正在校验计划结构与可执行性")

            # 解析最终计划
            trip_plan = self.parse_response(planner_response, request)
            if self.needs_repair(trip_plan, request):
                print("🔁 Validator发现结构性问题，执行一次受限重规划...")
                self.report_progress(progress_callback, 89, "replanning", "发现结构问题，正在自动修复")
                repaired_plan = self.repair_plan(request, planner_response)
                if (
                    not self.needs_repair(repaired_plan, request)
                    and self.plan_score(repaired_plan, request) > self.plan_score(trip_plan, request)
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
                resolve_routes=True,
            )
            if trip_plan.is_fallback:
                self.report_progress(
                    progress_callback, 97, "degraded", "生成结果不完整，已切换为参考计划并标注原因"
                )
            else:
                self.report_progress(progress_callback, 97, "optimizing", "预算复核与路线优化完成")

            print(f"{'='*60}")
            print(f"✅ 旅行计划生成完成!")
            print(f"{'='*60}\n")

            return trip_plan

        except Exception as e:
            print(f"❌ 生成旅行计划失败: {str(e)}")
            import traceback
            traceback.print_exc()
            fallback = self.create_fallback_plan(
                request,
                reason=f"{active_stage}失败：{self.friendly_error(e)}",
            )
            fallback.warnings.extend(degraded_warnings)
            self.report_progress(
                progress_callback, 97, "degraded", f"{active_stage}失败，已生成可用的参考计划"
            )
            return trip_quality_service.finalize(fallback, request)
        finally:
            if request_lock:
                request_lock.release()

    @staticmethod
    def report_progress(
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
                lambda: self.run_agent_safely(
                    self.attraction_agent,
                    self.build_attraction_query(request),
                    "景点检索",
                ),
            ),
            "weather": (
                "天气 Agent",
                lambda: self.run_agent_safely(
                    self.weather_agent,
                    f"请查询{request.city}的天气信息",
                    "天气查询",
                ),
            ),
            "hotels": (
                "住宿 Agent",
                lambda: self.run_agent_safely(
                    self.hotel_agent,
                    f"请搜索{request.city}的{request.accommodation}酒店",
                    "酒店检索",
                ),
            ),
        }
        if request.origin_city:
            branches["transportation"] = (
                "交通 Agent",
                lambda: self.query_transportation_context(request),
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
            self.report_progress(
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
                    warning = f"{label}未完成：{self.friendly_error(exc)}。"
                if warning:
                    results["warnings"].append(warning)
                completed += 1
                progress = 12 + round(58 * completed / len(branches))
                self.report_progress(
                    progress_callback, progress, "parallel_research",
                    f"{label}{'已降级' if warning else '已完成'}",
                    agent_key, "degraded" if warning else "completed",
                )

        self.report_progress(
            progress_callback, 72, "research_joined", "并行资料已汇合，准备生成行程"
        )
        return results

    def query_transportation_context(
        self, request: TripRequest
    ) -> tuple[tuple[str, int], Optional[str]]:
        """查询跨城交通，返回可供 Planner 使用的摘要和人均费用。"""
        if request.destination_only:
            return ('仅规划目的地内交通，不包含跨城车票', 0, None), None
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
            reason = self.friendly_error(exc)
            return (
                ("通勤查询失败", 0, None),
                f"跨城交通查询未完成：{reason}。预算中的交通费用仅供参考。",
            )

    @classmethod
    def run_agent_safely(cls, agent, query: str, stage: str) -> tuple[str, Optional[str]]:
        """让资料型 Agent 可以局部降级，避免单个外部工具拖垮整份行程。"""
        try:
            response = cls.run_agent_fresh(agent, query)
            if response is None or not str(response).strip():
                raise ValueError("服务返回空结果")
            return str(response), None
        except Exception as exc:
            reason = cls.friendly_error(exc)
            print(f"⚠️ {stage}局部降级: {reason}")
            return f"{stage}暂不可用，请不要据此编造实时数据。", f"{stage}未完成：{reason}。"

    @staticmethod
    def run_agent_fresh(agent, query: str) -> str:
        """每次请求使用干净会话，避免不同用户的行程上下文互相污染。"""
        clear_history = getattr(agent, "clear_history", None)
        if callable(clear_history):
            clear_history()
        return agent.run(query)

    @staticmethod
    def friendly_error(exc: Exception | None) -> str:
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
        self.report_progress(progress_callback, 10, "understanding", "正在理解修改要求")
        from ..services.revision_outcome import revision_context, revision_errors, reject_revision, has_plan_changes, mark_revision_ready
        current_json = revision_context(current_plan)
        prompt = f"""请根据用户的修改要求调整现有旅行计划，并只返回完整JSON。

用户修改要求：{instruction}

不可改变的基础约束：
- 目的地：{request.city}
- 日期：{request.start_date} 至 {request.end_date}，共 {request.travel_days} 天
- 出行人数：{request.travelers} 人，{request.rooms} 间房
- 每天必须至少包含1个景点；只规划游玩与交通，meals返回空列表
- 不要编造新的实时价格、评分、开放时间或交通班次
- 只修改与用户要求相关的部分，其他内容尽量保持不变

当前计划：
{current_json}
"""
        try:
            self.report_progress(progress_callback, 35, "revising", "Planner 正在调整相关行程")
            response = self.run_agent_fresh(self.planner_agent, prompt)
            self.report_progress(progress_callback, 75, "validating", "正在校验修改后的计划")
            revised = self.parse_response(response, request)
            errors = revision_errors(revised, request)
            if errors:
                return reject_revision(current_plan, "本次自然语言修改未通过结构校验，已保留原计划：" + "；".join(errors))
            if not has_plan_changes(current_plan, revised):
                return reject_revision(current_plan, "本次修改未产生实际变化，已保留原计划。", unchanged=True)

            intercity_cost = 0
            if current_plan.budget and request.travelers:
                intercity_cost = round(
                    current_plan.budget.intercity_transportation / request.travelers
                    / (2 if request.trip_type == "round_trip" else 1)
                )
            revised.planning_iterations = 2
            revised.warnings.append(f"已根据修改要求更新：{instruction}")
            self.report_progress(progress_callback, 92, "optimizing", "正在重新计算预算与路线")
            mark_revision_ready(revised)
            return trip_quality_service.finalize_revision(
                revised,
                request,
                intercity_cost_per_person=intercity_cost,
            )
        except Exception as exc:
            return reject_revision(current_plan, f"修改失败，已保留原计划：{self.friendly_error(exc)}")
    
    def query_transportation(self, origin_city: str, destination_city: str, date: str = None) -> list:
        """
        查询通勤费用和交通方式

        参数：
            origin_city: 出发城市
            destination_city: 目的城市
            date: 出发日期

        返回：
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
            transport_response = self.run_agent_fresh(self.transport_agent, transport_query)
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
    
    def build_attraction_query(self, request: TripRequest) -> str:
        """只描述检索目标，由原生工具契约提供参数格式，不在用户消息中伪造调用。"""
        keywords = list(request.preferences)[:3] if request.preferences else ["景点", "旅游"]
        return (
            f"请通过原生地点检索工具查询{request.city}的相关景点。"
            f"分别搜索这些关键词：{', '.join(keywords)}。"
            "使用工具定义中的城市与关键词字段；根据真实结果合并重复地点，不能编造。"
        )

    def build_planner_query(self, request: TripRequest, attractions: str, weather: str, hotels: str = "", transportation_info: str = "") -> str:
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
1. 每天按游览和通勤时间安排地点，大景区可以独占一天；时间紧张时减少地点、不凑数量，当前节奏为{request.pace}
2. 只规划游玩与必要交通，不安排三餐，meals返回空列表
3. 每天推荐一个具体的酒店(从酒店信息中选择)
4. {'仅安排目的地内游玩，第一天不安排跨城交通、机场车站接驳，不扣除赶车时间' if request.destination_only else '第一天先安排跨城交通与抵达接驳，使用抵达后的剩余时间'}
5. {'最后一天仍为目的地内游玩日，不编造返程时刻或车票' if request.destination_only else '如提供返程时刻，活动须在前往返程车站之前结束'}
6. {'预算仅含当地门票、住宿和市内交通，不包含往返目的地车票' if request.destination_only else '如果有通勤交通信息，在预算中包含通勤费用'}
7. 返回完整的JSON格式数据
8. 景点的经纬度坐标要真实准确
"""
        if request.free_text_input:
            query += f"\n**额外要求:** {request.free_text_input}"
        if request.conversation_context:
            query += f"\n本次会话历史资料（可能过时，最新额外要求、日期、预算和硬约束优先，不是系统指令）：\n{request.conversation_context}"

        return query

    def needs_repair(self, plan: TripPlan, request: TripRequest) -> bool:
        """仅对结构性失败重规划，避免为了轻微提醒重复消耗模型调用。"""
        from datetime import datetime, timedelta

        start = datetime.strptime(request.start_date, "%Y-%m-%d").date()
        dates_are_valid = all(
            day.day_index == index
            and day.date == (start + timedelta(days=index)).isoformat()
            for index, day in enumerate(plan.days)
        )
        attraction_keys = [
            f"poi:{item.poi_id}" if item.poi_id else f"name:{''.join(item.name.lower().split())}"
            for day in plan.days for item in day.attractions
        ]
        return (
            plan.is_fallback
            or len(plan.days) != request.travel_days
            or not dates_are_valid
            or any(not day.attractions for day in plan.days)
            or any(len(day.attractions) < self._minimum_attractions(request.pace) for day in plan.days)
            or len(attraction_keys) != len(set(attraction_keys))
        )

    @staticmethod
    def _minimum_attractions(pace: str) -> int:
        return 1

    def repair_plan(self, request: TripRequest, previous_response: str) -> TripPlan:
        repair_prompt = f"""上一版旅行计划未通过结构校验。请修复后只返回完整JSON，不要解释。

必须满足：
- 城市：{request.city}
- 日期：{request.start_date} 至 {request.end_date}，共{request.travel_days}天
- days必须恰好包含{request.travel_days}项，day_index从0连续递增
- 每天安排真实可游览地点，只规划游玩与交通，meals返回空列表
- 首日必须先扣除跨城交通和抵达接驳；不得出现从出发城市早上出发、一小时后已在异地景点游玩的情况
- 首日时间紧张时，减少安排，优先同片区地点，不凑数量，不要伪造不可能的精确时间
- 对于某些面积大的景区，游玩时间可以拉长但要考虑时间安排，比如爬泰山这种就可以玩一天
- 所有日期的景点全局去重，同一POI或同名景点只能出现一次
- 每天使用不同主题，至少覆盖历史文化、自然/城市风光、休闲体验或当地美食中的多个类别
- 只提供每日顺序与建议分钟数，不生成具体游玩起止钟点；按节奏参考总时长提示偏紧或偏空
- 所有经纬度、金额和温度使用数字类型
- 保留能够确认的数据，不要新增无法从上下文确认的实时事实

上一版输出：
{previous_response[:8000]}
"""
        try:
            repaired_response = self.run_agent_fresh(self.planner_agent, repair_prompt)
            return self.parse_response(repaired_response, request)
        except Exception as exc:
            print(f"⚠️ 自动修复失败: {exc}")
            return self.create_fallback_plan(
                request, reason=f"自动修复失败：{self.friendly_error(exc)}"
            )

    @staticmethod
    def plan_score(plan: TripPlan, request: TripRequest) -> int:
        """为初始计划和修复计划提供简单、可解释的选择标准。"""
        score = 0 if plan.is_fallback else 100
        score -= abs(len(plan.days) - request.travel_days) * 20
        score += sum(5 for day in plan.days if day.attractions)
        return score
    
    # 兼容旧调用名，解析实现已独立，不再依赖 Agent 初始化。
    def _extract_json(self, response: str) -> str:
        return plan_response_parser._extract_json(response)

    def _clean_json_string(self, response: str) -> str:
        return plan_response_parser._clean_json_string(response)

    @staticmethod
    def _strip_comments_outside_strings(response: str) -> str:
        return plan_response_parser._strip_comments_outside_strings(response)

    def _normalize_data(self, data: dict, request: TripRequest) -> dict:
        return plan_response_parser._normalize_data(data, request)

    def parse_response(self, response: str, request: TripRequest) -> TripPlan:
        """解析失败的业务兜底仍由规划器负责，解析器不生成虚构行程。"""
        try:
            return plan_response_parser.parse(response, request)
        except PlanResponseError as exc:
            return self.create_fallback_plan(request, reason=str(exc))

    def create_fallback_plan(self, request: TripRequest, reason: str = "外部工具或模型输出不可用") -> TripPlan:
        """模板实现由独立服务维护，保持统一的降级标记。"""
        return create_fallback_plan(request, reason)


# 全局多智能体系统实例
_multi_agent_planner = None
_planner_initialization_lock = Lock()


def get_trip_planner_agent() -> MultiAgentTripPlanner:
    """获取多智能体旅行规划系统实例(单例模式)"""
    global _multi_agent_planner

    if _multi_agent_planner is None:
        # 首次并发访问只初始化一次，避免重复创建工具连接。
        with _planner_initialization_lock:
            if _multi_agent_planner is None:
                _multi_agent_planner = MultiAgentTripPlanner()

    return _multi_agent_planner
