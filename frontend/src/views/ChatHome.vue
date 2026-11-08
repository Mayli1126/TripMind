<template>
  <div class="chat-home" :class="{ 'in-conversation': inConversation }">
    <div class="cloud-status">{{ cloud.syncMessage.value }} · <router-link to="/conversations">历史对话</router-link> · <router-link to="/memories">旅行偏好</router-link></div>
    <template v-if="!inConversation">
      <section id="planner" class="welcome">
        <h1>开始计划下一段旅程</h1>
        <p class="welcome-note">想去哪里、和谁出发、想怎样玩，聊聊就有了方向。</p>
        <form class="launch-composer" @submit.prevent="send()">
          <div class="composer-tabs"><span class="active">✦ AI 创建计划</span><router-link to="/guide">↗ 导入攻略</router-link></div>
          <div class="launch-input-row">
            <textarea v-model="input" rows="2" maxlength="3000" aria-label="描述你的旅行想法" placeholder="描述你的旅行想法，比如：和朋友去威海看海四天…" @keydown="onComposerKey" />
            <button class="send-button" type="submit" :disabled="!input.trim()" aria-label="开始对话">↑</button>
          </div>
          <div class="launch-bottom"><span>不用填表，把想法告诉我就好</span><span class="launch-shortcut">Enter 发送 <span aria-hidden="true">↵</span></span></div>
        </form>
        <div class="inspiration"><span>还没想好？试着聊聊</span><button v-for="idea in ideas" :key="idea" @click="send(idea)">{{ idea }} <span>↗</span></button></div>
      </section>
      <section class="recent-section">
        <div class="section-heading"><div><span class="eyebrow">YOUR JOURNEYS</span><h2>我的旅行计划<span v-if="recentTrips.length">{{ recentTrips.length }}</span></h2></div><router-link to="/trips">查看全部 ↗</router-link></div>
        <div v-if="recentLoading" class="recent-empty">正在整理你的旅行计划…</div>
        <div v-else-if="recentError" class="recent-empty"><p>{{ recentError }}</p><button class="text-button" @click="loadRecent">重新加载</button></div>
        <div v-else-if="recentTrips.length" class="recent-grid">
          <router-link v-for="trip in recentTrips.slice(0, 4)" :key="trip.id" :to="`/trips/${trip.id}`" class="recent-card">
            <div class="trip-cover" :style="trip.cover_image_url ? { backgroundImage: `url(${trip.cover_image_url})` } : {}"><span>{{ trip.city }}</span><small>{{ trip.days }} 天的旅行</small></div>
            <div class="trip-card-body"><h3>{{ trip.title }}</h3><p>{{ trip.start_date }} — {{ trip.end_date }}</p><span>继续这段旅程 <b>↗</b></span></div>
          </router-link>
        </div>
        <div v-else class="recent-empty"><span class="empty-spark">✧</span><h3>故事从你的第一段旅程开始</h3><p>在上方说出你的想法，生成后的旅行计划会保存在这里。</p></div>
      </section>
    </template>

    <section v-else class="conversation-workspace">
      <aside class="conversation-panel">
        <div class="conversation-header"><button class="back-button" :disabled="planning || sending" @click="resetConversation" aria-label="开始新对话">←</button><div><strong>一起计划这次旅行</strong><span><i />{{ planning ? '正在为你安排' : 'TripMind 旅行助手' }}</span></div><router-link to="/guide" title="导入攻略">↗ 攻略</router-link></div>
        <ConversationSummary :summary="conversationSummary" />
        <div ref="thread" class="thread" aria-live="polite">
          <div class="thread-intro"><span>✦</span><p>从一个想法，到一段值得期待的旅程。</p></div>
          <article v-for="(item, index) in messages" :key="index" class="chat-message" :class="item.role">
            <span v-if="item.role === 'assistant'" class="assistant-avatar">✦</span>
            <div class="message-body"><span v-if="item.role === 'assistant'" class="message-author">TripMind</span><p>{{ item.content }}</p></div>
          </article>
          <article v-if="sending" class="chat-message assistant"><span class="assistant-avatar">✦</span><div class="typing-indicator"><i /><i /><i /><span>正在整理你的想法</span></div></article>
          <div v-if="error" class="conversation-error" role="alert"><p>{{ error }}</p><button v-if="!planning" @click="retryMessage">再试一次</button></div>
          <div v-if="!sending && !planning && suggestions.length" class="reply-suggestions"><button v-for="suggestion in suggestions" :key="suggestion" @click="send(suggestion)">{{ suggestion }} ↗</button></div>
          <article v-if="readyRequest && !sending && !planning" class="confirmation-card">
            <span class="eyebrow">准备好，就出发</span><h3>{{ readyRequest.city }} · {{ readyRequest.travel_days }} 天</h3>
            <p>{{ readyRequest.origin_city }}出发，{{ readyRequest.start_date }} 至 {{ readyRequest.end_date }}</p>
            <p>{{ readyRequest.travelers }} 人 · {{ readyRequest.rooms }} 间房 · {{ readyRequest.accommodation }}</p>
            <p>{{ transportLabel(readyRequest.transportation) }} · {{ paceLabel(readyRequest.pace) }} · {{ readyRequest.trip_type === 'one_way' ? '单程' : '往返' }} · {{ readyRequest.budget_limit ? `总预算 ¥${readyRequest.budget_limit}` : '未设置预算上限' }}</p>
            <small>没有特别指定的项目按以上默认安排；还想调整，直接告诉我。</small>
            <button class="plan-button" @click="startPlanning">✦ 就这样，开始规划 <span>→</span></button>
          </article>
          <article v-if="planning" class="live-progress">
            <div class="progress-heading"><span>{{ waitingInput ? '有件事想和你确认' : '正在把想法变成行程' }}</span><small>{{ elapsedLabel }}</small></div>
            <p>{{ waitingInput?.question || progressMessage }}</p>
            <div class="progress-track"><span :style="{ width: `${progress}%` }" /></div>
            <div class="stage-clock"><span>{{ currentPhaseLabel }}</span><strong>{{ formatDuration(currentStageMs) }}</strong></div>
            <details v-if="timingRows.length" class="stage-breakdown"><summary>查看各阶段耗时 <span>{{ metrics.retry_count ? `已重试 ${metrics.retry_count} 次` : '按实际执行记录' }}</span></summary><div v-for="row in timingRows" :key="row.key" :class="{ 'active-stage': row.active }"><span><i />{{ row.label }}<small v-if="row.visits > 1"> ×{{ row.visits }}</small></span><strong>{{ formatDuration(row.duration) }}<small v-if="row.active"> · 进行中</small></strong></div></details>
            <p v-if="metrics.waiting_hint" class="waiting-hint">{{ metrics.waiting_hint }}</p>
            <div class="research-status"><span v-for="agent in agents" :key="agent.key" :class="agent.status"><i />{{ agent.label }}<small>{{ agent.status === 'completed' ? '就绪' : agent.status === 'degraded' ? '参考数据' : agent.status === 'running' ? '查询中' : '待查询' }}</small></span></div>
            <div class="status-sync"><span>{{ syncLabel }}</span><button :disabled="checkingStatus || !jobId" @click="checkStatus">{{ checkingStatus ? '同步中…' : '查看当前状态 ↻' }}</button></div>
            <TaskDiagnostics :job-id="jobId" :job="latestJob" @state="receiveSnapshot" />
            <small v-if="!waitingInput" class="progress-footnote">{{ progress >= 72 ? '正在编排、核对行程，完成后会自动打开计划。' : '在核对目的地资料，不需要重复提交。' }}</small>
            <div v-if="waitingInput" class="decision-replies"><button v-for="option in waitingInput.options" :key="option.value" :disabled="resuming" @click="chooseOption(option.value)">{{ option.label }} <span>↗</span></button><small>点击回答，或在下面输入对应选项。</small></div>
          </article>
          <div ref="threadEnd" />
        </div>
        <form class="chat-composer" @submit.prevent="send()"><textarea v-model="input" rows="2" maxlength="3000" :disabled="sending || (planning && !waitingInput)" :placeholder="waitingInput ? '告诉我你选择哪个方案…' : planning ? '正在规划，稍后就能看到行程…' : '补充想法，或告诉我想调整什么…'" aria-label="发送旅行需求" @keydown="onComposerKey" /><div><span>Enter 发送 · Shift + Enter 换行</span><button class="send-button" type="submit" :disabled="!input.trim() || sending || (planning && !waitingInput)" aria-label="发送消息">↑</button></div></form>
      </aside>

      <div class="destination-canvas">
        <div ref="mapElement" class="destination-map" />
        <div v-if="!mapReady" class="map-placeholder"><div class="map-orbit"><span>⌖</span></div><h2>{{ draft.city ? `下一站，${draft.city}` : '世界很大，下一站去哪？' }}</h2><p>{{ mapError || (draft.city ? '正在展开目的地地图' : '聊聊你的目的地，地图会在这里展开。') }}</p></div>
        <div class="map-topline"><span class="map-tag"><i />{{ draft.city || '你的下一段旅程' }}</span><button @click="fitMap" :disabled="!mapReady" class="map-tag">⌖ 回到目的地</button></div>
        <aside class="trip-brief"><div class="brief-heading"><span>旅行灵感簿</span><small>随对话更新</small></div><h2>{{ draft.city || '还没决定，也没关系' }}<small v-if="draft.travel_days">{{ draft.travel_days }} 天</small></h2><div class="brief-route">{{ draft.origin_city || '出发地待聊聊' }} <span>⟶</span> {{ draft.city || '下一站' }}</div><div class="brief-details"><span>出发日期<strong>{{ draft.start_date || '还没定' }}</strong></span><span>同行人数<strong>{{ draft.travelers ? `${draft.travelers} 人` : '待确认 · 默认 1 人' }}</strong></span><span>旅行节奏<strong>{{ draft.pace ? paceLabel(draft.pace) : '适中（默认）' }}</strong></span><span>总预算<strong>{{ draft.budget_limit ? `¥ ${draft.budget_limit}` : '暂不限' }}</strong></span></div><div v-if="draft.preferences?.length" class="preference-tags"><span v-for="tag in draft.preferences" :key="tag">{{ tag }}</span></div><div v-if="draft.free_text_input" class="brief-notes">{{ draft.free_text_input }}</div><div v-if="candidates.length" class="candidate-notes"><strong>来自你的攻略 · {{ candidates.length }} 个地点</strong><p>{{ candidates.slice(0, 6).map(place => place.canonical_name).join(' · ') }}{{ candidates.length > 6 ? ' …' : '' }}</p></div><p class="brief-tip">想改日期、预算或玩法？<br />直接在对话里告诉我。</p></aside>
        <span class="map-caption">聊出旅行的样子，再把它变成路线。</span>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import TaskDiagnostics from '@/components/TaskDiagnostics.vue'
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getConversation, useConversation } from '@/services/conversations'
import AMapLoader from '@amap/amap-jsapi-loader'
import { createSavedTrip, fetchGuideCandidates, generateTripPlanWithProgress, getPlanningJob, listSavedTrips, resumePlanningJob, sendPlannerMessage, waitForExistingJob } from '@/services/api'
import ConversationSummary from '@/components/ConversationSummary.vue'
import type { ConversationSummary as SummaryData } from '@/services/conversations'
import type { AgentExecutionState, CandidatePlace, PlannerChatMessage, PlannerChatResponse, PlanningJobResponse, PlanningMetrics, PlanningProgressEvent, SavedTripSummary, TripFormData, TripPlan, WaitingInputPayload } from '@/types'

