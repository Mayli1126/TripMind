"""攻略地点提取与地图校准服务（产品化计划 7.2 节 extract_facts / poi_verify）。

职责边界：
- extract_places：LLM 只负责从攻略正文提取地点与推荐理由，绝不产出坐标；
- verify_extracted_place：确定性代码回流高德校准名称/坐标（poi_id 为准），
  查不到时 fact_status=unverified、location=None，绝不伪造坐标参与路线计算。
"""

from __future__ import annotations

import json
import re
from typing import Optional

from ..infrastructure.llm_client import LLMClient
from ..models.candidate import CandidateCategory, CandidatePlace
from ..models.guide import ExtractedPlace, GuideDocument, GuideFact
from .candidate_pool_service import make_candidate_id

# CandidateCategory 白名单：LLM 输出不在表内时回退 attraction
_VALID_CATEGORIES = {
    "attraction", "museum", "street", "natural", "food",
    "night", "leisure", "transport_hub", "hotel",
}

# 类别中文提示：帮助 LLM 稳定输出约定枚举
_CATEGORY_HINTS = """attraction(景观/观光) museum(博物馆/展馆) street(商业街/打卡街) natural(自然风光)
food(餐饮) night(夜市/夜景) leisure(休闲/乐园) transport_hub(车站/机场接驳) hotel(酒店)"""

_EXTRACT_PROMPT = """你是旅游攻略解析器。从下面的攻略正文中提取"具体可去的地点"及攻略给出的理由。

攻略标题：{title}
用户提示的目的地城市：{city_hint}（为空时请从正文推断主要城市）

解析规则：
- 只提取"用户可以前往"的地点：景点/博物馆/街区/餐饮/夜市/乐园/车站等；
- 城市、路线建议、泛泛的描述（如"老城区很多胡同"）不要提取；
- 地点名称保持攻略原写法，常见别称（如"故宫"与"故宫博物院"）放进 aliases；
- category 只能取：{categories}；
- recommendation_reasons 提炼攻略的推荐理由（每条一句话，最多 3 条）；
- warnings 提取攻略提示的风险（需预约/闭馆日/体力强度/容易排队等，没有则空数组）；
- confidence 表示你对"这是真实存在且可前往的地点"的把握（0~1 小数）；
- 单篇攻略最多提取 30 个地点，按攻略中重要性排序。

只返回 JSON，不要输出其他文字：
{{
  "city": "攻略主要涉及的城市",
  "places": [
    {{
      "name": "地点名称",
      "aliases": ["别称"],
      "category": "attraction",
      "suggested_duration_minutes": 90,
      "recommendation_reasons": ["推荐理由"],
      "warnings": ["需提前在官方公众号预约"],
      "confidence": 0.8
    }}
  ]
}}

攻略正文：
---
{content}
---"""


def _extract_json(raw: str) -> dict:
    """从 LLM 输出中提取 JSON 对象（容忍 ```json 包裹与前后杂文本）。"""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError("LLM 输出中未找到 JSON")
    return json.loads(match.group(0))


def _safe_place(data: dict) -> Optional[ExtractedPlace]:
    """单条提取结果转 ExtractedPlace；字段异常时跳过而不是整篇失败。"""
    name = str(data.get("name") or "").strip()
    if not name:
        return None
    category = data.get("category")
    if category not in _VALID_CATEGORIES:
        category = "attraction"
    try:
        duration = int(data.get("suggested_duration_minutes") or 90)
    except (TypeError, ValueError):
        duration = 90
    return ExtractedPlace(
        name=name,
        aliases=[str(a).strip() for a in (data.get("aliases") or []) if str(a).strip()],
        category=category,  # type: ignore[arg-type]
        suggested_duration_minutes=max(15, min(720, duration)),
        recommendation_reasons=[
            str(r).strip() for r in (data.get("recommendation_reasons") or []) if str(r).strip()
        ],
        warnings=[str(w).strip() for w in (data.get("warnings") or []) if str(w).strip()],
        confidence=_clip_confidence(data.get("confidence")),
    )


def _clip_confidence(value) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


# 攻略提取专用模型客户端：结构化提取不需要最强模型，优先用 LLM_GUIDE_MODEL
# （如 qwen3.8-27b，比主模型快数倍）；未配置时回退主模型。进程级缓存避免重复建连。
_guide_llm: Optional[LLMClient] = None


def _get_guide_llm() -> LLMClient:
    global _guide_llm
    if _guide_llm is None:
        import os

        model = (os.getenv("LLM_GUIDE_MODEL") or "").strip()
        if model:
            _guide_llm = LLMClient(
                model=model,
                temperature=1.0,
                max_tokens=int(os.getenv("LLM_MAX_TOKENS", "8192")),
            )
        else:
            from .llm_service import get_llm

            _guide_llm = get_llm()
    return _guide_llm


# ---------------------------------------------------------------------------
# 截图提取（视觉模型）：截图 -> 文字转写 + 地点结构化，一步完成
# ---------------------------------------------------------------------------

# 截图转写上限：超长截图（长图攻略）只转写前 12000 字参与提取
_VISION_TRANSCRIPT_MAX = 12000

