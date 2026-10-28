"""PlanPatch 确定性应用服务（产品化计划 6.3 节）。

原则：
- LLM 只负责把自然语言转译成 PlanPatch，绝不直接改计划；
- 本模块全部为确定性代码：定位、增删换移、POI 校准、稳定 ID 保护；
- 校验失败返回 issues，调用方保留原版本，不产生半成品版本；
- 无坐标的地点不得进入计划（add/replace 必须经高德 POI 校准，
  或直取候选池中已校准的 CandidatePlace）。
"""

from __future__ import annotations

import hashlib
from typing import Iterable, Optional, Tuple

from ..models.candidate import CandidatePlace
from ..models.patch import PatchOperation, PlanPatch
from ..models.plan_issue import PlanIssue
from ..models.schemas import Attraction, POIInfo, TripPlan

# 候选类别（英文枚举）到计划景点类别（中文）的显示映射
_CATEGORY_DISPLAY = {
    "attraction": "景点",
    "museum": "博物馆",
    "street": "商业街",
    "natural": "自然风光",
    "food": "餐饮",
    "night": "夜市",
    "leisure": "休闲娱乐",
    "transport_hub": "交通枢纽",
    "hotel": "酒店",
}


def _stable_id(day_index: int, name: str, seq: int) -> str:
    digest = hashlib.md5(f"{day_index}|{name}".encode("utf-8")).hexdigest()[:8]
    return f"d{day_index}-a{seq}-{digest}"


def ensure_stable_ids(plan: TripPlan) -> None:
    """为缺失 stable_id 的景点补全确定性 ID（幂等，原地修改）。

    已有 stable_id 的景点保持不变——这是"修改第二天不动第一天"的基础。
    """
    for day in plan.days:
        seq = 0
        for attraction in day.attractions:
            if not attraction.stable_id:
                seq += 1
                attraction.stable_id = _stable_id(day.day_index, attraction.name, seq)


def _find_attraction(plan: TripPlan, stable_id: str):
    for day in plan.days:
        for attraction in day.attractions:
            if attraction.stable_id == stable_id:
                return day, attraction
    return None, None


def _poi_to_attraction(poi: POIInfo, fallback_duration: int = 90) -> Attraction:
    return Attraction(
        name=poi.name,
        address=poi.address or "",
        location=poi.location,
        visit_duration=fallback_duration,
        description=f"来自高德 POI 校准（{poi.type or '景点'}）",
        category=poi.type or "景点",
        poi_id=poi.id,
    )


def _search_poi(name: str, city: str) -> Optional[POIInfo]:
    """高德 POI 校准：名称→真实坐标。查不到返回 None，绝不伪造坐标。"""
    try:
        from .amap_service import AmapService

        results = AmapService().search_poi(name, city)
        if not results:
            return None
        # 名称完全一致优先，其次取第一个结果
        for poi in results:
            if poi.name == name:
                return poi
        return results[0]
    except Exception:
        return None


def _candidate_to_attraction(candidate: CandidatePlace) -> Attraction:
    """已校准候选直接构造景点：坐标/poi_id 可信，推荐理由进描述，省一次高德查询。"""
    return Attraction(
        name=candidate.canonical_name,
        address="",
        location=candidate.location,  # is_usable_for_routing 保证非 None
        visit_duration=candidate.suggested_duration_minutes,
        description="；".join(candidate.recommendation_reasons[:2]) or "来自攻略候选地点",
        category=_CATEGORY_DISPLAY.get(candidate.category, "景点"),
        poi_id=candidate.poi_id,
    )


def _resolve_new_attraction(
    op: PatchOperation,
    plan: TripPlan,
    candidates: dict[str, CandidatePlace],
    searched_pois: dict[str, Optional[POIInfo]],
) -> Optional[Attraction]:
    """解析 add/replace 的新地点：候选直取优先，否则高德检索；都失败返回 None。"""
    # 阶段 2：指令里带候选 ID 时直取已校准候选（免高德查询，坐标以地图核对为准）
    if op.replacement_candidate_id and op.replacement_candidate_id in candidates:
        candidate = candidates[op.replacement_candidate_id]
        if candidate.is_usable_for_routing():
            return _candidate_to_attraction(candidate)
    name = op.replacement_name
    if not name:
        return None
    if name not in searched_pois:
        searched_pois[name] = _search_poi(name, plan.city)
    poi = searched_pois[name]
    if poi is None:
        return None
    return _poi_to_attraction(poi)


