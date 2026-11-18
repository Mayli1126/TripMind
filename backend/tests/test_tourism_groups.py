"""规划取高德首条；仍保留历史多候选分组的防重复校验。"""
import json
from unittest import TestCase
from unittest.mock import Mock, patch
from test_tourism_search import request, fixture
from app.services.tourism_research_service import research_tourism
from app.services.planner_arrangement_service import arrange_researched_plan
from app.services.planner_context_service import build_planner_context
from app.services.day_pipeline_service import build_day_capacities
from app.services.planner_arrangement_service import Arrangement


class TourismGroupTests(TestCase):
    def candidates(self, legacy_pair=False):
        draft,refs,pois=fixture()
        draft['places']=draft['places'][:1]
        base=pois['天坛公园'][0]
        rows=[base.model_copy(update={'id':f'poi-{i}','name':f'地图结果{i}'}) for i in range(3)]
        search,amap=Mock(),Mock()
        search.generate_itinerary.return_value={'content':json.dumps(draft),'references':refs}
        amap.search_tourism_pois.return_value=rows
        candidates,_=research_tourism(request(),search,amap)
        if legacy_pair:
            # 旧记录仍可能带两条候选，分组校验不能随新策略删除。
            candidates.append(candidates[0].model_copy(deep=True, update={
                'candidate_id':'legacy-second','poi_id':'poi-1','canonical_name':'地图结果1'}))
        return candidates

    def test_only_first_map_result_is_passed_to_planner(self):
        candidates=self.candidates()
        self.assertEqual(len(candidates),1)
        req=request().model_copy(update={'candidate_places':candidates})
        payload,_=build_planner_context(req,{}, {c.candidate_id:c for c in candidates},build_day_capacities(req),Arrangement.model_json_schema())
        self.assertEqual(payload['recommendation_groups'],[])
        self.assertEqual(candidates[0].aliases,['天坛公园'])
        self.assertEqual(payload['candidates'][0]['map_type'],'风景名胜;公园')
        llm=Mock(max_tokens=8192)
        llm.invoke.return_value=json.dumps({'days':[{'date':req.start_date,'theme':'文化','places':[{'candidate_id':'P1','duration_minutes':90}]}]})
        plan,_=arrange_researched_plan(req,{},llm)
        self.assertEqual(plan.days[0].attractions[0].poi_id,'poi-0')
        self.assertEqual(plan.days[0].attractions[0].location,candidates[0].location)
        llm.invoke.assert_called_once()

    def test_same_group_cannot_be_used_twice_even_as_internal_recommendation(self):
        candidates=self.candidates(legacy_pair=True)
        req=request().model_copy(update={'candidate_places':candidates})
        for places in ([{'candidate_id':'P1','duration_minutes':90},{'candidate_id':'P2','duration_minutes':90}],
                       [{'candidate_id':'P1','duration_minutes':90,'recommended_child_ids':['P2']}]):
            llm=Mock(max_tokens=8192)
            llm.invoke.return_value=json.dumps({'days':[{'date':req.start_date,'theme':'文化','places':places}]})
            with self.assertRaisesRegex(ValueError,'同一推荐地点'):
                arrange_researched_plan(req,{},llm)
            self.assertEqual(llm.invoke.call_count,2)

    def test_amap_planning_limit_is_passed_to_request_and_results_are_capped(self):
        from app.services.amap_service import AmapService
        service=AmapService.__new__(AmapService)
        with patch.object(service,'_search_poi_cached_direct',return_value=[1,2,3]) as search:
            self.assertEqual(service.search_tourism_pois('天坛','北京'),[1])
        search.assert_called_once_with('天坛','北京',True,limit=1)
