"""GuideFusionGraph 前半：攻略输入 -> 候选地点（产品化计划 7.2 节）。

完整链路设计：
    ingest -> extract -> verify -> merge -> user_select[interrupt] -> PlanningGraph

本图实现前半（ingest/extract/verify/merge）：
- 每篇攻略独立解析，单篇失败记入 results（parse_status=failed），
  不影响其他来源，可单独重试（对应 12.4 节 retry API）；
- ingest   Provider 抓取正文（文本透传 / 链接抓取）；
- extract  LLM 提取地点与推荐理由（不产出坐标）；
- verify   高德 POI 校准，生成 CandidatePlace（命中 verified，未命中 unverified）；
- merge    CandidatePool 合并去重（poi_id 优先，归一化名称兜底）。

user_select_candidates interrupt 与 PlanningGraph 接入属于后半，
待候选池全量接入主规划图（阶段 2/3）时实现。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional, Sequence, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from ..models.candidate import CandidatePlace
from ..models.guide import (
    ExtractedPlace,
    GuideDocument,
    GuideFact,
    GuideInput,
    GuideParseResult,
)
from ..services.candidate_pool_service import CandidatePool
from ..services.guide_extract_service import (
    VisionModelNotConfigured,
    build_facts,
    extract_places,
    extract_places_from_image,
    verify_extracted_place,
)
from ..services.guide_provider import GuideFetchError, fetch_guide_document

# 进度回调签名：(百分比, 阶段标识, 用户可读消息)
ProgressCallback = Optional[Callable[[int, str, str], None]]

_guide_fusion_graph = None


class GuideFusionState(TypedDict, total=False):
    """图状态：当前篇的中间产物 + 全部结果 + 候选池。"""

    sources: list[GuideInput]
    city_hint: str
    city: str                          # LLM 推断出的攻略城市（覆盖 city_hint 用于校准）
    source_ids: list[str]
    source_index: int
    document: Optional[GuideDocument]   # 当前篇抓取到的正文
    extracted: list[ExtractedPlace]     # 当前篇 LLM 提取结果
    facts: list[GuideFact]              # 当前篇的攻略事实
    results: list[GuideParseResult]     # 每篇的解析结果（含失败）
    pool: Optional[CandidatePool]       # 跨来源合并后的候选池


def _callback_from_config(config: RunnableConfig) -> ProgressCallback:
    if not config:
        return None
    return (config.get("configurable") or {}).get("progress_callback")


def _report(callback: ProgressCallback, percent: int, stage: str, message: str) -> None:
    if callback is not None:
        try:
            callback(percent, stage, message)
        except Exception:
            pass  # 进度回调异常不影响解析主流程


def _failed_result(state: GuideFusionState, message: str, hint: Optional[str] = None) -> GuideParseResult:
    """构造单篇失败结果：记录错误与补救提示，交给 retry API 单独重试。"""
    source = state["sources"][state["source_index"]]
    source_ids = state.get("source_ids") or []
    return GuideParseResult(
        source_id=source_ids[state["source_index"]] if state["source_index"] < len(source_ids) else "",
        parse_status="failed",
        error_message=message,
        error_hint=hint,
        title=source.display_title(),
        city=state.get("city_hint", ""),
    )


def _advance(state: GuideFusionState) -> dict:
    """当前篇处理结束（无论成败），推进到下一篇。"""
    return {"source_index": state["source_index"] + 1, "document": None, "extracted": [], "facts": []}


# ---------------------------------------------------------------------------
# 节点
# ---------------------------------------------------------------------------

def ingest_node(state: GuideFusionState, config: RunnableConfig) -> dict:
    """抓取当前篇攻略正文；链接不可读时该篇失败并引导粘贴正文。"""
    callback = _callback_from_config(config)
    source: GuideInput = state["sources"][state["source_index"]]
    total = len(state["sources"])
    _report(callback, 5, "ingesting", f"正在读取攻略（{state['source_index'] + 1}/{total}）")

    try:
        document = fetch_guide_document(source)
    except GuideFetchError as exc:
        result = _failed_result(
            {**state, "document": None},
            str(exc),
            hint="链接无法读取时，请直接复制攻略正文粘贴导入",
        )
        return {**_advance(state), "results": state.get("results", []) + [result]}

    return {"document": document}


def extract_node(state: GuideFusionState, config: RunnableConfig) -> dict:
    """LLM 提取当前篇的地点与推荐理由；失败不影响其他来源。

    截图来源走视觉模型（转写+提取一步完成），文本/链接走纯文本提取。
    """
    callback = _callback_from_config(config)
    document: GuideDocument = state["document"]
    source: GuideInput = state["sources"][state["source_index"]]
    _report(callback, 40, "extracting", "正在解析攻略中的地点与建议" if source.input_type != "image" else "正在识别截图内容与地点")

    try:
        if source.input_type == "image":
            city, transcript, places = extract_places_from_image(
                source.image_data, source.image_mime, state.get("city_hint", "")
            )
            # 转写回填文档：document_chars 与结果标题跟文本攻略口径一致
            document = document.model_copy(update={
                "content": transcript,
                "title": document.title or "攻略截图",
            })
        else:
            city, places = extract_places(document, state.get("city_hint", ""))
    except VisionModelNotConfigured as exc:
        result = _failed_result(state, str(exc), hint="请联系管理员在 .env 配置 LLM_VISION_MODEL，或改用粘贴正文导入")
        return {**_advance(state), "results": state.get("results", []) + [result], "document": document}
    except Exception as exc:
        result = _failed_result(state, f"攻略解析失败：{exc}")
        return {**_advance(state), "results": state.get("results", []) + [result], "document": document}

    facts: list[GuideFact] = []
    for place in places:
        facts.extend(build_facts(place))
    return {
        "document": document,
        "extracted": places,
        "facts": facts,
        "city": state.get("city_hint") or city,
    }


def verify_node(state: GuideFusionState, config: RunnableConfig) -> dict:
    """高德 POI 校准并组装当前篇的解析结果（每篇独立成功/失败）。

    校准按地点并发执行（MCP 会话池 4 路轮询，线程安全），
    单个地点失败只降级为 unverified，不中断整篇。
    """
    callback = _callback_from_config(config)
    source: GuideInput = state["sources"][state["source_index"]]
    source_ids = state.get("source_ids") or []
    source_id = source_ids[state["source_index"]] if state["source_index"] < len(source_ids) else ""
    city = state.get("city", "")
    places: list[ExtractedPlace] = state["extracted"]

    _report(callback, 65, "verifying", f"正在核对 {len(places)} 个地点的地图信息")
    source_ref = f"guide:{source_id}" if source_id else "guide:paste"
    if len(places) > 1:
        with ThreadPoolExecutor(max_workers=min(4, len(places))) as pool:
            candidates = list(
                pool.map(lambda p: verify_extracted_place(p, city, source_ref), places)
            )
    else:
        candidates = [verify_extracted_place(places[0], city, source_ref)] if places else []

    document: GuideDocument = state["document"]
    result = GuideParseResult(
        source_id=source_id,
        parse_status="parsed",
        title=document.title or source.display_title(),
        city=city,
        document_chars=len(document.content),
        candidates=candidates,
        facts=state.get("facts", []),
    )
    _report(callback, 80, "verified", f"已核对完成：{sum(1 for c in candidates if c.fact_status == 'verified')}/{len(candidates)} 个地点命中地图")
    return {**_advance(state), "results": state.get("results", []) + [result]}


def merge_node(state: GuideFusionState, config: RunnableConfig) -> dict:
    """跨来源合并候选池：poi_id 优先去重，字段并集，来源可追溯。"""
    callback = _callback_from_config(config)
    pool = CandidatePool()
    for result in state.get("results", []):
        if result.parse_status != "parsed":
            continue
        for candidate in result.candidates:
            pool.add_or_merge(candidate)
    _report(callback, 90, "merged", f"共整理出 {len(pool)} 个候选地点")
    return {"pool": pool}


# ---------------------------------------------------------------------------
# 图构建与执行
# ---------------------------------------------------------------------------

def _next_or_merge(state: GuideFusionState) -> str:
    """当前篇结束后：还有未处理的来源则回到 ingest，否则进入合并。

    注意 _advance 已把 source_index 指向下一篇，这里不再 +1。
    """
    if state["source_index"] < len(state["sources"]):
        return "ingest"
    return "merge"


def build_guide_fusion_graph():
    builder = StateGraph(GuideFusionState)
    builder.add_node("ingest", ingest_node)
    builder.add_node("extract", extract_node)
    builder.add_node("verify", verify_node)
    builder.add_node("merge", merge_node)

    builder.add_edge(START, "ingest")
    # 抓取成功才进入提取；失败已记入 results，直接推进/合并
    builder.add_conditional_edges(
        "ingest",
        lambda state: "extract" if state.get("document") is not None else _next_or_merge(state),
        {"extract": "extract", "ingest": "ingest", "merge": "merge"},
    )
    # 提取成功才进入校准
    builder.add_conditional_edges(
        "extract",
        lambda state: "verify" if state.get("extracted") else _next_or_merge(state),
        {"verify": "verify", "ingest": "ingest", "merge": "merge"},
    )
    builder.add_conditional_edges(
        "verify",
        lambda state: _next_or_merge(state),
        {"ingest": "ingest", "merge": "merge"},
    )
    builder.add_edge("merge", END)
    return builder.compile()


def get_guide_fusion_graph():
    global _guide_fusion_graph
    if _guide_fusion_graph is None:
        _guide_fusion_graph = build_guide_fusion_graph()
    return _guide_fusion_graph


def run_guide_fusion(
    sources: Sequence[GuideInput],
    city_hint: str = "",
    source_ids: Optional[Sequence[str]] = None,
    progress_callback: ProgressCallback = None,
) -> tuple[CandidatePool, list[GuideParseResult]]:
    """同步执行攻略融合前半图，返回（候选池, 每篇结果）。

    单篇攻略的任何失败都被捕获为 failed 结果，调用方按 source_id 单独重试。
    """
    state = GuideFusionState(
        sources=list(sources),
        city_hint=city_hint,
        source_ids=list(source_ids or []),
        source_index=0,
        results=[],
    )
    config = {"configurable": {"progress_callback": progress_callback}}
    final_state = get_guide_fusion_graph().invoke(state, config=config)
    pool = final_state.get("pool") or CandidatePool()
    results = final_state.get("results", [])
    # 失败结果补齐城市信息（失败时 LLM 城市推断不可用）
    for result in results:
        if not result.city:
            result.city = city_hint
    return pool, results
