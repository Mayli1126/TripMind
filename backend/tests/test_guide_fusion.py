"""阶段 2 前置测试：GuideFusionGraph 前半 + CandidatePool（产品化计划 7.2/12.4）。

核心验收：
- 攻略只提供推荐理由，名称/坐标必须回流高德校准（poi_id 为准，查不到不伪造坐标）；
- CandidatePool 合并去重：poi_id 优先，归一化名称兜底，字段并集、事实状态只升不降；
- 单篇攻略解析失败不影响其他来源，失败可单独重试；
- 解析结果与事实落 SQLite（guide_sources/guide_facts）。
"""

import tempfile
from pathlib import Path
from unittest import mock
from unittest import TestCase

from fastapi import HTTPException
from httpx import ConnectError, Request as HttpxRequest, Response as HttpxResponse

from app.api.routes import guide as guide_routes
from app.graphs.guide_fusion_graph import run_guide_fusion
from app.models.candidate import CandidatePlace
from app.models.guide import ExtractedPlace, GuideDocument, GuideFact, GuideInput, GuideParseResult
from app.services.candidate_pool_service import CandidatePool, normalize_name
from app.services.guide_extract_service import extract_places, verify_extracted_place
from app.services.guide_provider import (
    GuideFetchError,
    TextGuideProvider,
    UrlGuideProvider,
    fetch_guide_document,
)
from app.services.guide_store_service import GuideStoreService
from app.models.schemas import Location, POIInfo


# ---------------------------------------------------------------------------
# 通用构造
# ---------------------------------------------------------------------------

def _candidate(name: str, poi_id=None, city="北京", **kwargs) -> CandidatePlace:
    return CandidatePlace(
        candidate_id=f"tmp-{name}",
        canonical_name=name,
        city=city,
        poi_id=poi_id,
        location=Location(longitude=116.4, latitude=39.9) if poi_id else None,
        fact_status="verified" if poi_id else "unverified",
        **kwargs,
    )


def _poi(name: str, poi_id="B000TEST1") -> POIInfo:
    return POIInfo(
        id=poi_id, name=name, type="风景名胜",
        address="测试地址", location=Location(longitude=116.39, latitude=39.91),
    )


class _StubLLM:
    """固定 JSON 输出的 LLM 替身。"""

    def __init__(self, payload: str) -> None:
        self.payload = payload

    def invoke(self, messages, **kwargs) -> str:
        return self.payload


# ---------------------------------------------------------------------------
# CandidatePool
# ---------------------------------------------------------------------------