_VISION_EXTRACT_PROMPT = """你是旅游攻略截图解析器。先逐段念出截图里的行程文字（transcript），再从这些文字中提取"具体可去的地点"。

用户提示的目的地城市：{city_hint}（为空时请从截图内容推断主要城市）

规则：
- transcript 忠实转写截图中的标题/行程/备注文字（忽略水印、点赞数、头像、广告）；
- 只提取"用户可以前往"的地点，泛泛描述不要提取；
- 地点名称保持截图原写法，常见别称放进 aliases；
- category 只能取：{categories}；
- recommendation_reasons 提炼截图中的推荐理由（每条一句话，最多 3 条）；
- warnings 提取截图提示的风险（需预约/闭馆日/体力强度等，没有则空数组）；
- 单张截图最多提取 30 个地点，按截图中重要性排序。

只返回 JSON：
{{
  "city": "攻略主要涉及的城市",
  "transcript": "截图文字转写",
  "places": [
    {{"name": "地点名称", "aliases": ["别称"], "category": "attraction", "suggested_duration_minutes": 90,
      "recommendation_reasons": ["推荐理由"], "warnings": ["需提前预约"], "confidence": 0.8}}
  ]
}}"""


class VisionModelNotConfigured(Exception):
    """LLM_VISION_MODEL 未配置或为空，截图导入不可用。"""


_vision_llm: Optional[LLMClient] = None


def _get_vision_llm() -> LLMClient:
    global _vision_llm
    if _vision_llm is None:
        import os

        from ..config import get_settings

        model = (get_settings().vision_model or os.getenv("LLM_VISION_MODEL") or "").strip()
        if not model:
            raise VisionModelNotConfigured(
                "截图导入需要在 .env 配置 LLM_VISION_MODEL（视觉模型，如 qwen3-vl-plus）"
            )
        _vision_llm = LLMClient(model=model, temperature=1.0, max_tokens=8192)
    return _vision_llm


def extract_places_from_image(
    image_base64: str,
    image_mime: str,
    city_hint: str = "",
) -> tuple[str, str, list[ExtractedPlace]]:
    """视觉模型解析攻略截图：返回 (城市, 文字转写, 地点列表)。

    转写会作为 GuideDocument.content 保存，让文档长度/可追溯性与文本攻略一致。
    """
    client = _get_vision_llm()
    prompt = _VISION_EXTRACT_PROMPT.format(city_hint=city_hint or "（未提供）", categories=_CATEGORY_HINTS)
    raw = client.invoke([
        {"role": "system", "content": "你是严谨的攻略截图结构化助手，只输出 JSON。"},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:{image_mime};base64,{image_base64}"}},
            {"type": "text", "text": prompt},
        ]},
    ])
    data = _extract_json(raw)
    city = str(data.get("city") or city_hint or "").strip()
    transcript = str(data.get("transcript") or "")[:_VISION_TRANSCRIPT_MAX]
    places = [p for p in (_safe_place(item) for item in (data.get("places") or [])) if p]
    if not places:
        raise ValueError("截图中未提取到任何可前往的地点")
    return city, transcript, places


def extract_places(
    document: GuideDocument,
    city_hint: str = "",
    llm: Optional[LLMClient] = None,
) -> tuple[str, list[ExtractedPlace]]:
    """LLM 提取：返回 (城市, 地点列表)。LLM 不可用时抛异常，由上层降级。"""
    client = llm or _get_guide_llm()
    prompt = _EXTRACT_PROMPT.format(
        title=document.title or "（无标题）",
        city_hint=city_hint or "（未提供）",
        categories=_CATEGORY_HINTS,
        content=document.content,
    )
    raw = client.invoke([
        {"role": "system", "content": "你是严谨的旅游攻略结构化助手，只输出 JSON。"},
        {"role": "user", "content": prompt},
    ])
    data = _extract_json(raw)
    city = str(data.get("city") or city_hint or "").strip()
    places = [p for p in (_safe_place(item) for item in (data.get("places") or [])) if p]
    if not places:
        raise ValueError("攻略中未提取到任何可前往的地点")
    return city, places


# ---------------------------------------------------------------------------
# POI 校准（确定性代码，7.2 节 poi_verify）
# ---------------------------------------------------------------------------

def _search_poi(name: str, city: str):
    """高德 POI 检索：名称 -> 真实 POI。查不到返回 None，绝不伪造坐标。"""
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


def verify_extracted_place(place: ExtractedPlace, city: str, source_ref: str) -> CandidatePlace:
    """把 LLM 提取的地点校准为候选：地图命中即 verified，未命中保持 unverified。

    canonical_name 以地图数据为准；攻略原名称降级为别名（契约 6.1 节）。
    """
    aliases = list(place.aliases)
    poi = _search_poi(place.name, city) if city else None
    if poi is not None and poi.name != place.name:
        aliases.append(place.name)
    verified = poi is not None
    return CandidatePlace(
        candidate_id=make_candidate_id(poi.id if poi else None, place.name, city),
        canonical_name=poi.name if verified else place.name,
        aliases=aliases,
        city=city,
        poi_id=poi.id if poi else None,
        location=poi.location if poi else None,
        category=place.category,
        suggested_duration_minutes=place.suggested_duration_minutes,
        source_refs=[source_ref] if source_ref else [],
        recommendation_reasons=list(place.recommendation_reasons),
        warnings=list(place.warnings),
        confidence=place.confidence if verified else round(place.confidence * 0.6, 2),
        fact_status="verified" if verified else "unverified",
    )


def build_facts(place: ExtractedPlace) -> list[GuideFact]:
    """把单条提取结果的攻略主张拆成 guide_facts 行（保留 claim 与来源语义）。"""
    facts: list[GuideFact] = []
    for reason in place.recommendation_reasons:
        facts.append(GuideFact(fact_type="reason", subject=place.name, claim=reason, confidence=place.confidence))
    for warning in place.warnings:
        facts.append(GuideFact(fact_type="warning", subject=place.name, claim=warning, confidence=place.confidence))
    facts.append(GuideFact(
        fact_type="duration",
        subject=place.name,
        claim=f"建议游玩 {place.suggested_duration_minutes} 分钟",
        confidence=place.confidence,
    ))
    return facts