const router = useRouter()
const route = useRoute()
const cloud = useConversation('planner')
const CHAT_KEY = 'tripmind.planner_chat.v1'
const ACTIVE_JOB_KEY = 'tripmind.active_job'
const CANDIDATE_KEY = 'tripmind.candidate_pool.v1'
const input = ref('')
const messages = ref<PlannerChatMessage[]>([])
const draft = ref<PlannerChatResponse['draft']>({})
const suggestions = ref<string[]>([])
const readyRequest = ref<TripFormData | null>(null)
const sending = ref(false)
const conversationSummary = ref<SummaryData | null>(null)
const planning = ref(false)
const resuming = ref(false)
const error = ref('')
const progress = ref(0)
const progressMessage = ref('正在创建规划任务')
const events = ref<PlanningProgressEvent[]>([])
const metrics = ref<Partial<PlanningMetrics>>({})
const agentStates = ref<Record<string, AgentExecutionState>>({})
const checkingStatus = ref(false)
const lastSnapshotAt = ref(0)
const clockNow = ref(Date.now())
const waitingInput = ref<WaitingInputPayload | null>(null)
const jobId = ref('')
const latestJob = ref<PlanningJobResponse | null>(null)
const recentTrips = ref<SavedTripSummary[]>([])
const recentLoading = ref(true)
const recentError = ref('')
const candidates = ref<CandidatePlace[]>([])
const thread = ref<HTMLElement>()
const threadEnd = ref<HTMLElement>()
const mapElement = ref<HTMLElement>()
const mapReady = ref(false)
const mapError = ref('')
const elapsed = ref(0)
let startedAt = 0
let clock: ReturnType<typeof setInterval> | undefined
let alive = true
let map: any
let amap: any
let geocoder: any
let markers: any[] = []
let activeTask: (Promise<TripPlan> & { cancel: () => void }) | null = null
const inConversation = computed(() => messages.value.length > 0 || planning.value)
const elapsedLabel = computed(() => elapsed.value < 60 ? `${elapsed.value} 秒` : `${Math.floor(elapsed.value / 60)} 分 ${elapsed.value % 60} 秒`)
const ideas = ['去威海看海，慢慢玩四天', '和朋友来一场成都美食之旅', '周末去南京，逛博物馆和老街']
const agents = computed(() => [
  { key: 'attractions', label: '景点' }, { key: 'weather', label: '天气' },
  { key: 'hotels', label: '住宿' }, { key: 'transportation', label: '交通' }
].map(agent => ({ ...agent, status: agentStates.value[agent.key]?.status || 'waiting' })))
const phaseLabels: Record<string, string> = {
  queued: '排队等待', initializing: '准备规划服务', research: '查询旅行资料', capacity: '计算可用时间',
  planning: '编排行程', skeleton_generation: '生成每日行程骨架', fallback_generation: '重新生成完整行程',
  materializing: '核对地点与坐标', validating: '校验行程', repair_generation: '调整与修复行程',
  optimizing: '优化路线与预算', tips: '整理出行建议', finalized: '整理最终结果',
  waiting_input: '等待你的选择', retrying: '等待自动重试'
}
const currentPhaseLabel = computed(() => phaseLabels[metrics.value.current_stage || ''] || '准备开始')
const currentStageMs = computed(() => (metrics.value.stage_elapsed_ms || 0) + (metrics.value.current_stage && lastSnapshotAt.value ? Math.max(0, clockNow.value - lastSnapshotAt.value) : 0))
const timingRows = computed(() => Object.entries(phaseLabels).map(([key, label]) => ({
  key, label, active: metrics.value.current_stage === key,
  duration: (metrics.value.stage_duration_ms?.[key] || 0) + (metrics.value.current_stage === key ? currentStageMs.value : 0),
  visits: metrics.value.stage_visits?.[key] || 0
})).filter(row => row.active || row.visits > 0 || row.duration > 0))
const syncLabel = computed(() => {
  if (!lastSnapshotAt.value) return '正在连接任务进度'
  const seconds = Math.max(0, Math.floor((clockNow.value - lastSnapshotAt.value) / 1000))
  return seconds > 20 ? `已有 ${seconds} 秒未同步状态，可手动查看` : `最近同步 ${seconds} 秒前`
})
const formatDuration = (ms: number) => ms < 60_000 ? `${(ms / 1000).toFixed(1)} 秒` : `${Math.floor(ms / 60_000)} 分 ${Math.floor(ms % 60_000 / 1000)} 秒`
const transportLabel = (value: string) => ({ high_speed_train: '高铁', train: '火车', plane: '飞机', bus: '大巴' }[value] || value)
const paceLabel = (value: string) => ({ relaxed: '轻松慢游', balanced: '节奏适中', intensive: '充实打卡' }[value] || value)

