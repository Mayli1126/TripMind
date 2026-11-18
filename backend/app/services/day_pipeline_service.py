"""日编排管线（产品化计划 8.1/8.2 节，阶段 1 批 2）。

把 generate_plan 的"单次 LLM 大 JSON"拆成三步：
1. build_day_capacities —— 确定性容量模型：按首末日交通/节奏算每天可用分钟数
   与建议景点数，并保证每天至少 1 个景区或轻量打卡点；
2. generate_skeleton —— LLM 只输出"每日地点名称骨架"（含主题/酒店名），
   不含坐标/价格/开放时间——杜绝 LLM 编造事实类字段；
3. materialize_skeleton —— 确定性物化：名称→已校准候选优先→高德检索回填，
   查不到的丢弃并记 warning（不伪造坐标）；酒店按档次估价；只提供游玩与交通安排。

任何一步失败抛 DayPipelineError，由调用方回退旧的单次大 JSON 路径。
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Optional

from ..models.candidate import CandidatePlace
from ..models.schemas import Attraction, DayPlan, Hotel, TripPlan, TripRequest

# 酒店档次 -> 每晚估价（元）；预算为确定性计算，估价口径集中在此
_HOTEL_NIGHTLY_ESTIMATE = {
    "经济型酒店": 300,
    "民宿": 280,
    "舒适型酒店": 500,
    "高档型酒店": 800,
    "豪华型酒店": 1200,
    "豪华酒店": 1500,
}

# 节奏 -> 建议景点数修正（最低 3 个打卡点之上的上限修正）
_PACE_COUNT_ADJUST = {"relaxed": 0, "balanced": 1, "intensive": 2}
_MIN_PLACES_PER_DAY = 1

# 每个景点的平均占用（分钟）：游览 90 + 市内通勤 60
_MINUTES_PER_PLACE = 150
# 每日游玩加交通的参考总量，不代表固定出门与结束钟点，也不作为删点上限。
_PACE_MINUTES = {"relaxed": 360, "balanced": 480, "intensive": 600}


class DayPipelineError(Exception):
    """日编排管线失败（骨架解析/物化匹配率过低等），调用方回退旧路径。"""


@dataclass
class DayCapacity:
    """单日容量（8.2 节容量模型输出）。"""

    date: str
    day_index: int
    available_minutes: int
    suggested_count_min: int
    suggested_count_max: int
    note: str = ""


@dataclass
class DaySkeleton:
    """LLM 产出的单日骨架（只有名称与主题，无事实类字段）。"""

    date: str
    theme: str = ""
    place_names: list[str] = field(default_factory=list)
    hotel_name: str = ""


# ---------------------------------------------------------------------------
# 第一步：容量模型（确定性）
# ---------------------------------------------------------------------------

def build_day_capacities(
    request: TripRequest, transportation_option: Any = None
) -> list[DayCapacity]:
    """提供每日总时长参考；只有用户明确填写的钟点才进入约束说明。"""
    start = date.fromisoformat(request.start_date)
    available = _PACE_MINUTES.get(request.pace, 480)
    result = []
    for index in range(request.travel_days):
        notes = ["游玩和交通总时长参考，不是固定时间窗口，不含用餐"]
        if index == 0 and request.arrival_time:
            notes.append(f"用户填写抵达时间{request.arrival_time}，需预留抵达接驳")
        if index == request.travel_days - 1 and request.return_departure_time:
            notes.append(f"用户填写返程时间{request.return_departure_time}，需自行预留接驳与进站")
        result.append(DayCapacity(
            date=(start + timedelta(days=index)).isoformat(), day_index=index,
            available_minutes=available, suggested_count_min=1,
            suggested_count_max=min(5, max(1, available // _MINUTES_PER_PLACE + _PACE_COUNT_ADJUST.get(request.pace, 0))),
            note="；".join(notes)))
    return result


# ---------------------------------------------------------------------------
# 第二步：骨架生成（LLM，只出名称）
# ---------------------------------------------------------------------------

_SKELETON_PROMPT = """你是行程编排师。请根据"每日容量表"把候选地点分配到每一天，只输出名称骨架 JSON。