class CandidatePoolTestCase(TestCase):
    def test_merges_by_poi_id(self):
        pool = CandidatePool()
        first = _candidate("故宫", poi_id="B000A1", recommendation_reasons=["攻略A推荐"], confidence=0.6)
        second = _candidate("故宫博物院", poi_id="B000A1", recommendation_reasons=["攻略B推荐"], confidence=0.9)
        cid1, merged1 = pool.add_or_merge(first)
        cid2, merged2 = pool.add_or_merge(second)
        self.assertFalse(merged1)
        self.assertTrue(merged2)
        self.assertEqual(cid1, cid2)
        merged = pool.get(cid1)
        # poi_id 命中即合并：理由并集、置信度取最大、先到名称保留
        self.assertEqual(merged.recommendation_reasons, ["攻略A推荐", "攻略B推荐"])
        self.assertEqual(merged.confidence, 0.9)
        self.assertEqual(merged.canonical_name, "故宫")
        self.assertEqual(len(pool), 1)

    def test_merges_by_normalized_name_without_poi(self):
        pool = CandidatePool()
        pool.add_or_merge(_candidate("南 锣鼓巷。"))
        _, merged = pool.add_or_merge(_candidate("南锣鼓巷"))
        self.assertTrue(merged)
        self.assertEqual(len(pool), 1)

    def test_merges_name_variants_containment(self):
        # 攻略常见变体：前缀"古"、后缀"景区/步行街"——应归并为同一地点
        pool = CandidatePool()
        pool.add_or_merge(_candidate("古鸡鸣寺", poi_id="B000J1"))
        _, merged = pool.add_or_merge(_candidate("鸡鸣寺", poi_id="B000J1"))
        self.assertTrue(merged)
        self.assertEqual(len(pool), 1)

        pool2 = CandidatePool()
        pool2.add_or_merge(_candidate("玄武湖", poi_id="B000J2"))
        pool2.add_or_merge(_candidate("玄武湖景区", poi_id="B000J3"))  # 不同 poi 但名称包含
        self.assertEqual(len(pool2), 1)
        pool2.add_or_merge(_candidate("老门东", poi_id="B000J4"))
        _, m = pool2.add_or_merge(_candidate("老门东-步行街", poi_id="B000J5"))
        self.assertTrue(m)
        self.assertEqual(len(pool2), 2)  # 老门东并入后共 2 个地点

    def test_transport_station_not_merged_into_place(self):
        # "南锣鼓巷" 不应被并进 "南锣鼓巷地铁站"（反之亦然）
        pool = CandidatePool()
        pool.add_or_merge(_candidate("南锣鼓巷", poi_id="B000J6"))
        pool.add_or_merge(_candidate("南锣鼓巷地铁站", poi_id="B000J7"))
        self.assertEqual(len(pool), 2)

    def test_variant_name_becomes_alias_after_merge(self):
        pool = CandidatePool()
        cid, _ = pool.add_or_merge(_candidate("古鸡鸣寺", poi_id="B000J8"))
        pool.add_or_merge(_candidate("鸡鸣寺", poi_id="B000J8"))
        merged = pool.get(cid)
        self.assertEqual(merged.canonical_name, "古鸡鸣寺")
        self.assertIn("鸡鸣寺", merged.aliases)

    def test_normalize_name(self):
        self.assertEqual(normalize_name("ＡＢＣ 故宫（博物院）"), "abc故宫博物院")

    def test_fact_status_only_upgrades(self):
        pool = CandidatePool()
        pool.add_or_merge(_candidate("颐和园"))  # unverified
        cid, did_merge = pool.add_or_merge(_candidate("颐和园", poi_id="B000A2"))
        self.assertTrue(did_merge)
        place = pool.get(cid)
        self.assertEqual(place.fact_status, "verified")
        self.assertIsNotNone(place.location)
        self.assertEqual(place.poi_id, "B000A2")
        # 已验证后不再被未验证结果降级
        pool.add_or_merge(_candidate("颐和园"))
        self.assertEqual(pool.get(cid).fact_status, "verified")

    def test_user_choice_not_overwritten_by_merge(self):
        pool = CandidatePool()
        cid, _ = pool.add_or_merge(_candidate("天坛", poi_id="B000A3"))
        pool.set_user_choice(cid, "excluded")
        pool.add_or_merge(_candidate("天坛", poi_id="B000A3"))
        self.assertEqual(pool.get(cid).user_choice, "excluded")
        self.assertEqual(pool.stats()["excluded"], 1)

    def test_sorted_verified_first(self):
        pool = CandidatePool()
        pool.add_or_merge(_candidate("未验证景点"))
        pool.add_or_merge(_candidate("已验证景点", poi_id="B000A4"))
        names = [p.canonical_name for p in pool.to_list()]
        self.assertEqual(names, ["已验证景点", "未验证景点"])


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class ProviderTestCase(TestCase):
    def test_text_provider_passthrough_and_truncate(self):
        provider = TextGuideProvider()
        doc = provider.fetch(GuideInput(input_type="text", content="  第一天去故宫  ", title="北京一日"))
        self.assertEqual(doc.content, "第一天去故宫")
        self.assertFalse(doc.truncated)

        long_doc = provider.fetch(GuideInput(input_type="text", content="字" * 30000))
        self.assertTrue(long_doc.truncated)
        self.assertEqual(len(long_doc.content), 20000)

    def test_url_provider_extracts_text(self):
        html = """<html><head><title>北京攻略 - 游记</title>
        <style>body{color:red}</style></head>
        <body><script>var x=1;</script>
        <h1>故宫</h1><p>需要提前预约。</p><div>第二天去颐和园。</div></body></html>"""
        response = HttpxResponse(200, text=html, request=HttpxRequest("GET", "https://example.com/guide"))
        with mock.patch.object(UrlGuideProvider, "USER_AGENT", UrlGuideProvider.USER_AGENT), \
             mock.patch("app.services.guide_provider.httpx.get", return_value=response):
            doc = UrlGuideProvider().fetch(GuideInput(input_type="url", source_url="https://example.com/guide"))

        self.assertEqual(doc.title, "北京攻略 - 游记")
        self.assertIn("故宫", doc.content)
        self.assertIn("需要提前预约", doc.content)
        self.assertNotIn("var x=1", doc.content)
        self.assertNotIn("color:red", doc.content)
        self.assertNotIn("<p>", doc.content)

    def test_url_provider_network_error_raises_friendly(self):
        with mock.patch("app.services.guide_provider.httpx.get", side_effect=ConnectError("拒绝连接")):
            with self.assertRaises(GuideFetchError):
                UrlGuideProvider().fetch(GuideInput(input_type="url", source_url="https://example.com/x"))

    def test_spa_shell_page_gets_specific_hint(self):
        # 小红书等单页应用：HTTP 200 但正文全靠 JS 渲染，静态抓取只剩空壳
        shell = ('<html><head><title>小红书</title></head><body><div id="app"></div>'
                 + "<script>" + "var x=1;" * 3000 + "</script></body></html>")
        response = HttpxResponse(200, text=shell, request=HttpxRequest("GET", "https://www.xiaohongshu.com/x"))
        with mock.patch("app.services.guide_provider.httpx.get", return_value=response):
            with self.assertRaises(GuideFetchError) as ctx:
                UrlGuideProvider().fetch(GuideInput(input_type="url", source_url="https://www.xiaohongshu.com/x"))
        self.assertIn("浏览器渲染", str(ctx.exception))
        self.assertIn("粘贴", str(ctx.exception))

    def test_dispatch_unknown_type(self):
        source = GuideInput(input_type="text", content="内容")
        self.assertTrue(TextGuideProvider().can_handle(source))
        self.assertFalse(UrlGuideProvider().can_handle(source))
        doc = fetch_guide_document(source)
        self.assertEqual(doc.content, "内容")


