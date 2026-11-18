"""核验父景区内部起终点；模型只提名称，坐标与归属必须由地图证明。"""
from ..models.schemas import RouteAnchor
from time import monotonic
from .amap_service import AmapService
from .candidate_pool_service import normalize_name
from .tourism_research_service import _city_name, _scenic_parent_name, _place_name


def resolve_plan_anchors(plan, day_indexes=None, *, amap=None):
    amap = amap or AmapService()
    remaining = 4  # 每次收尾最多四个名称查询；已核验或失败过的同一地点不重复请求。
    deadline = monotonic() + 6
    for index, day in enumerate(plan.days):
        if day_indexes is not None and index not in day_indexes:
            continue
        for attraction in day.attractions:
            if attraction.route_anchors_checked:
                continue
            if attraction.match_evidence.get("planning_scope") != "parent_scenic_area":
                continue
            names = list(dict.fromkeys(n for n in (attraction.proposed_entry_name, attraction.proposed_exit_name) if n))
            points = {}
            for name in names:
                if remaining <= 0 or monotonic() >= deadline:
                    break
                remaining -= 1
                try:
                    matches = {}
                    for poi in amap.search_poi_once(name, plan.city, timeout=max(.2, min(2, deadline - monotonic()))):
                        parent_name, child_name = _scenic_parent_name(poi.name)
                        relation = (poi.parent_poi_id == attraction.poi_id or
                                    (parent_name and _place_name(parent_name, plan.city) == _place_name(attraction.name, plan.city)))
                        named = normalize_name(name) in {normalize_name(poi.name), normalize_name(child_name)}
                        if (relation and named and poi.id != attraction.poi_id and poi.city_name
                                and (poi.location.longitude != 0 or poi.location.latitude != 0)
                                and _city_name(poi.city_name) == _city_name(plan.city)):
                            matches[poi.id] = poi
                    if len(matches) == 1:
                        poi = next(iter(matches.values()))
                        points[name] = RouteAnchor(poi_id=poi.id, name=poi.name, location=poi.location)
                except Exception:
                    # 单个锚点失败不阻断行程，但绝不把模型名称配上猜测坐标。
                    pass
            attraction.route_entry = points.get(attraction.proposed_entry_name)
            attraction.route_exit = points.get(attraction.proposed_exit_name)
            attraction.route_anchors_checked = True
            if not attraction.route_entry or not attraction.route_exit:
                plan.warnings.append(f"「{attraction.name}」部分游览起终点未取得明确地图归属，相关交通仍用景区代表坐标粗略参考。")
