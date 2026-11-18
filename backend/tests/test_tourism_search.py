"""联网选点的少量离线回归：替换 HTTP 与地图，不花真实接口额度。"""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.graphs import planning_graph as pg
from app.infrastructure.database import Database
from app.models.schemas import POIInfo, Location, TripRequest
from app.services.baidu_search_service import BaiduSearchService, SearchUnavailable
from app.services.day_pipeline_service import materialize_skeleton, parse_skeleton, build_day_capacities
from app.services.tourism_research_service import (
    TourismResearchError, research_tourism, enforce_researched_plan,
    evidence_mentions_place,
)


def request():
    day = (date.today() + timedelta(days=30)).isoformat()
    return TripRequest(city="北京", start_date=day, end_date=day, travel_days=1,
                       transportation="公共交通", accommodation="舒适型酒店")


def fixture():
    names = ["天坛公园", "故宫博物院", "景山公园"]
    places = [{"name": name, "source_id": i + 1, "quote": name + "是值得游览的景点",
               "reason": "经典文化景观", "duration_minutes": 90} for i, name in enumerate(names)]
    draft = {"places": places, "days": [{"date": request().start_date, "theme": "北京文化", "places": names}],
             "overall_suggestions": "注意休息"}
    references = [{"id": i + 1, "title": name, "content": places[i]["quote"],
                   "url": f"https://example.org/travel/{i}", "type": "web"} for i, name in enumerate(names)]
    pois = {name: [POIInfo(id=f"B{i}", name=name, type="风景名胜;公园", type_code="110101",
                          city_name="北京市", address="北京", location=Location(longitude=116.4, latitude=39.9))]
            for i, name in enumerate(names)}
    return draft, references, pois


