<template>
  <div class="transport-choice">
    <button class="transport-summary" :disabled="disabled" :aria-expanded="expanded" @click="toggle">
      {{ item.description }} <span>{{ expanded ? '收起' : '切换方式 ›' }}</span>
    </button>
    <div v-if="expanded" class="transport-options" aria-label="选择交通方式">
      <strong>选择交通方式</strong>
      <p v-if="loading" role="status">正在查询道路路线…</p>
      <p v-else-if="error" role="alert">{{ error }} <button @click="load">重试</button></p>
      <template v-else>
        <button v-for="option in options" :key="option.mode" class="transport-option"
          :class="{ active: (item.selected_route_mode || item.route_leg?.mode) === option.mode }"
          :disabled="disabled || !option.available" @click="$emit('select', option.mode)">
          <span>{{ labels[option.mode] }}</span>
          <span v-if="option.route">{{ option.route.distance_km }}公里 · 约{{ option.route.duration_minutes }}分钟</span>
          <span v-else>暂无路线</span>
        </button>
        <small>按参考时段查询；驾车不含停车时间，骑行不含找车时间。费用以实际出行为准。</small>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { apiClient } from '@/services/api'
import type { ScheduleItem, TransportMode } from '@/types'
const props = defineProps<{ item: ScheduleItem; city: string; date: string; disabled?: boolean }>()
defineEmits<{ select: [mode: TransportMode] }>()
const labels: Record<TransportMode, string> = { walking: '步行', cycling: '骑行', driving: '驾车', transit: '公共交通' }
const expanded = ref(false)
const loading = ref(false)
const error = ref('')
const options = ref<{ mode: TransportMode; available: boolean; route?: ScheduleItem['route_leg'] }[]>([])
// 每个路段首次展开才查询；重复展开复用结果，失败可由用户主动重试。
async function load() {
  if (!props.item.route_origin || !props.item.route_destination) {
    error.value = '该路段缺少接驳坐标，请先重新生成路线'
    return
  }
  loading.value = true
  error.value = ''
  try {
    const { data } = await apiClient.post('/api/map/transport-options', {
      city: props.city, date: props.date, origin: props.item.route_origin,
      destination: props.item.route_destination, reference_time: props.item.route_reference_time || '10:00'
    })
    options.value = data.options
  } catch { error.value = '交通查询失败，请稍后重试' }
  finally { loading.value = false }
}
function toggle() {
  expanded.value = !expanded.value
  if (expanded.value && !options.value.length && !loading.value) void load()
}
</script>

<style scoped>
.transport-choice { margin: 8px 0 14px 48px; }
.transport-summary { width: 100%; text-align: left; border: 0; background: transparent; color: #52646a; padding: 8px 0; cursor: pointer; font-size: 13px; }
.transport-summary span { float: right; font-size: 12px; }
.transport-options { padding: 16px; border: 1px solid #dbe3e2; border-radius: 14px; background: #fff; }
.transport-options strong { display: block; margin-bottom: 10px; }
.transport-option { display: flex; justify-content: space-between; gap: 10px; width: 100%; padding: 12px 10px; margin: 4px 0; border: 1px solid transparent; background: transparent; border-radius: 8px; cursor: pointer; text-align: left; }
.transport-option.active { border-color: #315f55; background: #f0f6f3; }
.transport-option:disabled { color: #8c9294; cursor: default; }
.transport-options small { display: block; color: #6e7977; margin-top: 10px; line-height: 1.6; }
</style>
