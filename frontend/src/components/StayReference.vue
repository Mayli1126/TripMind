<template>
  <div class="stay-reference" :class="{ compact }">
    <div class="stay-symbol" aria-hidden="true">
      <svg viewBox="0 0 32 32" fill="none"><path d="M7 27V6h18v21M4 27h24M13 27v-7h6v7M12 11h2m4 0h2m-8 5h2m4 0h2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" /></svg>
    </div>
    <div class="stay-copy">
      <span v-if="hotel?.type || accommodation" class="stay-type">{{ hotel?.type || accommodation }}</span>
      <h3>{{ hotel?.name || '酒店待选择' }}</h3>
      <p v-if="hotel?.address" class="stay-address">{{ hotel.address }}</p>
      <p v-else class="stay-address">{{ hotel?.name ? '具体位置请在预订时确认' : '尚未推荐具体酒店，可在行程对话中补充住宿要求。' }}</p>
    </div>
    <div class="stay-price">
      <span>{{ hotel?.amap_average_cost != null ? '高德人均消费参考' : hotel?.price_source === 'llm_estimate' ? 'AI房费估价' : '住宿预算预留' }}</span>
      <strong>{{ hotel?.amap_average_cost != null ? `¥${hotel.amap_average_cost}/人` : hotel?.estimated_cost ? `约 ¥${hotel.estimated_cost}/间/晚` : '价格待确认' }}</strong>
      <small>{{ hotel?.amap_average_cost != null ? '高德人均消费，非每间每晚房价；入住价格以预订为准' : hotel?.price_source === 'llm_estimate' ? 'AI依据已有知识估算，未核验入住日期、房型和库存，以预订为准' : '按住宿档次估算，非该酒店报价；日期、房型和库存需预订时确认' }}</small>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { Hotel } from '@/types'
defineProps<{ hotel?: Hotel | null; accommodation?: string; compact?: boolean }>()
</script>

<style scoped>
.stay-reference{display:flex;align-items:center;gap:22px;padding:26px 28px;background:#f7fafb;border:1px solid #e3ebee;border-radius:16px;color:#183c50}
.stay-symbol{display:grid;place-items:center;flex:0 0 58px;height:66px;color:#247f82;background:#e8f3f2;border-radius:12px}
.stay-symbol svg{width:34px;height:34px}
.stay-copy{flex:1;min-width:0}.stay-type{font-size:12px;color:#526d79}
.stay-copy h3{font-family:inherit;font-size:20px;font-weight:600;line-height:1.5;margin:5px 0 7px;overflow-wrap:anywhere;color:#183c50}
.stay-address{margin:0;color:#607682;font-size:13px;line-height:1.7;overflow-wrap:anywhere}
.stay-price{display:flex;flex-direction:column;gap:7px;flex:0 1 240px;text-align:right;padding-left:24px;border-left:1px solid #dce7e9}
.stay-price>span{font-size:12px;color:#526d79}.stay-price strong{font-size:20px;line-height:1.5;font-weight:600;color:#247779;overflow-wrap:anywhere}.stay-price small{font-size:11px;color:#607682}
.stay-reference.compact{flex-wrap:wrap}.compact .stay-price{flex-basis:100%;text-align:left;padding:16px 0 0;border-left:0;border-top:1px solid #dce7e9}
@media(max-width:640px){.stay-reference{padding:20px;gap:16px;align-items:flex-start;flex-wrap:wrap}.stay-symbol{flex-basis:44px;height:52px}.stay-symbol svg{width:28px;height:28px}.stay-copy{flex-basis:calc(100% - 60px)}.stay-copy h3{font-size:18px}.stay-price{flex-basis:100%;text-align:left;padding:16px 0 0;border-left:0;border-top:1px solid #dce7e9}.stay-price strong{font-size:19px}}
</style>
