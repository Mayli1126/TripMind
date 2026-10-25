"""候选池容器与合并去重（产品化计划 6.1/7.2 节）。

三种输入管线（快速规划 / 多攻略融合 / 平台模板）统一产出 CandidatePlace
进入候选池。本模块只做确定性代码：合并键判定、字段并集、状态单向升级；
不做任何 LLM 调用，保证合并结果可复现。

合并规则（7.2 节"保留多个 claim 与来源"）：
- 同一地点判定：poi_id 相同，或同城市下归一化名称相同；
- 合并策略：别名/来源/理由/警告取并集，confidence 取最大；
- 事实状态只升不降：unverified -> partially_verified -> verified；
- user_choice 以先入池者为准，后到的同名候选不覆盖用户已做的选择。
"""

from __future__ import annotations

import hashlib
import unicodedata
from typing import Optional

from ..models.candidate import CandidatePlace, FactStatus, UserChoice

# 事实状态升级序：索引越大越可信
_FACT_RANK: dict[str, int] = {"unverified": 0, "partially_verified": 1, "verified": 2}


def normalize_name(name: str) -> str:
    """名称归一化：全角转半角、去空白与常见标点差异，只用于合并判定。

    注意：归一化不能解决"故宫/故宫博物院"这类别名关系，
    真正的跨来源合并以高德 poi_id 为准，归一化只是兜底。
    """
    text = unicodedata.normalize("NFKC", name or "")
    removed = " \t\r\n　·•，。,.''\"\"\"()（）【】[]"
    return "".join(ch for ch in text if ch not in removed).lower()


# 名称包含归并的长度下限：太短的子串（如"西湖"⊂"西湖区"）容易误合并
_MIN_CONTAIN_LEN = 2
# 交通设施关键词：命中则不做包含归并，避免"南锣鼓巷"并进"南锣鼓巷地铁站"
_TRANSPORT_NAME_TOKENS = ("地铁站", "火车站", "机场", "汽车站", "公交站", "站")


def _is_transport_name(norm_name: str) -> bool:
    return any(token in norm_name for token in _TRANSPORT_NAME_TOKENS)


def is_same_place(name_a: str, name_b: str) -> bool:
    """名称级"同一地点"判定：归一化相等，或一方包含另一方（别名/前后缀变体）。

    覆盖攻略间的常见变体："鸡鸣寺/古鸡鸣寺"、"玄武湖/玄武湖景区"、
    "老门东/老门东-步行街"。交通设施（地铁站等）不参与包含归并。
    """
    a, b = normalize_name(name_a), normalize_name(name_b)
    if not a or not b:
        return False
    if a == b:
        return True
    if _is_transport_name(a) or _is_transport_name(b):
        return False
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= _MIN_CONTAIN_LEN and short in long


def make_candidate_id(poi_id: Optional[str], name: str, city: str) -> str:
    """生成稳定候选 ID：同 poi_id 或同名称重复加入时 ID 不变。"""
    key = f"{poi_id or ''}|{normalize_name(name)}|{city}"
    return f"cand-{hashlib.md5(key.encode('utf-8')).hexdigest()[:12]}"


