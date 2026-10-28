"""RevisionGraph：自然语言增量修改（产品化计划 6.3/阶段1批1）。

流程：
    parse_patch   LLM 把指令转译为 PlanPatch（只翻译，不改计划）
    apply_patch   确定性代码应用 Patch（patch_service）
    validate      结构校验（PlanIssue）；有 error 时保留原版本

降级策略：
- LLM 解析失败 / 图执行异常 → 回退旧 revise_trip（整份重生成），
  保证"目的地改成青岛"这类超范围修改仍可用，E2E 不回退。
- Patch 校验失败 → 保留原版本并附加原因（不产生半成品版本）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Callable, Optional

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from ..agents.trip_planner_agent import MultiAgentTripPlanner, get_trip_planner_agent
from ..infrastructure.llm_client import LLMClient
from ..models.patch import PlanPatch
from ..models.schemas import RevisionChange, TripPlan, TripRequest
from ..services.itinerary_schedule_service import itinerary_schedule_service
from ..services.patch_service import apply_plan_patch, ensure_stable_ids
from ..services.trip_quality_service import trip_quality_service
from .state import PlanningState

ProgressCallback = Optional[Callable[..., None]]

_revision_graph = None

_PARSE_PROMPT = """你是旅行计划的修改指令解析器。把用户的修改要求转译为一个 JSON 对象，不要修改计划本身。

输出的 JSON 顶层直接包含 base_version、operations、affected_day_indexes、explanation 四个字段；
不要用 "PlanPatch" 之类的键名再包一层。

用户修改要求：{instruction}

当前计划骨架（stable_id 是每个景点的唯一标识）：
{plan_skeleton}

只返回 JSON，结构如下：
{{
  "base_version": 1,
  "operations": [
    {{"op": "remove_place", "target_stable_id": "d1-a1-xxxx", "reason": "..."}}
  ],
  "affected_day_indexes": [1],
  "required_research": [],
  "preserved_stable_ids": ["不应被触碰的stable_id"],
  "explanation": "面向用户的修改摘要"
}}

可选 op 及必填字段：
- add_place: day_index, position(可选), replacement_name
- remove_place: target_stable_id
- replace_place: target_stable_id, replacement_name
- move_place: target_stable_id, day_index, position(可选)
- reorder_day: day_index, ordered_stable_ids(当日完整顺序)
- change_duration: target_stable_id, duration_minutes
- change_start_time: day_index, start_time(HH:MM)
- change_pace: day_index, pace(relaxed/standard/intense)
- update_transport: day_index(可选，缺省为全部天), transport

