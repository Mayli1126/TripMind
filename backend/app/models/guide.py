"""攻略导入数据契约（产品化计划 6.2/7.2/11.2 节）。

GuideFusionGraph 前半的输入与输出：
- GuideInput：用户提交的一篇攻略（正文粘贴 / 公开链接）；
- GuideDocument：抓取/整理后的纯文本，LLM 提取的唯一输入；
- ExtractedPlace：LLM 提取的"待校准地点"中间产物（只有名称与理由，没有坐标）；
- GuideFact：攻略中的单条结构化事实（对应 guide_facts 表）；
- GuideParseResult：单篇攻略的解析结果（候选列表 + 事实 + 状态）。

设计要点（与执行计划 7.2 一致）：
- LLM 只提取地点与推荐理由，名称/坐标一律回流高德校准；
- 解析失败的单篇攻略记录 parse_status=failed，可单独重试，不影响其他来源。
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from .candidate import CandidateCategory, CandidatePlace

# 攻略输入类型：text 正文粘贴 / url 公开链接 / image 截图（需视觉模型）
GuideInputType = Literal["text", "url", "image"]

# 解析状态机：pending(待解析) -> parsed(成功) / failed(失败)
GuideParseStatus = Literal["pending", "parsed", "failed"]


class GuideInput(BaseModel):
    """一篇攻略的原始输入。"""

    input_type: GuideInputType = "text"
    content: str = Field(default="", description="input_type=text 时的攻略正文")
    source_url: str = Field(default="", description="input_type=url 时的公开链接")
    # input_type=image 时的截图（base64，不含 data: 前缀；上限约 6MB 撑住高清截图）
    image_data: str = Field(default="", max_length=8_000_000, description="截图 base64")
    image_mime: str = Field(default="image/jpeg", description="截图 MIME 类型")
    title: str = Field(default="", description="攻略标题，可空；URL 抓取时自动取页面标题")
    city: str = Field(default="", description="目的地城市提示，用于高德校准；可空由 LLM 推断")

    @model_validator(mode="after")
    def _check_payload(self) -> "GuideInput":
        """按输入类型校验必填内容，尽早拦截空提交。"""
        if self.input_type == "text" and not self.content.strip():
            raise ValueError("文本攻略的正文不能为空")
        if self.input_type == "url":
            url = self.source_url.strip()
            if not url.startswith(("http://", "https://")):
                raise ValueError("攻略链接必须是 http(s) 地址")
        if self.input_type == "image":
            if not self.image_data.strip():
                raise ValueError("截图攻略必须提供图片")
            if self.image_mime.split("/")[0] != "image":
                raise ValueError("仅支持图片类型的截图")
        return self

    def display_title(self) -> str:
        """给用户看/存库用的标题兜底。"""
        if self.title.strip():
            return self.title.strip()
        if self.input_type == "url":
            url = self.source_url.strip()
            # 长 URL 截断显示，失败卡片不至于被链接刷屏
            return (url[:60] + "…") if len(url) > 60 else url
        if self.input_type == "image":
            return "攻略截图"
        text = self.content.strip()
        return (text[:30] + "…") if len(text) > 30 else text


class GuideDocument(BaseModel):
    """抓取/整理后的攻略纯文本。"""

    title: str = ""
    content: str = ""
    truncated: bool = Field(default=False, description="正文超长被截断时为 True")


class ExtractedPlace(BaseModel):
    """LLM 从攻略中提取的地点（未经地图校准，坐标与名称不可信）。

    攻略只提供推荐理由；此处绝不允许出现坐标字段，防止 LLM 编造位置。
    """

    name: str = Field(..., description="攻略中的地点名称")
    aliases: list[str] = Field(default_factory=list, description="攻略中的别称/简称")
    category: CandidateCategory = "attraction"
    suggested_duration_minutes: int = Field(default=90, ge=15, le=720)
    recommendation_reasons: list[str] = Field(default_factory=list, description="攻略给出的推荐理由")
    warnings: list[str] = Field(default_factory=list, description="攻略提示的风险：需预约/闭馆日/体力强度等")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class GuideFact(BaseModel):
    """攻略中的一条结构化事实（对应 11.2 节 guide_facts 表）。"""

    fact_type: str = Field(..., description="事实类型：reason/warning/duration/booking 等")
    subject: str = Field(..., description="事实主体（通常为地点名称）")
    claim: str = Field(..., description="事实内容")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class GuideParseResult(BaseModel):
    """单篇攻略的解析产物。"""

    source_id: str = ""
    parse_status: GuideParseStatus = "pending"
    error_message: str = ""
    title: str = ""
    city: str = Field(default="", description="LLM 推断或用户指定的城市")
    document_chars: int = Field(default=0, description="参与解析的正文长度")
    candidates: list[CandidatePlace] = Field(default_factory=list)
    facts: list[GuideFact] = Field(default_factory=list)
    error_hint: Optional[str] = Field(default=None, description="失败时给用户的补救提示")


class GuideSourceRecord(BaseModel):
    """guide_sources 表的完整记录（API 返回体）。"""

    id: str
    input_type: GuideInputType
    source_url: str = ""
    title: str = ""
    content_hash: str = ""
    parse_status: GuideParseStatus = "pending"
    error_message: str = ""
    created_at: str = ""
    result: Optional[GuideParseResult] = None
