"""手动验收一次真实交通查询及无版本副作用；不调用模型，不写用户行程库。"""
import json
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


def main():
    started = time.perf_counter()
    root = Path(__file__).resolve().parents[1]
    seed = json.loads((root / 'data/diagnostics/baidu-plan-20261117-230055.json').read_text(encoding='utf-8'))
    points = seed['plan']['days'][0]['attractions'][:2]
    real_connect = sqlite3.connect
    with tempfile.TemporaryDirectory(prefix='tripmind-live-transport-') as folder:
        def connect(path, *args, **kwargs):
            if Path(str(path)).resolve() == (root / 'data/tripmind.db').resolve():
                path = Path(folder) / 'default.db'
            return real_connect(path, *args, **kwargs)
        with patch('sqlite3.connect', side_effect=connect):
            from app.api.routes.map import TransportOptionsRequest, transport_options
            from app.services.transport_selection_service import select_transport
            from app.services.trip_store_service import TripStoreService
            from app.models.schemas import TripPlan, TripRequest, DayPlan, Attraction, Location, SavedTripCreate
            from app.services.itinerary_schedule_service import itinerary_schedule_service
            date = (datetime.now(ZoneInfo('Asia/Shanghai')).date() + timedelta(days=7)).isoformat()
            request = TripRequest(city='杭州', start_date=date, end_date=date, travel_days=1,
                                  transportation='公共交通', accommodation='经济型酒店', destination_only=True)
            locations = [Location.model_validate(p['location']) for p in points]
            begin = time.perf_counter()
            result = transport_options(TransportOptionsRequest(city='杭州', date=date,
                origin=locations[0], destination=locations[1], reference_time='18:00'))
            report = dict(origin=points[0]['name'], destination=points[1]['name'],
                options_seconds=round(time.perf_counter()-begin, 3),
                options=[dict(mode=x['mode'], available=x['available'],
                    duration_minutes=x['route'].duration_minutes if x['route'] else None,
                    distance_km=x['route'].distance_km if x['route'] else None) for x in result['options']])
            available = [x for x in result['options'] if x['available']]
            if not available:
                report.update(ok=False, reason='四种方式均未取得真实道路路线')
            else:
                # 优先验证驾车；此操作只改临时测试行程。
                mode = next((x['mode'] for x in available if x['mode']=='driving'), available[0]['mode'])
                attractions = [Attraction.model_validate(p) for p in points]
                plan = TripPlan(city='杭州', start_date=date, end_date=date, overall_suggestions='交通验收', days=[
                    DayPlan(date=date, day_index=0, description='交通验收', transportation='公共交通',
                            accommodation='经济型酒店', attractions=attractions)])
                itinerary_schedule_service.build(plan)
                segment = next(s for s in plan.days[0].schedule if s.route_origin and s.route_destination)
                segment.route_reference_time='18:00'
                store=TripStoreService(Path(folder)/'transport.db')
                saved=store.create_trip(SavedTripCreate(request=request, plan=plan))
                begin=time.perf_counter()
                changed=select_transport(plan, request, segment.stable_id, mode)
                # 切换只用于查看，不调用版本保存。
                from app.services.revision_outcome import has_plan_changes
                assert not has_plan_changes(plan, changed)
                loaded=store.get_trip(saved.id)
                assert loaded.current_version == 1
                final=next(s for s in changed.days[0].schedule if s.stable_id==segment.stable_id)
                assert final.selected_route_mode==mode and final.route_leg.source=='amap_route'
                assert [a.model_dump() for a in loaded.plan.days[0].attractions]==[a.model_dump() for a in plan.days[0].attractions]
                report.update(ok=True, selected=mode, saved_version=loaded.current_version,
                              selected_minutes=final.duration_minutes, transport_minutes=changed.days[0].transport_minutes,
                              visit_minutes=changed.days[0].visit_minutes,
                              selection_check_seconds=round(time.perf_counter()-begin,3))
    report['elapsed_seconds']=round(time.perf_counter()-started,3)
    target=root/'data/diagnostics'/('transport-'+datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S')+'.json')
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
    print('报告：',target)
    return 0 if report['ok'] else 1


if __name__=='__main__':
    raise SystemExit(main())