class ImageGuideTestCase(TestCase):
    """截图导入：契约校验 + Provider 透传 + 图链路（视觉提取 mock）。"""

    def test_image_input_contract(self):
        # 缺图被拒
        with self.assertRaises(ValueError):
            GuideInput(input_type="image")
        # 非 image mime 被拒
        with self.assertRaises(ValueError):
            GuideInput(input_type="image", image_data="abc", image_mime="video/mp4")
        # 合法
        ok = GuideInput(input_type="image", image_data="abc123", image_mime="image/png")
        self.assertEqual(ok.display_title(), "攻略截图")

    def test_image_provider_passthrough(self):
        from app.services.guide_provider import ImageGuideProvider

        provider = ImageGuideProvider()
        source = GuideInput(input_type="image", image_data="abc", image_mime="image/png")
        self.assertTrue(provider.can_handle(source))
        doc = provider.fetch(source)  # 不调 LLM，内容留空给视觉阶段
        self.assertEqual(doc.content, "")

    def test_graph_image_branch_uses_vision(self):
        """图链路：image 来源走 extract_places_from_image（视觉），不走文本提取。"""
        source = GuideInput(input_type="image", image_data="aGk=", image_mime="image/png", city="北京")
        with mock.patch("app.graphs.guide_fusion_graph.extract_places_from_image") as fake_vision, \
             mock.patch("app.graphs.guide_fusion_graph.extract_places") as fake_text, \
             mock.patch("app.services.guide_extract_service._search_poi", return_value=_poi("故宫博物院", "B000A8")):
            fake_vision.return_value = ("北京", "第一天 故宫 需预约", [
                ExtractedPlace(name="故宫", recommendation_reasons=["截图推荐"], confidence=0.9),
            ])
            pool, results = run_guide_fusion([source], city_hint="北京", source_ids=["img1"])
        fake_vision.assert_called_once()
        fake_text.assert_not_called()  # 文本提取路径未被触碰
        self.assertEqual(results[0].parse_status, "parsed")
        self.assertEqual(results[0].document_chars, len("第一天 故宫 需预约"))  # 转写回填
        candidate = pool.to_list()[0]
        self.assertEqual(candidate.canonical_name, "故宫博物院")  # 高德校准生效
        self.assertTrue(candidate.is_usable_for_routing())

    def test_vision_not_configured_friendly_failure(self):
        from app.services import guide_extract_service as ges

        source = GuideInput(input_type="image", image_data="aGk=", image_mime="image/png")
        with mock.patch.object(
            ges, "_get_vision_llm",
            side_effect=ges.VisionModelNotConfigured("未配置 LLM_VISION_MODEL"),
        ):
            pool, results = run_guide_fusion([source], city_hint="北京")
        self.assertEqual(results[0].parse_status, "failed")
        self.assertIn("LLM_VISION_MODEL", results[0].error_message)
        self.assertEqual(len(pool), 0)