class CandidatePool:
    """候选地点池：add_or_merge 幂等合并，供确认界面与主规划图消费。"""

    def __init__(self) -> None:
        self._places: dict[str, CandidatePlace] = {}
        # poi_id -> candidate_id / 归一化名称 -> candidate_id，加速查重
        self._by_poi: dict[str, str] = {}
        self._by_name: dict[str, str] = {}

    # ------------------------------------------------------------------
    def _find_existing(self, place: CandidatePlace) -> Optional[CandidatePlace]:
        """查重：poi_id 精确命中 -> 归一化名称相等 -> 名称包含归并（别名变体）。"""
        if place.poi_id and place.poi_id in self._by_poi:
            return self._places.get(self._by_poi[place.poi_id])
        key = normalize_name(place.canonical_name)
        if key and key in self._by_name:
            return self._places.get(self._by_name[key])
        # 名称包含归并："鸡鸣寺/古鸡鸣寺"、"玄武湖/玄武湖景区"等变体视为同一地点；
        # 候选池规模有限（单次导入 ≤ 数百），线性扫描可接受
        for existing in self._places.values():
            if existing.city != place.city:
                continue
            if is_same_place(existing.canonical_name, place.canonical_name):
                return existing
        return None

    def add_or_merge(self, place: CandidatePlace) -> tuple[str, bool]:
        """加入候选池；命中已有地点时合并字段。

        返回 (candidate_id, merged)：merged=False 表示这是新地点。
        """
        existing = self._find_existing(place)
        if existing is None:
            candidate_id = place.candidate_id or make_candidate_id(
                place.poi_id, place.canonical_name, place.city
            )
            place.candidate_id = candidate_id
            self._places[candidate_id] = place
            self._register(place)
            return candidate_id, False
        self._merge_into(existing, place)
        return existing.candidate_id, True

    def _register(self, place: CandidatePlace) -> None:
        if place.poi_id:
            self._by_poi[place.poi_id] = place.candidate_id
        key = normalize_name(place.canonical_name)
        if key:
            self._by_name[key] = place.candidate_id

    @staticmethod
    def _merge_into(target: CandidatePlace, incoming: CandidatePlace) -> None:
        """把 incoming 并入 target（确定性并集，不改 user_choice）。"""

        def union(base: list[str], extra: list[str]) -> list[str]:
            seen = set(base)
            return base + [item for item in extra if item and item not in seen]

        target.aliases = union(target.aliases, incoming.aliases)
        target.source_refs = union(target.source_refs, incoming.source_refs)
        target.recommendation_reasons = union(target.recommendation_reasons, incoming.recommendation_reasons)
        target.warnings = union(target.warnings, incoming.warnings)
        target.confidence = max(target.confidence, incoming.confidence)
        # 被合并方的名称沉淀为别名，确认界面可见"也叫：XX"
        if incoming.canonical_name != target.canonical_name:
            target.aliases = union(target.aliases, [incoming.canonical_name])
        # 事实状态只升不降；坐标与 poi_id 有真值就填上
        if _FACT_RANK[incoming.fact_status] > _FACT_RANK[target.fact_status]:
            target.fact_status = incoming.fact_status
        if target.poi_id is None and incoming.poi_id:
            target.poi_id = incoming.poi_id
            target.canonical_name = incoming.canonical_name  # 地图标准名为准
        if target.location is None and incoming.location is not None:
            target.location = incoming.location
        # 类别信息更具体时允许修正（unverified 的兜底类别 -> 已校验地点的类别）
        if incoming.fact_status == "verified" and target.fact_status != "verified":
            target.category = incoming.category

    # ------------------------------------------------------------------
    def get(self, candidate_id: str) -> Optional[CandidatePlace]:
        return self._places.get(candidate_id)

    def set_user_choice(self, candidate_id: str, choice: UserChoice) -> bool:
        """确认界面调用的选择接口；返回是否命中。"""
        place = self._places.get(candidate_id)
        if place is None:
            return False
        place.user_choice = choice
        return True

    def to_list(self, include_excluded: bool = True) -> list[CandidatePlace]:
        """导出候选列表（verified 优先，其余按 confidence 降序）。"""
        places = list(self._places.values())
        if not include_excluded:
            places = [p for p in places if p.user_choice != "excluded"]
        return sorted(
            places,
            key=lambda p: (p.fact_status == "verified", p.confidence),
            reverse=True,
        )

    def stats(self) -> dict[str, int]:
        """按 user_choice 统计，供确认界面展示概览。"""
        result = {"required": 0, "preferred": 0, "optional": 0, "excluded": 0}
        for place in self._places.values():
            result[place.user_choice] = result.get(place.user_choice, 0) + 1
        return result

    def __len__(self) -> int:
        return len(self._places)


# ---------------------------------------------------------------------------
# 阶段 2：候选池接入主规划图
# ---------------------------------------------------------------------------

