<template>
  <div class="candidate-board">
    <!-- 概览统计 -->
    <div class="board-summary">
      <span class="summary-item"><i class="dot dot-required"></i>必去 {{ stats.required }}</span>
      <span class="summary-item"><i class="dot dot-preferred"></i>想去 {{ stats.preferred }}</span>
      <span class="summary-item"><i class="dot dot-optional"></i>可选 {{ stats.optional }}</span>
      <span class="summary-item"><i class="dot dot-excluded"></i>不去 {{ stats.excluded }}</span>
      <span v-if="unverifiedCount" class="summary-warn">⚠️ {{ unverifiedCount }} 个地点未在地图确认</span>
    </div>

    <ul class="candidate-list">
      <li
        v-for="place in candidates"
        :key="place.candidate_id"
        class="candidate-item"
        :class="{ 'is-excluded': place.user_choice === 'excluded' }"
      >
        <div class="item-main">
          <div class="item-head">
            <strong class="item-name">{{ place.canonical_name }}</strong>
            <span v-if="place.aliases.length" class="item-aliases">也叫：{{ place.aliases.join(' / ') }}</span>
            <a-tag :color="CATEGORY_COLORS[place.category] || 'default'" class="item-tag">
              {{ CATEGORY_LABELS[place.category] || place.category }}
            </a-tag>
            <a-tag v-if="place.fact_status === 'verified'" color="green" class="item-tag">已核对地图</a-tag>
            <a-tooltip v-else title="未在高德地图确认到该地点，规划时不会参与路线计算">
              <a-tag color="orange" class="item-tag">未确认位置</a-tag>
            </a-tooltip>
          </div>

          <p v-if="place.recommendation_reasons.length" class="item-reasons">
            {{ place.recommendation_reasons.join('；') }}
          </p>
          <p v-for="warning in place.warnings" :key="warning" class="item-warning">⚠ {{ warning }}</p>
          <p class="item-meta">
            建议游玩 {{ place.suggested_duration_minutes }} 分钟
            <template v-if="place.source_refs.length"> · 来源 {{ place.source_refs.length }} 篇</template>
          </p>
        </div>

        <!-- 四档选择：必去 / 想去 / 可选 / 不去 -->
        <a-segmented
          :value="place.user_choice"
          :options="CHOICE_OPTIONS"
          size="small"
          class="item-choice"
          @change="(value) => onChoice(place, value as UserChoice)"
        />
      </li>
    </ul>

    <a-empty v-if="!candidates.length" description="还没有候选地点，先导入一篇攻略吧" />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Empty as AEmpty, Segmented as ASegmented, Tag as ATag, Tooltip as ATooltip } from 'ant-design-vue'
import type { CandidatePlace, UserChoice } from '@/types'

// 受控数据由父页面持有；选择变化通过 change 事件通知父页面持久化
const props = defineProps<{ candidates: CandidatePlace[] }>()
const emit = defineEmits<{ (e: 'change', place: CandidatePlace, choice: UserChoice): void }>()

const CHOICE_OPTIONS: { label: string; value: UserChoice }[] = [
  { label: '必去', value: 'required' },
  { label: '想去', value: 'preferred' },
  { label: '可选', value: 'optional' },
  { label: '不去', value: 'excluded' }
]

const CATEGORY_LABELS: Record<string, string> = {
  attraction: '景点',
  museum: '展馆',
  street: '街区',
  natural: '自然',
  food: '餐饮',
  night: '夜市',
  leisure: '休闲',
  transport_hub: '交通',
  hotel: '住宿'
}

const CATEGORY_COLORS: Record<string, string> = {
  attraction: 'blue',
  museum: 'geekblue',
  street: 'purple',
  natural: 'cyan',
  food: 'orange',
  night: 'magenta',
  leisure: 'lime',
  transport_hub: 'default',
  hotel: 'gold'
}

const stats = computed(() => {
  const result = { required: 0, preferred: 0, optional: 0, excluded: 0 }
  for (const place of props.candidates) result[place.user_choice] += 1
  return result
})

const unverifiedCount = computed(
  () => props.candidates.filter((place) => place.fact_status !== 'verified').length
)

function onChoice(place: CandidatePlace, choice: UserChoice) {
  place.user_choice = choice
  emit('change', place, choice)
}
</script>

<style scoped>
.candidate-board {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.board-summary {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 16px;
  padding: 10px 14px;
  border-radius: 10px;
  background: #f6f7fa;
  font-size: 12px;
  color: #4a5a72;
  font-weight: 600;
}
.summary-item { display: inline-flex; align-items: center; gap: 6px; }
.summary-warn { color: #b26a00; }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.dot-required { background: #ff6b4a; }
.dot-preferred { background: #4776e6; }
.dot-optional { background: #9aa7bb; }
.dot-excluded { background: #d3dae4; }

.candidate-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.candidate-item {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 12px 14px;
  border: 1px solid #e6eaf1;
  border-radius: 12px;
  background: #fff;
}
.candidate-item.is-excluded { opacity: .55; }
.item-main { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.item-head { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.item-name { font-size: 14px; color: #14213d; }
.item-aliases { font-size: 11px; color: #8a96a8; }
.item-tag { margin-inline-end: 0; }
.item-reasons { margin: 0; font-size: 12px; color: #4a5a72; line-height: 1.6; }
.item-warning { margin: 0; font-size: 12px; color: #b26a00; line-height: 1.5; }
.item-meta { margin: 0; font-size: 11px; color: #97a2b4; }
.item-choice { flex: 0 0 auto; }
</style>