# ---------------------------------------------------------------------------
# LLM 提取
# ---------------------------------------------------------------------------

class ExtractPlacesTestCase(TestCase):
    def test_parses_llm_json_and_fills_defaults(self):
        payload = """```json
        {"city": "北京", "places": [
            {"name": "故宫", "aliases": ["紫禁城"], "category": "museum",
             "suggested_duration_minutes": 180,
             "recommendation_reasons": ["攻略重点推荐"], "warnings": ["需提前预约"], "confidence": 0.9},
            {"name": "无名地点", "category": "不存在的类别"}
        ]}
        ```"""
        city, places = extract_places(
            GuideDocument(content="正文"), llm=_StubLLM(payload)
        )
        self.assertEqual(city, "北京")
        self.assertEqual(len(places), 2)
        self.assertEqual(places[0].category, "museum")
        self.assertEqual(places[0].suggested_duration_minutes, 180)
        # 非法类别回退 attraction，缺失字段用默认值
        self.assertEqual(places[1].category, "attraction")
        self.assertEqual(places[1].confidence, 0.5)

    def test_skips_entries_without_name(self):
        payload = '{"city": "北京", "places": [{"name": ""}, {"name": "天坛"}]}'

        _, places = extract_places(GuideDocument(content="x"), llm=_StubLLM(payload))
        self.assertEqual([p.name for p in places], ["天坛"])

    def test_raises_when_no_places(self):

        with self.assertRaises(ValueError):
            extract_places(GuideDocument(content="x"), llm=_StubLLM('{"city": "北京", "places": []}'))


# ---------------------------------------------------------------------------
# 高德校准
# ---------------------------------------------------------------------------

class VerifyPlaceTestCase(TestCase):
    def test_verified_place_gets_map_facts(self):
        with mock.patch("app.services.guide_extract_service._search_poi", return_value=_poi("故宫博物院", "B000A8")):
            candidate = verify_extracted_place(
                ExtractedPlace(
                    name="故宫", aliases=["紫禁城"], recommendation_reasons=["必去"], confidence=0.8,
                ),
                city="北京", source_ref="guide:g1",
            )
        self.assertTrue(candidate.is_usable_for_routing())
        self.assertEqual(candidate.canonical_name, "故宫博物院")  # 地图名称为准
        self.assertEqual(candidate.poi_id, "B000A8")
        self.assertIn("故宫", candidate.aliases)  # 攻略原名降级为别名
        self.assertEqual(candidate.source_refs, ["guide:g1"])

    def test_unverified_place_keeps_no_coordinates(self):
        with mock.patch("app.services.guide_extract_service._search_poi", return_value=None):
            candidate = verify_extracted_place(
                ExtractedPlace(name="不存在的店"),
                city="北京", source_ref="guide:g1",
            )
        self.assertFalse(candidate.is_usable_for_routing())
        self.assertIsNone(candidate.location)
        self.assertIsNone(candidate.poi_id)
        self.assertEqual(candidate.fact_status, "unverified")


# ---------------------------------------------------------------------------
# GuideFusionGraph 前半
# ---------------------------------------------------------------------------

