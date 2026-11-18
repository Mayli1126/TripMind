"""RevisionGraph：自然语言增量修改（产品化计划 6.3/阶段1批1）。

流程：
    parse_patch   LLM 把指令转译为 PlanPatch（只翻译，不改计划）
    apply_patch   确定性代码应用 Patch（patch_service）
    validate      结构校验（PlanIssue）；有 error 时保留原版本

明确排序优先确定性解析；格式问题最多修复一次。
失败、歧义或超范围时保留原计划，绝不自动扩大为整份重生成。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Callable, Optional

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from ..agents.trip_planner_agent import MultiAgentTripPlanner
from ..infrastructure.llm_client import LLMClient
from ..models.patch import ManualRevisionRequest, PlanPatch
from ..models.schemas import RevisionChange, TripPlan, TripRequest
from ..services.patch_service import apply_plan_patch, ensure_stable_ids
from ..services.trip_quality_service import trip_quality_service
from ..services.revision_outcome import has_plan_changes, reject_revision, mark_revision_ready
from ..services.revision_intent import fast_order_patch, ClarificationNeeded, order_postconditions, dependent_days
from .state import PlanningState

ProgressCallback = Optional[Callable[..., None]]

_revision_graph = None

_PARSE_PROMPT = """你是旅行计划的修改指令解析器。把用户的修改要求转译为一个 JSON 对象，不要修改计划本身。

输出的 JSON 顶层直接包含 base_version、operations、affected_day_indexes、explanation 四个字段；
不要用 "PlanPatch" 之类的键名再包一层。

用户修改要求：{instruction}

本次真实基础版本：{base_version}。day_index 从0开始：第1天是0，第2天是1。

当前计划骨架（stable_id 是每个景点的唯一标识）：
{plan_skeleton}