def _apply_single(
    plan: TripPlan,
    op: PatchOperation,
    issues: list[PlanIssue],
    searched_pois: dict[str, Optional[POIInfo]],
    candidates: Optional[dict[str, CandidatePlace]] = None,
) -> bool:
    """应用单个操作。成功返回 True，失败写入 issue 并返回 False。

    candidates：请求携带的攻略候选（candidate_id -> CandidatePlace），
    add/replace 可直取已校准候选，免高德查询。
    """

    def fail(code: str, message: str, **kwargs) -> bool:
        issues.append(PlanIssue(code=code, severity="error", message=message, **kwargs))
        return False

    if op.op == "remove_place":
        day, attraction = _find_attraction(plan, op.target_stable_id)
        if attraction is None:
            return fail("UNVERIFIED_REQUIRED_POI",
                        f"未找到要删除的地点（stable_id={op.target_stable_id}）")
        day.attractions.remove(attraction)
        return True

    if op.op == "move_place":
        src_day, attraction = _find_attraction(plan, op.target_stable_id)
        if attraction is None:
            return fail("UNVERIFIED_REQUIRED_POI", f"未找到要移动的地点（stable_id={op.target_stable_id}）")
        if op.day_index is None or op.day_index >= len(plan.days):
            return fail("DAILY_OVERLOAD", f"移动目标日无效（day_index={op.day_index}）")
        if op.day_index in plan.days and src_day is not None and plan.days[op.day_index] is src_day:
            pass  # 同日内移动等价于重排，允许
        src_day.attractions.remove(attraction)
        dest = plan.days[op.day_index]
        pos = min(op.position if op.position is not None else len(dest.attractions), len(dest.attractions))
        dest.attractions.insert(pos, attraction)
        return True

    if op.op == "reorder_day":
        if op.day_index is None or op.day_index >= len(plan.days):
            return fail("DAILY_OVERLOAD", f"重排目标日无效（day_index={op.day_index}）")
        day = plan.days[op.day_index]
        by_id = {a.stable_id: a for a in day.attractions}
        ordered = op.ordered_stable_ids or []
        missing = [sid for sid in ordered if sid not in by_id]
        if missing:
            return fail("UNVERIFIED_REQUIRED_POI", f"重排列表包含不属于该日的地点: {missing}")
        if set(ordered) != set(by_id):
            return fail("DUPLICATE_POI", "重排列表与当日地点集合不一致")
        # 重复 id 会静默复制景点（[A,B,A] 通过 set 校验但产生 3 个景点），
        # 必须逐位无重复
        if len(ordered) != len(by_id):
            return fail("DUPLICATE_POI", "重排列表包含重复地点，每个地点只能出现一次")
        day.attractions = [by_id[sid] for sid in ordered]
        return True

    if op.op == "change_duration":
        _, attraction = _find_attraction(plan, op.target_stable_id)
        if attraction is None:
            return fail("UNVERIFIED_REQUIRED_POI", f"未找到地点（stable_id={op.target_stable_id}）")
        if op.duration_minutes is None:
            return fail("DAILY_OVERLOAD", "change_duration 缺少 duration_minutes")
        attraction.visit_duration = op.duration_minutes
        return True

    if op.op == "change_pace":
        if op.day_index is None or op.day_index >= len(plan.days):
            return fail("DAILY_OVERLOAD", f"节奏目标日无效（day_index={op.day_index}）")
        plan.days[op.day_index].pace = op.pace or ""
        return True

    if op.op == "change_start_time":
        if op.day_index is None or op.day_index >= len(plan.days):
            return fail("DAILY_OVERLOAD", f"出发时间目标日无效（day_index={op.day_index}）")
        plan.days[op.day_index].preferred_start_time = op.start_time or ""
        return True

    if op.op == "update_transport":
        if op.day_index is not None:
            if op.day_index >= len(plan.days):
                return fail("DAILY_OVERLOAD", f"交通目标日无效（day_index={op.day_index}）")
            plan.days[op.day_index].transportation = op.transport or ""
        else:
            for day in plan.days:
                day.transportation = op.transport or ""
        return True

    if op.op in {"add_place", "replace_place"}:
        new_attraction = _resolve_new_attraction(op, plan, candidates or {}, searched_pois)
        if new_attraction is None:
            label = op.replacement_candidate_id or op.replacement_name or "（未指定）"
            return fail(
                "UNVERIFIED_REQUIRED_POI",
                f"无法在地图或候选池中确认「{label}」，已拒绝该修改（不伪造坐标）",
                day_index=op.day_index,
            )
        if op.op == "add_place":
            if op.day_index is None or op.day_index >= len(plan.days):
                return fail("DAILY_OVERLOAD", f"新增目标日无效（day_index={op.day_index}）")
            dest = plan.days[op.day_index]
            pos = min(op.position if op.position is not None else len(dest.attractions), len(dest.attractions))
            dest.attractions.insert(pos, new_attraction)
        else:
            src_day, old = _find_attraction(plan, op.target_stable_id)
            if old is None:
                return fail("UNVERIFIED_REQUIRED_POI", f"未找到要替换的地点（stable_id={op.target_stable_id}）")
            new_attraction.stable_id = old.stable_id  # 替换保持稳定 ID，执行进度不丢
            idx = src_day.attractions.index(old)
            src_day.attractions[idx] = new_attraction
        return True

    return fail("UNVERIFIED_REQUIRED_POI", f"暂不支持的操作类型: {op.op}")