class GuideFusionGraphTestCase(TestCase):
    def _run(self, sources, city="北京", source_ids=None):
        with mock.patch("app.graphs.guide_fusion_graph.extract_places") as fake_extract, \
             mock.patch("app.services.guide_extract_service._search_poi", return_value=_poi("故宫博物院", "B000A8")):
            fake_extract.return_value = ("北京", [
                ExtractedPlace(name="故宫", confidence=0.8),
            ])
            events = []
            pool, results = run_guide_fusion(
                sources, city_hint=city, source_ids=source_ids,
                progress_callback=lambda p, s, m: events.append((p, s)),
            )
        return pool, results, events

    def test_text_source_full_pipeline(self):
        source = GuideInput(input_type="text", content="第一天去故宫", city="北京")
        pool, results, events = self._run([source], source_ids=["g1"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].parse_status, "parsed")
        self.assertEqual(results[0].source_id, "g1")
        # 候选带攻略来源引用
        candidate = pool.to_list()[0]
        self.assertEqual(candidate.source_refs, ["guide:g1"])
        self.assertTrue(candidate.is_usable_for_routing())
        # 进度回调有事件
        stages = [s for _, s in events]
        self.assertIn("ingesting", stages)
        self.assertIn("verifying", stages)
        self.assertIn("merged", stages)

    def test_multi_source_merge_dedupes_by_poi(self):
        sources = [
            GuideInput(input_type="text", content="攻略A：故宫", city="北京"),
            GuideInput(input_type="text", content="攻略B：故宫博物院", city="北京"),
        ]

        def extract_side_effect(document, hint):
            name = "故宫" if "攻略A" in document.content else "故宫博物院"
            return "北京", [ExtractedPlace(
                name=name, recommendation_reasons=[f"{name}来自本篇"], confidence=0.7,
            )]

        with mock.patch("app.graphs.guide_fusion_graph.extract_places", side_effect=extract_side_effect), \
             mock.patch("app.services.guide_extract_service._search_poi", return_value=_poi("故宫博物院", "B000A8")):
            pool, results = run_guide_fusion(sources, city_hint="北京", source_ids=["gA", "gB"])
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.parse_status == "parsed" for r in results))
        # 同一 POI 跨来源去重：池内 1 个，来源引用 2 条
        merged = pool.to_list()
        self.assertEqual(len(merged), 1)
        self.assertEqual(len(merged[0].source_refs), 2)
        self.assertEqual(merged[0].recommendation_reasons, ["故宫来自本篇", "故宫博物院来自本篇"])

    def test_single_failure_isolated_with_hint(self):
        sources = [
            GuideInput(input_type="url", source_url="https://bad.example.com/x", city="北京"),
            GuideInput(input_type="text", content="第一天去故宫", city="北京"),
        ]

        real_dispatch = fetch_guide_document

        def dispatch(source):
            if source.input_type == "url":
                raise GuideFetchError("链接读取失败")
            return TextGuideProvider().fetch(source)

        with mock.patch("app.graphs.guide_fusion_graph.fetch_guide_document", side_effect=dispatch), \
             mock.patch("app.graphs.guide_fusion_graph.extract_places") as fake_extract, \
             mock.patch("app.services.guide_extract_service._search_poi", return_value=_poi("故宫博物院", "B000A8")):
            fake_extract.return_value = ("北京", [
                ExtractedPlace(name="故宫"),
            ])
            pool, results = run_guide_fusion(sources, city_hint="北京")

        statuses = [r.parse_status for r in results]
        self.assertEqual(statuses, ["failed", "parsed"])  # 单篇失败不影响下一篇
        self.assertIn("正文", results[0].error_hint)  # 引导用户改用粘贴正文
        self.assertEqual(len(pool), 1)

    def test_extract_failure_recorded_as_failed(self):
        source = GuideInput(input_type="text", content="乱码正文", city="北京")
        with mock.patch("app.graphs.guide_fusion_graph.extract_places", side_effect=ValueError("攻略中未提取到任何可前往的地点")):
            pool, results = run_guide_fusion([source], city_hint="北京")
        self.assertEqual(results[0].parse_status, "failed")
        self.assertIn("未提取到", results[0].error_message)
        self.assertEqual(len(pool), 0)


