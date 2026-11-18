import type { Attraction, TripPlan } from '@/types'

export function revisionFailure(plan: TripPlan): string | null {
  if (plan.revision_status === 'rejected' || plan.revision_status === 'unchanged' || plan.revision_status === 'clarify_needed') {
    return plan.revision_message || '本次修改未生效，当前计划保持不变。'
  }
  // 兼容没有显式状态的历史草稿，避免将失败结果再次提交保存。
  if (!plan.revision_status) {
    const failure = (plan.warnings || []).find(text =>
      /本次.*(?:未通过|未产生)|修改失败.*保留原计划/.test(text))
    if (failure) return failure
  }
  return null
}

export interface ManualOperation {
  op: 'remove_place' | 'move_place' | 'reorder_day' | 'change_duration' | 'update_description'
  target_stable_id?: string
  day_index?: number
  position?: number
  ordered_stable_ids?: string[]
  duration_minutes?: number
  description?: string
  reason: string
}

// 将编辑前后的差异转换为明确操作；只发送用户修改的字段，不提交整份任意行程。
export function manualRevisionOperations(before: TripPlan, after: TripPlan): ManualOperation[] {
  if (before.days.length !== after.days.length) throw new Error('手动编辑不能改变行程天数')
  const flatten = (plan: TripPlan) => {
    const result = new Map<string, { item: Attraction; day: number }>()
    plan.days.forEach((day, index) => day.attractions.forEach(item => {
      if (!item.stable_id || result.has(item.stable_id)) throw new Error('景点标识缺失或重复，请刷新行程后重试')
      result.set(item.stable_id, { item, day: index })
    }))
    return result
  }
  const old = flatten(before), next = flatten(after)
  const operations: ManualOperation[] = []
  for (const [id, entry] of old) {
    if (!next.has(id)) operations.push({ op: 'remove_place', target_stable_id: id, reason: `删除「${entry.item.name}」` })
  }
  for (const [id, entry] of next) {
    const previous = old.get(id)
    if (!previous) throw new Error('添加地点请使用地图选点或自然语言修改入口')
    if (entry.day !== previous.day) operations.push({ op: 'move_place', target_stable_id: id, day_index: entry.day,
      reason: `将「${entry.item.name}」从第${previous.day + 1}天移到第${entry.day + 1}天` })
  }
  after.days.forEach((day, index) => {
    if (!day.attractions.length) throw new Error(`第${index + 1}天至少保留一个景点`)
    const ids = day.attractions.map(item => item.stable_id!)
    if (JSON.stringify(ids) !== JSON.stringify(before.days[index].attractions.map(item => item.stable_id))) {
      // 先完成删除和跨日移动，再提交目标日期的完整顺序，消除索引偏移。
      operations.push({ op: 'reorder_day', day_index: index, ordered_stable_ids: ids,
        reason: day.attractions.map(item => item.name).join(' → ') })
    }
  })
  for (const [id, entry] of next) {
    const previous = old.get(id)!.item
    if (entry.item.visit_duration !== previous.visit_duration && (!Number.isInteger(entry.item.visit_duration) || entry.item.visit_duration < 15 || entry.item.visit_duration > 720)) {
      throw new Error(`「${entry.item.name}」的游览时长需要填写15～720的整数分钟`)
    }
    if (entry.item.visit_duration !== previous.visit_duration) operations.push({ op: 'change_duration', target_stable_id: id,
      duration_minutes: entry.item.visit_duration, reason: `「${entry.item.name}」：${previous.visit_duration} → ${entry.item.visit_duration} 分钟` })
    if (entry.item.description !== previous.description) operations.push({ op: 'update_description', target_stable_id: id,
      description: entry.item.description, reason: `「${entry.item.name}」介绍：${entry.item.description || '（清空）'}` })
  }
  return operations
}