只返回 JSON，结构如下：
{{
  "base_version": {base_version},
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
- 无法唯一定位地点或指令有歧义时，返回 {{"clarify_needed": true, "reason": "需要用户补充的问题"}}，不要猜测；
- 只修改与要求相关的天，其他天的 stable_id 全部列入 preserved_stable_ids；
- "第二天少走路"这类要求用 move_place/reorder_day/change_pace 表达，不要删掉用户可能想去的景点；
- 目的地变更、天数变更等超出局部修改范围时，返回 {{"out_of_scope": true}}；
- **op 只能取以下枚举值**：add_place、remove_place、replace_place、move_place、reorder_day、change_duration、change_start_time、change_pace、update_transport；
- **禁止 JSON-Patch 格式**：不要输出 path/value 字段，不要用 add/remove/replace 这类裸 op；
- 输出的顶层必须包含 base_version、operations、affected_day_indexes、explanation 四个字段。
"""


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
                max_tokens=2200,
            )
        else:
            from ..services.llm_service import get_llm

            _patch_llm = get_llm()
    return _patch_llm


def _callback_from_config(config: RunnableConfig) -> ProgressCallback:
    if not config:
        return None
    return (config.get("configurable") or {}).get("progress_callback")


def _report(callback, *args):
    if callback:
        callback(*args)


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
    base_version 缺省补 1，解析节点随后绑定服务端快照版本。
    """
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError("LLM 输出中未找到 JSON")
    data = json.loads(match.group(0))
    if data.get("clarify_needed"):
        raise ClarificationNeeded(str(data.get("reason") or "请补充要修改的景点和目标位置。"))
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
    callback = _callback_from_config(config)
    plan: TripPlan = state["plan"]
    instruction: str = state.get("revision_instruction", "")

    _report(callback, 15, "understanding", "正在理解修改要求")
    version = state.get("base_version") or 1
    try:
        fast = fast_order_patch(plan, instruction, version)
    except ClarificationNeeded as exc:
        return {"patch_error": f"clarify: {exc}"}
    if fast is not None:
        _report(callback, 40, "patch_parsed", "已直接识别景点位置调整，无需模型生成")
        return {"plan_patch": fast, "patch_error": None}
    prompt = _PARSE_PROMPT.format(instruction=instruction, plan_skeleton=_plan_skeleton(plan), base_version=version)
    messages = [
        {"role": "system", "content": "你是严谨的旅行计划修改解析器，只输出一个不含包裹键名的 JSON 对象。"},
        {"role": "user", "content": prompt},
    ]
    for attempt in range(2):
        try:
            raw = _get_patch_llm().invoke(messages, max_tokens=2200)
        except Exception as exc:
            return {"patch_error": "service_failed: " + MultiAgentTripPlanner.friendly_error(exc)}
        try:
            patch = _parse_patch_response(raw).model_copy(update={"base_version": version})
            break
        except ClarificationNeeded as exc:
            return {"patch_error": f"clarify: {exc}"}
        except RevisionOutOfScope:
            return {"patch_error": "out_of_scope: 该要求超出局部修改范围，请在首页明确发起重新规划，当前行程保持不变。"}
        except (ValueError, TypeError, AttributeError) as exc:
            if attempt:
                return {"patch_error": "parse_failed: 未能可靠解析本次修改，请明确景点名称、日期和目标位置后重试，原计划未改变。"}
            messages.extend([
                {"role": "assistant", "content": raw[:6000]},
                {"role": "user", "content": "仅修复上述JSON格式和字段，不扩大修改范围。错误：" + str(exc)[:500]},
            ])

    _report(
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
    "update_description": "修改景点介绍",
    "change_start_time": "调整出发时间",
    "change_pace": "调整当日节奏",
    "update_transport": "更新交通方式",
}


def _build_revision_diff(patch: PlanPatch, plan: TripPlan, before: TripPlan | None = None) -> list[RevisionChange]:
    # change_duration/remove 等操作只带 target_stable_id，从计划反查所在日
    id_to_day: dict[str, int] = {}
    for day in plan.days:
        for attraction in day.attractions:
            if attraction.stable_id:
                id_to_day[attraction.stable_id] = day.day_index
    old_by_id = {a.stable_id: (day.day_index, a) for day in (before.days if before else []) for a in day.attractions}
    new_by_id = {a.stable_id: a for day in plan.days for a in day.attractions}

    changes: list[RevisionChange] = []
    for op in patch.operations:
        day_index = op.day_index
        if day_index is None and op.target_stable_id:
            day_index = id_to_day.get(op.target_stable_id)
            if day_index is None and op.target_stable_id in old_by_id:
                day_index = old_by_id[op.target_stable_id][0]
        day_label = f"第{day_index + 1}天" if day_index is not None else "全程"
        detail = op.reason or ""
        # 差异预览使用实际结果补充名称、顺序与分钟数，不能只展示模型的一句话。
        old_entry = old_by_id.get(op.target_stable_id)
        attraction = new_by_id.get(op.target_stable_id)
        if op.op == "reorder_day" and day_index is not None:
            detail = " → ".join(a.name for a in plan.days[day_index].attractions)
        elif op.op == "remove_place" and old_entry:
            detail = f"删除「{old_entry[1].name}」"
        elif op.op == "move_place" and attraction:
            detail = f"「{attraction.name}」移至第{day_index + 1}天第{plan.days[day_index].attractions.index(attraction) + 1}站"
        elif op.op == "change_duration" and attraction:
            previous = f"{old_entry[1].visit_duration} → " if old_entry else ""
            detail = f"「{attraction.name}」：{previous}{attraction.visit_duration} 分钟"
        elif op.op == "update_description" and attraction:
            detail = f"「{attraction.name}」介绍：{attraction.description or '（清空）'}"
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
    callback = _callback_from_config(config)
    plan: TripPlan = state["plan"]
    patch: PlanPatch = state["plan_patch"]
    # 请求携带的攻略候选：add/replace 可按 replacement_candidate_id 直取已校准地点
    candidates = getattr(state.get("request"), "candidate_places", None)

    _report(callback, 65, "patching", "正在应用修改")
    new_plan, issues = apply_plan_patch(
        plan, patch, candidates, current_version=state.get("base_version")
    )
    errors = [i for i in issues if i.severity == "error"]
    post_errors = order_postconditions(plan, new_plan, patch) if not errors else []
    if errors or post_errors:
        preserved = reject_revision(plan, "本次修改未通过校验，已保留原计划：" + "；".join([i.message for i in errors] + post_errors))
        _report(callback, 90, "rejected", "修改被校验拒绝，已保留原计划")
        # 只写 result 不写 patched_plan：路由边据此跳过 finalize_revision
        return {"result": preserved}
    if not has_plan_changes(plan, new_plan):
        return {"result": reject_revision(plan, "本次指令未改变行程，请明确需要调整的地点和顺序。", unchanged=True)}
    mark_revision_ready(new_plan)
    new_plan.revision_diff = _build_revision_diff(patch, new_plan, plan)
    return {"patched_plan": new_plan, "patch_issues": [i.model_dump() for i in issues]}


def finalize_revision_node(state: PlanningState, config: RunnableConfig) -> dict:
    """重算预算并落定结果（不做全盘路线重排，保住未受影响日）。

    Patch 改变了当日景点集合（增删/移动/时长），必须重建受影响日的游玩顺序与参考耗时，
    否则 schedule 里残留已删除景点、顺序与时长不更新（游玩顺序与参考耗时与列表不一致）。
    """
    callback = _callback_from_config(config)
    request: TripRequest = state["request"]
    plan: TripPlan = state["patched_plan"]
    patch: PlanPatch = state["plan_patch"]

    # 保存收尾前的明确结果，增删、移动、时长混合修改也必须保持操作语义。
    def requested_content(value):
        return [[(a.stable_id, a.name, a.visit_duration, a.description) for a in day.attractions] for day in value.days]
    expected_content = requested_content(plan)

    intercity_cost = 0
    if plan.budget and request.travelers:
        intercity_cost = round(plan.budget.intercity_transportation / request.travelers
                              / (2 if request.trip_type == "round_trip" else 1))
    plan.warnings.append(f"已根据修改要求更新：{state.get('revision_instruction', '')}")
    # 这里只记录规划修复次数，不是行程保存版本号。
    plan.planning_iterations = min(2, (plan.planning_iterations or 0) + 1)
    # 重建游玩顺序与参考耗时：确定性的按新景点集合重排（受保护日的景点集合未变，仅游玩顺序与参考耗时重算）
    changed_days = set(patch.affected_day_indexes)
    original = state.get("plan")
    if isinstance(original, TripPlan):
        # 跨日移动必须同时重算来源日，即使模型漏报该日期也要补齐。
        for index, day in enumerate(plan.days):
            if index >= len(original.days) or day.attractions != original.days[index].attractions:
                changed_days.add(index)
        changed_days = dependent_days(original, plan, changed_days)
    plan = trip_quality_service.finalize_revision(
        plan, request, changed_days=changed_days, intercity_cost_per_person=intercity_cost,
    )

    post_errors = order_postconditions(original, plan, patch)
    if requested_content(plan) != expected_content:
        post_errors.append("游玩顺序与参考耗时收尾改变了指定的景点、顺序、分钟数或介绍")
    # 两种编辑入口都必须保留用户指定的分钟数和介绍，不能被收尾流程覆盖。
    by_id = {a.stable_id: a for day in plan.days for a in day.attractions}
    for op in patch.operations:
        attraction = by_id.get(op.target_stable_id)
        if op.op == "change_duration" and (not attraction or attraction.visit_duration != op.duration_minutes):
            post_errors.append("游览时长未按要求生效")
        if op.op == "update_description" and (not attraction or attraction.description != op.description):
            post_errors.append("景点介绍未按要求生效")
    if post_errors:
        return {"result": reject_revision(original, "修改后校验失败，原计划未改变：" + "；".join(post_errors))}
    _report(callback, 95, "optimizing", "预算与游玩顺序与参考耗时已重新计算")
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
        # 失败或超范围只反馈结果，不自动扩大用户授权的修改范围。
        return "apply" if state.get("plan_patch") is not None else "abort"

    def route_after_apply(state: PlanningState) -> str:
        # 校验被拒：result 已是保留的原计划，跳过 finalize（无 patched_plan 可取）
        return "finalize" if state.get("patched_plan") is not None else "end"

    def abort_revision(state, config):
        error = state.get("patch_error") or "修改未完成，原计划保持不变。"
        message = error.split(": ", 1)[-1]
        result = reject_revision(state["plan"], message)
        if error.startswith(("clarify:", "out_of_scope:")):
            result.revision_status = "clarify_needed"
        return {"result": result}
    builder.add_node("abort_revision", abort_revision)
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


def run_manual_revision(request: TripRequest, current_plan: TripPlan, payload: ManualRevisionRequest) -> TripPlan:
    """手动操作跳过模型解析，复用自然语言修改的应用、校验和增量收尾节点。"""
    prepared = current_plan.model_copy(deep=True)
    ensure_stable_ids(prepared)
    affected = set()
    by_id = {a.stable_id: index for index, day in enumerate(prepared.days) for a in day.attractions}
    for op in payload.operations:
        if op.day_index is not None:
            affected.add(op.day_index)
        if op.target_stable_id in by_id:
            affected.add(by_id[op.target_stable_id])
    if not affected:
        return reject_revision(prepared, "未找到需要修改的日期或景点，请刷新后重试。")
    patch = PlanPatch(base_version=payload.base_version, operations=payload.operations,
                      affected_day_indexes=sorted(affected), explanation="手动调整行程")
    state = dict(plan=prepared, request=request, plan_patch=patch,
                 base_version=payload.base_version, revision_instruction="手动调整行程")
    try:
        state.update(apply_patch_node(state, {}))
        if "patched_plan" not in state:
            return state["result"]
        return finalize_revision_node(state, {})["result"]
    except Exception:
        return reject_revision(prepared, "暂时无法完成校验，原行程未改变，请稍后重试。")


def run_revision_graph(
    request: TripRequest,
    current_plan: TripPlan,
    instruction: str,
    progress_callback: ProgressCallback = None,
    base_version: Optional[int] = None,
) -> TripPlan:
    """同步执行修订图；失败保留原计划，不自动整份重生成。

    base_version：行程库当前版本号（乐观锁），None 表示不校验版本。
    """
    try:
        prepared_plan = current_plan.model_copy(deep=True)
        ensure_stable_ids(prepared_plan)
        state = PlanningState(
            mode="revision",
            request=request,
            plan=prepared_plan,
            base_version=base_version,
            revision_instruction=instruction,
            warnings=[],
        )
        config = {"configurable": {"progress_callback": progress_callback}}
        final_state = asyncio.run(get_revision_graph().ainvoke(state, config=config))
        result = final_state.get("result")
        if isinstance(result, TripPlan):
            return result
        return reject_revision(current_plan, "本次修改没有产生可靠结果，原计划保持不变。")
    except Exception as exc:
        return reject_revision(current_plan, f"修改失败，已保留原计划：{MultiAgentTripPlanner.friendly_error(exc)}")
