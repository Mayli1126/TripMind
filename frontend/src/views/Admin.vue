<template>
  <section class="admin-page"><div class="admin-heading"><div><span>TRIPMIND · ADMIN</span><h1>旅行管理中心</h1><p>查看全部用户的行程、攻略和任务；个人页面仍只展示你自己的记录。</p></div><button @click="load" :disabled="loading">{{ loading ? '正在更新…' : '刷新记录 ↻' }}</button></div>
    <p v-if="error" class="admin-error">{{ error }}</p>
    <div class="admin-stats"><article v-for="stat in stats" :key="stat.label"><span>{{ stat.label }}</span><strong>{{ stat.count }}</strong></article></div>
    <section class="runtime-card"><div class="runtime-heading"><div><span class="runtime-light" :class="{ ready: runtime?.ready }" /><strong>{{ runtime?.ready ? '规划服务已就绪' : '规划服务尚未就绪' }}</strong></div><button :disabled="warming || loading" @click="warmRuntime">{{ warming ? '正在预热地图会话…' : '重新预热地图工具' }}</button></div><div v-if="runtime" class="runtime-facts"><span>地图会话 {{ runtime.mcp.ready_sessions }}/{{ runtime.mcp.pool_size }}</span><span>模型配置 {{ runtime.llm_configured ? '已填写，调用时验证' : '未填写' }}</span><span>{{ runtime.redis_enabled ? `规划 Worker ${runtime.worker.running ? '运行中' : '未启动'}` : '内存任务队列' }}</span><span>工具依赖：离线缓存加载</span></div><p v-if="runtime?.error">{{ runtime.error }}</p><p v-if="runtimeError">{{ runtimeError }}</p><small>首次部署或更换环境，请先在后端运行 <code>python -m app.services.mcp_setup prepare</code>。这里重新预热只读缓存，不会触发依赖下载或调用模型。</small></section>
    <div class="admin-tabs"><button v-for="tab in tabs" :key="tab.key" :class="{ active: section === tab.key }" @click="section = tab.key">{{ tab.label }}</button></div>
    <div class="admin-table-wrap">
      <table v-if="section === 'users'"><thead><tr><th>用户</th><th>身份</th><th>行程</th><th>攻略</th><th>注册时间</th></tr></thead><tbody><tr v-for="user in data.users" :key="user.id"><td>{{ user.username }}</td><td>{{ user.role === 'admin' ? '管理员' : '普通用户' }}</td><td>{{ user.trip_count }}</td><td>{{ user.guide_count }}</td><td>{{ formatDate(user.created_at) }}</td></tr></tbody></table>
      <table v-else-if="section === 'trips'"><thead><tr><th>行程</th><th>所属用户</th><th>最近更新</th><th /></tr></thead><tbody><tr v-for="trip in data.trips" :key="trip.id"><td>{{ trip.title }}</td><td>{{ trip.username || '待迁移' }}</td><td>{{ trip.updated_at }}</td><td><router-link :to="`/trips/${trip.id}`">查看行程 ↗</router-link></td></tr></tbody></table>
      <table v-else-if="section === 'guides'"><thead><tr><th>攻略</th><th>所属用户</th><th>状态</th><th /></tr></thead><tbody><tr v-for="guide in data.guides" :key="guide.id"><td>{{ guide.title || '未命名攻略' }}</td><td>{{ guide.username || '待迁移' }}</td><td>{{ guide.parse_status }}</td><td><button @click="showGuide(guide.id)">查看解析 ↗</button></td></tr></tbody></table>
      <table v-else><thead><tr><th>任务 ID（最近 100 条）</th><th>所属用户</th><th>提交时间</th><th /></tr></thead><tbody><tr v-for="job in data.jobs" :key="job.id"><td class="job-id">{{ job.id }}</td><td>{{ job.username }}</td><td>{{ formatDate(job.created_at) }}</td><td><button @click="showJob(job.id)">查看状态 ↗</button></td></tr></tbody></table>
      <p v-if="!loading && !data[section].length" class="empty-admin">这里还没有记录。</p>
    </div>
    <section v-if="detail" class="admin-detail"><button @click="detail = ''">收起 ×</button><h3>{{ detailTitle }}</h3><pre>{{ detail }}</pre></section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import apiClient, { getPlanningJob } from '@/services/api'

interface UserRow { id: string; username: string; role: string; created_at: number; trip_count: number; guide_count: number }
interface TripRow { id: string; title: string; updated_at: string; username: string }
interface GuideRow { id: string; title: string; username: string; parse_status: string }
interface JobRow { id: string; username: string; created_at: number }
interface Overview { users: UserRow[]; trips: TripRow[]; guides: GuideRow[]; jobs: JobRow[] }
interface RuntimeState { ready: boolean; phase: string; error: string; llm_configured: boolean; redis_enabled: boolean; mcp: { ready_sessions: number; pool_size: number }; worker: { running: boolean } }
type Section = keyof Overview
const data = ref<Overview>({ users: [], trips: [], guides: [], jobs: [] })
const section = ref<Section>('users')
const loading = ref(false)
const error = ref('')
const runtime = ref<RuntimeState | null>(null)
const runtimeError = ref('')
const warming = ref(false)
const detail = ref('')
const detailTitle = ref('')
const tabs: { key: Section; label: string }[] = [{ key: 'users', label: '用户' }, { key: 'trips', label: '全部行程' }, { key: 'guides', label: '全部攻略' }, { key: 'jobs', label: '规划任务' }]
const stats = computed(() => tabs.map(tab => ({ label: tab.label, count: data.value[tab.key].length })))
const formatDate = (value: number) => new Date(value * 1000).toLocaleString('zh-CN')