def apply_plan_patch(
    plan: TripPlan,
    patch: PlanPatch,
    candidates: Optional[Iterable[CandidatePlace]] = None,
    current_version: Optional[int] = None,
) -> Tuple[TripPlan, list[PlanIssue]]:
    """把 PlanPatch 应用到计划副本上。

    返回 (新计划, issues)。issues 含 error 时调用方必须丢弃新计划、保留原版本。
    candidates：请求携带的攻略候选，add/replace 按 replacement_candidate_id 直取。
    current_version：行程库当前版本号（乐观锁）。提供时与 patch.base_version 比对，
    不等说明行程在编辑期间被别处更新过，拒绝本次修改防覆盖。
    """
    ensure_stable_ids(plan)
    issues: list[PlanIssue] = []
    candidate_map = {c.candidate_id: c for c in (candidates or [])}

    if current_version is not None and patch.base_version != current_version:
        issues.append(PlanIssue(
            code="BASE_VERSION_CONFLICT", severity="error",
            message=(
                f"行程已在别处更新（当前 v{current_version}，本次修改基于 v{patch.base_version}），"
                "请刷新后重试"
            ),
        ))
        return plan, issues

    # 目标日范围预检
    for idx in patch.affected_day_indexes:
        if idx >= len(plan.days):
            issues.append(PlanIssue(
                code="DAILY_OVERLOAD", severity="error",
                message=f"修改目标日超出范围（day_index={idx}，共 {len(plan.days)} 天）",
            ))
            return plan, issues

    # preserved_stable_ids 不允许出现在任何操作里
    for op in patch.operations:
        if op.target_stable_id and op.target_stable_id in patch.preserved_stable_ids:
            issues.append(PlanIssue(
                code="DUPLICATE_POI", severity="error",
                message=f"操作试图修改受保护的地点（{op.target_stable_id}）",
                day_index=op.day_index,
            ))
            return plan, issues

    new_plan = plan.model_copy(deep=True)
    ensure_stable_ids(new_plan)
    searched: dict[str, Optional[POIInfo]] = {}
    for op in patch.operations:
        _apply_single(new_plan, op, issues, searched, candidate_map)

    # 硬校验：未受影响日的 stable_id 与名称必须原样保留
    protected = set(patch.preserved_stable_ids)
    if protected:
        current_ids = {a.stable_id for d in new_plan.days for a in d.attractions}
        missing = protected - current_ids
        if missing:
            issues.append(PlanIssue(
                code="DUPLICATE_POI", severity="error",
                message=f"修改波及了应保留的内容: {sorted(missing)}",
            ))

    # 修改后补全新增景点的 stable_id
    ensure_stable_ids(new_plan)
    return new_plan, issues
