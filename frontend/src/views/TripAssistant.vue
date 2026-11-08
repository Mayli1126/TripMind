<template>
  <main class="assistant-page" :class="{ embedded }">
    <header v-if="!embedded">
      <router-link to="/trips">← 我的行程</router-link>
      <span>TRIPMIND · 行程对话</span>
      <router-link v-if="trip" :to="`/trips/${trip.id}`">查看完整计划 ↗</router-link>
    </header>
    <div v-if="loading" class="loading">正在打开行程…</div>
    <div v-else-if="!trip" class="loading">{{ error || '无法读取这份行程' }}</div>
    <div v-else class="workspace">
      <section class="conversation">
        <div class="intro"><span class="eyebrow">聊聊你的下一程</span><h1>{{ trip.title }}</h1><p>解释安排，也一起调整。问答不会改计划，修改后需要你确认。</p><small>{{ cloud.syncMessage.value }}</small></div>
        <ConversationSummary :summary="conversationSummary" />
        <div ref="thread" class="thread" aria-live="polite">
          <div v-if="!messages.length" class="welcome">
            <div class="spark">✦</div><h2>这份行程，哪里想再聊聊？</h2>
            <p>我会根据当前 v{{ trip.current_version }} 的已保存计划回答。</p>
            <div class="prompts"><button v-for="prompt in prompts" :key="prompt" :disabled="busy" @click="send(prompt)">{{ prompt }} ↗</button></div>
          </div>
          <article v-for="(item, index) in messages" :key="index" :class="['bubble', item.role]">
            <small>{{ item.role === 'user' ? '你' : '✦ TripMind' }}</small><p>{{ item.content }}</p>
            <template v-if="item.result">
              <details v-if="item.result.evidence.length"><summary>查看计划依据 · v{{ item.result.base_version }}</summary><div v-for="entry in item.result.evidence" :key="entry.path" class="evidence"><code>{{ entry.path }}</code><span>{{ entry.value }}</span></div></details>
              <p v-for="note in item.result.uncertainties" :key="note" class="uncertainty">ⓘ {{ note }}</p>
              <div v-if="item.result.can_apply && item.result.proposed_instruction" class="proposal">
                <small>修改草稿 · 尚未执行</small><p>{{ item.result.proposed_instruction }}</p>
                <button :disabled="busy || !!pending || item.result.base_version !== trip.current_version" @click="revise(item.result)">按此建议生成修改</button>
              </div>
            </template>
          </article>
          <p v-if="asking" class="thinking">✦ 正在结合当前计划整理回答…</p>
          <div v-if="revising" class="progress"><strong>{{ progressMessage }}</strong><progress :value="progress" max="100" /><p>正在生成草稿，原计划保持不变。</p></div>
          <TaskDiagnostics v-if="revising" :job-id="jobId" :job="latestJob" @state="latestJob=$event" />
          <div v-if="waiting" class="proposal"><p>{{ waiting.question }}</p><button v-for="option in waiting.options" :key="option.value" :disabled="resuming" @click="resume(option.value)">{{ option.label }}</button></div>
          <div v-if="pending" class="review">
            <h2>先看看这些修改</h2><RevisionDiff :changes="pending.plan.revision_diff || []" :explanation="pending.instruction" />
            <details><summary>查看修改后的逐日安排</summary><div v-for="(day, i) in pending.plan.days" :key="i" class="day"><strong>{{ day.date }}</strong><p>{{ day.attractions.map(place => place.name).join(' → ') }}</p></div></details>
            <div class="actions"><button :disabled="saving" @click="confirm">{{ saving ? '正在保存…' : '确认并保存新版本' }}</button><button class="ghost" :disabled="saving" @click="pending = null">放弃本次修改</button></div>
          </div>
        </div>
        <div v-if="error" class="error">{{ error }} <button v-if="conflict" :disabled="busy" @click="load">读取最新版并重新开始</button></div>
        <form class="composer" @submit.prevent="send()"><textarea v-model="input" :disabled="busy || !!pending || conflict" placeholder="例如：第二天太赶了，帮我减少一个景点" rows="2" maxlength="6000" @keydown.enter.exact.prevent="send()" /><button :disabled="busy || !input.trim() || !!pending || conflict" aria-label="发送">↑</button><small>Enter 发送 · Shift + Enter 换行 · 不提供实时票务与预订</small></form>
      </section>
      <aside v-if="!embedded"><span class="eyebrow">当前保存的计划</span><h2>{{ trip.city }} · {{ trip.days }} 天</h2><p>{{ trip.start_date }} — {{ trip.end_date }}</p><span class="version">v{{ trip.current_version }}</span><div v-for="(day, index) in trip.plan.days" :key="index" class="day"><small>DAY {{ index + 1 }} · {{ day.date }}</small><p>{{ day.attractions.map(place => place.name).join(' → ') || '自由活动' }}</p></div><p class="aside-note">对话和修改草稿同步到账号，可从历史对话继续。草稿只有确认保存后，才成为行程新版本。</p></aside>
    </div>
  </main>