function persist() {
  sessionStorage.setItem(CHAT_KEY, JSON.stringify({ messages: messages.value, draft: draft.value, suggestions: suggestions.value, request: readyRequest.value }))
  const state = { messages: messages.value, draft: draft.value, suggestions: suggestions.value, request: readyRequest.value, job_id: jobId.value }
  const target = cloud.identity().id
  return cloud.save(state, draft.value.city ? `${draft.value.city}旅行规划` : messages.value[0]?.content || '旅行对话').then(() => {
    if (alive && cloud.identity().id === target) sessionStorage.setItem(CHAT_KEY, JSON.stringify({ ...state, conversation_id: target }))
  })
}

async function scrollThread() {
  await nextTick()
  if (thread.value) thread.value.scrollTop = thread.value.scrollHeight
}

function onComposerKey(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault()
    void send()
  }
}

async function send(text?: string) {
  const content = (typeof text === 'string' ? text : input.value).trim()
  if (!content || sending.value || resuming.value) return
  if (planning.value) {
    if (!waitingInput.value) return
    const options = waitingInput.value.options
    const numbered = /^\d+$/.test(content) ? options[Number(content) - 1] : undefined
    const option = numbered || options.find(item => item.value === content || item.label === content || item.label.includes(content))
    if (!option) { error.value = '请点击对话中的一个方案，或输入对应选项文字。'; return }
    input.value = ''
    await chooseOption(option.value)
    return
  }
  messages.value.push({ role: 'user', content })
  input.value = ''
  suggestions.value = []
  readyRequest.value = null
  error.value = ''
  await requestReply()
}

async function requestReply() {
  sending.value = true
  void scrollThread()
  try {
    await persist()
    if (!alive) return
    const response = await sendPlannerMessage(messages.value.slice(-40), draft.value, cloud.syncedId())
    if (!alive) return
    draft.value = response.draft
    conversationSummary.value = response.conversation_summary || null
    suggestions.value = response.suggestions
    readyRequest.value = response.ready ? response.request || null : null
    messages.value.push({ role: 'assistant', content: response.reply })
    persist()
  } catch (cause: any) {
    if (alive) error.value = cause.response?.data?.detail || cause.message || '消息暂时没有发送成功，请再试一次。'
  } finally {
    sending.value = false
    void scrollThread()
  }
}

function retryMessage() {
  error.value = ''
  if (messages.value[messages.value.length - 1]?.role === 'user') void requestReply()
  else void startPlanning()
}

async function loadRecent() {
  recentLoading.value = true
  recentError.value = ''
  try {
    const list = await listSavedTrips()
    recentTrips.value = Array.isArray(list) ? list : []
  } catch {
    recentError.value = '暂时没能读取旅行计划，仍然可以开始一段新旅程。'
  } finally { recentLoading.value = false }
}

