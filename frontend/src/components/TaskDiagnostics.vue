<template>
  <div v-if="jobId" class="job-runtime">
    <div class="job-actions"><span>任务 {{ jobId.slice(0, 8) }}</span><button v-if="!terminal" :disabled="busy || state?.status === 'cancelling'" @click="cancelTask">{{ state?.status === 'cancelling' ? '正在停止…' : '取消生成' }}</button></div>
    <details @toggle="loadOnOpen"><summary>运行详情</summary><p class="job-id">任务编号：{{ jobId }}</p><p v-if="state?.recovery_count">异常接管 {{ state.recovery_count }} 次</p>
      <p v-if="state?.worker_id">执行器：{{ state.worker_id }}</p>
      <p v-if="state?.status === 'cancelled'">任务已取消，未完成的草稿不会自动保存。</p>
      <div v-for="(count,key) in state?.diagnostics?.services || {}" :key="key" class="runtime-row"><b>{{ labels[key] || key }}</b><span>{{ count.completed }} 次完成 / {{ count.failed }} 次失败<span v-if="count.hits || count.misses"> · 缓存命中 {{ count.hits }}/{{ count.hits+count.misses }}</span><span v-if="count.retrying"> · 重试 {{ count.retrying }}</span><span v-if="count.degraded"> · 降级 {{ count.degraded }}</span></span></div>
      <p v-for="(call,id) in state?.diagnostics?.active_calls || {}" :key="id">等待 {{ labels[call.service] || call.service }} 返回（{{ call.operation }}）</p>
      <button :disabled="busy" @click="refresh">刷新运行详情</button><small>取消会停止本地执行；已经发给外部服务的请求可能仍计费。关闭页面只停止监听，不等于取消。</small>
    </details><p v-if="error" role="alert">{{ error }}</p>
  </div>
</template>
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { PlanningJobResponse } from '@/types'
import {cancelPlanningJob,getPlanningJob} from '@/services/api'
const props=defineProps<{jobId:string;job?:PlanningJobResponse|null}>()
const emit=defineEmits<{state:[PlanningJobResponse]}>()
const loaded=ref<PlanningJobResponse|null>(null),busy=ref(false),error=ref('')
const state=computed(()=>props.job || loaded.value)
const terminal=computed(()=>['completed','failed','cancelled'].includes(state.value?.status || ''))
const labels:Record<string,string>={llm:'模型',agent_tool:'原生工具执行',map_mcp:'地图工具',map_http:'地点查询',map_route:'道路路线',cache:'缓存',runtime:'执行器',workflow:'工作流'}
watch(()=>props.jobId,()=>{loaded.value=null;error.value=''})
async function refresh(){const id=props.jobId;busy.value=true;try{const job=await getPlanningJob(id);if(id===props.jobId){loaded.value=job;emit('state',job)}}catch{error.value='暂时无法读取运行详情'}finally{busy.value=false}}
function loadOnOpen(event:Event){if((event.target as HTMLDetailsElement).open)void refresh()}
async function cancelTask(){if(!window.confirm('停止当前生成？已保存的计划不变，外部服务已产生的费用无法撤回。'))return;const id=props.jobId;busy.value=true;error.value='';try{const job=await cancelPlanningJob(id);if(id===props.jobId){loaded.value=job;emit('state',job)}}catch{error.value='取消请求未确认，请查看当前状态后重试'}finally{busy.value=false}}
</script>
<style scoped>
.job-runtime{font-size:11px;color:#58727e;margin:14px 0;overflow-wrap:anywhere}.job-actions{display:flex;align-items:center;justify-content:space-between;gap:8px}.job-runtime button{font:inherit;border:1px solid #d7e5e9;border-radius:7px;padding:5px 9px;background:#fff;color:#315e6d;cursor:pointer}.job-runtime button:disabled{opacity:.5;cursor:default}.job-runtime button:focus-visible,.job-runtime summary:focus-visible{outline:2px solid #167c80;outline-offset:2px}.job-runtime details{margin-top:10px}.job-runtime summary{cursor:pointer}.job-runtime small{display:block;margin-top:10px;line-height:1.7}.runtime-row{display:flex;flex-wrap:wrap;gap:4px 10px;margin:8px 0}.runtime-row b{min-width:50px}.job-id{user-select:all}
</style>
