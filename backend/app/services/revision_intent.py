"""快速识别明确地点名称的排序指令，只覆盖有把握的有限表达。"""
import re
from collections import Counter

from ..models.patch import PlanPatch, PatchOperation


class ClarificationNeeded(ValueError):
    pass


def _day(value):
    if value.isdigit():
        return int(value) - 1
    numbers = {c: i for i, c in enumerate("一二三四五六七八九", 1)}
    if value == "十":
        return 9
    if value in numbers:
        return numbers[value] - 1
    raise ClarificationNeeded("请用数字说明目标是第几天。")


def _locate(plan, text):
    text = text.strip().strip('「」“”\"\'')
    scoped = re.fullmatch(r"第([一二三四五六七八九十\d]+)天的?(.+)", text)
    scope = _day(scoped[1]) if scoped else None
    name = scoped[2] if scoped else text
    matches = [(i, a) for i, day in enumerate(plan.days) for a in day.attractions
               if a.name == name and (scope is None or scope == i)]
    if len(matches) != 1:
        raise ClarificationNeeded(f"无法唯一定位「{text}」，请使用行程中的完整景点名称，重名时注明第几天。")
    return matches[0]


def fast_order_patch(plan, instruction, version):
    """不支持的表达返回 None，不能只解析复合指令的一部分。"""
    text = instruction.strip().rstrip("。！!？?")
    if re.search(r"并且|同时|然后|以及|再把|[，,；;]", text):
        return None
    prefix = r"(?:请)?(?:帮我)?(?:把|将)?"
    verb = r"(?:移到|放到|调整到|排到|挪到|安排到)"
    relation = re.fullmatch(prefix + r"(.+?)" + verb + r"(.+?)(?:的)?(前面|后面|之前|之后|前|后)", text)
    day_move = re.fullmatch(prefix + r"(.+?)" + verb + r"第([一二三四五六七八九十\d]+)天(?:的)?(最前面|最前|第一位|第一个|开头|最后面|最后|最后一位|末尾)?", text)
    swap = re.fullmatch(prefix + r"(.+?)(?:和|与)(.+?)(?:交换(?:顺序|位置)|对调(?:顺序|位置)?)", text)
    if not any((relation, day_move, swap)):
        return None
    original = [[a.stable_id for a in day.attractions] for day in plan.days]
    orders = [list(ids) for ids in original]
    if relation:
        source_day, source = _locate(plan, relation[1])
        target_day, target = _locate(plan, relation[2])
        if source.stable_id == target.stable_id:
            raise ClarificationNeeded("两个位置指向同一个景点，请说明要移动到哪个景点之前或之后。")
        orders[source_day].remove(source.stable_id)
        position = orders[target_day].index(target.stable_id)
        if relation[3] in ("后面", "之后", "后"):
            position += 1
        orders[target_day].insert(position, source.stable_id)
    elif day_move:
        source_day, source = _locate(plan, day_move[1])
        target_day = _day(day_move[2])
        if not 0 <= target_day < len(orders):
            raise ClarificationNeeded("目标日期超出了当前行程，请指定行程内的日期。")
        orders[source_day].remove(source.stable_id)
        position = 0 if day_move[3] in ("最前面", "最前", "第一位", "第一个", "开头") else len(orders[target_day])
        orders[target_day].insert(position, source.stable_id)
    else:
        first_day, first = _locate(plan, swap[1])
        second_day, second = _locate(plan, swap[2])
        first_pos, second_pos = orders[first_day].index(first.stable_id), orders[second_day].index(second.stable_id)
        orders[first_day][first_pos], orders[second_day][second_pos] = second.stable_id, first.stable_id
    changed = [i for i in range(len(orders)) if original[i] != orders[i]]
    # 跨日成员变化使用移动操作，同日排序保持为一个重排操作。
    operations = []
    working = [list(ids) for ids in original]
    for target_day in changed:
        for sid in orders[target_day]:
            source_day = next(i for i, ids in enumerate(working) if sid in ids)
            if source_day != target_day:
                working[source_day].remove(sid)
                working[target_day].append(sid)
                operations.append(PatchOperation(op="move_place", target_stable_id=sid, day_index=target_day))
    for index in changed:
        if orders[index]:
            operations.append(PatchOperation(op="reorder_day", day_index=index, ordered_stable_ids=orders[index]))
    if not operations:
        operations = [PatchOperation(op="reorder_day", day_index=0, ordered_stable_ids=original[0])] if original[0] else []
    if not operations:
        raise ClarificationNeeded("当前没有可调整顺序的景点。")
    return PlanPatch(base_version=version, operations=operations, affected_day_indexes=changed or [0],
                     explanation="已按指定地点和位置调整顺序，其他内容保持不变。")


def order_postconditions(before, after, patch):
    """对纯排序修改校验最终顺序、地点集合及未涉及内容。"""
    if not all(op.op in {"move_place", "reorder_day"} for op in patch.operations):
        return []
    expected = [[a.stable_id for a in day.attractions] for day in before.days]
    try:
        for op in patch.operations:
            if op.op == "move_place":
                source = next(ids for ids in expected if op.target_stable_id in ids)
                source.remove(op.target_stable_id)
                target = expected[op.day_index]
                target.insert(min(op.position if op.position is not None else len(target), len(target)), op.target_stable_id)
            else:
                expected[op.day_index] = list(op.ordered_stable_ids)
        actual = [[a.stable_id for a in day.attractions] for day in after.days]
        if actual != expected:
            return ["修改结果没有保持指定的日期和景点顺序。"]
        routing = {"route_entry", "route_exit", "route_anchors_checked"}
        old = {a.stable_id: a.model_dump(exclude=routing) for day in before.days for a in day.attractions}
        new = {a.stable_id: a.model_dump(exclude=routing) for day in after.days for a in day.attractions}
        if Counter(sid for ids in expected for sid in ids) != Counter(old.keys()) or old != new:
            return ["排序修改改变了景点集合或游览信息，已拒绝保存。"]
        for index, day in enumerate(before.days):
            # 旧餐饮字段也在重建时清空，不能因此把正确排序误判为修改失败。
            derived = {"attractions", "schedule", "theme", "meals", "schedule_mode", "visit_minutes", "transport_minutes", "total_minutes", "timing_notes"}
            if day.model_dump(exclude=derived) != after.days[index].model_dump(exclude=derived):
                return ["排序修改影响了其他行程设置。"]
    except (IndexError, TypeError, StopIteration):
        return ["无法验证修改的目标日期或地点。"]
    return []


def dependent_days(before, after, changed):
    """日终接驳起点变化时，还需要重算次日交通。"""
    result = set(changed)
    def origin(day):
        if day.hotel and day.hotel.location:
            return day.hotel.location
        return day.attractions[-1].location if day.attractions else None
    for index in changed:
        if index + 1 < len(after.days) and origin(before.days[index]) != origin(after.days[index]):
            result.add(index + 1)
    return result