async function loadCandidates(city: string) {
  let local: CandidatePlace[] = []
  try { local = JSON.parse(localStorage.getItem(CANDIDATE_KEY) || '[]') } catch { /* No local selection yet. */ }
  let list = local
  try { list = await fetchGuideCandidates(city) } catch { /* Retain the user's local selection. */ }
  if (draft.value.city !== city) return
  const choices = new Map(local.map(place => [place.candidate_id, place.user_choice]))
  candidates.value = list.map(place => ({ ...place, user_choice: choices.get(place.candidate_id) ?? place.user_choice }))
    .filter(place => place.user_choice !== 'excluded' && (!place.city || place.city === city))
  drawMarkers()
}

function recordProgress(event: PlanningProgressEvent) {
  if (!alive) return
  progress.value = event.progress
  progressMessage.value = event.message
  events.value.push(event)
  if (events.value.length > 100) events.value.shift()
  if (event.agent && event.agent_status) agentStates.value[event.agent] = { status: event.agent_status, message: event.message, duration_ms: event.duration_ms }
}

function receiveSnapshot(job: PlanningJobResponse) {
  if (!alive) return
  latestJob.value=job
  const now = Date.now()
  lastSnapshotAt.value = now
  clockNow.value = now
  metrics.value = job.metrics || {}
  agentStates.value = job.agents || {}
  progress.value = job.progress
  progressMessage.value = job.message
  if (job.metrics?.elapsed_ms != null) {
    startedAt = now - job.metrics.elapsed_ms
    elapsed.value = Math.floor(job.metrics.elapsed_ms / 1000)
  }
  if (job.waiting_input) showDecision(job.job_id, job.waiting_input)
}

async function checkStatus() {
  if (!jobId.value || checkingStatus.value) return
  checkingStatus.value = true
  try { receiveSnapshot(await getPlanningJob(jobId.value)); error.value = '' }
  catch (cause: any) { error.value = cause.response?.data?.detail || '暂时未能同步状态，任务可能仍在后台执行，请稍后再查看。' }
  finally { checkingStatus.value = false }
}

function showDecision(id: string, payload: WaitingInputPayload) {
  if (!alive) return
  jobId.value = id
  waitingInput.value = payload
  void scrollThread()
}

function startClock() {
  startedAt = Date.now()
  elapsed.value = 0
  if (clock) clearInterval(clock)
  clock = setInterval(() => { clockNow.value = Date.now(); elapsed.value = Math.floor((clockNow.value - startedAt) / 1000) }, 1000)
}

async function startPlanning() {
  if (!readyRequest.value || planning.value) return
  if (sessionStorage.getItem(ACTIVE_JOB_KEY)) {
    await restoreTask()
    return
  }
  planning.value = true
  error.value = ''
  events.value = []
  metrics.value = {}
  agentStates.value = {}
  lastSnapshotAt.value = 0
  progress.value = 0
  progressMessage.value = '正在创建规划任务'
  startClock()
  void scrollThread()
  try {
    await loadCandidates(readyRequest.value.city)
    if (!alive) return
    const request: TripFormData = { ...readyRequest.value, candidate_places: candidates.value }
    readyRequest.value = request
    sessionStorage.setItem('tripRequest', JSON.stringify(request))
    messages.value.push({ role: 'assistant', content: `收到，开始为你安排${request.city}${request.travel_days}天的旅程。我会核对景点、天气、住宿和交通；需要你决定的地方，我们在这里确认。` })
    persist()
    activeTask = generateTripPlanWithProgress(request, recordProgress, showDecision, id => {
      jobId.value = id
      sessionStorage.setItem(ACTIVE_JOB_KEY, id)
      persist()
    }, receiveSnapshot)
    await finishTask(activeTask, request)
  } catch (cause: any) {
    handleTaskError(cause)
  } finally { stopPlanning() }
}

async function finishTask(task: Promise<TripPlan>, request: TripFormData) {
  const plan = await task
  if (!alive) return
  sessionStorage.setItem('tripPlan', JSON.stringify(plan))
  sessionStorage.removeItem(ACTIVE_JOB_KEY)
  progress.value = 100
  progressMessage.value = '行程已准备好，正在为你打开'
  let destination = '/result'
  try {
    const saved = await createSavedTrip(request, plan)
    destination = `/trips/${saved.id}`
  } catch {
    // The completed itinerary remains viewable even if saving is temporarily unavailable.
  }
  if (!alive) return
  sessionStorage.removeItem(CHAT_KEY)
  jobId.value = ''
  await cloud.save({ messages: messages.value, draft: draft.value, suggestions: suggestions.value, request, result_url: destination, job_id: '' }, `${request.city}旅行规划`)
  await router.push(destination)
}

function handleTaskError(cause: any) {
  if (!alive || cause?.name === 'AbortError') return
  if(cause?.name==='JobCancelled'){
    messages.value.push({role:'assistant',content:'这次规划已取消，已保存的行程保持不变。你可以调整需求后重新生成。'})
    sessionStorage.removeItem(ACTIVE_JOB_KEY);jobId.value='';error.value='';persist();return
  }
  error.value = cause.response?.data?.detail || cause.message || '这次规划没有完成，可以重新尝试。'
  // Keep the job identity: a connection failure does not mean the worker failed.
}

function stopPlanning() {
  planning.value = false
  waitingInput.value = null
  activeTask = null
  if (clock) clearInterval(clock)
  void scrollThread()
}

async function chooseOption(value: string) {
  if (!jobId.value || resuming.value || !waitingInput.value) return
  const label = waitingInput.value.options.find(option => option.value === value)?.label || value
  resuming.value = true
  error.value = ''
  try {
    await resumePlanningJob(jobId.value, value)
    messages.value.push({ role: 'user', content: label })
    waitingInput.value = null
    progressMessage.value = '已收到你的选择，继续安排中'
    persist()
  } catch (cause: any) {
    error.value = cause.response?.data?.detail || '没能提交这个选择，请再试一次。'
  } finally { resuming.value = false; void scrollThread() }
}

async function restoreTask() {
  const id = sessionStorage.getItem(ACTIVE_JOB_KEY)
  const raw = sessionStorage.getItem('tripRequest')
  if (!id || !raw) return
  try {
    const request = JSON.parse(raw) as TripFormData
    const job = await getPlanningJob(id)
    if (!alive) return
    readyRequest.value = request
    if (!messages.value.length) messages.value.push({ role: 'assistant', content: `已找到上次的${request.city}旅行规划，正在恢复进度。` })
    if (job.status === 'failed' || job.status === 'cancelled') { sessionStorage.removeItem(ACTIVE_JOB_KEY); error.value = job.status === 'cancelled' ? '上次任务已取消，可调整需求后重新生成。' : job.error || '上次规划没有完成，可以重试。'; return }
    planning.value = true
    jobId.value = id
    startClock()
    receiveSnapshot(job)
    progress.value = job.progress
    progressMessage.value = job.message
    if (job.waiting_input) showDecision(id, job.waiting_input)
    if (job.status === 'completed' && job.data) await finishTask(Promise.resolve(job.data), request)
    else {
      activeTask = waitForExistingJob(id, recordProgress, showDecision, receiveSnapshot)
      await finishTask(activeTask, request)
    }
  } catch (cause: any) {
    if (cause.response?.status === 404) sessionStorage.removeItem(ACTIVE_JOB_KEY)
    else handleTaskError(cause)
  } finally { stopPlanning() }
}

