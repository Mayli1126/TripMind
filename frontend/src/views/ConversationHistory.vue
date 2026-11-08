<template><main class="history"><h1>我的对话</h1><p>需求、问答和修改草稿保存在账号中，可以在其他设备接着聊。</p><router-link to="/">＋ 开始新旅行</router-link><p v-if="error">{{ error }}</p><article v-for="item in items" :key="item.id"><div><h3>{{ item.title }}</h3><small>{{ new Date(item.updated_at * 1000).toLocaleString() }} · {{ item.kind === 'planner' ? '规划需求' : '行程问答' }}</small></div><router-link :to="item.kind === 'planner' ? { path: '/', query: { conversation: item.id } } : { path: `/trips/${item.trip_id}/assistant`, query: { conversation: item.id } }">继续对话 →</router-link><button @click="remove(item.id)">删除</button></article><p v-if="!items.length && !error">还没有已保存的对话。</p></main></template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { deleteConversation, listConversations } from '@/services/conversations'
import type { Conversation } from '@/services/conversations'
const items = ref<Conversation[]>([]), error = ref('')
onMounted(async () => { try { items.value = await listConversations() } catch { error.value = '读取失败，请稍后再试' } })
async function remove(id: string) { if (!window.confirm('删除此对话记录？不会删除关联行程。')) return; try { await deleteConversation(id); items.value = items.value.filter(x => x.id !== id) } catch { error.value = '删除失败' } }
</script>
<style scoped>.history{max-width:1000px;margin:40px auto;padding:24px}.history>p,small{color:#7c919c}article{display:flex;align-items:center;gap:24px;margin:16px 0;padding:20px;border:1px solid #e1ecef;border-radius:16px;background:white}article div{flex:1}button{border:0;background:#fff0eb;color:#9a5949;padding:8px;border-radius:8px;cursor:pointer}@media(max-width:650px){article{flex-wrap:wrap}article div{flex-basis:100%}}</style>