class TourismSearchTests(unittest.TestCase):
    def test_evidence_can_use_alias_but_not_nearby_road(self):
        # 网页引用真实性检查仍在使用，不能随旧地点匹配算法一起删除。
        self.assertTrue(evidence_mentions_place("故宫博物院", "故宫是皇家宫殿", "北京"))
        self.assertFalse(evidence_mentions_place("天坛公园", "天坛东路沿街商铺", "北京"))


    def test_common_short_names_keep_real_poi_and_source(self):
        draft, refs, pois = fixture()
        # 重现真实失败：原文只写天坛；地图把圆明园写成圆明园遗址公园。
        draft["places"][0]["quote"] = "天坛是明清皇家祭天场所"
        refs[0]["content"] = draft["places"][0]["quote"]
        old_name = draft["places"][1]["name"]
        draft["places"][1].update(name="圆明园", quote="圆明园曾是清代大型皇家园林")
        draft["days"][0]["places"][1] = "圆明园"
        refs[1].update(title="圆明园", content=draft["places"][1]["quote"])
        pois["圆明园"] = [pois.pop(old_name)[0].model_copy(update={"name": "圆明园遗址公园"})]
        search, amap = Mock(), Mock()
        search.generate_itinerary.return_value = {"content": json.dumps(draft), "references": refs}
        amap.search_tourism_pois.side_effect = lambda name, *args, **kwargs: pois[name]
        candidates, raw = research_tourism(request(), search, amap)
        req = request().model_copy(update={"candidate_places": candidates})
        skeletons, overall = parse_skeleton(raw, build_day_capacities(req))
        plan = materialize_skeleton(req, skeletons, overall)
        enforce_researched_plan(plan, candidates)
        self.assertEqual(plan.days[0].attractions[1].name, "圆明园遗址公园")
        self.assertEqual(plan.days[0].attractions[1].poi_id, "B1")
        self.assertIn("圆明园", candidates[1].aliases)
        self.assertTrue(all(a.recommendation_sources for a in plan.days[0].attractions))


    def test_concurrent_requests_do_not_overdraw_either_version(self):
        draft, refs, _ = fixture()
        settings = SimpleNamespace(baidu_search_enabled=True, baidu_search_api_key="test",
            baidu_search_timeout_seconds=5, baidu_search_daily_limit=1, baidu_search_cache_hours=24,
            baidu_search_standard_enabled=True, baidu_search_standard_daily_limit=1,
            baidu_search_standard_model="deepseek-v4-flash")
        response = Mock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": json.dumps(draft)}}], "references": refs}
        with tempfile.TemporaryDirectory() as temp, patch("app.services.baidu_search_service.requests.post", return_value=response) as post:
            service = BaiduSearchService(settings, Database(Path(temp) / "search.db"))
            def run(index):
                try:
                    service.generate_itinerary(f"不同需求{index}", "只返回JSON")
                    return True
                except SearchUnavailable:
                    return False
            with ThreadPoolExecutor(max_workers=3) as pool:
                completed = list(pool.map(run, range(3)))
            self.assertEqual(sum(completed), 2)
            self.assertEqual(post.call_count, 2)
            self.assertEqual([p["used"] for p in service.quota_status()["providers"]], [1, 1])

    def test_legacy_count_is_preserved_and_exhaustion_selects_standard(self):
        draft, refs, _ = fixture()
        settings = SimpleNamespace(baidu_search_enabled=True, baidu_search_api_key="test",
            baidu_search_timeout_seconds=5, baidu_search_daily_limit=90, baidu_search_cache_hours=24,
            baidu_search_standard_enabled=True, baidu_search_standard_daily_limit=90,
            baidu_search_standard_model="deepseek-v4-flash")
        response = Mock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": json.dumps(draft)}}], "references": refs}
        with tempfile.TemporaryDirectory() as temp, patch("app.services.baidu_search_service.requests.post", return_value=response) as post:
            db = Database(Path(temp) / "search.db")
            service = BaiduSearchService(settings, db)
            day = service.quota_status()["date"]
            with db.connection() as conn:
                service._prepare(conn)
                conn.execute("INSERT INTO search_web_budget VALUES(?,?,?)", (day, 90, 0))
            self.assertEqual(service.quota_status()["active_provider"], "standard")
            service.generate_itinerary("北京", "只返回JSON")
            self.assertEqual(post.call_count, 1)
            self.assertEqual(post.call_args.args[0], service.standard_endpoint)
            body = post.call_args.kwargs["json"]
            self.assertEqual(body["model"], "deepseek-v4-flash")
            self.assertFalse(body["enable_deep_search"])
            rows = {p["id"]: p for p in service.quota_status()["providers"]}
            self.assertEqual(rows["high_performance"]["used"], 90)
            self.assertEqual(rows["standard"]["used"], 1)
            self.assertEqual(rows["standard"]["remaining"], 89)

    def test_both_versions_exhausted_do_not_send_request(self):
        settings = SimpleNamespace(baidu_search_enabled=True, baidu_search_api_key="test",
            baidu_search_timeout_seconds=5, baidu_search_daily_limit=0, baidu_search_cache_hours=24,
            baidu_search_standard_enabled=True, baidu_search_standard_daily_limit=0,
            baidu_search_standard_model="deepseek-v4-flash")
        with tempfile.TemporaryDirectory() as temp, patch("app.services.baidu_search_service.requests.post") as post:
            service = BaiduSearchService(settings, Database(Path(temp) / "search.db"))
            with self.assertRaises(SearchUnavailable):
                service.generate_itinerary("北京", "返回JSON")
            self.assertIsNone(service.quota_status()["active_provider"])
            post.assert_not_called()

    def test_provider_uses_high_performance_and_persistent_cache_budget(self):
        draft, refs, _ = fixture()
        settings = SimpleNamespace(baidu_search_enabled=True, baidu_search_api_key="private-test-key",
            baidu_search_timeout_seconds=5, baidu_search_daily_limit=1, baidu_search_cache_hours=24,
            baidu_search_standard_enabled=False)
        response = Mock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": json.dumps(draft)}}], "references": refs}
        with tempfile.TemporaryDirectory() as temp, patch("app.services.baidu_search_service.requests.post", return_value=response) as post:
            db = Database(Path(temp) / "search.db")
            self.assertEqual(BaiduSearchService(settings, db).quota_status()["remaining"], 1)
            first = BaiduSearchService(settings, db).generate_itinerary("北京", "返回JSON")
            self.assertEqual(first, BaiduSearchService(settings, db).generate_itinerary("北京", "返回JSON"))
            with self.assertRaises(SearchUnavailable):
                BaiduSearchService(settings, db).generate_itinerary("苏州", "返回JSON")
            self.assertEqual(post.call_count, 1)
            self.assertEqual(BaiduSearchService(settings, db).quota_status()["used"], 1)
            self.assertEqual(BaiduSearchService(settings, db).quota_status()["remaining"], 0)
            self.assertTrue(post.call_args.args[0].endswith("/ai_search/web_summary"))
            self.assertEqual(post.call_args.kwargs["json"]["model"], "non_thinking")
            self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer private-test-key")

    def test_auth_failure_does_not_retry_or_expose_key(self):
        settings = SimpleNamespace(baidu_search_enabled=True, baidu_search_api_key="private-test-key",
            baidu_search_timeout_seconds=5, baidu_search_daily_limit=1, baidu_search_cache_hours=24,
            baidu_search_standard_enabled=False)
        with tempfile.TemporaryDirectory() as temp, patch("app.services.baidu_search_service.requests.post", return_value=Mock(status_code=401)) as post:
            service = BaiduSearchService(settings, Database(Path(temp) / "search.db"))
            with self.assertRaises(SearchUnavailable) as caught:
                service.generate_itinerary("北京", "返回JSON")
            self.assertNotIn("private-test-key", str(caught.exception))
            self.assertEqual(post.call_count, 1)

    def test_researched_candidates_use_one_planner_call(self):
        draft, refs, pois = fixture()
        search = Mock()
        search.generate_itinerary.return_value = {"content": json.dumps(draft, ensure_ascii=False), "references": refs}
        amap = Mock()
        amap.search_tourism_pois.side_effect = lambda name, *args, **kwargs: pois[name]
        candidates, raw = research_tourism(request(), search, amap)
        planner = Mock()
        planner.needs_repair.side_effect = AssertionError("不可重复修复草案")
        planner.planner_agent.llm.max_tokens = 8192
        planner.planner_agent.llm.invoke.return_value = json.dumps({"days": [{"date": request().start_date,
            "theme": "文化", "places": [{"candidate_id": c.candidate_id, "duration_minutes": 90} for c in candidates]}],
            "overall_suggestions": "建议"})
        context = {"tourism_candidates": [c.model_dump(mode="json") for c in candidates], "tourism_skeleton": raw}
        with patch.object(pg, "_get_planner", return_value=planner):
            generated = pg.generate_plan_node({"request": request(), "context": context}, {})
        plan = generated["plan"]
        self.assertEqual([a.name for a in plan.days[0].attractions], draft["days"][0]["places"])
        self.assertTrue(all(a.recommendation_sources for a in plan.days[0].attractions))
        self.assertEqual(search.generate_itinerary.call_count, 1)
        planner.planner_agent.llm.invoke.assert_called_once()


    def test_fabricated_quote_is_not_used_but_map_identity_is_still_checked(self):
        draft, refs, pois = fixture()
        for row in draft["places"]:
            row["quote"] = row["name"] + "是网页里没有写过的推荐"
        search, amap = Mock(), Mock()
        search.generate_itinerary.return_value = {"content": json.dumps(draft), "references": refs}
        amap.search_tourism_pois.side_effect = lambda name, *args, **kwargs: pois[name]
        candidates, _ = research_tourism(request(), search, amap)
        self.assertEqual(len(candidates), 3)
        self.assertEqual(amap.search_tourism_pois.call_count, 3)
        self.assertTrue(all(c.match_evidence['recommendation_evidence_status'] == 'source_context_verified' for c in candidates))
        self.assertTrue(all('网页里没有写过' not in str(c.recommendation_reasons) for c in candidates))

    def test_real_tourism_poi_is_not_rejected_for_mismatched_quote_or_missing_reference(self):
        for name, quote, label in [('清河坊街', '首日不要暴走赶景点', '特色商业街'),
                                   ('灵隐飞来峰景区', '灵隐必须预约', '风景名胜')]:
            with self.subTest(name=name):
                draft, _, pois = fixture()
                draft['places'] = [dict(name=name, quote=quote, reason='历史文化体验', duration_minutes=90)]
                search, amap = Mock(), Mock()
                search.generate_itinerary.return_value = {'content':json.dumps(draft), 'references':[]}
                poi = pois['天坛公园'][0].model_copy(update={'name':name, 'city_name':'杭州市', 'type':label})
                amap.search_tourism_pois.return_value = [poi]
                candidates, _ = research_tourism(request().model_copy(update={'city':'杭州'}), search, amap)
                self.assertEqual(len(candidates), 1)
                self.assertEqual(candidates[0].match_evidence['recommendation_evidence_status'], 'unverified')
                self.assertEqual(candidates[0].source_refs, ['amap:' + poi.id])
                self.assertTrue(candidates[0].warnings)

    def test_road_candidate_is_left_to_planner_with_unverified_source_warning(self):
        draft, _, pois = fixture()
        draft['places'] = [dict(name='普通路', reason='模型说这是热门步行街', quote='热门步行街')]
        search, amap = Mock(), Mock()
        search.generate_itinerary.return_value = {'content':json.dumps(draft), 'references':[]}
        amap.search_tourism_pois.return_value = [pois['天坛公园'][0].model_copy(update={
            'name':'普通路', 'type':'地名地址;道路名', 'type_code':'190301'})]
        candidates, _ = research_tourism(request(), search, amap)
        self.assertEqual(len(candidates),1)
        self.assertTrue(candidates[0].warnings)
        self.assertEqual(candidates[0].match_evidence["map_category"],"地名地址;道路名")
        amap.search_tourism_pois.assert_called_once()

    def test_unverified_draft_cannot_silently_fall_back(self):
        planner, graph = Mock(), Mock()
        graph.invoke.side_effect = TourismResearchError("同城景点未核验")
        with patch.object(pg, "_get_planner", return_value=planner), patch.object(pg, "get_planning_graph", return_value=graph):
            with self.assertRaises(TourismResearchError):
                pg.run_planning_graph(request())
        planner.create_fallback_plan.assert_not_called()

    def test_final_guard_rejects_injected_attraction(self):
        draft, refs, pois = fixture()
        search, amap = Mock(), Mock()
        search.generate_itinerary.return_value = {"content": json.dumps(draft), "references": refs}
        amap.search_tourism_pois.side_effect = lambda name, *args, **kwargs: pois[name]
        candidates, raw = research_tourism(request(), search, amap)
        req = request().model_copy(update={"candidate_places": candidates})
        skeletons, overall = parse_skeleton(raw, build_day_capacities(req))
        plan = materialize_skeleton(req, skeletons, overall)
        plan.days[0].attractions[0].name = "莫名其妙的社区"
        with self.assertRaises(TourismResearchError):
            enforce_researched_plan(plan, candidates)


if __name__ == "__main__":
    unittest.main()
