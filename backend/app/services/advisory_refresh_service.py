"""Date-scoped live opening data and opt-in, source-validated policy adapter.

Amap business hours are NOT proof of reservation policy. No LLM verification.
"""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pydantic import BaseModel, Field
from ..config import get_settings
from .auth_service import auth_service


def initialize():
    with auth_service.connection() as db:
        db.execute('CREATE TABLE IF NOT EXISTS advisory_cache (cache_key TEXT PRIMARY KEY, value_json TEXT NOT NULL)')


def key(poi_id, visit_date):
    return f'advisory:v1:{poi_id}:{visit_date}'


def cached(poi_id, visit_date):
    try:
        with auth_service.connection() as db:
            row = db.execute('SELECT value_json FROM advisory_cache WHERE cache_key=?', (key(poi_id, visit_date),)).fetchone()
    except sqlite3.OperationalError:
        return {}
    return json.loads(row['value_json']) if row else {}


class OfficialPolicy(BaseModel):
    poi_id: str
    visit_date: str
    source_url: str
    reservation_required: bool
    reservation_channel: str = Field(default='', max_length=300)
    reservation_url: str = ''
    verified_at: datetime
    expires_at: datetime


def refresh(poi_id, visit_date):
    settings = get_settings()
    value = cached(poi_id, visit_date)
    now = datetime.now(timezone.utc)
    if not settings.cache_enabled and value.get('next_check') and datetime.fromisoformat(value['next_check']) > now:
        return value
    errors = []
    # Preserve previous successful facts if either source fails; never extend their expiry on failure.
    if settings.amap_api_key:
        try:
            from .public_advisory_cache import opening
            value['opening'] = opening(poi_id, visit_date, settings)
        except Exception:
            errors.append('营业时间刷新未成功，保留已有信息；请向景区核实')
    else:
        errors.append('未配置地图Web服务Key')
    if settings.advisory_provider_url:
        try:
            from .public_advisory_cache import official_policy
            value['policy'] = official_policy(poi_id, visit_date, settings)
        except Exception:
            errors.append('官方预约政策刷新失败，保留旧值并按原时效提示')
    else:
        errors.append('未接入官方预约政策源；地图营业时间不代表预约要求已核验')
    value['errors'] = errors
    value['last_attempt'] = now.isoformat()
    # Short backoff on partial failure; successful facts carry independent expiry times.
    value['next_check'] = (now + timedelta(minutes=10 if errors else 60)).isoformat()
    with auth_service.connection() as db:
        db.execute('INSERT OR REPLACE INTO advisory_cache VALUES (?, ?)', (key(poi_id, visit_date), json.dumps(value, ensure_ascii=False)))
    return value


def overlay(advisory, poi_id, visit_date):
    value = cached(poi_id, visit_date)
    from ..models.advisory import AttractionAdvisory
    from .public_advisory_cache import validate_policy
    now = datetime.now(timezone.utc)
    settings = get_settings()
    # Stored trip projections and SQLite snapshots must not retain expired verification.
    if advisory and advisory.fact_status == 'verified':
        advisory.fact_status = 'unverified'
        advisory.reservation_required = 'unknown'
        advisory.reservation_channel = advisory.reservation_url = ''
    if advisory and advisory.opening_expires_at:
        try:
            valid_opening = datetime.fromisoformat(advisory.opening_expires_at) > now
        except (ValueError, TypeError):
            valid_opening = False
        if not valid_opening:
            advisory.opening_hours_note = ''
    if not value:
        return advisory
    advisory = advisory or AttractionAdvisory()
    advisory.refresh_errors = value.get('errors', [])
    opening = value.get('opening')
    if opening:
        try:
            if datetime.fromisoformat(opening['expires_at']) <= now:
                opening = None
        except (KeyError, ValueError, TypeError):
            opening = None
    if opening:
        advisory.opening_hours_note = opening['note']
        advisory.opening_checked_at = opening['checked_at']
        advisory.opening_expires_at = opening['expires_at']
        advisory.source_refs = list(dict.fromkeys([*advisory.source_refs, '地图营业时间：' + opening['source']]))
    policy = value.get('policy')
    if policy:
        try:
            if not settings.advisory_provider_url:
                raise ValueError('Policy source disabled')
            policy = validate_policy(OfficialPolicy.model_validate(policy), poi_id, visit_date, settings).model_dump(mode='json')
        except (ValueError, TypeError):
            policy = None
    if policy:
        advisory.reservation_required = 'true' if policy['reservation_required'] else 'false'
        advisory.reservation_channel = policy['reservation_channel']
        advisory.reservation_url = policy['reservation_url']
        advisory.verified_at = policy['verified_at']
        advisory.expires_at = policy['expires_at']
        advisory.fact_status = 'verified'  # policy only, not guide tips or closure rules
        advisory.source_refs = list(dict.fromkeys([*advisory.source_refs, '预约政策：' + policy['source_url']]))
    return advisory


def refresh_trip(trip):
    pairs = list(dict.fromkeys((a.poi_id, day.date) for day in trip.plan.days for a in day.attractions if a.poi_id))
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda item: refresh(*item), pairs[:100]))
    return {'checked': len(results), 'skipped': max(0, len(pairs) - 100),
            'partial': sum(bool(item.get('errors')) for item in results)}


def refresh_due():
    """Operator-triggered refresh for saved trips occurring within seven days."""
    from ..models.schemas import get_app_today
    from .trip_store_service import trip_store_service
    today = get_app_today()
    end = today + timedelta(days=7)
    initialize()
    with auth_service.connection() as db:
        ids = [row['id'] for row in db.execute('SELECT id FROM trips')]
    count = 0
    for trip_id in ids:
        try:
            trip = trip_store_service.get_trip(trip_id)
            trip.plan.days = [day for day in trip.plan.days if today.isoformat() <= day.date <= end.isoformat()]
            if trip.plan.days:
                refresh_trip(trip)
                count += 1
        except Exception:
            print('一份行程刷新未完成，保留原数据。')
    print(f'已处理近期行程：{count} 份。预约政策源未配置时仍显示待核实。')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='刷新未来七天的已保存行程提醒')
    parser.add_argument('--due', action='store_true', required=True)
    parser.parse_args()
    refresh_due()