function resetConversation() {
  if (planning.value || sending.value) return
  if (route.query.conversation) void router.replace({ path: '/' })
  cloud.reset()
  conversationSummary.value = null
  jobId.value = ''
  latestJob.value=null
  sessionStorage.removeItem(ACTIVE_JOB_KEY)
  messages.value = []
  draft.value = {}
  suggestions.value = []
  readyRequest.value = null
  candidates.value = []
  error.value = ''
  input.value = ''
  sessionStorage.removeItem(CHAT_KEY)
  map?.destroy()
  map = null
  mapReady.value = false
  void loadRecent()
}

async function updateMap(city: string) {
  await nextTick()
  if (!mapElement.value || !city || !alive) return
  mapError.value = ''
  const key = import.meta.env.VITE_AMAP_WEB_JS_KEY
  if (!key || key.startsWith('your_')) { mapError.value = '配置地图 Key 后可查看目的地地图，对话规划仍可使用。'; return }
  try {
    amap = await AMapLoader.load({ key, version: '2.0', plugins: ['AMap.Geocoder', 'AMap.ToolBar'] })
    if (!alive || draft.value.city !== city || !mapElement.value) return
    if (!map) {
      map = new amap.Map(mapElement.value, { zoom: 12, viewMode: '2D', mapStyle: 'amap://styles/normal' })
      map.addControl(new amap.ToolBar({ position: 'LB' }))
      geocoder = new amap.Geocoder()
    }
    geocoder.getLocation(city, (status: string, result: any) => {
      if (!alive || draft.value.city !== city) return
      if (status === 'complete' && result.geocodes?.length) {
        map.setZoomAndCenter(12, result.geocodes[0].location)
        mapReady.value = true
        drawMarkers()
      } else mapError.value = '暂时没能定位这座城市，仍可继续对话规划。'
    })
  } catch { if (alive) mapError.value = '地图暂时未能加载，你可以继续对话规划。' }
}

function drawMarkers() {
  if (!map || !amap) return
  map.remove(markers)
  markers = candidates.value.filter(place => place.location).map(place => new amap.Marker({ position: [place.location!.longitude, place.location!.latitude], title: place.canonical_name }))
  if (markers.length) { map.add(markers); map.setFitView(markers, false, [80, 260, 80, 50]) }
}

function fitMap() {
  if (markers.length) map?.setFitView(markers, false, [80, 260, 80, 50])
  else if (draft.value.city) void updateMap(draft.value.city)
}

watch(() => draft.value.city, city => {
  candidates.value = []
  mapReady.value = false
  if (city) { void updateMap(city); void loadCandidates(city) }
})

onMounted(async () => {
  void loadRecent()
  try {
    const saved = JSON.parse(sessionStorage.getItem(CHAT_KEY) || 'null')
    if (saved) { messages.value = saved.messages || []; draft.value = saved.draft || {}; suggestions.value = saved.suggestions || []; readyRequest.value = saved.request || null }
  } catch { sessionStorage.removeItem(CHAT_KEY) }
  const localConversation = (() => { try { return JSON.parse(sessionStorage.getItem(CHAT_KEY) || '{}').conversation_id } catch { return null } })()
  if (typeof route.query.conversation === 'string' || localConversation) {
    try {
      const record = await getConversation(typeof route.query.conversation === 'string' ? route.query.conversation : localConversation)
      if (record.kind !== 'planner') throw new Error('会话类型不匹配')
      cloud.reset(record)
      conversationSummary.value = record.summary || null
      const state = record.snapshot || {}
      messages.value = state.messages || []; draft.value = state.draft || {}; suggestions.value = state.suggestions || []; readyRequest.value = state.request || null
      sessionStorage.removeItem(ACTIVE_JOB_KEY)
      if (state.job_id && state.request) { sessionStorage.setItem(ACTIVE_JOB_KEY, state.job_id); sessionStorage.setItem('tripRequest', JSON.stringify(state.request)) }
      if (state.result_url) messages.value.push({ role: 'assistant', content: '上次行程已生成，可以在“我的行程”查看。' })
    } catch (cause: any) { error.value = cause.response?.data?.detail || cause.message; return }
  }
  const city = sessionStorage.getItem('tripmind.guide_prefill_city')
  if (city) {
    sessionStorage.removeItem('tripmind.guide_prefill_city')
    input.value = `想去${city}旅行，请结合我导入的攻略帮我规划。`
  }
  void restoreTask()
  void scrollThread()
})

onUnmounted(() => {
  alive = false
  activeTask?.cancel()
  if (clock) clearInterval(clock)
  map?.destroy()
})
</script>

