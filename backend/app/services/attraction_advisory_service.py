"""保守投影攻略线索并保留来源，不发起外部查询。"""
from ..models.advisory import AttractionAdvisory
from ..models.candidate import CandidatePlace
from .candidate_pool_service import normalize_name


def enrich_advisories(plan, request):
    candidates = [CandidatePlace.model_validate(c) for c in (getattr(request, 'candidate_places', None) or [])]
    candidates = [c for c in candidates
                  if c.user_choice != 'excluded' and c.city == request.city]
    notes = []
    for day in plan.days:
        for attraction in day.attractions:
            # 尚无可信实时提醒来源，不能接受生成内容声称已核验。
            attraction.advisory = None
            if attraction.poi_id:
                matches = [c for c in candidates if c.poi_id == attraction.poi_id]
            else:
                matches = [c for c in candidates if normalize_name(attraction.name) in
                           {normalize_name(c.canonical_name), *(normalize_name(a) for a in c.aliases)}]
                # 不能仅凭名称将不同地图地点合并为同一地点。
                if len({c.poi_id for c in matches if c.poi_id}) > 1:
                    matches = []
            tips = list(dict.fromkeys(tip for c in matches for tip in c.warnings if tip.strip()))
            if not tips:
                if attraction.poi_id:
                    from .advisory_refresh_service import overlay
                    attraction.advisory = overlay(None, attraction.poi_id, day.date)
                continue
            reservation = any(any(token in tip for token in ('需预约', '需要预约', '提前预约', '必须预约', '预约制'))
                              and not any(token in tip for token in ('无需预约', '不需预约', '不需要预约', '非预约制')) for tip in tips)
            closure = [tip for tip in tips if any(word in tip for word in ('闭馆', '闭园', '暂停开放', '不开放'))]
            attraction.advisory = AttractionAdvisory(
                reservation_required='true' if reservation else 'unknown',
                closure_rules=closure, pitfall_tips=[tip for tip in tips if tip not in closure],
                source_refs=list(dict.fromkeys(ref for c in matches for ref in c.source_refs)),
                # 地图地点核验不能证明预约政策或营业状态。
                fact_status='unverified', confidence=0,
            )
            if reservation or closure:
                notes.append(f'「{attraction.name}」存在攻略预约或闭馆线索，出发前请向官方核实。')
            if attraction.poi_id:
                from .advisory_refresh_service import overlay
                attraction.advisory = overlay(attraction.advisory, attraction.poi_id, day.date)
    return list(dict.fromkeys(notes))
