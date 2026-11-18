"""规划协议示例：虚构短例演示首次编排，苏州示例演示局部修复。"""

_D1, _D2 = "2099-11-24", "2099-11-25"


def _place(key, minutes, reason, slot="day"):
    return dict(candidate_id="EXAMPLE_" + key, duration_minutes=minutes,
                visit_mode="classic", duration_reason=reason, suitable_time=slot,
                entry_name="", exit_name="")


# 独立示例编号避免与真实候选混淆；只演示邻近分天、父子展示和备用点。
# 地点与距离均为虚构，不承载营业、预约或真实地图知识。
INITIAL_EXAMPLE = {
    "input": {
        "request": {"requirements": "经典游玩，必须去湖畔景区，可以逛夜间街区"},
        "days": [{"date": _D1}, {"date": _D2}],
        "candidates": [
            {"id": "EXAMPLE_P1", "name": "湖畔景区", "minutes": 240, "required": True},
            {"id": "EXAMPLE_P2", "name": "景区内古桥", "parent": "EXAMPLE_P1"},
            {"id": "EXAMPLE_P3", "name": "湖畔历史街区", "minutes": 90, "time": "evening"},
            {"id": "EXAMPLE_P4", "name": "城市博物馆", "minutes": 120},
            {"id": "EXAMPLE_P5", "name": "老城步行街", "minutes": 90},
            {"id": "EXAMPLE_P6", "name": "远郊公园", "minutes": 120},
        ],
        "distance_km": [["EXAMPLE_P1", "EXAMPLE_P3", 1.2],
                        ["EXAMPLE_P4", "EXAMPLE_P5", 0.8],
                        ["EXAMPLE_P1", "EXAMPLE_P4", 8],
                        ["EXAMPLE_P5", "EXAMPLE_P6", 25]],
    },
    "output": {
        "days": [
            {"date": _D1, "theme": "湖畔景区与历史街区", "places": [
                {"candidate_id": "EXAMPLE_P1", "duration_minutes": 240,
                 "recommended_child_ids": ["EXAMPLE_P2"],
                 "internal_visit_suggestions": "沿湖漫步并游览古桥，已包含在景区时长内。"},
                {"candidate_id": "EXAMPLE_P3", "duration_minutes": 90, "suitable_time": "evening"}]},
            {"date": _D2, "theme": "博物馆与老城漫步", "places": [
                {"candidate_id": "EXAMPLE_P4", "duration_minutes": 120},
                {"candidate_id": "EXAMPLE_P5", "duration_minutes": 90}]},
        ],
    },
}


REPAIR_EXAMPLE = {
    "input": {
        "assumptions": "示例假定备用点已通过同城、身份与附近范围核验；不代表实时交通和预约信息。",
        "editable_dates": [_D2], "frozen_dates": [_D1],
        "current_days": [
            dict(date=_D1, frozen=True, place_names=["拙政园", "苏州博物馆", "平江路历史街区"]),
            dict(date=_D2, places=[dict(id="EXAMPLE_EXISTING_LIUYUAN", name="留园", duration_minutes=120,
                                      required=True, duration_source="user_confirmed")])],
        "issue": "第二天只有留园约两小时，普通完整游览日偏空。保留留园及其时长，只补第二天。",
        "backup_candidates": [
            dict(candidate_id="EXAMPLE_XIYUAN", name="西园寺", nearby_dates=[_D2], suitable_time="day"),
            dict(candidate_id="EXAMPLE_SHANTANG", name="山塘街", nearby_dates=[_D2], suitable_time="evening")],
        "transport_reference": "示例假定留园至西园寺约15分钟，西园寺至山塘街约25分钟，真实耗时由程序复核。",
    },
    "output": {
        "days": [dict(date=_D2,
            ordered_place_ids=["EXAMPLE_EXISTING_LIUYUAN", "EXAMPLE_XIYUAN", "EXAMPLE_SHANTANG"],
            additional_places=[_place("XIYUAN", 90, "参观寺院建筑与园林。"),
                               _place("SHANTANG", 90, "历史街区漫步观景，不重复计时。", "evening")],
            arrangement_reason="保留留园原120分钟，补入附近西园寺和山塘街；先白天项目，再晚间街区，不改第一天。",
            single_place_reason="")],
        "duration_updates": [], "omitted_place_ids": [],
    },
}
