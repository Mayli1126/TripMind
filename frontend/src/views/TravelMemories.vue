<template>
  <main class="memories">
    <header><h1>我的旅行偏好</h1><p>把习惯留在这里，让下一次出发少一点重复说明。</p></header>
    <div class="memory-rule">本次对话要求优先；同一项偏好，城市专属设置优先于通用设置。只保存你亲自填写的内容。</div>
    <form @submit.prevent="save(false)">
      <h2>{{ editing ? '编辑偏好' : '记住一条偏好' }}</h2>
      <div class="fields">
        <label>偏好分类<select v-model="category" aria-label="偏好分类" @change="value = ''; polarity = 'prefer'; conflicts = []"><option v-for="(label, key) in categories" :key="key" :value="key">{{ label }}</option></select></label>
        <label v-if="choices">偏好内容<select v-model="value" aria-label="偏好内容" required><option value="" disabled>请选择</option><option v-for="(label, key) in choices" :key="key" :value="key">{{ label }}</option></select></label>
        <label v-else-if="category !== 'general'">偏好对象<input v-model="value" maxlength="100" required :placeholder="category === 'interest' ? '例如：博物馆' : '例如：辣味'" /></label>
        <label v-if="category === 'interest' || category === 'diet'">倾向<select v-model="polarity"><option value="prefer">偏好</option><option value="avoid">避免</option></select></label>
        <label>适用城市<input v-model="city" maxlength="50" placeholder="留空表示所有城市" /></label>
        <label>有效期至<input v-model="expiry" type="date" :min="today" /><small>留空表示长期有效</small></label>
      </div>
      <label v-if="category === 'general'" class="free-text">偏好内容<textarea v-model="content" maxlength="400" required placeholder="例如：喜欢沿着海边散步，不喜欢把行程排得太满" /></label>
      <p class="helper">兴趣、饮食请一条写一个对象，方便识别同一对象的偏好冲突。自由备注不自动判断语义相反。</p>
      <div v-if="conflicts.length" class="conflict" role="alert"><strong>同一范围已有相同或冲突的偏好</strong><ul><li v-for="item in conflicts" :key="item.id">{{ item.content }}</li></ul><p>替换会暂停这些旧记录，不会删除。请确认上方内容后再操作。</p><button type="button" :disabled="busy" @click="save(true)">暂停旧记录并保存</button><button type="button" @click="conflicts = []">暂不替换</button></div>
      <div class="actions"><button class="primary" :disabled="busy">{{ busy ? '保存中…' : editing ? '保存编辑' : '保存偏好' }}</button><button v-if="editing" type="button" :disabled="busy" @click="reset">取消编辑</button></div>
    </form>
    <p class="status" role="status">{{ status }}</p>
    <section class="saved"><h2>已保存的偏好 <span>{{ items.length }}</span></h2><p v-if="!items.length" class="empty">还没有保存偏好。你可以先记住旅行节奏，或常用的住宿方式。</p>
      <article v-for="item in items" :key="item.id" :class="{ inactive: !item.active }">
        <div class="preference"><span class="category">{{ categories[item.category] || '自由备注' }}</span><h3>{{ item.content }}</h3><p>{{ item.city || '所有城市' }}<span v-if="item.expires_at"> · 有效期至 {{ formatDate(item.expires_at) }}</span><span> · {{ item.expired ? '已过期' : item.enabled ? '使用中' : '已暂停' }}</span></p></div>
        <div class="row-actions"><button :disabled="busy" @click="edit(item)">编辑</button><button :disabled="busy || !!item.expired" @click="toggle(item)">{{ item.enabled ? '暂停' : '启用' }}</button><button class="delete" :disabled="busy" @click="remove(item.id)">删除</button></div>
      </article>
    </section>
  </main>