# user_choice 的 prompt 文案：required/preferred 是硬约束，optional 是软建议
_CHOICE_DIRECTIVES: dict[str, str] = {
    "required": "【必去】用户明确要求必须安排进行程",
    "preferred": "【想去】用户想去，优先安排",
    "optional": "【可选】如当日时间富余可以考虑",
}


def active_candidates(candidates) -> list[CandidatePlace]:
    """参与规划的候选：剔除用户排除的（正常情况下前端也不会发送 excluded）。"""
    return [c for c in candidates if c.user_choice != "excluded"]


def render_candidates_for_prompt(candidates) -> str:
    """把候选渲染成规划 prompt 的附加段落。

    - verified 候选附带已核对的坐标与 poi_id，要求 LLM 直接使用（不许改写）；
    - unverified 候选只有名称，要求 LLM 谨慎参考；
    - 无候选时返回空字符串，prompt 保持原样。
    """
    active = active_candidates(candidates)
    if not active:
        return ""

    lines = ["", "**用户攻略候选地点（已由用户确认）:**"]
    for index, place in enumerate(active, start=1):
        directive = _CHOICE_DIRECTIVES.get(place.user_choice, "【想去】优先安排")
        line = f"{index}. {place.canonical_name}（{directive}"
        if place.fact_status == "verified" and place.location is not None:
            line += (
                f"；坐标已核对: {place.location.longitude},{place.location.latitude}"
                + (f"，poi_id: {place.poi_id}" if place.poi_id else "")
            )
        else:
            line += "；位置未在地图确认"
        line += "）"
        if place.suggested_duration_minutes:
            line += f" 建议游玩{place.suggested_duration_minutes}分钟"
        lines.append(line)
        # 推荐理由与警告各取一条，控制 prompt 长度
        if place.recommendation_reasons:
            lines.append(f"   理由: {place.recommendation_reasons[0]}")
        for warning in place.warnings[:1]:
            lines.append(f"   注意: {warning}")
    lines.append(
        "上述候选是用户从自己攻略中确认的地点：必去/想去地点必须出现在行程中并使用给出的坐标；"
        "确实因天数放不下时，在 warnings 中说明原因。"
    )
    return "\n".join(lines)


def apply_candidates_to_plan(plan, request) -> list[str]:
    """生成计划后与候选对账（确定性代码，无 LLM）：

    - 计划中同名景点回填已核对候选的 poi_id 与坐标（地图数据为准，修正 LLM 编写的坐标）；
    - 【必去】候选未出现在计划中时返回提示，由调用方写入 warnings。
    """
    notes: list[str] = []
    active = active_candidates(getattr(request, "candidate_places", []) or [])
    if not active:
        return notes

    verified_by_name = {
        normalize_name(c.canonical_name): c
        for c in active
        if c.fact_status == "verified" and c.location is not None
    }
    required_names = {normalize_name(c.canonical_name) for c in active if c.user_choice == "required"}
    seen_required: set[str] = set()

    for day in plan.days:
        for attraction in day.attractions:
            key = normalize_name(attraction.name)
            candidate = verified_by_name.get(key)
            if candidate is not None:
                # 坐标与 poi_id 以候选（地图核对）为准
                attraction.poi_id = candidate.poi_id or attraction.poi_id
                attraction.location = candidate.location
            # 别名也参与必去命中判定（攻略原名 vs 地图标准名）
            if key in required_names:
                seen_required.add(key)
                continue
            for cand in active:
                if cand.user_choice != "required":
                    continue
                if key in {normalize_name(a) for a in cand.aliases}:
                    seen_required.add(normalize_name(cand.canonical_name))

    missing = required_names - seen_required
    for name in missing:
        matching = next(
            (c for c in active if c.user_choice == "required" and normalize_name(c.canonical_name) == name),
            None,
        )
        display = matching.canonical_name if matching else name
        notes.append(f"攻略必去地点「{display}」未纳入行程（可能天数或预算放不下），建议手动确认。")
    return notes
