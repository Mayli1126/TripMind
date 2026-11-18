"""百度智能搜索生成高性能版：固定接口、持久缓存、每日限额和跨进程限速。

接口文档：https://cloud.baidu.com/doc/qianfan-api/s/bmwbqlazc
优先 web_summary；仅在本项目高性能版当日额度用完时切换普通版 chat/completions。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests

from ..config import get_settings
from ..infrastructure.database import application_database
from ..infrastructure.task_trace import emit


class SearchUnavailable(RuntimeError):
    """只携带可展示的固定原因，禁止把响应、请求头或 Key 拼入错误。"""


def safe_source_url(value: str) -> bool:
    """只保存网页地址；应用不主动抓取结果 URL，避免执行网页中的内容。"""
    try:
        parsed = urlsplit(value)
        return parsed.scheme in {"https", "http"} and bool(parsed.hostname) and not parsed.username
    except ValueError:
        return False


class SearchBudgetExhausted(SearchUnavailable):
    """本地额度用完才允许切换，网络、鉴权或格式错误不扩大调用次数。"""


class BaiduSearchService:
    endpoint = "https://qianfan.baidubce.com/v2/ai_search/web_summary"
    standard_endpoint = "https://qianfan.baidubce.com/v2/ai_search/chat/completions"

    def _providers(self):
        normal_enabled = getattr(self.settings, "baidu_search_standard_enabled", True)
        model = getattr(self.settings, "baidu_search_standard_model", "deepseek-v4-flash").strip()
        return [dict(id="high_performance", label="智能搜索生成高性能版", endpoint=self.endpoint,
                     daily_limit=self.settings.baidu_search_daily_limit, model="non_thinking", enabled=True),
                dict(id="standard", label="智能搜索生成普通版", endpoint=self.standard_endpoint,
                     daily_limit=getattr(self.settings, "baidu_search_standard_daily_limit", 90),
                     model=model, enabled=normal_enabled and bool(model))]

    def __init__(self, settings=None, database=None):
        self.settings = settings or get_settings()
        self.database = database or application_database

    @property
    def configured(self) -> bool:
        return self.settings.baidu_search_enabled and bool(self.settings.baidu_search_api_key.strip())

    def _prepare(self, db):
        # 与行程数据库共用持久卷；重启或切换工作进程不会清空当日额度。
        db.execute("CREATE TABLE IF NOT EXISTS search_web_cache (key TEXT PRIMARY KEY, expires REAL NOT NULL, payload TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS search_web_budget (day TEXT PRIMARY KEY, used INTEGER NOT NULL, last_at REAL NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS search_provider_budget (day TEXT NOT NULL, provider TEXT NOT NULL, used INTEGER NOT NULL, last_at REAL NOT NULL, PRIMARY KEY(day,provider))")

    def _used(self, db, day, provider):
        """旧版已用次数归入高性能版，升级不能把今日计数清零。"""
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='search_provider_budget'").fetchone()
        row = db.execute("SELECT used,last_at FROM search_provider_budget WHERE day=? AND provider=?", (day, provider)).fetchone() if exists else None
        if row is None and provider == "high_performance":
            legacy = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='search_web_budget'").fetchone()
            row = db.execute("SELECT used,last_at FROM search_web_budget WHERE day=?", (day,)).fetchone() if legacy else None
        return (int(row[0]), float(row[1])) if row else (0, 0)

    def quota_status(self) -> dict:
        """读取两个版本的本项目额度；不请求百度账号余额接口。"""
        day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        providers = self._providers()
        with self.database.connection() as db:
            for provider in providers:
                used, _ = self._used(db, day, provider["id"])
                provider.update(used=used, remaining=max(0, provider["daily_limit"] - used))
        active = next((p["id"] for p in providers if p["enabled"] and p["remaining"] > 0), None) if self.configured else None
        enabled = [p for p in providers if p["enabled"]]
        return {"provider": "baidu_search_auto", "configured": self.configured,
                "date": day, "timezone": "Asia/Shanghai", "active_provider": active,
                "daily_limit": sum(p["daily_limit"] for p in enabled),
                "used": sum(p["used"] for p in providers),
                "remaining": sum(p["remaining"] for p in enabled), "providers": providers,
                "scope": "this_project_database", "provider_remaining": None,
                "note": "本项目保守计数，缓存不扣次数；高性能版用完切普通版，普通版模型费用另算。百度账号实际额度请查控制台。"}

    def generate_itinerary(self, query: str, instruction: str) -> dict:
        """仅在本地额度不足时切换版本；其他错误不重复消费搜索额度。"""
        for provider in self._providers():
            if not provider["enabled"]:
                continue
            try:
                return self._generate_with_provider(query, instruction, provider)
            except SearchBudgetExhausted:
                continue
        raise SearchUnavailable("今日两个搜索版本的可用次数均已用完，请明天再试")

    def _generate_with_provider(self, query: str, instruction: str, provider: dict) -> dict:
        """每次请求的选路、额度预占和共享限速均在事务内完成。"""
        if not self.configured:
            raise SearchUnavailable("百度搜索尚未配置 Key 或已关闭")
        query = query.strip()[:12000]
        if not query:
            raise SearchUnavailable("搜索内容为空")
        body = {"messages": [{"role": "user", "content": query}],
                "instruction": instruction, "stream": False, "model": "non_thinking",
                "response_format": {"type": "json_object"},
                "resource_type_filter": [{"type": "web", "top_k": 20}]}
        if provider["id"] == "standard":
            # 普通版必须指定千帆模型名；关闭深搜索，防止单次请求触发多次计费搜索。
            body.update(model=provider["model"], search_source="baidu_search_v2",
                        enable_deep_search=False, enable_corner_markers=False,
                        enable_followup_queries=False, max_completion_tokens=8192,
                        resource_type_filter=[{"type": "web", "top_k": 10}])
            body.pop("response_format", None)
        prefix = "baidu-summary-v1:" if provider["id"] == "high_performance" else "baidu-standard-v1:"
        operation = "web_summary" if provider["id"] == "high_performance" else "chat_completions"
        key = hashlib.sha256((prefix + json.dumps(body, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()
        now = time.time()
        day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        with self.database.connection() as db:
            self._prepare(db)
            db.execute("BEGIN IMMEDIATE")
            cached = db.execute("SELECT payload FROM search_web_cache WHERE key=? AND expires>?", (key, now)).fetchone()
            if cached:
                emit("baidu_search", operation, "completed", hit=True)
                return json.loads(cached[0])
            used, last = self._used(db, day, provider["id"])
            if used >= provider["daily_limit"]:
                raise SearchBudgetExhausted("该版本已达到本项目每日上限")
            # 两个版本共用限速，避免切换瞬间形成突发请求。
            latest = db.execute("SELECT MAX(last_at) FROM search_provider_budget WHERE day=?", (day,)).fetchone()
            scheduled = max(now, last + .4, (latest[0] or 0) + .4)
            if scheduled - now > self.settings.baidu_search_timeout_seconds:
                raise SearchUnavailable("搜索请求较多，请稍后重试")
            db.execute("INSERT INTO search_provider_budget(day,provider,used,last_at) VALUES(?,?,?,?) ON CONFLICT(day,provider) DO UPDATE SET used=excluded.used,last_at=excluded.last_at", (day, provider["id"], used + 1, scheduled))
        # 先释放数据库事务再等待/请求，避免在网络期间锁住账号和行程写入。
        delay = scheduled - time.time()
        if delay > 0:
            time.sleep(delay)
        started = time.perf_counter()
        try:
            response = requests.post(provider["endpoint"],
                headers={"Authorization": "Bearer " + self.settings.baidu_search_api_key.strip()},
                json=body,
                timeout=(4, self.settings.baidu_search_timeout_seconds), allow_redirects=False)
            if response.status_code != 200:
                reason = {401: "百度搜索 Key 无效", 403: "百度搜索权限未开通", 429: "百度搜索额度或频率受限"}.get(response.status_code, "百度搜索服务暂不可用")
                raise SearchUnavailable(reason)
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("code") not in (None, "", 0, "0"):
                raise SearchUnavailable("百度搜索返回业务错误，请检查控制台权限和额度")
            references = payload.get("references")
            if not isinstance(references, list):
                raise SearchUnavailable("百度搜索响应缺少网页结果")
            results = []
            for item in references[:20]:
                if not isinstance(item, dict) or item.get("type", "web") != "web":
                    continue
                url = str(item.get("url") or "")
                if not safe_source_url(url):
                    continue
                results.append({"id": item.get("id"), "url": url, "title": str(item.get("title") or "")[:200],
                                "content": str(item.get("content") or item.get("snippet") or "")[:4000]})
            if not results:
                raise SearchUnavailable("百度智能搜索生成未返回可追溯网页资料")
            choices = payload.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise SearchUnavailable("百度智能搜索生成未返回行程草案")
            choice = choices[0]
            if choice.get("finish_reason") in {"length", "content_filter"}:
                raise SearchUnavailable("百度智能搜索生成输出未完成")
            message = choice.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, str) or not content.strip() or len(content) > 120000:
                raise SearchUnavailable("百度智能搜索生成草案格式异常")
            normalized = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
            draft = json.loads(normalized)
            if not isinstance(draft, dict) or not isinstance(draft.get("places"), list):
                raise SearchUnavailable("百度智能搜索生成未返回约定的行程 JSON")
            result = {"content": content, "references": results}
        except SearchUnavailable:
            emit("baidu_search", operation, "failed", duration_ms=int((time.perf_counter() - started) * 1000))
            raise
        except (requests.RequestException, ValueError):
            emit("baidu_search", operation, "failed", duration_ms=int((time.perf_counter() - started) * 1000))
            raise SearchUnavailable("百度搜索网络超时或响应异常，请稍后重试") from None
        with self.database.connection() as db:
            db.execute("DELETE FROM search_web_cache WHERE expires<=?", (now,))
            db.execute("INSERT OR REPLACE INTO search_web_cache VALUES(?,?,?)", (key, time.time() + self.settings.baidu_search_cache_hours * 3600, json.dumps(result, ensure_ascii=False)))
        emit("baidu_search", operation, "completed", hit=False, duration_ms=int((time.perf_counter() - started) * 1000))
        return result
