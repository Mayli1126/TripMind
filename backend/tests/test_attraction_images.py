from unittest import TestCase
from unittest.mock import AsyncMock, patch

from app.services import attraction_image_service as ais
from app.services.attraction_image_service import AttractionImageService


def _patch_sources(amap=None, wikipedia=None, commons=None, unsplash=None):
    """按来源隔离打桩：不依赖真实网络，行为完全确定。

    同时隔离 Redis 二级缓存：测试不读写真实 Redis，避免用例间串缓存。
    """
    patchers = [
        patch.object(AttractionImageService, "_async_from_amap", AsyncMock(return_value=amap)),
        patch.object(AttractionImageService, "_async_from_chinese_wikipedia", AsyncMock(return_value=wikipedia)),
        patch.object(AttractionImageService, "_async_from_commons", AsyncMock(return_value=commons)),
        patch.object(AttractionImageService, "_async_from_unsplash", AsyncMock(return_value=unsplash)),
        patch.object(ais, "_image_cache_read", lambda name, city: None),
        patch.object(ais, "_image_cache_write", lambda name, city, result: None),
    ]
    return patchers


class _PatchedSourcesTestCase(TestCase):
    def _run_with(self, **sources):
        patchers = _patch_sources(**sources)
        for patcher in patchers:
            patcher.start()
        try:
            # 清空 lru_cache，避免用例间串缓存
            AttractionImageService.get_attraction_image.cache_clear()
            return AttractionImageService().get_attraction_image("故宫博物院", "北京")
        finally:
            for patcher in patchers:
                patcher.stop()


class AttractionImageTests(_PatchedSourcesTestCase):
    def test_title_score_rejects_unrelated_stock_photo(self):
        service = AttractionImageService()
        self.assertLess(
            service._title_score("故宫博物院", "File:Forbidden City Beijing.jpg"),
            0.68,
        )
        self.assertEqual(
            service._title_score("故宫博物院", "File:故宫博物院午门.jpg"),
            1.0,
        )

    def test_exact_wikipedia_page_is_preferred(self):
        result = self._run_with(
            wikipedia={
                "photo_url": "https://upload.wikimedia.org/palace.jpg",
                "source": "Wikipedia",
                "source_url": "https://zh.wikipedia.org/wiki/故宫博物院",
                "attribution": "图片来自中文维基百科",
                "match_type": "exact_page",
            },
        )
        self.assertTrue(result["photo_url"].endswith("palace.jpg"))
        self.assertEqual(result["source"], "Wikipedia")
        self.assertEqual(result["match_type"], "exact_page")

    def test_amap_photo_is_first_choice_for_domestic(self):
        """国内首选高德 POI 实拍图：高德有图时不再访问境外图源。"""
        result = self._run_with(
            amap={
                "photo_url": "https://store.is.autonavi.com/showpic/palace",
                "source": "高德地图",
                "source_url": "",
                "attribution": "图片来自高德地图POI(故宫博物院)",
                "match_type": "title_match",
            },
            wikipedia={
                "photo_url": "https://upload.wikimedia.org/palace.jpg",
                "source": "Wikipedia",
                "match_type": "exact_page",
            },
        )
        self.assertEqual(result["source"], "高德地图")
        self.assertTrue(result["photo_url"].endswith("palace"))

    def test_returns_none_instead_of_unrelated_image(self):
        result = self._run_with()
        self.assertIsNone(result)

    def test_unsplash_is_last_fallback(self):
        result = self._run_with(
            unsplash={
                "photo_url": "https://images.unsplash.com/photo-xxx",
                "source": "Unsplash",
                "match_type": "keyword_search",
            },
        )
        self.assertEqual(result["source"], "Unsplash")
