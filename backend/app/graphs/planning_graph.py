"""PlanningGraph：共享规划内核（产品化计划 8.1 节）。

当前流程：
- 地点、天气、酒店并行研究；仅兼容非目的地模式时查询跨城信息；
- 优先生成每日骨架并用地图资料填充，失败时保留完整 JSON 降级路径；
- 路线与预算确认通过 checkpointer 暂停/恢复；
- 质量服务统一收尾，任务层负责配额、队列与状态通知。
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..agents.trip_planner_agent import MultiAgentTripPlanner, get_trip_planner_agent
from ..models.schemas import TripPlan, TripRequest
from ..services.candidate_pool_service import apply_candidates_to_plan, render_candidates_for_prompt
from ..services.day_pipeline_service import (
    DayPipelineError,
    build_day_capacities,
    build_skeleton_prompt,
    materialize_skeleton,
    parse_skeleton,
)
from ..services.trip_quality_service import trip_quality_service
from ..services.hotel_research_service import research_hotels, hotels_digest, bind_hotels_to_plan
from ..services.tourism_research_service import (
    TourismResearchError, bind_researched_request, enforce_researched_plan, research_tourism,
)
from .state import PlanningState

ProgressCallback = Optional[Callable[..., None]]

_planning_graph = None
_checkpointer = None
_checkpointer_initialized = False
_checkpointer_lock = threading.Lock()

# 住宿档次降级顺序：index 越大越豪华；已是最低档返回 None
_ACCOMMODATION_TIERS = [
    "经济型酒店",
    "民宿",
    "舒适型酒店",
    "高档型酒店",
    "豪华型酒店",
    "豪华酒店",
]


class PlanningWaitingInput(Exception):
    """规划图触发 interrupt，等待用户确认。question 为 interrupt 载荷。"""

    def __init__(self, question: Optional[dict[str, Any]] = None) -> None:
        self.question = question or {}
        super().__init__("规划任务等待用户确认")


def _get_planner() -> MultiAgentTripPlanner:
    # 运行时解析，保证测试替身（monkeypatch get_trip_planner_agent）继续生效。
    return get_trip_planner_agent()


def _callback_from_config(config: RunnableConfig) -> ProgressCallback:
    if not config:
        return None
    return (config.get("configurable") or {}).get("progress_callback")


def _unwrap_lc_deep(value: Any) -> Any:
    """递归剥掉 langchain serde 的 lc constructor 包装。

    RedisSaver 恢复时不还原模型：存储形如
    {"lc": 2, "type": "constructor", "id": [...], "kwargs": {...}}，
    且**嵌套模型**（如 plan.kwargs.budget）同样是 lc 包装——只剥外层会让
    内层模型从残缺 dict 构造出全默认值（踩过：over_budget 变 False 导致
    降级逻辑被跳过）。因此必须逐层剥到纯业务字段。
    """
    if isinstance(value, dict):
        if (
            value.get("lc")
            and value.get("type") == "constructor"
            and isinstance(value.get("kwargs"), dict)
        ):
            return _unwrap_lc_deep(value["kwargs"])
        return {key: _unwrap_lc_deep(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_unwrap_lc_deep(item) for item in value]
    return value


def _as_request(value: Any) -> TripRequest:
    """真实 Checkpointer（RedisSaver）序列化后 pydantic 模型以 lc 包装 dict 存储，
    resume 恢复时不会自动还原为模型；内存 saver 直通对象。此守卫统一还原。"""
    value = _unwrap_lc_deep(value)
    if isinstance(value, dict):
        return TripRequest.model_validate(value)
    return value


def _as_plan(value: Any) -> Optional[TripPlan]:
    value = _unwrap_lc_deep(value)
    if isinstance(value, dict):
        return TripPlan.model_validate(value)
    return value


def get_checkpointer():
    """Redis 启用且可用时返回 RedisSaver（已建 RediSearch 索引），否则 None。

    结果进程级缓存一次：无论成败都不重复 setup（索引创建幂等但成本高）。
    锁 + 双检：setup 耗时数秒，无锁时并发首请求会拿到 None 并把
    "无 checkpointer 的图"永久缓存，interrupt 功能静默失效直到重启。
    """
    global _checkpointer, _checkpointer_initialized
    if _checkpointer_initialized:
        return _checkpointer
    with _checkpointer_lock:
        if _checkpointer_initialized:
            return _checkpointer
        try:
            from ..config import get_settings
            from ..infrastructure import redis_client

            settings = get_settings()
            if not settings.redis_enabled:
                _checkpointer = None
            elif not redis_client.redis_available():
                _checkpointer = None
            else:
                from langgraph.checkpoint.redis import RedisSaver

                saver = RedisSaver(settings.redis_url)
                saver.setup()
                _checkpointer = saver
                print("✅ LangGraph Redis Checkpointer 已就绪（interrupt/恢复可用）")
        except Exception as exc:
            print(f"⚠️ Checkpointer 初始化失败，interrupt 功能不可用: {exc}")
            _checkpointer = None
        # setup 完成（或确定失败）后才置标志，避免竞态期间其他线程拿到半初始化状态
        _checkpointer_initialized = True
    return _checkpointer


# ---------------------------------------------------------------------------
# 节点
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# research 并行扇出：四个独立节点（产品化计划 7.1/批1）
# 每个节点只写自己的 context key，由 state.merge_context reducer 合并；
# 单分支失败只产生自己的 warning 并降级，不影响其他分支。
# ---------------------------------------------------------------------------

_RESEARCH_DEFAULTS = {
    "attractions": "景点检索暂不可用",
    "weather": "天气查询暂不可用",
    "hotels": "酒店检索暂不可用",
}


def poi_research_node(state: PlanningState, config: RunnableConfig) -> dict:
    """无攻略时优先联网生成并核验草案；用户已有攻略候选时沿用既有研究。"""
    planner = _get_planner()
    callback = _callback_from_config(config)
    request: TripRequest = state["request"]
    planner.report_progress(
        callback, 12, "parallel_research", "景点 Agent 已开始工作", "attractions", "running"
    )
    from ..services.baidu_search_service import BaiduSearchService
    if not request.candidate_places and BaiduSearchService().configured:
        planner.report_progress(callback, 16, "tourism_search", "正在联网搜索并生成逐日草案", "attractions", "running")
        try:
            candidates, skeleton_raw = research_tourism(request)
        except TourismResearchError as exc:
            planner.report_progress(callback, 30, "tourism_validation", str(exc), "attractions", "degraded")
            # 把可展示原因带到汇合点，统一停止；绝不走旧景点检索绕过核验。
            return {"context": {"tourism_error": str(exc), "tourism_error_details": exc.details}}
        except Exception:
            # 存储或适配器异常同样不能绕过选点核验；对外只显示固定原因。
            return {"context": {"tourism_error": "联网选点服务暂不可用，请稍后重试"}}
        planner.report_progress(callback, 30, "tourism_validation", f"已核对 {len(candidates)} 个同城旅游地点", "attractions", "completed")
        return {"context": {"attractions": "地点已由联网资料和高德核对，按草案继续落地。",
                            "tourism_candidates": [c.model_dump(mode="json") for c in candidates],
                            "tourism_skeleton": skeleton_raw}}
    try:
        value, warning = planner.run_agent_safely(
            planner.attraction_agent,
            planner.build_attraction_query(request),
            "景点检索",
        )
    except Exception as exc:
        value = _RESEARCH_DEFAULTS["attractions"]
        warning = f"景点 Agent未完成：{planner.friendly_error(exc)}。"
    planner.report_progress(
        callback, 30, "parallel_research",
        f"景点 Agent{'已降级' if warning else '已完成'}",
        "attractions", "degraded" if warning else "completed",
    )
    return {"context": {"attractions": value}, "warnings": [warning] if warning else []}


def weather_research_node(state: PlanningState, config: RunnableConfig) -> dict:
    """天气研究分支：目的地天气预报查询。"""
    planner = _get_planner()
    callback = _callback_from_config(config)
    request: TripRequest = state["request"]
    planner.report_progress(
        callback, 12, "parallel_research", "天气 Agent 已开始工作", "weather", "running"
    )
    try:
        value, warning = planner.run_agent_safely(
            planner.weather_agent,
            f"请查询{request.city}的天气信息",
            "天气查询",
        )
    except Exception as exc:
        value = _RESEARCH_DEFAULTS["weather"]
        warning = f"天气 Agent未完成：{planner.friendly_error(exc)}。"
    planner.report_progress(
        callback, 45, "parallel_research",
        f"天气 Agent{'已降级' if warning else '已完成'}",
        "weather", "degraded" if warning else "completed",
    )
    return {"context": {"weather": value}, "warnings": [warning] if warning else []}


def hotel_research_node(state: PlanningState, config: RunnableConfig) -> dict:
    """酒店固定检索：不让模型反复决策、查详情或扩写推荐。"""
    planner = _get_planner()
    callback = _callback_from_config(config)
    request: TripRequest = state["request"]
    planner.report_progress(
        callback, 12, "parallel_research", "正在快速检索酒店地点", "hotels", "running"
    )
    try:
        hotels, warning = research_hotels(request)
        value = hotels_digest(hotels)
    except Exception as exc:
        value = _RESEARCH_DEFAULTS["hotels"]
        hotels = []
        warning = "酒店地点检索暂不可用，住宿暂按档次预留预算。"
    planner.report_progress(
        callback, 60, "parallel_research",
        f"酒店检索{'已降级，保留住宿预算' if warning else '已完成'}",
        "hotels", "degraded" if warning else "completed",
    )
    return {"context": {"hotels": value, "hotel_candidates": [h.model_dump(mode="json") for h in hotels]},
            "warnings": [warning] if warning else []}


def intercity_research_node(state: PlanningState, config: RunnableConfig) -> dict:
    """跨城交通研究分支：无出发地时静默跳过（与旧行为一致，不产生事件）。"""
    planner = _get_planner()
    callback = _callback_from_config(config)
    request: TripRequest = state["request"]
    if request.destination_only or not request.origin_city:
        return {}
    planner.report_progress(
        callback, 12, "parallel_research", "交通 Agent 已开始工作", "transportation", "running"
    )
    try:
        value, warning = planner.query_transportation_context(request)
        transportation = value[0]
        cost = value[1]
        # 兼容测试替身及旧的自定义交通分支返回值。
        option = value[2] if len(value) > 2 else None
    except Exception as exc:
        transportation, cost, option = "", 0, None
        warning = f"交通 Agent未完成：{planner.friendly_error(exc)}。"
    planner.report_progress(
        callback, 70, "parallel_research",
        f"交通 Agent{'已降级' if warning else '已完成'}",
        "transportation", "degraded" if warning else "completed",
    )
    return {
        "context": {
            "transportation": transportation,
            "intercity_cost_per_person": cost,
            "transportation_option": option,
        },
        "warnings": [warning] if warning else [],
    }


def _planner_query_with_candidates(
    planner: MultiAgentTripPlanner, request: TripRequest, context: dict
) -> str:
    """构造规划 prompt；请求携带攻略候选时附加候选段落（阶段 2 候选池接入）。"""
    query = planner.build_planner_query(
        request,
        context.get("attractions", "景点检索暂不可用"),
        context.get("weather", "天气查询暂不可用"),
        context.get("hotels", "酒店检索暂不可用"),
        context.get("transportation", ""),
    )
    # 必去/想去候选的硬约束与已核对坐标只通过 prompt 传递；excluded 已在上游过滤
    memory = getattr(request, 'memory_context', '')
    return query + render_candidates_for_prompt(getattr(request, "candidate_places", []) or []) + (
        '\n长期偏好参考（仅软偏好，本轮需求与系统硬约束优先）：\n' + memory if memory else '')


def generate_plan_node(state: PlanningState, config: RunnableConfig) -> dict:
    """Planner 大 JSON 生成 + 受限修复循环（对应旧路径 76%~89% 区段）。"""
    planner = _get_planner()
    callback = _callback_from_config(config)
    request = _as_request(state["request"])
    context = state.get("context", {})
    request = bind_researched_request(request, context)
    researched = "tourism_candidates" in context

    planner.report_progress(callback, 72, "research_joined", "并行资料已汇合，准备生成行程")
    if getattr(request, "candidate_places", None) and not researched:
        planner.report_progress(
            callback, 74, "candidate_alignment", f"将优先安排你确认的 {len(request.candidate_places)} 个攻略候选地点"
        )
    # 批2 日编排管线优先：容量(确定性) -> 骨架(LLM 只出名称) -> 物化(候选/高德回填)。
    # 旧模式失败可回退完整 JSON；联网核验模式失败必须明确停止。
    planner_response = ""
    trip_plan = None
    try:
        planner.report_progress(callback, 75, "capacity", "正在计算每日时间容量")
        capacities = build_day_capacities(request, context.get("transportation_option"))
        skeleton_prompt = build_skeleton_prompt(
            request, capacities,
            context.get("attractions", ""),
            context.get("hotels", ""),
        )
        planner.report_progress(callback, 78, "skeleton_generation", "正在根据时间和偏好编排行程骨架")
        from ..config import get_settings
        if researched:
            from ..services.planner_arrangement_service import arrange_researched_plan
            try:
                trip_plan, skeleton_raw = arrange_researched_plan(request, context, planner.planner_agent.llm)
            except Exception as exc:
                from ..services.planning_repair_service import record_planning_diagnostic
                details = {"error_type": type(exc).__name__}
                if hasattr(exc, "errors"):
                    details["validation_errors"] = exc.errors(include_input=False)
                elif isinstance(exc, (ValueError, KeyError)):
                    details["reason"] = str(exc)[:800]
                record_planning_diagnostic("initial_planner_rejected", **details)
                raise TourismResearchError("Planner 编排结构校验失败，未切换为未经核验的整份重生成", details) from None
        elif get_settings().skeleton_direct_enabled and hasattr(planner.planner_agent, 'llm'):
            # 生成行程骨架不需要工具，也不需要完整规划智能体的系统提示词。
            # 通过单次请求参数设置输出上限，避免并发修改共享客户端。
            # 推理模型会与隐藏思考共用额度，因此保留配置中的最低输出预算。
            skeleton_raw = planner.planner_agent.llm.invoke([
                {'role': 'system', 'content': '只返回紧凑的行程骨架JSON，不输出推理过程、不调用工具。'},
                {'role': 'user', 'content': skeleton_prompt},
            ], max_tokens=max(planner.planner_agent.llm.max_tokens,
                              min(16384, max(2048, request.travel_days * 450 + 512))))
        else:
            skeleton_raw = planner.run_agent_fresh(planner.planner_agent, skeleton_prompt)
        # 若后续结构校验仍需修复，至少把原始骨架交给修复器，不能传空字符串。
        planner_response = skeleton_raw
        if not researched:
            skeletons, overall = parse_skeleton(skeleton_raw, capacities)
            planner.report_progress(callback, 82, "materializing", "正在用地图数据填充景点坐标")
            trip_plan = materialize_skeleton(request, skeletons, overall)
        planner.report_progress(callback, 86, "validating", "正在校验计划结构与可执行性")
    except DayPipelineError as exc:
        if researched:
            raise TourismResearchError("联网草案未通过每日编排校验，请调整需求后重试") from None
        print(f"⚠️ 日编排管线回退单次生成：{exc}")
        planner.report_progress(callback, 78, "fallback_generation", "骨架需要调整，正在重新生成完整行程")
        trip_plan = None

    if trip_plan is None:
        planner_query = _planner_query_with_candidates(planner, request, context)
        planner_response = planner.run_agent_fresh(planner.planner_agent, planner_query)
        print(f"行程规划结果: {planner_response[:300]}...")
        planner.report_progress(callback, 86, "validating", "正在校验计划结构与可执行性")
        trip_plan = planner.parse_response(planner_response, request)
    replan_count = 0
    if not researched and planner.needs_repair(trip_plan, request):
        print("🔁 Validator发现结构性问题，执行一次受限重规划...")
        planner.report_progress(callback, 89, "repair_generation", "发现结构问题，正在自动修复")
        repaired_plan = planner.repair_plan(request, planner_response)
        replan_count = 1
        if (
            not planner.needs_repair(repaired_plan, request)
            and planner.plan_score(repaired_plan, request) > planner.plan_score(trip_plan, request)
        ):
            trip_plan = repaired_plan
            trip_plan.planning_iterations = 2
            trip_plan.warnings.append("初始计划未通过结构校验，系统已自动修复一次。")
        else:
            trip_plan.warnings.append("自动修复未产生更好的结果，已保留原计划。")

    # 候选对账：回填已核对坐标/poi_id，必去缺失写提示（不强制塞入行程）
    trip_plan.warnings.extend(apply_candidates_to_plan(trip_plan, request))
    if "hotel_candidates" in context:
        # 联网草案不含酒店名时，也使用同一批真实候选回填坐标与地址。
        bind_hotels_to_plan(trip_plan, request, context["hotel_candidates"])
    if researched:
        enforce_researched_plan(trip_plan, context["tourism_candidates"])
        trip_plan.warnings.append("景点依据来自百度联网搜索并经高德核对；推荐理由不等于营业时间、预约或门票已实时核实。")
    trip_plan.warnings.extend(state.get("warnings", []))
    return {
        "plan": trip_plan,
        "planner_response": planner_response,
        "replan_count": replan_count,
        "intercity_cost_per_person": context.get("intercity_cost_per_person", 0),
        "transportation_option": context.get("transportation_option"),
    }


def downgrade_accommodation(request: TripRequest) -> Optional[TripRequest]:
    """住宿降级一档；已是最低档返回 None。"""
    try:
        idx = _ACCOMMODATION_TIERS.index(request.accommodation)
    except ValueError:
        return None
    if idx == 0:
        return None
    data = request.model_dump()
    data["accommodation"] = _ACCOMMODATION_TIERS[idx - 1]
    return TripRequest.model_validate(data)


# ---------------------------------------------------------------------------
# 多路线选择 interrupt（骨架先行，产品化计划四-3）：
# 方案由确定性代码从当前偏好构造（住宿档位/节奏变体），不额外消耗 LLM token；
# 用户选中方向后再完整生成。未来可换成 LLM 产出差异化骨架。
# ---------------------------------------------------------------------------

def _accommodation_shift(request: TripRequest, offset: int) -> Optional[str]:
    """住宿档位平移：offset=+1 升档 / -1 降档；越界或未知档次返回 None。"""
    try:
        idx = _ACCOMMODATION_TIERS.index(request.accommodation)
    except ValueError:
        return None
    target = idx + offset
    if not 0 <= target < len(_ACCOMMODATION_TIERS):
        return None
    return _ACCOMMODATION_TIERS[target]


def _build_route_options(request: TripRequest) -> list[dict]:
    """构造 2-3 个行程方向选项：当前偏好 / 舒适升级 / 经济轻松。"""
    options = [{
        "value": "balanced",
        "label": f"按当前偏好生成（{request.accommodation} · {request.pace}节奏）",
        "description": "严格按你填写的住宿档次与行程节奏",
    }]
    upgrade = _accommodation_shift(request, +1)
    if upgrade:
        options.append({
            "value": "comfort",
            "label": f"舒适升级（住宿升为{upgrade}）",
            "description": "住宿更好，预算会相应提高",
        })
    downgrade = _accommodation_shift(request, -1)
    if downgrade:
        options.append({
            "value": "economy",
            "label": f"经济轻松（住宿降为{downgrade} · 轻松节奏）",
            "description": "更省钱，景点安排更宽松",
        })
    return options


def _apply_route_choice(request: TripRequest, answer: str) -> TripRequest:
    """把用户选择的方向落成请求参数调整；未知选择按当前偏好。"""
    if answer == "comfort":
        upgrade = _accommodation_shift(request, +1)
        if upgrade:
            return request.model_copy(update={"accommodation": upgrade})
    if answer == "economy":
        downgrade = _accommodation_shift(request, -1)
        if downgrade:
            return request.model_copy(
                update={"accommodation": downgrade, "pace": "relaxed"}
            )
    return request


def route_confirm_node(state: PlanningState, config: RunnableConfig) -> dict:
    """多路线选择 interrupt：生成前让用户选行程方向。

    仅在请求显式开启（ask_route_options）且有可选差异时暂停；
    resume 后按选择调整住宿/节奏，再进入 generate_plan。
    """
    planner = _get_planner()
    callback = _callback_from_config(config)
    request = _as_request(state["request"])
    if not getattr(request, "ask_route_options", False):
        return {}

    options = _build_route_options(request)
    if len(options) <= 1:
        return {}  # 档位已到边界，没有有意义的变体
    planner.report_progress(callback, 70, "route_confirm", "已准备多个行程方向，等待选择")
    answer = interrupt({
        "type": "route_confirm",
        "question": "生成前先选一个行程方向（都会包含你确认的攻略候选地点）：",
        "options": options,
        "progress": 70,
        "current_accommodation": request.accommodation,
        "current_pace": request.pace,
    })
    adjusted = _apply_route_choice(request, str(answer))
    planner.report_progress(
        callback, 72, "route_confirmed", f"已按「{adjusted.accommodation} · {adjusted.pace}节奏」方向生成行程"
    )
    return {"request": adjusted}


def budget_confirm_node(state: PlanningState, config: RunnableConfig) -> dict:
    """预算确认 interrupt：超预算时暂停询问用户是否降级住宿重新生成。

    - 生成路径的 `parse_response` 会 pop budget（预算延迟到 finalize 算），
      所以这里必须**现算**预算判断，读 plan.budget 恒为 None（曾因此死代码）；
    - 恢复执行时 interrupt() 返回用户答案（"downgrade" / "keep"）；
    - 最多降级重生成 1 次（budget_downgrade_attempts 熔断），防循环。
    """
    planner = _get_planner()
    callback = _callback_from_config(config)
    request = _as_request(state["request"])
    plan = _as_plan(state.get("plan"))
    attempts = state.get("budget_downgrade_attempts", 0)
    intercity_cost = state.get("intercity_cost_per_person", 0)
    # 现算预算：plan.budget 在生成路径上尚未填充（fallback 模板除外）
    budget = (
        plan.budget
        if plan and plan.budget is not None
        else (
            trip_quality_service.calculate_budget(plan, request, intercity_cost_per_person=intercity_cost)
            if plan
            else None
        )
    )

    if (
        plan is None
        or not request.budget_limit
        or budget is None
        or not budget.over_budget
        or attempts >= 1
    ):
        return {}

    over_amount = budget.total - request.budget_limit
    answer = interrupt({
        "type": "budget_confirm",
        "question": (
            f"当前预算约 ¥{budget.total}，超出上限 ¥{request.budget_limit}（超 ¥{over_amount}）。"
            f"是否将住宿从「{request.accommodation}」降级一档后重新生成？"
        ),
        "over_budget_amount": over_amount,
        "budget_limit": request.budget_limit,
        "current_accommodation": request.accommodation,
        "options": [
            {"value": "downgrade", "label": "降级住宿重新生成"},
            {"value": "keep", "label": "保留当前计划"},
        ],
    })

    if answer not in ("downgrade", "yes", "accept", True):
        plan.warnings.append("用户确认保留超出预算的计划。")
        # 必须把 plan 写回 state：Redis 模式下 _as_plan 是重建的新对象，
        # 只改本地副本不返回的话警告会随节点结束而丢失
        return {"plan": plan}

    downgraded = downgrade_accommodation(request)
    if downgraded is None:
        plan.warnings.append("已是最低住宿档次，无法降级，保留当前计划。")
        return {}

    planner.report_progress(
        callback, 90, "replanning", f"住宿已降级为「{downgraded.accommodation}」，正在重新生成行程"
    )
    context = state.get("context", {})
    # 降级重排同样携带攻略候选，用户必去地点不因预算降级丢失
    planner_query = _planner_query_with_candidates(planner, downgraded, context)
    planner_response = planner.run_agent_fresh(planner.planner_agent, planner_query)
    new_plan = planner.parse_response(planner_response, downgraded)
    if planner.needs_repair(new_plan, downgraded):
        new_plan = planner.repair_plan(downgraded, planner_response)
    new_plan.warnings.append(
        f"为满足预算上限 ¥{request.budget_limit}，住宿已降级为「{downgraded.accommodation}」并重新生成。"
    )
    new_plan.warnings.extend(apply_candidates_to_plan(new_plan, downgraded))
    new_plan.warnings.extend(state.get("warnings", []))
    return {
        "plan": new_plan,
        "request": downgraded,
        "planner_response": planner_response,
        "budget_downgrade_attempts": attempts + 1,
        "intercity_cost_per_person": context.get("intercity_cost_per_person", 0),
        "transportation_option": _as_transportation_option(context.get("transportation_option")),
    }


def _as_transportation_option(value: Any):
    """transportation_option 的 lc 包装还原守卫（RedisSaver resume 后是包装 dict，
    finalize 里 .departure_time 等属性访问会 AttributeError 导致整单降级模板）。"""
    from ..models.schemas import TransportationOption

    value = _unwrap_lc_deep(value)
    if isinstance(value, dict):
        return TransportationOption.model_validate(value)
    return value


def finalize_plan_node(state: PlanningState, config: RunnableConfig) -> dict:
    """质量校验、预算复核与路线优化（对应旧路径 97% 区段）。"""
    planner = _get_planner()
    callback = _callback_from_config(config)
    trip_plan = _as_plan(state["plan"])
    request = _as_request(state["request"])
    context = state.get("context", {})
    request = bind_researched_request(request, context)

    planner.report_progress(callback, 93, "optimizing", "正在核对游玩顺序与参考耗时、路线和预算")
    trip_plan = trip_quality_service.finalize(
        trip_plan,
        request,
        intercity_cost_per_person=state.get("intercity_cost_per_person", 0),
        transportation_option=_as_transportation_option(state.get("transportation_option")),
        resolve_routes=True,
        bind_meals=False,
        preserve_arrangement="tourism_candidates" in context,
    )
    if "tourism_candidates" in context:
        enforce_researched_plan(trip_plan, context["tourism_candidates"])
        from ..services.planning_supplement_service import supplement_candidates_once
        request = supplement_candidates_once(trip_plan, request)
        # 扩充的是系统候选池，最终校验必须使用同一池，不能把合法新增误判为越界。
        context = dict(context, tourism_candidates=[c.model_dump(mode="json") for c in request.candidate_places])
        from ..services.planning_repair_service import repair_planning_once
        from ..services.planning_feedback_service import build_planning_feedback
        review = build_planning_feedback(trip_plan, request)
        if review.conflicts or any(w.type == "underfilled_day" for w in review.soft_warnings):
            planner.report_progress(callback, 94, "local_repair", "正在检查排程冲突与偏空日期的备用候选")
        trip_plan, _, repaired = repair_planning_once(
            trip_plan, request, planner.planner_agent.llm,
            intercity_cost=state.get("intercity_cost_per_person", 0),
        )
        if repaired:
            trip_plan.planning_iterations = 2
        enforce_researched_plan(trip_plan, context["tourism_candidates"])
    # 总体建议：收尾时基于完整行程（含预算/天气/交通约束）重写为多条实用贴士；
    # 失败保留骨架阶段的两三句话原文，绝不影响主流程出结果
    planner.report_progress(callback, 96, "tips", "正在根据完整行程整理出行建议")
    try:
        trip_plan.overall_suggestions = trip_quality_service.refine_overall_suggestions(
            trip_plan, request
        )
    except Exception:
        pass
    if trip_plan.is_fallback:
        planner.report_progress(
            callback, 97, "degraded", "生成结果不完整，已切换为参考计划并标注原因"
        )
    else:
        planner.report_progress(callback, 97, "finalized", "行程与出行建议已整理完成")

    from ..services.planning_feedback_service import build_planning_feedback
    feedback = build_planning_feedback(trip_plan, request)
    trip_plan.warnings = list(dict.fromkeys([*trip_plan.warnings, *[w.message for w in feedback.soft_warnings]]))
    return {"result": trip_plan, "planning_feedback": feedback.model_dump(mode="json"), "context": context}


# ---------------------------------------------------------------------------
# 图构建
# ---------------------------------------------------------------------------

def build_planning_graph(checkpointer: Any = None):
    """编译规划图。带 checkpointer 时插入 budget_confirm interrupt 节点
    （interrupt 依赖 checkpointer 持久化断点，无 checkpointer 保持旧行为）。"""
    builder = StateGraph(PlanningState)
    builder.add_node("poi_research", poi_research_node)
    builder.add_node("weather_research", weather_research_node)
    builder.add_node("hotel_research", hotel_research_node)
    builder.add_node("intercity_research", intercity_research_node)
    builder.add_node("generate_plan", generate_plan_node)
    builder.add_node("finalize_plan", finalize_plan_node)

    # research 并行扇出：四个节点同一超步并发执行，reducer 合并各自 context key
    research_nodes = ("poi_research", "weather_research", "hotel_research", "intercity_research")
    if checkpointer is not None:
        # 带 Checkpointer：先多路线选择 interrupt，再生成，再预算确认 interrupt
        builder.add_node("route_confirm", route_confirm_node)
        for node in research_nodes:
            builder.add_edge(START, node)
            builder.add_edge(node, "route_confirm")
        builder.add_edge("route_confirm", "generate_plan")
        builder.add_node("budget_confirm", budget_confirm_node)
        builder.add_edge("generate_plan", "budget_confirm")
        builder.add_edge("budget_confirm", "finalize_plan")
    else:
        # 无 Checkpointer（内存模式）：interrupt 不可用，保持旧行为
        for node in research_nodes:
            builder.add_edge(START, node)
            builder.add_edge(node, "generate_plan")
        # TODO(P4 批2): validate/repair 拆成条件边循环；日编排管线拆解。
        builder.add_edge("generate_plan", "finalize_plan")
    builder.add_edge("finalize_plan", END)
    return builder.compile(checkpointer=checkpointer) if checkpointer else builder.compile()


def get_planning_graph():
    """图只编译一次，复用于所有任务；是否带 checkpointer 取决于 Redis 可用性。"""
    global _planning_graph
    if _planning_graph is None:
        _planning_graph = build_planning_graph(get_checkpointer())
    return _planning_graph


def reset_compiled_graph() -> None:
    """重置编译缓存（测试与 Checkpointer 可用性变化时使用）。"""
    global _planning_graph
    _planning_graph = None


# ---------------------------------------------------------------------------
# 同步执行入口（当前 Worker 为线程模型；P3 换 Async Worker 时改为 await）
# ---------------------------------------------------------------------------

def run_planning_graph(
    request: TripRequest,
    progress_callback: ProgressCallback = None,
    job_id: str = "",
    resume_value: Any = None,
    recover: bool = False,
    expected_question: Any = None,
) -> TripPlan:
    """同步执行规划图；旧模式异常可返回参考计划，联网核验失败明确停止。

    resume_value 不为 None 时，恢复 job_id 对应断点继续执行（interrupt 之后）。
    图触发 interrupt 时抛出 PlanningWaitingInput（由 Worker 落 waiting_input 状态）。
    """
    if progress_callback:
        progress_callback(3, "initializing", "正在准备地图工具与规划服务")
    planner = _get_planner()
    planner.report_progress(progress_callback, 5, "initializing", "正在理解旅行需求与约束")
    degraded_warnings: list[str] = []
    try:
        config = {
            "configurable": {
                "progress_callback": progress_callback,
                "thread_id": job_id or "adhoc",
            }
        }
        graph=get_planning_graph()
        checkpoint=graph.get_state(config) if recover and job_id and graph.checkpointer else None
        if checkpoint and checkpoint.values and not checkpoint.next and checkpoint.values.get('result'):
            return _as_plan(checkpoint.values['result'])
        interrupted=bool(checkpoint and any(getattr(task,'interrupts',()) for task in checkpoint.tasks))
        if interrupted and resume_value is not None and expected_question:
            questions=[getattr(interrupt,'value',None) for task in checkpoint.tasks for interrupt in getattr(task,'interrupts',())]
            if expected_question not in questions:
                resume_value=None  # 旧选项已处理，当前流程已经进入另一轮确认。
        if checkpoint and checkpoint.next and not interrupted:
            from ..infrastructure.task_trace import emit
            emit('runtime','checkpoint','resumed',reason='pending_nodes')
            payload=None
        elif resume_value is not None and job_id:
            payload: Any = Command(resume=resume_value)
        elif checkpoint and checkpoint.next:
            payload=None  # 恢复已持久化的中断状态，不重新执行资料查询。
        else:
            payload = PlanningState(
                mode="plan",
                job_id=job_id,
                request=request,
                warnings=[],
            )
        # 同步 invoke（勿改回 ainvoke）：langgraph-checkpoint-redis 0.2.1 的
        # RedisSaver 只实现了同步方法，aget_tuple 走基类默认直接抛
        # NotImplementedError（Windows 复现过：Redis 模式下所有规划任务失败）。
        # 本图节点全部为同步函数，sync invoke + sync checkpointer 是该版本唯一可用组合。
        final_state = graph.invoke(payload, config=config)
        interrupts = final_state.get("__interrupt__") if isinstance(final_state, dict) else None
        if interrupts:
            first = interrupts[0]
            question = getattr(first, "value", first)
            raise PlanningWaitingInput(question)
        result = final_state.get("result")
        if isinstance(result, TripPlan):
            return result
        raise RuntimeError("规划图未产出结果")
    except PlanningWaitingInput:
        raise
    except TourismResearchError as exc:
        # 核验失败属于明确业务失败，不能变成带虚构景点的“成功参考计划”。
        planner.report_progress(progress_callback, 97, "tourism_rejected", str(exc))
        raise
    except Exception as exc:
        from ..services.baidu_search_service import BaiduSearchService
        if not request.candidate_places and BaiduSearchService().configured:
            # 收尾等其他节点异常也不能把已启用的核验模式降级成未验证参考计划。
            raise TourismResearchError("联网行程未能完成校验，请稍后重试或提供明确攻略") from None
        print(f"❌ 生成旅行计划失败: {str(exc)}")
        import traceback
        traceback.print_exc()
        fallback = planner.create_fallback_plan(
            request,
            reason=f"规划执行失败：{planner.friendly_error(exc)}",
        )
        fallback.warnings.extend(degraded_warnings)
        planner.report_progress(
            progress_callback, 97, "degraded", "规划执行失败，已生成可用的参考计划"
        )
        return trip_quality_service.finalize(fallback, request)