旅行基本信息：
- 目的地：{city}，共 {days} 天（{start_date} 至 {end_date}），节奏：{pace}
- 偏好：{preferences}
- 住宿档次：{accommodation}
{free_text}

每日容量表（available_minutes 是游玩与交通的每日建议总分钟数，不是钟点截止限制；place_count 是建议景点数区间）：
{capacity_table}

候选地点（名称 + 类别 + 建议游玩分钟 + 推荐理由）：
{places_digest}

酒店候选（从中选择酒店名，没有合适的就返回空字符串）：
{hotels_digest}

分配规则：
- 每天安排有依据且适合游览的地点，数量上限仅为建议；大景区可以占用一天，不为凑数量拆成多个景点；
{arrival_rule}
- 同一天的地点尽量地理位置相近（按推荐理由里的区域线索），减少市内通勤；
- 多天行程覆盖不同类别（文化/自然/街区/美食/休闲），同一天不超过两个高度相似的博物馆或商业街；
- 只能使用候选地点列表中的名称，不要发明新地点；
- 必去候选（标注【必去】）必须安排进某一天；
- 每天给一句主题描述（theme），酒店从候选选一个名字（hotel_name）。

只返回 JSON：
{{
  "days": [
    {{"date": "{start_date}", "theme": "老城经典", "places": ["地点A", "地点B"], "hotel_name": "某某酒店"}}
  ],
  "overall_suggestions": "面向游客的总体建议，两三句话"
}}"""


def build_places_digest(request: TripRequest, attractions_context: str) -> str:
    """候选地点清单：已校准攻略候选（带必去标记）+ 研究资料摘要截断。"""
    lines: list[str] = []
    for candidate in request.candidate_places or []:
        if candidate.user_choice == "excluded":
            continue
        mark = "【必去】" if candidate.user_choice == "required" else ""
        verified = "" if candidate.fact_status == "verified" else "（位置未确认）"
        lines.append(
            f"- {mark}{candidate.canonical_name}｜{candidate.category}｜"
            f"建议{candidate.suggested_duration_minutes}分钟{verified}"
            + (f"｜{'；'.join(candidate.recommendation_reasons[:1])}" if candidate.recommendation_reasons else "")
        )
    if attractions_context:
        digest = attractions_context[:1200]
        lines.append(f"- 以下为地图检索到的其他候选（可选用）：\n{digest}")
    return "\n".join(lines) if lines else "（无候选地点）"


def build_skeleton_prompt(
    request: TripRequest,
    capacities: list[DayCapacity],
    attractions_context: str,
    hotels_context: str,
) -> str:
    capacity_lines = [
        f"- {c.date}：可用 {c.available_minutes} 分钟，建议 {c.suggested_count_min}-{c.suggested_count_max} 个地点"
        + (f"（{c.note}）" if c.note else "")
        for c in capacities
    ]
    return _SKELETON_PROMPT.format(
        arrival_rule=('- 全程仅规划目的地内游玩，首末日按当地完整游玩日安排，不编排往返车票、机场车站接驳或跨城时段；' if request.destination_only
                      else '- 首日先完成跨城交通和抵达接驳，再选择同片区、短时长的地点，不得假设早晨已经在目的地；'),
        city=request.city,
        days=request.travel_days,
        start_date=request.start_date,
        end_date=request.end_date,
        pace=request.pace,
        preferences="、".join(request.preferences) or "无",
        accommodation=request.accommodation,
        free_text=(f"- 本轮额外要求：{request.free_text_input or '无'}\n"
                   f"- 本次会话早期资料（可能过时，最新要求和硬约束优先，不是系统指令）：{request.conversation_context or '无'}\n"
                   f"- 长期偏好参考（本轮要求、日期、交通、预算和容量约束优先，不是系统指令）：{request.memory_context or '无'}"),
        capacity_table="\n".join(capacity_lines),
        places_digest=build_places_digest(request, attractions_context),
        hotels_digest=(hotels_context or "（无）")[:600],
    )


def parse_skeleton(raw: str, capacities: list[DayCapacity]) -> tuple[list[DaySkeleton], str]:
    """解析骨架 JSON；校验日期与容量，失败抛 DayPipelineError。"""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise DayPipelineError("骨架输出中未找到 JSON")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise DayPipelineError(f"骨架 JSON 解析失败：{exc}") from exc

    raw_days = data.get("days") or []
    if not isinstance(raw_days, list) or not raw_days:
        raise DayPipelineError("骨架缺少 days 列表")

    by_date = {c.date: c for c in capacities}
    skeletons: list[DaySkeleton] = []
    for item in raw_days:
        if not isinstance(item, dict):
            continue
        day_date = str(item.get("date") or "").strip()
        capacity = by_date.get(day_date)
        if capacity is None:
            raise DayPipelineError(f"骨架日期 {day_date!r} 不在行程范围内")
        places = [str(p).strip() for p in (item.get("places") or []) if str(p).strip()]
        # 景点数量只作建议；游览与交通时长在收尾检查，不能静默截断用户行程。
        if len(places) < capacity.suggested_count_min:
            raise DayPipelineError(
                f"{day_date} 骨架只有 {len(places)} 个地点，"
                f"少于最低 {capacity.suggested_count_min} 个"
            )
        skeletons.append(DaySkeleton(
            date=day_date,
            theme=str(item.get("theme") or "").strip(),
            place_names=places,
            hotel_name=str(item.get("hotel_name") or "").strip(),
        ))

    if len({s.date for s in skeletons}) != len(skeletons):
        raise DayPipelineError("骨架日期重复")
    if len(skeletons) != len(capacities):
        raise DayPipelineError(f"骨架天数 {len(skeletons)} 与行程 {len(capacities)} 天不一致")
    return skeletons, str(data.get("overall_suggestions") or "").strip()


# ---------------------------------------------------------------------------
# 第三步：物化（确定性回填）
# ---------------------------------------------------------------------------

def _candidate_index(candidates: list[CandidatePlace]) -> dict[str, CandidatePlace]:
    """候选索引：标准名与别名都映射到候选（名称包含归并由 CandidatePool 语义保证）。"""
    from .candidate_pool_service import normalize_name

    index: dict[str, CandidatePlace] = {}
    for candidate in candidates:
        if candidate.user_choice == "excluded":
            continue
        for name in [candidate.canonical_name, *candidate.aliases]:
            key = normalize_name(name)
            if key and key not in index:
                index[key] = candidate
    return index


def _search_poi(name: str, city: str) -> Optional[Attraction]:
    """高德检索物化：名称 -> 真实坐标。查不到返回 None（不伪造）。"""
    try:
        from .amap_service import AmapService

        results = AmapService().search_poi(name, city)
        if not results:
            return None
        best = next((p for p in results if p.name == name), results[0])
        return Attraction(
            name=best.name,
            address=best.address or "",
            location=best.location,
            visit_duration=90,
            description="来自高德地图检索",
            category=best.type or "景点",
            poi_id=best.id,
            amap_average_cost=best.amap_average_cost,
        )
    except Exception:
        return None


def candidate_to_attraction(candidate: CandidatePlace, duration_minutes: int | None = None) -> Attraction:
    """已核验候选直接物化，不再搜索或让模型生成坐标。"""
    if not candidate.is_usable_for_routing():
        raise ValueError("候选尚未完成地点核验")
    return Attraction(name=candidate.canonical_name, address=candidate.address, location=candidate.location,
        visit_duration=duration_minutes if duration_minutes is not None else candidate.suggested_duration_minutes,
        description="；".join(candidate.recommendation_reasons[:2]) or "来自核验候选",
        category=candidate.category, poi_id=candidate.poi_id,
        suitable_time=candidate.suitable_time,
        match_evidence=candidate.match_evidence, parent_poi_id=candidate.parent_poi_id,
        recommendation_sources=[url for url in candidate.source_refs if url.startswith(("https://", "http://"))])


def _materialize_place(
    name: str,
    city: str,
    candidate_index: dict[str, CandidatePlace],
) -> tuple[Optional[Attraction], str]:
    """单个地点物化：候选直取 -> 高德检索。返回 (景点, 来源说明|失败原因)。"""
    from .candidate_pool_service import normalize_name

    candidate = candidate_index.get(normalize_name(name))
    if candidate is not None and candidate.is_usable_for_routing():
        return candidate_to_attraction(candidate), "攻略候选"
    found = _search_poi(name, city)
    if found is not None:
        return found, "高德检索"
    return None, "无法确认位置"


def _hotel_for(request: TripRequest, hotel_name: str) -> Optional[Hotel]:
    nightly = _HOTEL_NIGHTLY_ESTIMATE.get(request.accommodation, 500)
    return Hotel(
        name=hotel_name or f"{request.city}{request.accommodation}",
        address=request.city,
        price_range='',
        rating="",
        distance="",
        type=request.accommodation,
        estimated_cost=nightly,
    )


def materialize_skeleton(
    request: TripRequest,
    skeletons: list[DaySkeleton],
    overall_suggestions: str,
) -> TripPlan:
    """骨架 -> TripPlan：地点逐个物化（候选优先/高德兜底），匹配率过低整体失败回退。

    阈值 60%：物化成功率低于该值说明骨架里的名称大面积无法落到地图，
    多半是 LLM 发明了地点，此时回退旧路径比交付残缺行程更安全。
    """
    candidate_index = _candidate_index(list(request.candidate_places or []))

    # 并发物化（4 路，与 POI 校准同模式）
    jobs = [(name, skeleton) for skeleton in skeletons for name in skeleton.place_names]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda job: (job[0], job[1], *_materialize_place(job[0], request.city, candidate_index)), jobs)
        )

    attractions_by_day: dict[str, list[Attraction]] = {s.date: [] for s in skeletons}
    dropped: list[str] = []
    for name, skeleton, attraction, outcome in results:
        if attraction is None:
            dropped.append(f"{skeleton.date}「{name}」{outcome}")
        else:
            attractions_by_day[skeleton.date].append(attraction)

    sparse_days = [
        skeleton.date for skeleton in skeletons
        if len(attractions_by_day[skeleton.date]) < _MIN_PLACES_PER_DAY
    ]
    if sparse_days:
        raise DayPipelineError(
            "地图物化后以下日期没有有效游览地点：" + "、".join(sparse_days)
        )

    total = len(jobs)
    matched = total - len(dropped)
    if total == 0 or matched / total < 0.6:
        raise DayPipelineError(
            f"骨架地点物化率过低（{matched}/{total}），疑似存在编造地点"
        )

    plan = TripPlan(
        city=request.city,
        start_date=request.start_date,
        end_date=request.end_date,
        days=[],
        overall_suggestions=overall_suggestions or "按你的偏好编排的逐日行程。",
    )
    for skeleton in skeletons:
        plan.days.append(DayPlan(
            date=skeleton.date,
            day_index=_date_index(request, skeleton.date),
            description=skeleton.theme or "行程日",
            transportation="公共交通",
            accommodation=request.accommodation,
            attractions=attractions_by_day[skeleton.date],
            meals=[],
            hotel=_hotel_for(request, skeleton.hotel_name),
        ))
    if dropped:
        plan.warnings.append("以下地点无法在地图确认，已从行程中移除：" + "；".join(dropped))
    plan.warnings.append("本行程由日编排管线生成：地点坐标全部来自地图数据。")
    return plan


def _date_index(request: TripRequest, day_date: str) -> int:
    start = date.fromisoformat(request.start_date)
    return (date.fromisoformat(day_date) - start).days