async function load() {
  loading.value = true
  error.value = ''
  try { data.value = (await apiClient.get<Overview>('/api/admin/overview')).data }
  catch (cause: any) { error.value = cause.response?.data?.detail || '暂时无法读取管理数据。' }
  finally { loading.value = false }
  await loadRuntime()
}

async function loadRuntime() {
  try { runtime.value = (await apiClient.get<RuntimeState>('/api/admin/runtime')).data; runtimeError.value = '' }
  catch { runtimeError.value = '暂时未能读取运行环境状态。' }
}

async function warmRuntime() {
  warming.value = true
  runtimeError.value = ''
  try { runtime.value = (await apiClient.post<RuntimeState>('/api/admin/runtime/warmup')).data }
  catch (cause: any) { runtimeError.value = cause.response?.data?.detail || '预热未完成，请检查部署准备步骤。' }
  finally { warming.value = false; await loadRuntime() }
}

async function showGuide(id: string) {
  try { detail.value = JSON.stringify((await apiClient.get(`/api/guide-sources/${id}`)).data, null, 2); detailTitle.value = '攻略解析详情' }
  catch { error.value = '未能读取攻略详情。' }
}

async function showJob(id: string) {
  try {
    const job = await getPlanningJob(id)
    detail.value = JSON.stringify({ id, status: job.status, progress: job.progress, message: job.message, agents: job.agents, metrics: job.metrics, error: job.error,trace_id:job.trace_id,worker_id:job.worker_id,run_id:job.run_id,heartbeat_at:job.heartbeat_at,recovery_count:job.recovery_count,diagnostics:job.diagnostics }, null, 2)
    detailTitle.value = '任务运行状态'
  } catch (cause: any) { error.value = cause.response?.status === 404 ? '该任务已过期，归属记录仍保留。' : '未能读取任务状态。' }
}

onMounted(load)
</script>

<style scoped>
.runtime-card { margin: 20px 0 30px; padding: 22px; border: 1px solid #e2edf0; border-radius: 20px; background: #fff; }
.runtime-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.runtime-heading > div { display: flex; align-items: center; gap: 9px; color: #557d8b; font-size: 13px; }
.runtime-light { width: 7px; height: 7px; border-radius: 50%; background: #d9af67; }
.runtime-light.ready { background: #5cac97; }
.runtime-heading button { border: 1px solid #dae9ee; background: #f8fcfd; color: #6093a1; border-radius: 16px; padding: 8px 12px; font-size: 11px; cursor: pointer; }
.runtime-heading button:disabled { opacity: .5; cursor: default; }
.runtime-facts { display: flex; flex-wrap: wrap; gap: 10px 22px; margin: 17px 0 12px; font-size: 11px; color: #8ca1aa; }
.runtime-card > p { color: #ae8b61; font-size: 11px; line-height: 1.8; }
.runtime-card > small { display: block; margin-top: 12px; font-size: 10px; line-height: 1.9; color: #a3b2b8; }
.runtime-card code { overflow-wrap: anywhere; }
.admin-page { max-width: 1240px; margin: auto; padding: 50px 28px 80px; }.admin-heading { display: flex; justify-content: space-between; gap: 20px; align-items: center; }.admin-heading span { color: #7f9da9; font-size: 10px; letter-spacing: 2px; }h1 { color: #29434e; font-size: 30px; margin: 12px 0; }.admin-heading p { color: #8a9da6; font-size: 12px; line-height: 1.8; }.admin-heading > button { border: 1px solid #dce7eb; background: white; color: #6a8996; border-radius: 20px; padding: 10px 16px; white-space: nowrap; cursor: pointer; }.admin-stats { display: grid; grid-template-columns: repeat(4,1fr); gap: 18px; margin: 32px 0; }.admin-stats article { display: flex; flex-direction: column; gap: 16px; border: 1px solid #e3ecef; border-radius: 20px; padding: 24px; background: white; }.admin-stats span { font-size: 12px; color: #8ca3ad; }.admin-stats strong { font-size: 30px; color: #305663; font-weight: 500; }.admin-tabs { display: flex; gap: 24px; margin: 28px 0 18px; }.admin-tabs button { border: 0; border-bottom: 2px solid transparent; color: #90a4ad; background: none; padding: 10px 0; font-size: 13px; cursor: pointer; }.admin-tabs .active { color: #326e7e; border-color: #60adbb; }.admin-table-wrap { overflow-x: auto; border: 1px solid #e6edf0; border-radius: 18px; background: white; }table { width: 100%; text-align: left; border-collapse: collapse; font-size: 12px; white-space: nowrap; }th { color: #8ea3ad; font-weight: 400; background: #f7fafb; }td { color: #5c7884; }th,td { padding: 18px 22px; border-bottom: 1px solid #eef3f5; }td a,td button { color: #288c9e; background: none; border: 0; text-decoration: none; font-size: 12px; cursor: pointer; }.job-id { font-family: monospace; font-size: 10px; }.empty-admin { padding: 25px; text-align: center; color: #9bafb8; font-size: 12px; }.admin-error { color: #b07157; font-size: 12px; }.admin-detail { margin-top: 25px; padding: 22px; border: 1px solid #e1e9ee; background: white; border-radius: 18px; }.admin-detail h3 { font-size: 15px; color: #537885; }.admin-detail > button { float: right; border: 0; background: none; color: #8ba1ab; cursor: pointer; }.admin-detail pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 450px; overflow-y: auto; color: #7a939f; font-size: 11px; line-height: 1.8; }@media(max-width:720px) { .admin-page { padding: 30px 18px; }.admin-stats { grid-template-columns: repeat(2,1fr); gap: 10px; }.admin-stats article { padding: 18px; }.admin-tabs { gap: 18px; } }
</style>
