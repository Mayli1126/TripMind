"""压缩只改表达，不丢用户约束，也不让短ID改变地点身份。"""
import json
from unittest import TestCase
from unittest.mock import Mock
from app.models.candidate import CandidatePlace
from app.models.schemas import Location
from app.services.planner_context_service import build_planner_context, compact_schema
from app.services.planner_arrangement_service import Arrangement, arrange_researched_plan
from app.services.day_pipeline_service import build_day_capacities
from test_planning_feedback import fixture


class PlannerContextTests(TestCase):
    def test_short_ids_roundtrip_and_constraints_are_preserved(self):
        _, request = fixture()
        request.free_text_input='必须去内部子点，不去寺庙；不要改停留要求'
        request.conversation_context='用户已确认第二天返程前留出接驳余量'
        request.arrival_time='11:30'
        location=Location(longitude=120,latitude=30)
        request.candidate_places=[CandidatePlace(candidate_id=key,canonical_name=name,city=request.city,
            poi_id=poi,location=location,fact_status='verified',parent_poi_id='map-parent' if key=='child-long-id' else '',
            user_choice='required' if key=='child-long-id' else 'preferred')
            for key,name,poi in [('parent-long-id','父景区','map-parent'),('child-long-id','内部景点','map-child'),('street-long-id','独立街区','map-street')]]
        candidates={c.candidate_id:c for c in request.candidate_places}
        payload,aliases=build_planner_context(request,{},candidates,build_day_capacities(request),Arrangement.model_json_schema())
        self.assertEqual(payload['request']['requirements'],request.free_text_input)
        self.assertEqual(payload['request']['conversation'],request.conversation_context)
        self.assertEqual(payload['request']['arrival_time'],'11:30')
        self.assertEqual(payload['candidates'][1]['parent'],'P1')
        self.assertTrue(payload['candidates'][1]['required'])
        self.assertNotIn('location',payload['candidates'][0])
        self.assertNotIn('poi_id',payload['candidates'][0])
        self.assertEqual(aliases['P1'],'parent-long-id')
        llm=Mock(max_tokens=8192)
        llm.invoke.return_value=json.dumps({'days':[
            {'date':request.start_date,'theme':'父景区','places':[{'candidate_id':'P1','duration_minutes':180,'recommended_child_ids':['P2']}]},
            {'date':request.end_date,'theme':'街区','places':[{'candidate_id':'P3','duration_minutes':90}]}]})
        plan,_=arrange_researched_plan(request,{},llm)
        self.assertEqual(plan.days[0].attractions[0].poi_id,'map-parent')
        self.assertEqual(plan.days[0].attractions[0].location,location)
        self.assertIn('内部景点',plan.days[0].attractions[0].description)
        self.assertEqual(plan.days[1].attractions[0].poi_id,'map-street')
        self.assertEqual(request.candidate_places[0].candidate_id,'parent-long-id')
        llm.invoke.assert_called_once()

    def test_schema_keeps_constraints_and_fields_named_like_metadata(self):
        original={'title':'输出','type':'object','properties':{
            'title':{'title':'标题','type':'string','minLength':1},
            'duration_minutes':{'type':'integer','minimum':15,'default':60}},'required':['title'],'additionalProperties':False}
        compact=compact_schema(original)
        self.assertNotIn('title',compact)
        self.assertIn('title',compact['properties'])
        self.assertEqual(compact['properties']['duration_minutes']['minimum'],15)
        self.assertEqual(compact['required'],['title'])
        self.assertFalse(compact['additionalProperties'])