规则：
- 只修改与要求相关的天，其他天的 stable_id 全部列入 preserved_stable_ids；
- "第二天少走路"这类要求用 move_place/reorder_day/change_pace 表达，不要删掉用户可能想去的景点；
- 目的地变更、天数变更等超出局部修改范围时，返回 {{"out_of_scope": true}}；
- **op 只能取以下枚举值**：add_place、remove_place、replace_place、move_place、reorder_day、change_duration、change_start_time、change_pace、update_transport；
- **禁止 JSON-Patch 格式**：不要输出 path/value 字段，不要用 add/remove/replace 这类裸 op；
- 输出的顶层必须包含 base_version、operations、affected_day_indexes、explanation 四个字段。
"""


def _get_planner() -> MultiAgentTripPlanner:
    return get_trip_planner_agent()


# Patch 解析专用客户端：指令 -> PlanPatch 是轻量结构化翻译，优先用 LLM_PATCH_MODEL
# （flash 级小模型，比 95B 主模型快数倍）；未配置时回退主模型。进程级缓存。
_patch_llm: Optional[LLMClient] = None


def _get_patch_llm() -> LLMClient:
    global _patch_llm
    if _patch_llm is None:
        model = (os.getenv("LLM_PATCH_MODEL") or "").strip()
        if model:
            _patch_llm = LLMClient(
                model=model,
                temperature=1.0,
                max_tokens=int(os.getenv("LLM_MAX_TOKENS", "8192")),
            )
        else:
            from ..services.llm_service import get_llm

            _patch_llm = get_llm()
    return _patch_llm


def _callback_from_config(config: RunnableConfig) -> ProgressCallback:
    if not config:
        return None
    return (config.get("configurable") or {}).get("progress_callback")


class RevisionOutOfScope(Exception):
    """指令超出局部修改范围（如换城市），需要走完整重规划。"""


def _plan_skeleton(plan: TripPlan) -> str:
    """提取计划骨架供 LLM 定位 stable_id，避免塞入完整 JSON。"""
    lines: list[str] = []
    for day in plan.days:
        lines.append(f"第{day.day_index + 1}天（{day.date}）：{day.description}")
        for attraction in day.attractions:
            lines.append(f"  - [{attraction.stable_id}] {attraction.name}（游览{attraction.visit_duration}分钟）")
    return "\n".join(lines)


def _parse_patch_response(raw: str) -> PlanPatch:
    """从 LLM 输出中提取 PlanPatch JSON（容忍 ```json 包裹与前后杂文本）。

    小模型的键名遵循度不稳定：兼容 {"planPatch": [...]} 等变体，
    base_version 缺省补 1，避免因格式差异落入整份重生成的慢路径。
    """
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError("LLM 输出中未找到 JSON")
    data = json.loads(match.group(0))
    if data.get("out_of_scope"):
        raise RevisionOutOfScope(str(data.get("reason", "修改超出局部调整范围")))
    operations = data.get("operations") or data.get("planPatch") or data.get("patch")
    if isinstance(operations, list):
        data["operations"] = operations
    data.setdefault("base_version", 1)
    if not isinstance(data.get("operations"), list):
        raise ValueError("PlanPatch JSON 缺少 operations 列表")
    # 小模型常省略这些字段：affected_day_indexes 从操作里反推，explanation 兜底
    if not data.get("affected_day_indexes"):
        days = set()
        for op in data["operations"]:
            if not isinstance(op, dict):
                continue
            if isinstance(op.get("day_index"), int):
                days.add(op["day_index"])
            # remove/move/change_duration 只带 target_stable_id：从骨架反查所在日
            sid = op.get("target_stable_id")
            if sid:
                match = re.search(r"^d(\d+)-", str(sid))
                if match:
                    days.add(int(match.group(1)))
        data["affected_day_indexes"] = sorted(days)
    if not data.get("explanation"):
        data["explanation"] = "已按你的要求生成修改方案"
    return PlanPatch.model_validate(data)


# ---------------------------------------------------------------------------
# 节点
# ---------------------------------------------------------------------------

def parse_patch_node(state: PlanningState, config: RunnableConfig) -> dict:
    """LLM 转译：修改指令 -> PlanPatch。失败通过 patch_error 传递给降级边。"""
    planner = _get_planner()
    callback = _callback_from_config(config)
    plan: TripPlan = state["plan"]
    instruction: str = state.get("revision_instruction", "")

    planner._report_progress(callback, 15, "understanding", "正在理解修改要求")
    prompt = _PARSE_PROMPT.format(instruction=instruction, plan_skeleton=_plan_skeleton(plan))
    try:
        # Patch 解析是纯文本翻译（无工具调用），直接走快速模型客户端
        raw = _get_patch_llm().invoke([
            {"role": "system", "content": "你是严谨的旅行计划修改解析器，只输出一个不含包裹键名的 JSON 对象。"},
            {"role": "user", "content": prompt},
        ])
        patch = _parse_patch_response(raw)
    except RevisionOutOfScope as exc:
        return {"patch_error": f"out_of_scope: {exc}"}
    except Exception as exc:
        return {"patch_error": f"parse_failed: {exc}"}

    planner._report_progress(
        callback, 40, "patch_parsed", f"修改指令已解析：{patch.explanation}"
    )
    return {"plan_patch": patch, "patch_error": None}


_OP_LABELS = {
    "add_place": "新增景点",
    "remove_place": "移除景点",
    "replace_place": "替换景点",
    "move_place": "移动景点",
    "reorder_day": "调整游览顺序",
    "change_duration": "调整游览时长",
    "change_start_time": "调整出发时间",
    "change_pace": "调整当日节奏",
    "update_transport": "更新交通方式",
}


def _build_revision_diff(patch: PlanPatch, plan: TripPlan) -> list[RevisionChange]:
    # change_duration/remove 等操作只带 target_stable_id，从计划反查所在日
    id_to_day: dict[str, int] = {}
    for day in plan.days:
        for attraction in day.attractions:
            if attraction.stable_id:
                id_to_day[attraction.stable_id] = day.day_index

    changes: list[RevisionChange] = []
    for op in patch.operations:
        day_index = op.day_index
        if day_index is None and op.target_stable_id:
            day_index = id_to_day.get(op.target_stable_id)
        day_label = f"第{day_index + 1}天" if day_index is not None else "全程"
        detail = op.reason or ""
        if op.replacement_name:
            detail = f"{op.replacement_name}（{detail}）" if detail else op.replacement_name
        changes.append(RevisionChange(
            op=op.op,
            day_index=day_index,
            title=f"{day_label} · {_OP_LABELS.get(op.op, op.op)}",
            detail=detail,
        ))
    return changes


def apply_patch_node(state: PlanningState, config: RunnableConfig) -> dict:
    """确定性应用 Patch；校验失败时保留原版本并带拒绝原因（patch_rejected 标记，
    路由边据此跳过 finalize_revision——直接进 finalize 会 KeyError）。"""
    planner = _get_planner()
    callback = _callback_from_config(config)
    plan: TripPlan = state["plan"]
    patch: PlanPatch = state["plan_patch"]
    # 请求携带的攻略候选：add/replace 可按 replacement_candidate_id 直取已校准地点
    candidates = getattr(state.get("request"), "candidate_places", None)

    planner._report_progress(callback, 65, "patching", "正在应用修改")
    new_plan, issues = apply_plan_patch(
        plan, patch, candidates, current_version=state.get("base_version")
    )
    errors = [i for i in issues if i.severity == "error"]
    if errors:
        preserved = plan.model_copy(deep=True)
        preserved.revision_diff = []  # 计划未变，清掉可能的旧差异
        preserved.warnings.append(
            "本次修改未通过校验，已保留原计划：" + "；".join(i.message for i in errors)
        )
        planner._report_progress(callback, 90, "rejected", "修改被校验拒绝，已保留原计划")
        # 只写 result 不写 patched_plan：路由边据此跳过 finalize_revision
        return {"result": preserved}
    new_plan.revision_diff = _build_revision_diff(patch, new_plan)
    return {"patched_plan": new_plan, "patch_issues": [i.model_dump() for i in issues]}


def finalize_revision_node(state: PlanningState, config: RunnableConfig) -> dict:
    """重算预算并落定结果（不做全盘路线重排，保住未受影响日）。

    Patch 改变了当日景点集合（增删/移动/时长），必须重建受影响日的时间轴，
    否则 schedule 里残留已删除景点、顺序与时长不更新（时间轴与列表不一致）。
    """
    planner = _get_planner()
    callback = _callback_from_config(config)
    request: TripRequest = state["request"]
    plan: TripPlan = state["patched_plan"]
    patch: PlanPatch = state["plan_patch"]

    intercity_cost = 0
    if plan.budget and request.travelers:
        intercity_cost = round(plan.budget.intercity_transportation / request.travelers)
    plan.budget = trip_quality_service.calculate_budget(
        plan, request, intercity_cost_per_person=intercity_cost
    )
    plan.warnings.append(f"已根据修改要求更新：{state.get('revision_instruction', '')}")
    plan.planning_iterations = (plan.planning_iterations or 0) + 1
    # 重建时间轴：确定性的按新景点集合重排（受保护日的景点集合未变，仅时间轴重算）
    itinerary_schedule_service.build(plan)

    planner._report_progress(callback, 95, "optimizing", "预算与时间轴已重新计算")
    return {"result": plan}


# ---------------------------------------------------------------------------
# 图构建与执行
# ---------------------------------------------------------------------------

def build_revision_graph():
    builder = StateGraph(PlanningState)
    builder.add_node("parse_patch", parse_patch_node)
    builder.add_node("apply_patch", apply_patch_node)
    builder.add_node("finalize_revision", finalize_revision_node)

    def route_after_parse(state: PlanningState) -> str:
        # LLM 解析失败/超范围 → 交由执行入口回退 legacy revise_trip
        return "apply" if state.get("plan_patch") is not None else "abort"

    def route_after_apply(state: PlanningState) -> str:
        # 校验被拒：result 已是保留的原计划，跳过 finalize（无 patched_plan 可取）
        return "finalize" if state.get("patched_plan") is not None else "end"

    builder.add_node("abort_revision", lambda state, config: {"result": None})
    builder.add_edge(START, "parse_patch")
    builder.add_conditional_edges("parse_patch", route_after_parse, {"apply": "apply_patch", "abort": "abort_revision"})
    builder.add_conditional_edges("apply_patch", route_after_apply, {"finalize": "finalize_revision", "end": END})
    builder.add_edge("finalize_revision", END)
    builder.add_edge("abort_revision", END)
    return builder.compile()


def get_revision_graph():
    global _revision_graph
    if _revision_graph is None:
        _revision_graph = build_revision_graph()
    return _revision_graph


def run_revision_graph(
    request: TripRequest,
    current_plan: TripPlan,
    instruction: str,
    progress_callback: ProgressCallback = None,
    base_version: Optional[int] = None,
) -> TripPlan:
    """同步执行修订图；解析失败或异常时回退 legacy revise_trip。

    base_version：行程库当前版本号（乐观锁），None 表示不校验版本。
    """
    planner = _get_planner()
    try:
        state = PlanningState(
            mode="revision",
            request=request,
            plan=current_plan,
            base_version=base_version,
            revision_instruction=instruction,
            warnings=[],
        )
        config = {"configurable": {"progress_callback": progress_callback}}
        final_state = asyncio.run(get_revision_graph().ainvoke(state, config=config))
        result = final_state.get("result")
        if isinstance(result, TripPlan):
            return result
        # 解析失败（abort 路径）→ 回退 legacy 整份重生成
        planner._report_progress(
            progress_callback, 30, "revising", "修改范围较大，正在重新编排相关行程"
        )
        result = planner.revise_trip(request, current_plan, instruction, progress_callback)
        result.revision_diff = [RevisionChange(
            op="full_regen",
            day_index=None,
            title="整体重新生成",
            detail="修改范围较大，AI 已重新编排相关行程",
        )]
        return result
    except Exception as exc:
        preserved = current_plan.model_copy(deep=True)
        preserved.revision_diff = []
        preserved.warnings.append(f"修改失败，已保留原计划：{planner._friendly_error(exc)}")
        return preserved
