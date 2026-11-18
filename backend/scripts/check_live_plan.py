"""执行一次小规模真实规划，使用已配置接口，不保存行程或用户。

启用 Redis 时需要本地服务，输出诊断与摘要时隐藏配置中的敏感值。
"""
import contextlib
from datetime import timedelta
import io
import json
import sys
import time
import os
from uuid import uuid4


def main():
    output = sys.stdout
    started = time.monotonic()
    last = [None]
    degraded = set()

    def progress(percent, stage, message, agent=None, agent_status=None):
        if agent_status in ('degraded', 'failed') and agent:
            degraded.add(agent)
        if stage != last[0]:
            print(f'{time.monotonic() - started:.1f}s {percent}% {stage}', file=output, flush=True)
            last[0] = stage

    logs = io.StringIO()
    class DiagnosticOutput:
        encoding = 'utf-8'
        def write(self, text):
            for name in ('AMAP_API_KEY', 'AMAP_MAPS_API_KEY', 'LLM_API_KEY', 'OPENAI_API_KEY', 'REDIS_URL'):
                value = os.getenv(name, '')
                if value:
                    text = text.replace(value, '[REDACTED]')
            logs.write(text)
            return output.write(text)
        def flush(self):
            output.flush()
        def fileno(self):
            return output.fileno()
    # MCP 子进程需要真实标准错误文件描述符，不能用 StringIO 替代。
    with contextlib.redirect_stdout(DiagnosticOutput()):
        from app.config import get_settings
        from app.infrastructure import redis_client
        from app.infrastructure.mcp_client import shutdown_amap_manager
        from app.services.runtime_service import warmup_planning_runtime
        from app.graphs.planning_graph import run_planning_graph
        from app.models.schemas import TripRequest, get_app_today
        try:
            if get_settings().redis_enabled and not redis_client.redis_available():
                print('LIVE_CHECK_BLOCKED: Redis unavailable', file=output, flush=True)
                return 1
            print('Preparing real map sessions...', file=output, flush=True)
            warmup_planning_runtime()
            start = get_app_today() + timedelta(days=7)
            request = TripRequest(origin_city='北京', city='北京', start_date=start.isoformat(),
                                  end_date=(start + timedelta(days=1)).isoformat(), travel_days=2,
                                  transportation='公共交通', accommodation='经济型酒店', travelers=1, rooms=1,
                                  preferences=['人文', '老街'], free_text_input='以经典景点与老街为主，不需要任何实际预订。')
            if '--reuse-research' in sys.argv:
                from app.graphs.planning_graph import get_planning_graph, _as_request, generate_plan_node, finalize_plan_node
                keys = list(redis_client._get_client().scan_iter(match='checkpoint:live-smoke-*'))
                if not keys:
                    raise RuntimeError('No previous smoke-check research checkpoint')
                latest = max(keys, key=lambda value: value.rsplit(':', 1)[-1])
                thread_id = latest.split(':')[1]
                state = dict(get_planning_graph().get_state({'configurable': {'thread_id': thread_id}}).values)
                state['request'] = _as_request(state['request'])
                config = {'configurable': {'progress_callback': progress}}
                print('Reusing smoke-check research; no repeat research calls.', file=output, flush=True)
                state.update(generate_plan_node(state, config))
                plan = finalize_plan_node(state, config)['result']
            else:
                plan = run_planning_graph(request, progress, job_id='live-smoke-' + uuid4().hex)
            counts = [len(day.attractions) for day in plan.days]
            coordinates_ok = all(a.location and -180 <= a.location.longitude <= 180 and -90 <= a.location.latitude <= 90
                                 for day in plan.days for a in day.attractions)
            passed = not plan.is_fallback and len(plan.days) == 2 and all(n >= 3 for n in counts) and coordinates_ok
            print(json.dumps({'passed': passed, 'elapsed_seconds': round(time.monotonic() - started, 1),
                              'days': len(plan.days), 'places_per_day': counts, 'is_fallback': plan.is_fallback,
                              'coordinates_ok': coordinates_ok, 'warning_count': len(plan.warnings),
                              'degraded_agents': sorted(degraded), 'has_budget': plan.budget is not None,
                              'schedule_items': [len(day.schedule) for day in plan.days]}, ensure_ascii=False), file=output, flush=True)
            return 0 if passed else 2
        except Exception as error:
            print('LIVE_CHECK_FAILED: ' + type(error).__name__, file=output, flush=True)
            detail = logs.getvalue()[-2400:]
            secrets = [get_settings().amap_api_key, get_settings().redis_url,
                       os.getenv('LLM_API_KEY', ''), os.getenv('OPENAI_API_KEY', '')]
            for secret in secrets:
                if secret:
                    detail = detail.replace(secret, '[REDACTED]')
            print(detail, file=output, flush=True)
            return 1
        finally:
            shutdown_amap_manager()


if __name__ == '__main__':
    raise SystemExit(main())
