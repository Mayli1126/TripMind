<template>
  <div v-if="advisory" class="advisory">
    <strong>{{ reservationLabel }}<span v-if="advisory.closure_rules.length"> · 攻略闭馆线索</span></strong>
    <p v-if="advisory.opening_hours_note">地图开放时间：{{ advisory.opening_hours_note }}</p>
    <small v-if="advisory.opening_checked_at">地图查询：{{ advisory.opening_checked_at }}{{ openingExpired ? ' · 已过期，请再次确认' : ' · 不保证未来临时调整' }}</small>
    <p v-if="advisory.reservation_channel">预约渠道：{{ advisory.reservation_channel }}</p>
    <a v-if="advisory.fact_status === 'verified' && !expired && safeBookingUrl" :href="safeBookingUrl" target="_blank" rel="noopener noreferrer">前往预约渠道 ↗</a>
    <p v-for="tip in tips.slice(0, 2)" :key="tip">{{ tip }}</p>
    <small>{{ expired ? '预约政策已过期，请再次确认' : advisory.fact_status === 'verified' ? '预约政策来自已配置的官方来源；攻略提醒仍待核实' : '预约要求未获官方核验，请出发前确认' }}</small>
    <p v-for="note in advisory.refresh_errors || []" :key="note">{{ note }}</p>
    <details><summary>全部提醒与来源</summary><p v-for="tip in tips" :key="tip">{{ tip }}</p><p v-for="source in advisory.source_refs" :key="source" class="source">{{ source }}</p><p v-if="!advisory.source_refs.length">来源记录缺失，不作为已核实事实。</p><p>最近核对：{{ advisory.verified_at || '尚未核对' }}</p></details>
  </div>
</template>
<script setup lang="ts">
import { computed } from 'vue'
import type { AttractionAdvisory } from '@/types'
const props = defineProps<{ advisory?: AttractionAdvisory | null }>()
const tips = computed(() => [...new Set([...(props.advisory?.closure_rules || []), ...(props.advisory?.pitfall_tips || [])])])
const expired = computed(() => !!props.advisory?.expires_at && Date.parse(props.advisory.expires_at) <= Date.now())
const openingExpired = computed(() => !!props.advisory?.opening_expires_at && Date.parse(props.advisory.opening_expires_at) <= Date.now())
const reservationLabel = computed(() => {
  const value = props.advisory
  if (!value || expired.value) return '预约要求待重新核实'
  if (value.fact_status === 'verified') return value.reservation_required === 'true' ? '需预约 · 官方政策源' : value.reservation_required === 'false' ? '政策源注明无需预约' : '预约要求待核实'
  return value.reservation_required === 'true' ? '攻略提示需预约' : '预约要求待核实'
})
const safeBookingUrl = computed(() => { try { const url = new URL(props.advisory?.reservation_url || ''); return url.protocol === 'https:' ? url.href : '' } catch { return '' } })
</script>
<style scoped>
.advisory{margin:12px 0;padding:12px 14px;border:1px solid #ead7ad;border-radius:10px;background:#fff9ec;color:#78572b;font-size:12px;line-height:1.8}.advisory strong{font-size:13px}.advisory p{margin:6px 0;white-space:pre-wrap}.advisory small{color:#977a53}.advisory summary{cursor:pointer;margin-top:6px}.source{overflow-wrap:anywhere;color:#8a826f}
</style>
