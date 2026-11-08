"""Public facts only. Never include trips, owners, guide text or preferences in keys."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
import httpx
from ..infrastructure.shared_cache import make_key
from ..infrastructure.cache_rebuild import cache_rebuilder
from ..infrastructure.provider_guard import map_http, provider_guard
from .cache_policy import make_entry


def opening(poi_id, visit_date, settings):
    def load():
        from .amap_service import _wait_for_amap_poi_slot
        _wait_for_amap_poi_slot()
        with httpx.Client(timeout=8, follow_redirects=False) as client:
            response = map_http(lambda: client.get('https://restapi.amap.com/v5/place/detail', params={
                'key': settings.amap_api_key, 'id': poi_id, 'show_fields': 'business'}), settings.amap_api_key)
            response.raise_for_status()
            data = response.json()
        if str(data.get('status')) != '1':
            raise ValueError('地图接口未返回成功状态')
        poi = next((p for p in data.get('pois', []) if p.get('id') == poi_id), None)
        hours = (poi.get('business') or {}).get('opentime_week') if poi else None
        if not isinstance(hours, str) or not hours.strip():
            raise ValueError('地图暂无可解析的开放时间')
        now = datetime.now(timezone.utc)
        entry = make_entry('opening_hours', dict(note=hours[:1500], checked_at=now.isoformat(),
            source='https://lbs.amap.com/api/webservice/guide/api/newpoisearch'), 'amap_v5_business',
            source_expires_at=(now + timedelta(hours=max(1, min(24, settings.advisory_cache_hours)))).timestamp())
        entry.data['expires_at'] = datetime.fromtimestamp(entry.hard_until, timezone.utc).isoformat()
        return entry
    key = make_key('opening_hours', dict(provider='amap', api='v5/place/detail', fields='business',
        poi_id=poi_id, visit_date=visit_date, hours_cap=max(1, min(24, settings.advisory_cache_hours))))
    return cache_rebuilder.get(key, load).data if settings.cache_enabled else load().data


def validate_policy(policy, poi_id, visit_date, settings, now=None):
    hosts = {h.strip().lower() for h in settings.advisory_official_hosts.split(',') if h.strip()}
    source = urlparse(policy.source_url)
    booking = urlparse(policy.reservation_url) if policy.reservation_url else None
    if source.scheme != 'https' or source.hostname not in hosts or (booking and (booking.scheme != 'https' or booking.hostname not in hosts)):
        raise ValueError('政策来源未获部署者信任')
    if policy.poi_id != poi_id or policy.visit_date != visit_date or not policy.verified_at.tzinfo or not policy.expires_at.tzinfo:
        raise ValueError('政策地点、日期或时区不匹配')
    now = now or datetime.now(timezone.utc)
    if not policy.verified_at <= now < policy.expires_at or policy.expires_at > policy.verified_at + timedelta(days=7):
        raise ValueError('政策时效不合规')
    return policy


def official_policy(poi_id, visit_date, settings):
    from .advisory_refresh_service import OfficialPolicy
    def load():
        if urlparse(settings.advisory_provider_url).scheme != 'https':
            raise ValueError('政策服务必须使用HTTPS')
        with httpx.Client(timeout=8, follow_redirects=False) as client:
            with provider_guard.slot('policy', '', settings.advisory_provider_url):
                response = client.get(settings.advisory_provider_url, params={'poi_id': poi_id, 'visit_date': visit_date})
                response.raise_for_status()
            policy = validate_policy(OfficialPolicy.model_validate(response.json()), poi_id, visit_date, settings)
        return make_entry('official_policy', policy.model_dump(mode='json'), 'trusted_policy_adapter',
            fetched_at=policy.verified_at.timestamp(), source_expires_at=policy.expires_at.timestamp())
    # Hash service identity and trust policy; no endpoint/credential text appears in cache keys or logs.
    scope = hashlib.sha256(json.dumps([settings.advisory_provider_url,
        sorted(h.strip().lower() for h in settings.advisory_official_hosts.split(',') if h.strip())]).encode()).hexdigest()
    key = make_key('official_policy', dict(provider='trusted_adapter_v1', trust_scope=scope,
        poi_id=poi_id, visit_date=visit_date))
    entry = cache_rebuilder.get(key, load) if settings.cache_enabled else load()
    policy = validate_policy(OfficialPolicy.model_validate(entry.data), poi_id, visit_date, settings)
    # Snapshot must not outlive the cache's original hard limit either.
    policy.expires_at = min(policy.expires_at, datetime.fromtimestamp(entry.hard_until, timezone.utc))
    return policy.model_dump(mode='json')
