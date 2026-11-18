<template>
  <div class="revision-diff">
    <p v-if="explanation" class="diff-explanation">{{ explanation }}</p>
    <ul v-if="changes.length" class="diff-list">
      <li v-for="(change, index) in changes" :key="index" class="diff-item">
        <span class="diff-icon" :class="`op-${change.op}`">{{ iconOf(change.op) }}</span>
        <div class="diff-body">
          <strong>{{ change.title }}</strong>
          <span v-if="change.detail" class="diff-detail">{{ change.detail }}</span>
        </div>
      </li>
    </ul>
    <p v-else class="diff-empty">未提供具体修改明细，请核对修改结果后再确认。</p>
    <p v-if="plan?.budget" class="diff-explanation">修改后预算参考：¥{{ plan.budget.total }}<span v-if="plan.budget.over_budget">（超出预算上限，请确认是否接受）</span></p>
    <details v-if="plan?.warnings?.length"><summary>查看时间安排与预算提醒（{{ plan.warnings.length }}）</summary><ul><li v-for="warning in plan.warnings" :key="warning">{{ warning }}</li></ul></details>
    <p class="diff-hint">确认前不会改动当前行程；未受影响的天保持原样。</p>
  </div>
</template>

<script setup lang="ts">
import type { RevisionChange, TripPlan } from '@/types'

defineProps<{
  changes: RevisionChange[]
  explanation?: string
  plan?: TripPlan
}>()

const ICONS: Record<string, string> = {
  add_place: '＋',
  remove_place: '－',
  replace_place: '⇄',
  move_place: '→',
  reorder_day: '↕',
  change_duration: '◷',
  update_description: '✎',
  change_start_time: '◐',
  change_pace: '≈',
  update_transport: '🚆',
  full_regen: '✦'
}

const iconOf = (op: string): string => ICONS[op] || '•'
</script>

<style scoped>
.revision-diff {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.diff-explanation {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: #14213d;
}
.diff-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.diff-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 12px;
  border-radius: 10px;
  background: #f6f7fa;
}
.diff-icon {
  width: 30px;
  height: 30px;
  flex: 0 0 auto;
  display: grid;
  place-items: center;
  border-radius: 9px;
  background: #14213d;
  color: #ffd166;
  font-size: 14px;
}
.diff-icon.op-remove_place { background: #8d99ae; color: #fff; }
.diff-icon.op-add_place { background: #2a9d8f; color: #fff; }
.diff-body {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.diff-body strong {
  font-size: 13px;
  color: #14213d;
}
.diff-detail {
  font-size: 12px;
  color: #778398;
  line-height: 1.5;
}
.diff-empty {
  margin: 0;
  font-size: 13px;
  color: #778398;
}
.diff-hint {
  margin: 0;
  font-size: 12px;
  color: #a8b0bf;
}
</style>