</template>
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { apiClient } from '@/services/api'
import { newConversationId } from '@/services/conversations'
interface Memory { id:string; revision:number; content:string; city:string; enabled:boolean; category:string; value:string; polarity:string; expires_at:number|null; expired:boolean; active:boolean }
const categories: Record<string,string> = { general:'自由备注', pace:'旅行节奏', schedule:'出发作息', hotel:'住宿习惯', transport:'市内交通', interest:'游玩兴趣', diet:'饮食偏好' }
const options: Record<string,Record<string,string>> = {
  pace:{relaxed:'轻松慢游',balanced:'节奏适中',intensive:'充实打卡'},
  schedule:{early:'愿意早出发',late:'不安排早出发',flexible:'出发时间灵活'},
  hotel:{economy:'经济型住宿',comfort:'舒适型住宿',luxury:'高档住宿',homestay:'民宿'},
  transport:{walking:'优先步行',transit:'优先公共交通',taxi:'优先打车',self_drive:'自驾'}
}
const items = ref<Memory[]>([]), content = ref(''), city = ref(''), editing = ref(''), status = ref(''), busy = ref(false)
const category = ref('general'), value = ref(''), polarity = ref('prefer'), expiry = ref('')
const conflicts = ref<{id:string;content:string;revision:number}[]>([])
const enableOnSave = ref(false)
const editingRevision = ref(0)
const choices = computed(() => options[category.value])
const formatDate = (seconds:number) => { const d = new Date(seconds*1000); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}` }
const today = formatDate(Date.now()/1000)
const load = async () => { items.value = (await apiClient.get('/api/library/memories')).data }
onMounted(() => load().catch(() => { status.value = '读取偏好失败，请刷新重试' }))
function reset() { editing.value=''; content.value=''; city.value=''; category.value='general'; value.value=''; polarity.value='prefer'; expiry.value=''; conflicts.value=[]; enableOnSave.value=false }
function edit(item:Memory) { editingRevision.value=item.revision; editing.value=item.id; content.value=item.content; city.value=item.city; category.value=item.category || 'general'; value.value=item.value || ''; polarity.value=item.polarity || 'prefer'; expiry.value=item.expires_at ? formatDate(item.expires_at) : ''; conflicts.value=[]; enableOnSave.value=false; window.scrollTo({top:0,behavior:'smooth'}) }
function failure(cause:any) { const detail=cause.response?.data?.detail; status.value=typeof detail==='string' ? detail : '保存失败，请检查内容并重试' }
async function save(replace:boolean) {
  if (busy.value) return
  busy.value=true; status.value=''
  try {
    const previous=items.value.find(x=>x.id===editing.value)
    await apiClient.put(`/api/library/memories/${editing.value || newConversationId()}`, {revision:editing.value ? editingRevision.value : 0,content:content.value,city:city.value,enabled:enableOnSave.value || (previous ? !!previous.enabled : true),category:category.value,value:value.value,polarity:polarity.value,expires_at:expiry.value ? Math.floor(new Date(`${expiry.value}T23:59:59`).getTime()/1000) : null,replace_conflicts:replace,conflict_ids:replace ? conflicts.value.map(item=>item.id) : [],conflict_revisions:replace ? Object.fromEntries(conflicts.value.map(item=>[item.id,item.revision])) : {}})
    reset(); await load(); status.value='已保存，后续请求会按适用范围参考；已有计划不会自动改动'
  } catch(cause:any) { if(cause.response?.status===409 && Array.isArray(cause.response.data.detail?.conflicts)) { conflicts.value=cause.response.data.detail.conflicts; status.value='尚未保存，请确认是否替换旧偏好' } else { conflicts.value=[]; failure(cause) } }
  finally {busy.value=false}
}
async function toggle(item:Memory) {
  busy.value=true
  try { await apiClient.put(`/api/library/memories/${item.id}`,{...item,enabled:!item.enabled}); await load(); status.value=item.enabled ? '已暂停，后续请求不再使用这条偏好' : '已启用' }
  catch(cause:any) { if(cause.response?.status===409 && Array.isArray(cause.response.data.detail?.conflicts)) { edit(item); enableOnSave.value=true; conflicts.value=cause.response.data.detail.conflicts; status.value='启用与现有偏好冲突，请先编辑并确认替换' } else failure(cause) }
  finally {busy.value=false}
}
async function remove(id:string) { const item=items.value.find(x=>x.id===id); if(!item || !window.confirm('删除后不再用于新请求；已有聊天和计划中的历史内容保持不变。确定删除？'))return; busy.value=true; try {await apiClient.delete(`/api/library/memories/${id}`,{params:{revision:item.revision}}); if(editing.value===id)reset(); await load(); status.value='已删除'}catch(cause){failure(cause)}finally{busy.value=false} }
</script>
<style scoped>
.memories{max-width:1000px;margin:44px auto;padding:0 28px 56px;color:#183c50;font-family:"PingFang SC","Microsoft YaHei",sans-serif}.memories h1{font-size:30px;letter-spacing:-1px;margin-bottom:8px}.memories header p,.helper,.preference p,.empty{color:#617883;font-size:13px;line-height:1.7}.memory-rule{margin:26px 0 20px;border-left:3px solid #167c80;padding:10px 16px;background:#edf7f7;font-size:13px;line-height:1.7}form{background:#fff;border:1px solid #e2e9ed;border-radius:16px;padding:26px}.memories h2{font-size:18px;margin:0 0 20px}.fields{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}label{display:flex;flex-direction:column;gap:8px;font-size:13px;font-weight:600}label small{font-size:11px;color:#617883;font-weight:400}input,select,textarea{width:100%;box-sizing:border-box;border:1px solid #cad8df;border-radius:8px;background:#fff;padding:11px;font:inherit;color:#183c50}textarea{min-height:90px;resize:vertical}.free-text{margin-top:18px}.helper{margin:16px 0}.actions,.row-actions{display:flex;gap:10px;flex-wrap:wrap}button{background:#edf4f5;color:#225b64;border:0;border-radius:8px;padding:10px 14px;cursor:pointer;font:inherit;font-size:13px}button.primary{background:#167c80;color:#fff}button:disabled{opacity:.5;cursor:default}button.delete{background:transparent;color:#8e5050}input:focus-visible,select:focus-visible,textarea:focus-visible,button:focus-visible{outline:2px solid #167c80;outline-offset:3px}.status{min-height:20px;font-size:13px;color:#28656a}.saved{margin-top:30px}.saved h2 span{font-size:13px;color:#617883;font-weight:400;margin-left:8px}article{display:flex;align-items:center;gap:20px;justify-content:space-between;padding:22px 0;border-bottom:1px solid #e2e9ed}.preference{min-width:0}.category{font-size:11px;color:#167c80}.preference h3{font-size:16px;margin:7px 0;white-space:pre-wrap;overflow-wrap:anywhere}.preference p{margin:0}.row-actions{flex-shrink:0}.inactive .preference{opacity:.6}.conflict{background:#fff7ec;border:1px solid #e8ca9c;padding:18px;border-radius:10px;font-size:13px;line-height:1.7}.conflict button{margin-right:8px}.conflict p{color:#685540}.empty{padding:20px 0}@media(max-width:640px){.memories{margin:28px auto;padding-inline:18px}.fields{grid-template-columns:1fr}form{padding:20px}article{align-items:flex-start;flex-direction:column;gap:12px}.row-actions{width:100%}.memories h1{font-size:26px}}
</style>
