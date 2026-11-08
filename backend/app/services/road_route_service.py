"""Date/time-scoped Amap road routes. Prices are references, never ticket quotes."""
from collections import OrderedDict
from datetime import datetime, timezone
from math import ceil, isfinite
from threading import Lock
from time import monotonic, sleep

import httpx

from ..config import get_settings
from ..infrastructure.provider_guard import ProviderBusyError
from ..models.schemas import RouteLeg
from .route_optimizer import route_optimizer

_CACHE = OrderedDict()
_LOCK = Lock()
_REQUEST_LOCK = Lock()
_LAST_REQUEST = 0.0


class _RouteFallback(Exception):
    def __init__(self, leg):
        self.leg = leg


class _NoRoadRoute(Exception):
    pass


class RoadRouteResolver:
    """One resolver per build; limit calls and stop new work at its deadline."""
    def __init__(self, city, enabled=True, preference=""):
        self.city = city
        self.preference = preference
        self.settings = get_settings()
        self.enabled = enabled and self.settings.road_routes_enabled
        self.deadline = monotonic() + max(0, self.settings.road_routes_max_seconds)
        self.calls = 0

    def resolve(self, start, end, date, time, preference=""):
        if not self.settings.cache_enabled or not (start and end and self.enabled and self.settings.amap_api_key):
            return self._resolve(start, end, date, time, preference)
        from ..infrastructure.shared_cache import make_key
        from ..infrastructure.cache_rebuild import cache_rebuilder, CacheBusyError
        from .cache_policy import make_entry
        from ..infrastructure.task_trace import emit
        if not all(isfinite(v) for p in (start, end) for v in (p.longitude, p.latitude)):
            return self._resolve(start, end, date, time, preference)
        pref = self.preference or preference
        self_drive = '自驾' in pref or pref == 'driving'
        driving = self_drive or pref in ('打车', '专车', 'taxi')
        mode = 'driving' if driving else 'walking' if route_optimizer.distance(start, end) < 1.5 else 'transit'
        bucket = f'{time[:3]}{int(time[3:]) // 30 * 30:02d}'
        key = make_key(f'{mode}_route', dict(provider='amap', api='v3/direction',
            policy='coordinates6-time30-v1', city=self.city.strip(), date=date, departure=bucket,
            mode=mode, self_drive=self_drive, strategy=0, extensions='all',
            origin=f'{start.longitude:.6f},{start.latitude:.6f}',
            destination=f'{end.longitude:.6f},{end.latitude:.6f}'))
        def load():
            try:
                result = self._resolve(start, end, date, time, preference)
            except _NoRoadRoute:
                return make_entry(f'{mode}_route', None, 'amap_v3_direction', empty=True)
            if result.source != 'amap_route':
                raise _RouteFallback(result)
            return make_entry(f'{mode}_route', result.model_dump(mode='json'), 'amap_v3_direction')
        try:
            entry = cache_rebuilder.get(key, load, wait_seconds=max(0, self.deadline-monotonic()))
            if entry.outcome != 'empty':
                return RouteLeg.model_validate(entry.data)
            emit('map_route', 'fallback', 'degraded', reason='provider_no_route')
        except _RouteFallback as exc:
            return exc.leg
        except CacheBusyError:
            emit('map_route', 'fallback', 'degraded', reason='cache_rebuild_busy')
        return self._resolve(start, end, date, time, preference, fallback_only=True)

    def _resolve(self, start, end, date, time, preference="", *, fallback_only=False):
        from ..infrastructure.task_trace import emit, span
        valid = start is not None and end is not None and all(
            isfinite(v) for p in (start, end) for v in (p.longitude, p.latitude)
        ) and all(-180 <= p.longitude <= 180 and -90 <= p.latitude <= 90
                  and (p.longitude != 0 or p.latitude != 0) for p in (start, end))
        distance = route_optimizer.distance(start, end) if valid else 0
        preference = self.preference or preference
        self_drive = "自驾" in preference or preference == "driving"
        driving = self_drive or preference in ("打车", "专车", "taxi")
        mode = "driving" if driving else "walking" if valid and distance < 1.5 else "transit"
        # Conservative road detour and boarding buffer, explicitly labelled as fallback.
        km = distance * (1.25 if mode == "walking" else 1.4)
        minutes = max(5, ceil(km / (4.5 if mode == "walking" else 22) * 60)
                      + (12 if mode == "transit" else 5)) if valid else 30
        fallback = RouteLeg(mode=mode, distance_km=round(km, 2), duration_minutes=minutes,
                            cost=15 if not valid else 0 if mode == "walking" else km * .8 if self_drive else max(12, km * 2.5) if driving else 5,
                            cost_unit="vehicle" if driving else "person",
                            note="坐标绕行估算，非道路导航；费用为经验参考" if valid else "缺少接驳坐标，暂按30分钟及参考费用预留")
        if fallback_only or not (valid and self.enabled and self.settings.amap_api_key):
            emit('map_route','fallback','degraded',reason='missing_coordinates' if not valid else 'not_enabled')
            return fallback
        # Direction, date, 30-minute departure bucket and mode are all cache dimensions.
        bucket = f"{time[:3]}{int(time[3:]) // 30 * 30:02d}"
        key = (self.city, date, bucket, mode, self_drive, *(round(v, 6) for p in (start, end)
                                                for v in (p.longitude, p.latitude)))
        with _LOCK:
            cached = None if self.settings.cache_enabled else _CACHE.get(key)
            if cached and cached[0] > monotonic():
                _CACHE.move_to_end(key)
                emit('cache','road_route','lookup',hit=True)
                return cached[1].model_copy(deep=True)
        emit('cache','road_route','lookup',hit=False)
        remaining = self.deadline - monotonic()
        if remaining <= 0 or self.calls >= self.settings.road_routes_max_calls:
            emit('map_route','fallback','degraded',reason='query_budget_exhausted')
            return fallback
        self.calls += 1
        global _LAST_REQUEST
        with _REQUEST_LOCK:
            delay = max(0, .35 - (monotonic() - _LAST_REQUEST))
            if monotonic() + delay >= self.deadline:
                return fallback
            sleep(delay)
            _LAST_REQUEST = monotonic()
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            return fallback
        params = dict(key=self.settings.amap_api_key,
                      origin=f"{start.longitude:.6f},{start.latitude:.6f}",
                      destination=f"{end.longitude:.6f},{end.latitude:.6f}", extensions="all")
        path = "transit/integrated" if mode == "transit" else mode
        if mode == "transit":
            params.update(city=self.city, cityd=self.city, date=date, time=bucket, strategy=0)
        try:
            # Socket timeouts plus build deadline prevent unbounded sequential queries.
            with httpx.Client(timeout=min(2, remaining), follow_redirects=False) as client:
                with span('map_route','direction'):
                    from ..infrastructure.provider_guard import map_http
                    response = map_http(lambda: client.get(f"https://restapi.amap.com/v3/direction/{path}", params=params))
                    response.raise_for_status()
                    payload = response.json()
            if str(payload.get("status")) != "1":
                emit('map_route','fallback','degraded',reason='provider_rejected')
                return fallback
            route = payload["route"]
            choices = route.get("transits" if mode == "transit" else "paths")
            if not isinstance(choices, list):
                raise ValueError('Missing route list')
            if not choices and self.settings.cache_enabled:
                raise _NoRoadRoute()
            part = choices[0]
            seconds = float(part["duration"])
            if mode == "transit":
                # route.distance is walking-only, not the complete transit journey.
                meters = sum(float(s.get("walking", {}).get("distance") or 0)
                             + sum(float(b.get("distance") or 0) for b in s.get("bus", {}).get("buslines", [])[:1])
                             for s in part.get("segments", []))
            else:
                meters = float(part["distance"])
            raw_cost = (meters / 1000 * .8 + float(part.get("tolls") or 0)) if self_drive else 0 if mode == "walking" else route.get("taxi_cost") if driving else part.get("cost")
            cost = float(raw_cost) if raw_cost not in (None, "", []) else fallback.cost
            if not all(isfinite(v) and v >= 0 for v in (seconds, meters, cost)) or seconds <= 0:
                return fallback
            result = RouteLeg(mode=mode, distance_km=round(meters / 1000, 2),
                              duration_minutes=ceil(seconds / 60), cost=cost,
                              cost_unit=fallback.cost_unit, source="amap_route",
                              is_estimated=False, cost_is_estimated=True,
                              cost_source="fuel_model" if self_drive else "heuristic" if raw_cost in (None, "", []) else "amap_reference",
                              checked_at=datetime.now(timezone.utc).isoformat(),
                              note="高德道路规划预期耗时，非未来路况保证；费用仅参考，不是订单报价"
                                   + ("；自驾按0.8元/公里油费加道路收费，不含停车与租车" if self_drive else "")
                                   + ("；接口缺价格，使用经验费用" if raw_cost in (None, "", []) else ""))
            if not self.settings.cache_enabled:
                with _LOCK:
                    _CACHE[key] = (monotonic() + 900, result)
                    while len(_CACHE) > 512:
                        _CACHE.popitem(last=False)
            return result.model_copy(deep=True)
        except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError, ProviderBusyError):
            emit('map_route','fallback','degraded',reason='query_or_parse_failed')
            return fallback