<style scoped>
.cloud-status { padding: 10px 24px; font-size: 12px; color: #63828d; text-align: right; }
.chat-home { min-height: calc(100vh - 72px); background: radial-gradient(ellipse 65% 420px at 18% 0, #d7f5fc 0, transparent 100%), radial-gradient(ellipse 60% 420px at 90% 0, #e2edff 0, transparent 100%), #fff; color: #182329; }
.chat-home:not(.in-conversation) { position:relative; isolation:isolate; background:#fff; }
.chat-home:not(.in-conversation)::before { content:''; position:absolute; z-index:-1; pointer-events:none; inset:0 0 auto; height:700px; background:radial-gradient(ellipse 58% 68% at 10% 0%,#74d4f5 0%,transparent 100%),radial-gradient(ellipse 62% 72% at 92% 0%,#b4d6fb 0%,transparent 100%); opacity:.38; transition:opacity .4s ease; }
.chat-home:has(.launch-composer:focus-within)::before { opacity:1; }
.chat-home:has(.launch-composer:focus-within) .recent-section { opacity:.5; }
.chat-home:has(.launch-composer:focus-within) .inspiration { opacity:.6; }
button { cursor: pointer; }
button:disabled { cursor: default; opacity: .45; }
button, a { -webkit-tap-highlight-color: transparent; }
button:focus-visible, a:focus-visible, textarea:focus-visible { outline: 2px solid #22b4d6; outline-offset: 4px; }
.eyebrow { color: #7e909b; font-size: 10px; letter-spacing: 2px; font-weight: 700; }
.welcome { max-width: 1020px; margin: auto; padding: 48px 28px 36px; text-align: center; scroll-margin-top: 85px; }
.welcome h1 { margin: 16px 0 14px; color: #172126; font-size: clamp(30px, 3.4vw, 44px); letter-spacing: -1.6px; line-height: 1.3; font-weight: 800; }
.mobile-break { display: none; }
.welcome-note { margin: 0; color: #85929b; font-size: 15px; }
.launch-composer { max-width: 860px; margin: 48px auto 24px; padding: 24px 28px 22px; border: 1px solid #ffffff; border-radius: 44px; background: #fff; box-shadow: 0 20px 65px #284d6d0b; text-align: left; transition:box-shadow .3s ease,border-color .3s ease; }
.launch-composer:focus-within { border-color:#c7e5ff; box-shadow:0 22px 75px #3187b91a,0 0 0 4px #ffffff70; }
.launch-input-row { display:flex; gap:24px; align-items:center; min-height:108px; }
.launch-input-row .send-button { width:50px; height:50px; font-size:29px; }
.launch-composer textarea:focus-visible { outline:none; }
.composer-tabs { display: inline-flex; gap: 4px; padding: 4px; background: #f4f5f4; border-radius: 30px; font-size: 13px; font-weight: 700; }
.composer-tabs span, .composer-tabs a { padding: 9px 16px; border-radius: 24px; text-decoration: none; color: #838888; }
.composer-tabs .active { color: white; background: #1c2425; box-shadow: 0 2px 5px #0002; }
textarea { width: 100%; resize: none; border: 0; outline: none; background: transparent; line-height: 1.8; color: #263d48; }
.launch-composer textarea { flex:1; min-width:0; margin:0; padding:16px 2px; font-family:inherit; font-size:18px; line-height:1.7; caret-color:#1998c9; max-height:240px; overflow-y:auto; }
textarea::placeholder { color: #a1adb4; }
.launch-bottom { display: flex; gap:12px; justify-content: space-between; align-items: center; padding-top: 20px; border-top: 1px solid #eff1f2; color: #788a95; font-size: 12px; }
.launch-shortcut { white-space:nowrap; color:#526975; }.launch-shortcut span { margin-left:8px; }
.send-button { width: 40px; height: 40px; flex-shrink: 0; display: grid; place-items: center; border: 0; border-radius: 50%; color: #fff; background: #19282e; font-size: 25px; transition: background .2s, transform .2s; }
.send-button:not(:disabled):hover { background: #168ca3; transform: translateY(-2px); }
.send-button:disabled { background: #d4dadc; opacity: 1; }
.inspiration { display: flex; flex-wrap: wrap; justify-content: center; align-items: center; gap: 9px; color: #929ea5; font-size: 11px; transition:opacity .3s ease; }
.inspiration > span { width: 100%; margin: 4px 0 8px; }
.inspiration button { padding: 9px 13px; border: 1px solid #e8edef; border-radius: 25px; background: #ffffffa8; color: #596d76; font-size: 11px; }
.inspiration button:hover { border-color: #93d4e3; background: #f3fcfe; }
.inspiration button span { margin-left: 10px; color: #9aabb1; }
.recent-section { max-width: 1240px; margin: auto; padding: 26px 28px 70px; transition:opacity .3s ease; }
.section-heading { display: flex; align-items: end; justify-content: space-between; margin-bottom: 24px; }
.section-heading h2 { display: flex; align-items: center; gap: 12px; margin: 7px 0 0; font-size: 25px; letter-spacing: -.7px; }
.section-heading h2 span { padding: 3px 8px; border-radius: 10px; background: #eff4f5; font-size: 12px; color: #7d909a; }
.section-heading > a { color: #647780; text-decoration: none; font-size: 12px; }
.recent-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 20px; }
.recent-card { overflow: hidden; border-radius: 22px; background: #f1f5d4; color: inherit; text-decoration: none; transition: transform .2s; }
.recent-card:nth-child(2n) { background: #e8f4f1; }
.recent-card:hover { transform: translateY(-4px); }
.trip-cover { height: 170px; position: relative; display: flex; align-items: end; padding: 16px; background: linear-gradient(135deg, #a3c5b6, #426c7b); background-size: cover; background-position: center; }
.trip-cover::before { content: ''; position: absolute; inset: 0; background: linear-gradient(transparent 40%, #14232a50); }
.trip-cover > span { position: relative; color: white; font-size: 28px; font-weight: 700; }
.trip-cover small { position: absolute; left: 14px; top: 14px; background: #fffffff0; padding: 5px 10px; border-radius: 20px; font-size: 11px; }
.trip-card-body { padding: 20px; }
.trip-card-body h3 { margin: 0 0 9px; font-size: 16px; line-height: 1.5; }
.trip-card-body p { margin: 0 0 20px; color: #7f8c79; font-size: 11px; }
.trip-card-body > span { display: flex; justify-content: space-between; font-size: 11px; color: #67755a; }
.recent-empty { padding: 44px 20px; border: 1px dashed #e2e9eb; border-radius: 24px; text-align: center; color: #91a0a8; font-size: 13px; }
.recent-empty h3 { color: #566d78; font-size: 16px; font-weight: 500; }
.empty-spark { font-size: 32px; color: #6cb5bc; }
.text-button { background: none; border: 0; color: #18899b; }
.conversation-workspace { display: grid; grid-template-columns: 430px minmax(0, 1fr); height: calc(100dvh - 72px); min-height: 550px; }
.conversation-panel { display: flex; flex-direction: column; min-height: 0; background: #fff; border-right: 1px solid #e8edef; z-index: 2; }
.conversation-header { min-height: 80px; padding: 15px 22px; display: flex; align-items: center; gap: 12px; border-bottom: 1px solid #f0f3f4; }
.back-button { border: 1px solid #e9eef0; background: white; border-radius: 50%; width: 34px; height: 34px; color: #657983; }
.conversation-header > div { display: flex; flex-direction: column; gap: 6px; }
.conversation-header strong { font-size: 14px; }
.conversation-header div span { display: flex; align-items: center; gap: 6px; color: #8b9ca3; font-size: 10px; }
.conversation-header i, .map-tag i { width: 5px; height: 5px; border-radius: 50%; background: #22b4b3; }
.conversation-header > a { margin-left: auto; color: #7a909a; font-size: 11px; text-decoration: none; white-space: nowrap; }
.thread { flex: 1; min-height: 0; overflow-y: auto; padding: 22px 22px 28px; scrollbar-width: thin; scrollbar-color: #e1e9ed transparent; }
.thread-intro { text-align: center; color: #a3b1b8; margin: 4px 0 26px; font-size: 10px; }
.thread-intro > span { color: #68b7c7; font-size: 24px; }
.chat-message { display: flex; align-items: flex-start; gap: 10px; margin-bottom: 22px; }
.assistant-avatar { display: grid; place-items: center; width: 26px; height: 26px; flex-shrink: 0; border-radius: 50%; color: #1fa7b5; background: #ebf9fa; font-size: 15px; }
.message-author { display: block; padding-top: 4px; margin-bottom: 8px; font-size: 10px; font-weight: 700; color: #6aa6b2; }
.message-body { min-width: 0; }
.message-body p { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; color: #415760; font-size: 14px; line-height: 1.95; }
.chat-message.user { justify-content: flex-end; }
.user .message-body { max-width: 90%; padding: 12px 17px; border-radius: 20px 20px 5px 20px; background: #ddf7fa; }
.user .message-body p { color: #254b56; }
.reply-suggestions { margin: -4px 0 24px 36px; display: flex; gap: 8px; flex-direction: column; align-items: flex-start; }
.reply-suggestions button { border: 1px solid #dcebef; color: #397b8b; background: #fbfefe; padding: 10px 14px; border-radius: 16px; text-align: left; font-size: 12px; line-height: 1.6; }
.reply-suggestions button:hover { background: #edfafd; }
.confirmation-card { padding: 20px; margin: 8px 0 20px 35px; border: 1px solid #e3ecee; border-radius: 22px; background: linear-gradient(145deg, #f5fcfc, #fff); }
.confirmation-card h3 { font-size: 21px; margin: 10px 0; }
.confirmation-card p { color: #73868e; font-size: 11px; line-height: 1.8; margin: 4px 0; }
.confirmation-card > small { display: block; color: #98a9af; font-size: 10px; margin: 12px 0; line-height: 1.8; }
.plan-button { width: 100%; display: flex; justify-content: space-between; align-items: center; border: 0; border-radius: 12px; padding: 13px 16px; background: #1d343c; color: white; font-size: 12px; }
.plan-button:hover { background: #167d90; }
.chat-composer { margin: 0 16px 16px; padding: 13px 15px 10px; border: 1px solid #e3eaee; border-radius: 22px; background: white; box-shadow: 0 5px 30px #23404d08; }
.chat-composer textarea { font-size: 13px; max-height: 150px; }
.chat-composer > div { display: flex; justify-content: space-between; align-items: center; }
.chat-composer div > span { font-size: 9px; color: #a7b3b8; }
.chat-composer .send-button { width: 32px; height: 32px; font-size: 20px; }
.typing-indicator { display: flex; align-items: center; gap: 4px; padding-top: 9px; }
.typing-indicator i { width: 4px; height: 4px; border-radius: 50%; background: #69b8c5; animation: breathe 1s infinite alternate; }
.typing-indicator i:nth-child(2) { animation-delay: .2s; }
.typing-indicator i:nth-child(3) { animation-delay: .4s; }
.typing-indicator span { margin-left: 8px; font-size: 10px; color: #9aabb2; }
@keyframes breathe { from { opacity: .25; } to { opacity: 1; } }
.conversation-error { margin: 10px 0 20px 35px; color: #ab674f; font-size: 12px; background: #fff8f4; border-radius: 14px; padding: 12px 15px; }
.conversation-error p { margin: 0; line-height: 1.7; }
.conversation-error button { border: 0; background: none; color: #ad634b; padding: 8px 0 0; text-decoration: underline; }
.live-progress { margin: 12px 0 15px 35px; padding: 18px; background: #f5fafb; border: 1px solid #e5eff0; border-radius: 20px; }
.progress-heading { display: flex; justify-content: space-between; align-items: center; font-size: 12px; color: #3e6572; font-weight: 600; }
.progress-heading small { font-size: 10px; color: #99adb5; font-weight: 400; }
.live-progress > p { font-size: 12px; line-height: 1.8; color: #7c939d; }
.progress-track { height: 3px; background: #e3edef; border-radius: 5px; overflow: hidden; }
.progress-track > span { display: block; height: 100%; background: #58b9c5; transition: width .5s; }
.stage-clock { display: flex; justify-content: space-between; gap: 12px; margin-top: 15px; color: #608491; font-size: 11px; }
.stage-clock strong { font-variant-numeric: tabular-nums; font-weight: 500; }
.stage-breakdown { margin-top: 16px; border-top: 1px solid #e4edf0; padding-top: 12px; }
.stage-breakdown summary { color: #7b949f; font-size: 10px; cursor: pointer; }
.stage-breakdown summary > span { float: right; color: #a0b0b8; font-size: 9px; }
.stage-breakdown > div { display: flex; justify-content: space-between; gap: 8px; margin-top: 11px; font-size: 10px; color: #9bafb7; }
.stage-breakdown > div > span { display: flex; align-items: center; gap: 5px; }
.stage-breakdown i { width: 4px; height: 4px; background: #c5d4d9; border-radius: 50%; }
.stage-breakdown strong { font-size: 10px; font-weight: 400; font-variant-numeric: tabular-nums; }
.stage-breakdown small { font-size: 9px; font-weight: 400; }
.stage-breakdown .active-stage { color: #3e9aaa; }
.stage-breakdown .active-stage i { background: #3e9aaa; }
.live-progress > .waiting-hint { background: #fffaf0; padding: 11px 12px; border-radius: 12px; color: #a18d66; font-size: 10px; line-height: 1.9; }
.status-sync { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin: 12px 0; font-size: 9px; color: #a3b4bb; }
.status-sync button { border: 0; background: none; padding: 3px 0; color: #729dab; font-size: 9px; }
.research-status { display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; margin: 18px 0; }
.research-status > span { display: flex; align-items: center; gap: 5px; color: #7f969e; font-size: 10px; }
.research-status i { width: 4px; height: 4px; background: #c5d3d8; border-radius: 50%; }
.research-status .running i { background: #25aab8; animation: breathe 1s infinite alternate; }
.research-status .completed i { background: #38aa86; }
.research-status .degraded i { background: #c99e51; }
.research-status small { margin-left: auto; font-size: 9px; color: #9aaeb5; }
.progress-footnote { color: #9daeb5; font-size: 10px; line-height: 1.8; }
.decision-replies { display: flex; flex-direction: column; gap: 8px; }
.decision-replies button { padding: 12px; border: 1px solid #d6e7e9; border-radius: 12px; background: white; text-align: left; font-size: 12px; line-height: 1.7; color: #437483; }
.decision-replies button > span { float: right; }
.decision-replies small { color: #9aadb3; font-size: 10px; }
.destination-canvas { position: relative; overflow: hidden; background: #eff5f4; min-width: 0; }
.destination-map { position: absolute; inset: 0; }
.map-placeholder { position: absolute; inset: 0; display: flex; flex-direction: column; justify-content: center; align-items: center; padding-right: 260px; background: radial-gradient(ellipse at 35% 40%, #d5f1ee, transparent 65%), linear-gradient(145deg,#f3f8f4,#e5eff5); }
.map-placeholder h2 { color: #5d8589; font-size: clamp(18px, 2vw, 30px); font-weight: 400; letter-spacing: -1px; margin: 26px 0 12px; }
.map-placeholder p { color: #99afb4; font-size: 12px; max-width: 260px; text-align: center; line-height: 1.8; }
.map-orbit { width: 160px; height: 160px; border: 1px solid #b8d6d35c; border-radius: 50%; padding: 22px; box-shadow: 0 0 0 35px #cfdfde25, 0 0 0 70px #cfdfde18; }
.map-orbit span { width: 100%; height: 100%; display: grid; place-items: center; color: #68a1a3; background: #ffffff60; border-radius: 50%; font-size: 50px; }
.map-topline { position: absolute; left: 24px; right: 24px; top: 20px; display: flex; justify-content: space-between; gap: 10px; }
.map-tag { display: inline-flex; align-items: center; gap: 8px; padding: 11px 16px; border-radius: 24px; background: #ffffffed; border: 1px solid #ffffffb0; box-shadow: 0 2px 20px #30495508; color: #53717e; font-size: 11px; }
.trip-brief { position: absolute; top: 82px; right: 24px; width: 245px; max-height: calc(100% - 150px); overflow-y: auto; padding: 23px; border-radius: 24px; background: #ffffffed; backdrop-filter: blur(12px); box-shadow: 0 8px 45px #2948560c; }
.brief-heading { display: flex; justify-content: space-between; align-items: center; font-size: 11px; color: #68818c; }
.brief-heading small { font-size: 9px; color: #a7b6bc; }
.trip-brief h2 { margin: 24px 0 10px; font-size: 24px; font-weight: 600; letter-spacing: -1px; }
.trip-brief h2 > small { margin-left: 8px; color: #89a6ad; font-size: 13px; font-weight: 400; }
.brief-route { color: #829aa4; font-size: 11px; line-height: 1.8; }
.brief-route span { color: #41afbe; margin: 0 8px; }
.brief-details { display: grid; grid-template-columns: 1fr 1fr; gap: 20px 12px; margin: 24px 0; padding: 21px 0; border-block: 1px solid #edf1f2; }
.brief-details span { display: flex; flex-direction: column; gap: 8px; color: #9aabb3; font-size: 10px; }
.brief-details strong { font-size: 11px; color: #4d6875; font-weight: 500; }
.preference-tags { display: flex; flex-wrap: wrap; gap: 6px; }
.preference-tags span { font-size: 10px; padding: 5px 9px; background: #edf8f6; color: #6b938c; border-radius: 20px; }
.brief-notes { margin-top: 16px; font-size: 11px; color: #8aa0a9; line-height: 1.8; white-space: pre-wrap; }
.candidate-notes { margin-top: 20px; font-size: 11px; color: #6d909b; line-height: 1.8; }
.candidate-notes strong { font-weight: 500; }
.candidate-notes p { color: #96aab2; }
.brief-tip { margin: 24px 0 0; font-size: 10px; color: #aab8bd; line-height: 1.9; }
.map-caption { position: absolute; bottom: 24px; left: 24px; color: #708c94; font-size: 10px; padding: 8px 14px; border-radius: 20px; background: #ffffffc9; }
@media (min-width: 1600px) { .conversation-workspace { grid-template-columns: 480px minmax(0,1fr); } .thread { padding-inline: 30px; } .trip-brief { width: 280px; padding: 28px; } }
@media (max-width: 1100px) { .conversation-workspace { grid-template-columns: 410px minmax(0,1fr); } .trip-brief { top: auto; bottom: 24px; right: 20px; left: 20px; width: auto; max-height: 43%; } .brief-details { margin: 14px 0; padding: 14px 0; gap: 10px; grid-template-columns: repeat(4,1fr); } .trip-brief h2 { margin-top: 14px; } .brief-tip, .map-caption, .brief-notes, .candidate-notes { display: none; } .map-placeholder { padding-right: 0; padding-bottom: 150px; } .recent-grid { grid-template-columns: repeat(2,1fr); } }
@media (max-width: 720px) { .welcome { padding: 45px 20px 28px; } .welcome h1 { font-size: 34px; letter-spacing: -1px; } .welcome-note { font-size: 12px; line-height: 1.8; } .launch-composer { margin-top: 30px; padding: 16px 18px; border-radius: 24px; box-shadow: 0 12px 40px #284d6d12; } .launch-composer textarea { font-size: 15px; } .launch-bottom { font-size: 10px; } .recent-section { padding: 20px 20px 40px; } .recent-grid { gap: 12px; } .trip-card-body { padding: 15px; } .trip-cover { height: 135px; } .section-heading h2 { font-size: 21px; } .conversation-workspace { height: calc(100dvh - 64px); min-height: 450px; grid-template-columns: 1fr; } .destination-canvas { display: none; } .conversation-header { min-height: 68px; } .thread { padding-inline: 18px; } .chat-composer { margin-bottom: max(12px, env(safe-area-inset-bottom)); } }
@media (max-width:720px) { .launch-composer { padding:20px; border-radius:30px; } .launch-input-row { min-height:116px; gap:14px; } .launch-input-row .send-button { width:42px; height:42px; } .launch-composer textarea { font-size:16px; padding:14px 0; } .launch-bottom { padding-top:16px; font-size:11px; } .launch-shortcut { display:none; } .chat-home:not(.in-conversation)::before { height:620px; } }
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation: none !important; transition: none !important; } }
</style>