</template>

<script setup lang="ts">
import TaskDiagnostics from '@/components/TaskDiagnostics.vue'
import type { PlanningJobResponse } from '@/types'
import ConversationSummary from '@/components/ConversationSummary.vue'
import type { ConversationSummary as SummaryData } from '@/services/conversations'
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute } from 'vue-router'
import RevisionDiff from '@/components/RevisionDiff.vue'
import { askSavedTrip, createAssistantRevision, getSavedTrip, resumePlanningJob, saveTripVersion, waitForPlanningJob } from '@/services/api'
import { getConversation, listConversations, useConversation } from '@/services/conversations'
import type { TripAssistantAnswer } from '@/services/api'
import type { PlannerChatMessage, SavedTripDetail, TripPlan, WaitingInputPayload } from '@/types'

const route = useRoute()
const props = withDefaults(defineProps<{ embedded?: boolean; tripId?: string; initialQuestion?: string }>(), { embedded: false, tripId: '', initialQuestion: '' })
const emit = defineEmits<{ updated: [trip: SavedTripDetail] }>()
const sourceTripId = computed(() => props.embedded ? props.tripId : String(route.params.id || ''))
const cloud = useConversation('trip')
const hydrated = ref(false)
const activeRevision = ref<{ job: string; instruction: string; version: number } | null>(null)
const trip = ref<SavedTripDetail | null>(null)
const messages = ref<(PlannerChatMessage & { result?: TripAssistantAnswer })[]>([])
const input = ref(''), error = ref(''), conflict = ref(false), loading = ref(true)
const asking = ref(false), revising = ref(false), saving = ref(false), resuming = ref(false)
const thread = ref<HTMLElement>(), progress = ref(0), progressMessage = ref('正在创建修改任务…')
const waiting = ref<WaitingInputPayload | null>(null), jobId = ref('')
const pending = ref<{ plan: TripPlan; instruction: string; version: number } | null>(null)
const busy = computed(() => asking.value || revising.value || saving.value)
const prompts = ['第二天的安排来得及吗？', '这份预算是怎么算的？', '哪些预约信息还需要核实？', '下雨的话可以怎么调整？']
let generation = 0, cancel: (() => void) | undefined
const conversationSummary = ref<SummaryData | null>(null)
const latestJob=ref<PlanningJobResponse|null>(null)
const scroll = async () => { await nextTick(); thread.value?.scrollTo({ top: thread.value.scrollHeight, behavior: 'smooth' }) }
function fail(cause: any) { error.value = cause.response?.data?.detail || cause.message || '请求失败，原计划未发生变化'; conflict.value = cause.response?.status === 409 }
async function load() {
  const current = ++generation
  hydrated.value = false
  cancel?.(); cancel = undefined
  asking.value = false; revising.value = false; waiting.value = null
  loading.value = true; error.value = ''; conflict.value = false; messages.value = []; pending.value = null; trip.value = null
  conversationSummary.value = null
  input.value = props.embedded ? props.initialQuestion.slice(0, 6000) : typeof route.query.ask === 'string' ? route.query.ask.slice(0, 6000) : ''
  try {
    const value = await getSavedTrip(sourceTripId.value); if (current !== generation) return; trip.value = value
    const id = !props.embedded && typeof route.query.conversation === 'string' ? route.query.conversation : (await listConversations()).find(x => x.kind === 'trip' && x.trip_id === value.id)?.id
    cloud.reset()
    activeRevision.value = null
    if (id) {
      const record = await getConversation(id)
      if (record.kind !== 'trip' || record.trip_id !== value.id) throw new Error('会话与行程不匹配')
      cloud.reset(record)
      conversationSummary.value = record.summary || null
      messages.value = record.snapshot?.messages || []
      if (record.snapshot?.base_version === value.current_version) { pending.value = record.snapshot?.pending || null; activeRevision.value = record.snapshot?.activeRevision || null }
      else messages.value.push({ role: 'assistant', content: `行程现为 v${value.current_version}，历史问答供参考，旧修改草稿不会自动应用。` })
    }
    hydrated.value = true
    if (activeRevision.value) { revising.value = true; void observeRevision(activeRevision.value, current) }
  }
  catch (cause) { if (current === generation) fail(cause) }
  finally { if (current === generation) loading.value = false }
}
async function send(text = input.value) {
  if (!trip.value || busy.value || pending.value || conflict.value || !text.trim()) return
  const current = generation
  messages.value.push({ role: 'user', content: text.trim() }); input.value = ''; asking.value = true; error.value = ''; void scroll()
  try {
    await cloud.save({ messages: messages.value, pending: pending.value, activeRevision: activeRevision.value, base_version: trip.value.current_version }, `${trip.value.title} · 行程问答`, trip.value.id)
    if (current !== generation || !trip.value) return
    const result = await askSavedTrip(trip.value.id, trip.value.current_version, messages.value.slice(-12).map(({ role, content }) => ({ role, content })), cloud.syncedId())
    if (current === generation) { conversationSummary.value = result.conversation_summary || null; messages.value.push({ role: 'assistant', content: result.answer, result }) }
  } catch (cause) { if (current === generation) fail(cause) }
  finally { if (current === generation) { asking.value = false; void scroll() } }
}
async function revise(answer: TripAssistantAnswer) {
  if (!trip.value || busy.value || pending.value || !answer.proposed_instruction) return
  revising.value = true; error.value = ''; progress.value = 0; progressMessage.value = '正在创建修改任务…'
  const current = generation
  try {
    const job = await createAssistantRevision(trip.value.id, answer.base_version, answer.proposed_instruction)
    if (current !== generation) return
    activeRevision.value = { job: job.job_id, instruction: answer.proposed_instruction, version: answer.base_version }
    await observeRevision(activeRevision.value, current)
  } catch (cause) { if (current === generation) fail(cause) }
  finally { if (current === generation) { revising.value = false; void scroll() } }
}
async function observeRevision(active: { job: string; instruction: string; version: number }, current: number) {
  try {
    const waiter = waitForPlanningJob(active.job, event => { progress.value = event.progress; progressMessage.value = event.message; void scroll() },
      (_, payload) => { waiting.value = payload; void scroll() },
      snapshot => { latestJob.value=snapshot;progressMessage.value = snapshot.message; if (snapshot.status !== 'waiting_input') waiting.value = null })
    jobId.value = active.job; cancel = waiter.cancel
    const plan = await waiter
    if (current === generation) { pending.value = { plan, instruction: active.instruction, version: active.version }; activeRevision.value = null }
  } catch (cause:any) { if (current === generation) { if(cause?.name==='JobCancelled'){activeRevision.value=null;messages.value.push({role:'assistant',content:'本次修改已取消，原计划未改变。'});error.value=''}else fail(cause) } }
  finally { if (current === generation) { revising.value = false; waiting.value = null; void scroll() } }
}
async function resume(value: string) {
  resuming.value = true
  try { await resumePlanningJob(jobId.value, value); waiting.value = null }
  catch (cause) { fail(cause) }
  finally { resuming.value = false }
}
async function confirm() {
  if (!trip.value || !pending.value || saving.value) return
  saving.value = true; error.value = ''
  try {
    trip.value = await saveTripVersion(trip.value.id, pending.value.plan, `对话修改：${pending.value.instruction}`.slice(0, 500), pending.value.version)
    emit('updated', trip.value)
    pending.value = null; messages.value.push({ role: 'assistant', content: `已保存为 v${trip.value.current_version}。后续回答将基于新计划，你还想调整哪里？` })
  } catch (cause) { fail(cause) }
  finally { saving.value = false; void scroll() }
}
const canLeave = () => !(busy.value || pending.value) || window.confirm('离开前请确认对话已同步。后台任务可能继续，但不会自动保存为行程新版本。确定离开？')
watch([messages, pending, activeRevision], () => {
  if (hydrated.value && trip.value) void cloud.save({ messages: messages.value, pending: pending.value, activeRevision: activeRevision.value, base_version: trip.value.current_version }, `${trip.value.title} · 行程问答`, trip.value.id)
}, { deep: true })
watch(sourceTripId, () => { void load() }, { immediate: true })
defineExpose({ canClose: canLeave })
onBeforeRouteLeave(canLeave)
onBeforeRouteUpdate(canLeave)
onBeforeUnmount(() => { generation++; cancel?.() })
</script>

