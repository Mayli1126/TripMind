"""手动执行一次真实联网规划并保存诊断；不自动重试，不写入行程库。"""
import contextlib
import argparse
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
import sys
import time
from uuid import uuid4
from zoneinfo import ZoneInfo


def main():
    parser = argparse.ArgumentParser(description='执行一次指定城市的真实联网规划')
    parser.add_argument('--city', default='北京', help='目的地城市')
    parser.add_argument('--days', type=int, default=2, choices=range(1, 8), help='测试天数，默认两天')
    parser.add_argument('--allow-supplement', action='store_true', help='允许首搜加一次附近补搜，百度真实请求最多两次')
    args = parser.parse_args()
    from app.config import get_settings
    from app.infrastructure import task_trace
    from app.infrastructure.mcp_client import shutdown_amap_manager
    from app.models.schemas import TripRequest, get_app_today
    from app.services.baidu_search_service import BaiduSearchService, SearchUnavailable
    from app.graphs.planning_graph import run_planning_graph
    from app.services.planning_repair_service import capture_repair_diagnostics
    import requests
    from unittest.mock import patch

    started = time.perf_counter()
    stamp = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S')
    folder = Path(__file__).resolve().parents[1] / 'data' / 'diagnostics'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'baidu-plan-{stamp}.json'
    console = sys.stdout
    service = BaiduSearchService()
    report = dict(started_at=stamp, quota_before=service.quota_status(), stages=[], events=[], search_requests=[], repair_diagnostics=[])
    secrets = [get_settings().baidu_search_api_key, get_settings().amap_api_key,
               os.getenv('LLM_API_KEY', ''), os.getenv('OPENAI_API_KEY', '')]

    def clean(value):
        text = json.dumps(value, ensure_ascii=False, default=str)
        for secret in secrets:
            if secret:
                text = text.replace(secret, '[REDACTED]')
        return text

    real_post = requests.post
    # 提前保存上限，避免与HTTP转发的可变位置参数同名。
    max_search_requests = 2 if args.allow_supplement else 1
    def capture_post(url, *args, **kwargs):
        if url not in {service.endpoint, service.standard_endpoint}:
            return real_post(url, *args, **kwargs)
        if len(report['search_requests']) >= max_search_requests:
            raise SearchUnavailable('已达到本次验收的百度真实请求上限；补搜验收需使用allow-supplement参数')
        entry = {'started_seconds': round(time.perf_counter() - started, 3), 'endpoint': url}
        report['search_requests'].append(entry)
        begin = time.perf_counter()
        try:
            response = real_post(url, *args, **kwargs)
            entry['http_status'] = response.status_code
            try:
                entry['response'] = response.json()
            except ValueError:
                entry['response_text'] = response.text[:4000]
            return response
        finally:
            entry['elapsed_seconds'] = round(time.perf_counter() - begin, 3)

    def progress(percent, stage, message, agent=None, agent_status=None):
        elapsed = round(time.perf_counter() - started, 3)
        report['stages'].append(dict(elapsed_seconds=elapsed, percent=percent, stage=stage,
                                     message=message, agent=agent, agent_status=agent_status))
        print(f'{elapsed:.1f}s {percent}% {stage}', file=console, flush=True)

    day = get_app_today() + timedelta(days=7)
    request = TripRequest(city=args.city, start_date=day.isoformat(), end_date=(day + timedelta(days=args.days - 1)).isoformat(),
                          travel_days=args.days, transportation='公共交通', accommodation='舒适型酒店',
                          preferences=['历史文化', '经典景点'], free_text_input='以经典景点为主，不要普通住宅小区，不需要实际预订。')
    report['request'] = request.model_dump(mode='json')
    task_trace.activate('live-baidu-' + uuid4().hex, 'manual-once', report['events'].append)
    try:
        if not service.configured:
            raise RuntimeError('未配置百度 Key')
        # 常规日志单独落盘；原始搜索响应保存在诊断报告中，不在终端展开。
        with (folder / f'baidu-plan-{stamp}.log').open('w', encoding='utf-8') as log:
            with contextlib.redirect_stdout(log), patch('requests.post', side_effect=capture_post), capture_repair_diagnostics(report['repair_diagnostics'].append):
                plan = run_planning_graph(request, progress, job_id='live-baidu-' + uuid4().hex)
        # 跑到收尾不等于可交付；列出的景点必须真正进入时间轴。
        missing = []
        for day_plan in plan.days:
            scheduled = {item.related_index for item in day_plan.schedule if item.item_type == 'attraction'}
            missing.extend({'date': day_plan.date, 'name': attraction.name}
                           for index, attraction in enumerate(day_plan.attractions) if index not in scheduled)
        report.update(ok=not plan.is_fallback and not missing,
                      pipeline_completed=not plan.is_fallback, unscheduled_attractions=missing,
                      plan=plan.model_dump(mode='json'))
        from app.services.planning_feedback_service import build_planning_feedback
        report['planning_feedback'] = build_planning_feedback(plan, request).model_dump(mode='json')
    except Exception as exc:
        report.update(ok=False, error_type=type(exc).__name__, error=str(exc)[:500])
        report['match_details'] = getattr(exc, 'details', {})
    finally:
        report['planning_elapsed_seconds'] = round(time.perf_counter() - started, 3)
        report['quota_after'] = service.quota_status()
        task_trace.deactivate()
        shutdown_amap_manager()
        report['elapsed_including_shutdown_seconds'] = round(time.perf_counter() - started, 3)
        path.write_text(clean(report), encoding='utf-8')
        print(clean(dict(ok=report['ok'], elapsed_seconds=report['planning_elapsed_seconds'],
                         search_seconds=[r['elapsed_seconds'] for r in report['search_requests']],
                         error=report.get('error'), quota=report['quota_after'], report_path=str(path))), file=console, flush=True)
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
