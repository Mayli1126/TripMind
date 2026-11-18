"""为首次编排准备最小决策资料；完整地图事实始终留在后端。"""
from ..models.schemas import Location
from .route_optimizer import RouteOptimizer


def compact_schema(value):
    """只删除展示元数据，保留类型、必填、枚举和取值范围等校验约束。"""
    if isinstance(value, list):
        return [compact_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in {"title", "description", "default", "examples"}:
            continue
        # properties/$defs的键是字段名，不是Schema元数据，不能按名字误删。
        result[key] = ({name: compact_schema(schema) for name, schema in item.items()}
                       if key in {"properties", "$defs"} else compact_schema(item))
    return result


def validate_recommendation_groups(candidates, selected_ids):
    """同一推荐的地图备选最多使用一个；这里只检查分组身份，不推断名称含义。"""
    selected = {}
    for key in selected_ids:
        candidate = candidates[key]
        for group in candidate.match_evidence.get("recommendation_groups", []):
            selected.setdefault(group["id"], set()).add(candidate.poi_id)
    if any(len(pois) > 1 for pois in selected.values()):
        raise ValueError("同一推荐地点的多个地图候选被同时安排，只能选一条")


def build_planner_context(request, context, candidates, capacities, schema):
    aliases = {f"P{i}": key for i, key in enumerate(candidates, 1)}
    short = {key: alias for alias, key in aliases.items()}
    poi_aliases = {c.poi_id: short[key] for key, c in candidates.items() if c.poi_id}
    places, groups = [], {}
    for key, c in candidates.items():
        place = dict(id=short[key], name=c.canonical_name, category=c.category,
            minutes=None if c.match_evidence.get("method") == "verified_parent_scenic" else c.suggested_duration_minutes,
            required=c.user_choice == "required", preferred=c.user_choice == "preferred",
            time=c.suitable_time, scope=c.match_evidence.get("planning_scope", "place"))
        for group in c.match_evidence.get("recommendation_groups", []):
            if group["id"] not in groups:
                groups[group["id"]] = dict(id=f"G{len(groups)+1}", recommendation=group["name"], options=[])
            groups[group["id"]]["options"].append(short[key])
        if c.match_evidence.get("method") == "amap_group_candidates":
            place["map_type"] = c.match_evidence.get("map_category", "")
            place["address"] = c.address
        if c.parent_poi_id in poi_aliases:
            place["parent"] = poi_aliases[c.parent_poi_id]
        if c.recommendation_reasons:
            place["reason"] = c.recommendation_reasons[0][:120]
        if c.warnings:
            place["warnings"] = c.warnings
        # 地图位置保留给模型作空间参考，完整地址与类型仅用于辨认首条结果。
        place["position"] = [round(c.location.longitude, 5), round(c.location.latitude, 5)]
        places.append(place)
    distances, seen = [], set()
    for key, c in candidates.items():
        neighbors = sorted((k for k in candidates if k != key),
            key=lambda k: RouteOptimizer.distance(c.location, candidates[k].location))
        for other in neighbors[:4]:
            pair = tuple(sorted((short[key], short[other])))
            if pair not in seen:
                seen.add(pair)
                distances.append([*pair, round(RouteOptimizer.distance(c.location, candidates[other].location), 1)])
    hotels, seen_hotels = [], set()
    for h in context.get("hotel_candidates", []):
        if not h.get("location"):
            continue
        location = Location.model_validate(h["location"])
        identity = (h.get("name", ""), location.longitude, location.latitude)
        if identity in seen_hotels:
            continue
        seen_hotels.add(identity)
        hotels.append(dict(name=h.get("name", ""), distances=[
            [short[key], round(RouteOptimizer.distance(location, c.location), 1)] for key, c in candidates.items()]))
    # 用户原文及确认的时间完整保留，不为缩短输入截断否定、必去和时长等要求。
    payload = dict(request=dict(city=request.city, pace=request.pace, preferences=request.preferences,
        requirements=request.free_text_input, memory=request.memory_context,
        conversation=request.conversation_context, travelers=request.travelers,
        transportation=request.transportation, optimization_goal=request.optimization_goal,
        arrival_time=request.arrival_time, return_departure_time=request.return_departure_time),
        days=[dict(date=c.date, reference_minutes=c.available_minutes, note=c.note) for c in capacities],
        candidates=places, recommendation_groups=[g for g in groups.values() if len(g["options"]) > 1], distance_km=distances, hotels=hotels,
        output_schema=compact_schema(schema))
    return payload, aliases
