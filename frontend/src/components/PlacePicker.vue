<template>
  <AModal :open="open" title="添加地点" :footer="null" @cancel="emit('close')">
    <p class="hint">{{ city }} · {{ target }}。加入后先生成调整草稿，确认后才保存。</p>
    <form class="search" @submit.prevent="search"><input v-model="query" aria-label="搜索地点" placeholder="搜索景点、街区或公园" autofocus /><button :disabled="loading || !query.trim()">{{ loading ? '搜索中…' : '搜索' }}</button></form>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <div class="results">
      <button v-for="place in places" :key="place.id" class="place" :disabled="existing.some(item => place.id === item.poi_id || place.name === item.name)" @click="emit('pick', place)"><span class="pin">⌖</span><span><strong>{{ place.name }}</strong><small>{{ place.address || city }}</small></span><b>{{ existing.some(item => place.id === item.poi_id || place.name === item.name) ? '已添加' : '加入' }}</b></button>
      <p v-if="searched && !loading && !places.length && !error" class="hint">没有找到匹配地点，换个关键词试试。</p>
    </div>
  </AModal>
</template>
<script setup lang="ts">
import { ref, watch } from 'vue'
import { Modal as AModal } from 'ant-design-vue'
import { apiClient } from '@/services/api'
import type { Attraction, Location } from '@/types'
export interface PlaceResult { id: string; name: string; address: string; location: Location }
const props = defineProps<{ open: boolean; city: string; target: string; existing: Attraction[] }>()
const emit = defineEmits<{ close: []; pick: [place: PlaceResult] }>()
const query = ref(''), places = ref<PlaceResult[]>([]), loading = ref(false), error = ref(''), searched = ref(false)
let request = 0
watch(() => props.open, () => { request++; query.value = ''; places.value = []; error.value = ''; loading.value = false; searched.value = false })
async function search() {
  if (!query.value.trim()) return
  const id = ++request; loading.value = true; error.value = ''; searched.value = true
  try {
    const { data } = await apiClient.get<{ success: boolean; data: PlaceResult[] }>('/api/map/poi', { params: { keywords: query.value.trim(), city: props.city, citylimit: true } })
    if (id === request) { if (!data.success) throw new Error('搜索暂时不可用'); places.value = data.data || [] }
  } catch { if (id === request) { places.value = []; error.value = '地点搜索失败，请稍后重试' } }
  finally { if (id === request) loading.value = false }
}
</script>
<style scoped>
.hint{color:#6b8190;font-size:13px;line-height:1.8}.search{display:flex;gap:8px;margin:20px 0}.search input{min-width:0;flex:1;height:48px;padding:0 14px;border:1px solid #c8dce6;border-radius:14px;font:inherit}.search button{border:0;border-radius:24px;background:#183c50;color:white;padding:0 20px;cursor:pointer}.search button:disabled{opacity:.5}.results{max-height:380px;overflow:auto}.place{display:flex;align-items:center;width:100%;gap:14px;padding:14px 8px;background:white;border:0;border-radius:14px;text-align:left;cursor:pointer;color:#244c62}.place:hover{background:#eef7fb}.place:disabled{opacity:.45;cursor:default}.place>span:nth-child(2){flex:1;min-width:0}.place strong{display:block;font-size:15px;font-weight:500}.place small{display:block;color:#6b8390;font-size:12px;margin-top:6px}.pin{font-size:24px;color:#2983a0}.place b{font-size:13px;font-weight:500}.error{color:#ad573d}button:focus-visible,input:focus-visible{outline:2px solid #218dab;outline-offset:3px}
</style>