<style scoped>
.assistant-page{min-height:calc(100vh - 80px);background:radial-gradient(ellipse at 20% 0,#dbf6fc,transparent 50%),#f7fafb;color:#172b36;padding:0 28px 30px}header{max-width:1360px;margin:auto;height:70px;display:flex;align-items:center;justify-content:space-between;gap:16px}header a{color:#456270}header span,.eyebrow{font-size:11px;letter-spacing:2px;color:#32899b;font-weight:800}.workspace{max-width:1360px;margin:auto;display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:24px}.conversation,aside{background:#fff;border:1px solid #e5eef0;border-radius:24px;box-shadow:0 12px 40px #28526908}.conversation{height:calc(100vh - 180px);min-height:620px;display:flex;flex-direction:column;overflow:hidden}.intro{padding:26px 30px 18px;border-bottom:1px solid #eef3f5}h1{font-size:25px;margin:8px 0}h2{font-size:20px}.intro p,.welcome p,aside>p{color:#7b8f99;font-size:13px}.thread{flex:1;overflow:auto;padding:24px 30px;scroll-behavior:smooth}.welcome{padding:28px 0;text-align:center}.spark{font-size:35px;color:#21b4cf}.prompts{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:24px auto;max-width:550px}button{cursor:pointer;border:0;border-radius:12px;padding:12px 16px;background:#143d4b;color:white;font:inherit;font-size:13px}button:disabled{opacity:.45;cursor:not-allowed}.prompts button{background:#f3f9fa;color:#41616d;text-align:left}.bubble{margin:0 0 24px;max-width:92%;line-height:1.8;overflow-wrap:anywhere}.bubble p{white-space:pre-wrap;margin:7px 0}.bubble small{color:#5397a5;font-size:11px}.bubble.user{margin-left:auto;background:#e0f7fb;padding:12px 18px;border-radius:18px 18px 3px 18px;width:fit-content}.bubble.assistant{padding:8px 0}.uncertainty{font-size:12px;color:#88979f}.evidence{display:flex;flex-direction:column;font-size:12px;background:#f5f9fa;padding:8px;margin:5px 0}.evidence code{color:#408697}details summary{cursor:pointer;font-size:12px;color:#527c89}.proposal,.review,.progress{background:#f3fafb;border:1px solid #d9eef2;border-radius:16px;padding:18px;margin:14px 0}.proposal button{margin:6px 8px 0 0}.review h2{margin-top:0}.actions{display:flex;gap:10px;margin-top:18px}.ghost{background:white;color:#55727d;border:1px solid #d6e5e9}.thinking{color:#4a96a6}.progress progress{display:block;width:100%;margin-top:12px;accent-color:#26b4cc}.progress p{font-size:12px;color:#81959e}.composer{margin:12px 24px 22px;border:1px solid #d9e8eb;border-radius:18px;display:grid;grid-template-columns:1fr 44px;padding:14px;gap:8px;background:#fff}.composer textarea{border:0;outline:0;resize:none;font:inherit;font-size:14px;background:transparent}.composer button{padding:0;font-size:24px;align-self:center;height:42px;border-radius:50%;background:#20adc8}.composer small{grid-column:1/-1;font-size:10px;color:#9baab0}.error{margin:8px 24px;color:#ae4e38;font-size:13px}.error button{margin:8px;background:#fff0e9;color:#9d503f}aside{padding:28px;height:fit-content}.version{display:inline-block;background:#e6f8fb;color:#2798ad;padding:4px 10px;border-radius:12px}.day{padding:18px 0;border-bottom:1px solid #edf3f5}.day small{color:#6d99a4;font-size:11px}.day p{line-height:1.9;font-size:13px;margin-bottom:0}.aside-note{line-height:1.8}.loading{text-align:center;padding:100px 20px}@media(max-width:900px){.workspace{grid-template-columns:1fr}aside{display:none}.assistant-page{padding:0 10px 16px}.conversation{height:calc(100dvh - 170px)}header span{display:none}.thread{padding:20px}.intro{padding:20px}.prompts{grid-template-columns:1fr}.composer{margin:10px 14px 16px}}
</style>
<style scoped>
.assistant-page.embedded { min-height: 0; height: 100%; padding: 0; background: #fff; }
.embedded .workspace { display: block; height: 100%; max-width: none; }
.embedded .conversation { height: 100%; min-height: 0; border: 0; border-radius: 0; box-shadow: none; }
.embedded .intro { padding: 18px 22px; }
.embedded .intro .eyebrow { display: none; }
.embedded h1 { font-size: 18px; color: #183c50; margin: 0 0 8px; }
.embedded .intro p { font-size: 12px; line-height: 1.7; margin: 0 0 6px; }
.embedded .thread { padding: 18px 22px; }
.embedded .prompts { grid-template-columns: 1fr; }
.embedded .welcome { padding: 16px 0; }
.embedded .welcome h2, .embedded .review h2 { font-size: 17px; }
.embedded .composer { margin: 10px 16px 16px; }
.embedded .composer small { line-height: 1.6; }
.embedded .composer button { background: #167c80; }
.embedded .actions { flex-wrap: wrap; }
.embedded button:focus-visible { outline: 2px solid #167c80; outline-offset: 2px; }
</style>
