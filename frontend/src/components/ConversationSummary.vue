<template>
  <details v-if="summary" class="conversation-summary">
    <summary>{{ summary.method === 'extractive' ? '此前对话原文摘录' : '此前对话记忆' }}<span>最近要求优先</span></summary>
    <p>仅用于这段会话，不会自动变成长期偏好。摘要可能不完整，你可以直接在对话中纠正。</p>
    <section v-for="section in sections" :key="section.key">
      <template v-if="summary[section.key]?.length"><h4>{{ section.label }}</h4><ul><li v-for="(item, index) in summary[section.key]" :key="index">{{ item.text }}<small>原文：{{ item.quote }}</small></li></ul></template>
    </section>
    <p v-if="summary.pending_notes?.length">尚未合并的原文：{{ summary.pending_notes.join('；') }}</p>
  </details>
</template>
<script setup lang="ts">
import type { ConversationSummary } from '@/services/conversations'
defineProps<{ summary: ConversationSummary | null }>()
const sections = [{key:'confirmed',label:'此前表达'}, {key:'rejected',label:'否定或排除'}, {key:'open_questions',label:'此前待确认'}] as const
</script>
<style scoped>
.conversation-summary{margin:8px 22px;padding:10px 14px;background:#f5f9fa;border-left:3px solid #167c80;color:#375662;font-size:12px;max-height:230px;overflow:auto}.conversation-summary summary{cursor:pointer;font-weight:600}.conversation-summary summary span{font-weight:400;margin-left:12px;color:#617883}.conversation-summary p{line-height:1.6}.conversation-summary h4{margin:10px 0 4px}.conversation-summary ul{padding-left:18px;margin:4px 0}.conversation-summary li{margin:6px 0;overflow-wrap:anywhere}.conversation-summary small{display:block;color:#617883}summary:focus-visible{outline:2px solid #167c80;outline-offset:3px}
</style>