# ---------------------------------------------------------------------------
# 存储 + API
# ---------------------------------------------------------------------------

class GuideStoreAndApiTestCase(TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = GuideStoreService(database_path=Path(self._tmp.name) / "test.db")
        from app.services.auth_service import AuthService
        AuthService(self.store.database_path).initialize()
        # 换成临时库，避免污染真实 tripmind.db
        self._patcher = mock.patch("app.api.routes.guide.guide_store_service", self.store)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmp.cleanup()

    def test_independent_writers_merge_candidate_choices(self):
        from concurrent.futures import ThreadPoolExecutor
        second = GuideStoreService(self.store.database_path)
        record = self.store.create_record(GuideInput(input_type='text', content='参观故宫与公园'))
        result = GuideParseResult(source_id=record.id, parse_status='parsed',
                                 candidates=[_candidate('故宫'), _candidate('公园')])
        self.store.save_result(record.id, result)
        choices = [(self.store, {'tmp-故宫': 'required'}), (second, {'tmp-公园': 'excluded'})]
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda pair: pair[0].update_candidate_choices(record.id, pair[1]), choices))
        saved = self.store.get_record(record.id)
        self.assertEqual({c.canonical_name: c.user_choice for c in saved.result.candidates},
                         {'故宫': 'required', '公园': 'excluded'})

    def test_deleted_source_not_resurrected_by_late_parse(self):
        record = self.store.create_record(GuideInput(input_type='text', content='参观故宫'))
        self.store.delete_record(record.id)
        with self.assertRaises(KeyError):
            self.store.save_result(record.id, GuideParseResult(source_id=record.id, parse_status='parsed'))
        self.assertIsNone(self.store.get_record(record.id))

    def test_parse_lease_excludes_other_instance_and_rejects_stale_owner(self):
        second = GuideStoreService(self.store.database_path)
        record = self.store.create_record(GuideInput(input_type='text', content='参观故宫'))
        token = self.store.begin_parse(record.id)
        with self.assertRaises(ValueError):
            second.begin_parse(record.id)
        with self.store._connection() as db:
            db.execute('UPDATE guide_parse_leases SET expires_at=0 WHERE source_id=?', (record.id,))
        replacement = second.begin_parse(record.id)
        result = GuideParseResult(source_id=record.id, parse_status='parsed')
        with self.assertRaises(ValueError):
            self.store.save_result(record.id, result, parse_token=token)
        self.assertEqual(second.save_result(record.id, result, parse_token=replacement).parse_status, 'parsed')

    def test_parse_commit_keeps_choices_changed_during_provider_call(self):
        record = self.store.create_record(GuideInput(input_type='text', content='参观故宫'))
        result = GuideParseResult(source_id=record.id, parse_status='parsed',
                                 candidates=[_candidate('故宫', poi_id='same-poi')])
        self.store.save_result(record.id, result)
        token = self.store.begin_parse(record.id)
        second = GuideStoreService(self.store.database_path)
        second.update_candidate_choices(record.id, {'tmp-故宫': 'excluded'})
        result.candidates[0].candidate_id = 'new-parser-id'
        saved = self.store.save_result(record.id, result, parse_token=token)
        self.assertEqual(saved.result.candidates[0].user_choice, 'excluded')
        # Provider failure must not erase a previously successful result.
        token = self.store.begin_parse(record.id)
        saved = self.store.save_result(record.id, GuideParseResult(source_id=record.id, parse_status='failed'), parse_token=token)
        self.assertEqual(saved.result.candidates[0].user_choice, 'excluded')
        self.assertEqual(saved.parse_status, 'parsed')

    def test_parse_deleted_during_provider_not_resurrected(self):
        record = self.store.create_record(GuideInput(input_type='text', content='参观故宫'))
        token = self.store.begin_parse(record.id)
        self.store.delete_record(record.id)
        with self.assertRaises(KeyError):
            self.store.save_result(record.id, GuideParseResult(source_id=record.id, parse_status='parsed'), parse_token=token)

    def test_store_roundtrip_and_facts(self):
        source = GuideInput(input_type="text", content="第一天去故宫，需预约", title="北京攻略", city="北京")
        record = self.store.create_record(source)
        result = GuideParseResult(
            source_id=record.id, parse_status="parsed", title="北京攻略", city="北京",
            candidates=[_candidate("故宫", poi_id="B000A8")],
            facts=[GuideFact(
                fact_type="warning", subject="故宫", claim="需预约", confidence=0.8,
            )],
        )
        saved = self.store.save_result(record.id, result)
        self.assertEqual(saved.parse_status, "parsed")
        self.assertEqual(saved.result.candidates[0].canonical_name, "故宫")

        loaded = self.store.get_record(record.id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.result.city, "北京")
        self.assertEqual(len(self.store.list_records()), 1)

        # 事实表已写入
        import sqlite3
        with self.store._connection() as connection:
            rows = connection.execute(
                "SELECT fact_type, subject, claim_json FROM guide_facts WHERE source_id=?", (record.id,)
            ).fetchall()
        self.assertEqual(rows[0]["fact_type"], "warning")
        self.assertIn("需预约", rows[0]["claim_json"])

    def test_api_create_parses_and_saves(self):
        source = GuideInput(input_type="text", content="第一天去故宫", city="北京")
        parsed = GuideParseResult(
            source_id="占位", parse_status="parsed", city="北京",
            candidates=[_candidate("故宫", poi_id="B000A8")],
        )
        # 配额在 redis_enabled=false 时直接放行；用桩 Request 提供客户端标识
        stub_request = mock.Mock(headers={}, client=mock.Mock(host="127.0.0.1"))
        stub_request.state.user = {'id': 'fixture-user', 'role': 'user'}
        with mock.patch("app.api.routes.guide.run_guide_fusion", return_value=(CandidatePool(), [parsed])):
            record = guide_routes.create_guide_source(source, stub_request)
        self.assertEqual(record.parse_status, "parsed")
        self.assertEqual(record.id, parsed.source_id)  # 回填了占位 ID
        self.assertEqual(record.result.candidates[0].poi_id, "B000A8")

    def test_retry_text_source_rejected(self):
        source = GuideInput(input_type="text", content="正文", title="t")
        record = self.store.create_record(source)
        failed = GuideParseResult(source_id=record.id, parse_status="failed", error_message="x")
        self.store.save_result(record.id, failed)
        with self.assertRaises(HTTPException) as ctx:
            guide_routes.retry_guide_source(record.id)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_retry_url_source_reruns(self):
        source = GuideInput(input_type="url", source_url="https://example.com/g", title="链接攻略")
        record = self.store.create_record(source)
        failed = GuideParseResult(source_id=record.id, parse_status="failed", error_message="x")
        self.store.save_result(record.id, failed)
        parsed = GuideParseResult(source_id=record.id, parse_status="parsed", city="北京")
        with mock.patch("app.api.routes.guide.run_guide_fusion", return_value=(CandidatePool(), [parsed])) as runner:
            updated = guide_routes.retry_guide_source(record.id)
        self.assertEqual(updated.parse_status, "parsed")
        # 重试时用原始 URL 重建输入
        self.assertEqual(runner.call_args.args[0][0].source_url, "https://example.com/g")

    def test_delete_source_removes_record_and_facts(self):
        source = GuideInput(input_type="text", content="第一天去故宫", title="待删攻略", city="北京")
        record = self.store.create_record(source)
        result = GuideParseResult(
            source_id=record.id, parse_status="parsed", city="北京",
            candidates=[_candidate("故宫", poi_id="B000A8")],
            facts=[GuideFact(fact_type="warning", subject="故宫", claim="需预约")],
        )
        self.store.save_result(record.id, result)

        self.assertTrue(guide_routes.delete_guide_source(record.id))
        self.assertIsNone(self.store.get_record(record.id))
        # 事实表级联清除
        import sqlite3
        with self.store._connection() as connection:
            rows = connection.execute(
                "SELECT COUNT(*) AS n FROM guide_facts WHERE source_id=?", (record.id,)
            ).fetchone()
        self.assertEqual(rows["n"], 0)
        # 再删一次：404
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            guide_routes.delete_guide_source(record.id)
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    import unittest
    unittest.main()
