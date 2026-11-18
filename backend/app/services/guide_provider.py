"""攻略内容抓取 Provider（产品化计划 7.2 节）。

解析器采用 Provider 接口，未来可以接浏览器、第三方服务或合规 API。
首版实现：
- TextGuideProvider：用户粘贴正文直接透传；
- UrlGuideProvider：公开链接用 httpx 抓 HTML 后提取正文。

自动全站搜索不是首版依赖；链接读取失败时抛 GuideFetchError，
由上层引导用户"粘贴正文"（对应 7.2 节首版输入范围第 4 条）。
"""

from __future__ import annotations

import re
from html import unescape
from typing import Protocol

import httpx

from ..models.guide import GuideDocument, GuideInput

# 参与解析的正文上限：防超长攻略撑爆 token，截断会在文档上留标记
MAX_DOCUMENT_CHARS = 20000

# 常见网页骨架标签：整个剔除，避免脚本/样式混入正文
_STRIP_BLOCKS = re.compile(
    r"<(script|style|noscript|svg|iframe|head|footer|nav)\b[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
_BLOCK_TAGS = re.compile(
    r"</?(p|div|br|li|tr|h[1-6]|section|article|blockquote|table|ul|ol)[^>]*>",
    re.IGNORECASE,
)
_ANY_TAG = re.compile(r"<[^>]+>")


class GuideFetchError(Exception):
    """攻略内容抓取失败（网络错误/反爬/非公开页面）。"""


class GuideProvider(Protocol):
    """攻略内容来源接口（7.2 节 Provider 约定）。"""

    def can_handle(self, source: GuideInput) -> bool: ...

    def fetch(self, source: GuideInput) -> GuideDocument: ...


class TextGuideProvider:
    """正文粘贴：直接透传，仅做长度截断。"""

    def can_handle(self, source: GuideInput) -> bool:
        return source.input_type == "text"

    def fetch(self, source: GuideInput) -> GuideDocument:
        content = source.content.strip()
        truncated = len(content) > MAX_DOCUMENT_CHARS
        return GuideDocument(
            title=source.title.strip(),
            content=content[:MAX_DOCUMENT_CHARS],
            truncated=truncated,
        )


class UrlGuideProvider:
    """公开链接抓取：httpx 拉取 HTML 后做保守的正文提取。"""

    # 自定义 UA，注明用途；不做任何绕过反爬的伪装
    USER_AGENT = "TripMindGuideBot/0.1 (+travel-guide-import)"

    def can_handle(self, source: GuideInput) -> bool:
        return source.input_type == "url" and source.source_url.strip().startswith(
            ("http://", "https://")
        )

    def fetch(self, source: GuideInput) -> GuideDocument:
        try:
            response = httpx.get(
                source.source_url.strip(),
                headers={"User-Agent": self.USER_AGENT},
                timeout=20,
                follow_redirects=True,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise GuideFetchError(f"链接读取失败：{exc}") from exc

        html = response.text
        title = self._extract_title(html) or source.title.strip()
        content = self._html_to_text(html)
        if not content.strip():
            # SPA 空壳页（小红书/微博等）：HTML 只有 JS 挂载点，正文需浏览器渲染
            if self._looks_like_spa_shell(html):
                raise GuideFetchError(
                    "该页面内容需要浏览器渲染（小红书/微博等平台），暂不支持链接直接解析；"
                    "请复制攻略正文粘贴导入"
                )
            raise GuideFetchError("页面没有可解析的正文，请直接粘贴攻略文字内容")
        truncated = len(content) > MAX_DOCUMENT_CHARS
        return GuideDocument(
            title=title,
            content=content[:MAX_DOCUMENT_CHARS],
            truncated=truncated,
        )

    @staticmethod
    def _looks_like_spa_shell(html: str) -> bool:
        """识别单页应用空壳：有 JS 挂载点 + 脚本占比高，静态抓取注定拿不到正文。"""
        has_mount_point = 'id="app"' in html or "id='app'" in html or 'id="root"' in html
        script_chars = sum(len(m) for m in re.findall(r"<script\b[^>]*>.*?</script>", html, re.DOTALL | re.IGNORECASE))
        return has_mount_point and script_chars > len(html) * 0.5

    @staticmethod
    def _extract_title(html: str) -> str:
        match = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
        return unescape(match.group(1)).strip() if match else ""

    @staticmethod
    def _html_to_text(html: str) -> str:
        """保守的 HTML 转文本：剔除脚本/样式，块级标签转换行，其余标签删除。"""
        text = _STRIP_BLOCKS.sub(" ", html)
        text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
        text = _BLOCK_TAGS.sub("\n", text)
        text = _ANY_TAG.sub(" ", text)
        text = unescape(text)
        # 压缩空白：保留换行结构，行内多空格合一
        lines = [re.sub(r"[ \t\u3000]+", " ", line).strip() for line in text.splitlines()]
        return "\n".join(line for line in lines if line)


class ImageGuideProvider:
    """截图攻略：内容获取阶段只透传元信息；

    实际的文字转写与地点提取由视觉模型在 extract 阶段完成
    （见 guide_extract_service.extract_places_from_image），
    Provider 层不调 LLM，保持职责边界。
    """

    def can_handle(self, source: GuideInput) -> bool:
        return source.input_type == "image" and bool(source.image_data.strip())

    def fetch(self, source: GuideInput) -> GuideDocument:
        # content 留空：截图没有可直接提取的文本，extract 阶段由视觉模型转写后回填
        return GuideDocument(title=source.title.strip(), content="", truncated=False)


# 上游提供方 注册表：按声明顺序匹配第一个 can_handle 的实现
PROVIDERS: list[GuideProvider] = [
    TextGuideProvider(),
    UrlGuideProvider(),
    ImageGuideProvider(),
]


def fetch_guide_document(source: GuideInput) -> GuideDocument:
    """按输入类型分发到对应 Provider；无匹配实现时报错。"""
    for provider in PROVIDERS:
        if provider.can_handle(source):
            return provider.fetch(source)
    raise GuideFetchError(f"暂不支持该攻略输入类型：{source.input_type}")
