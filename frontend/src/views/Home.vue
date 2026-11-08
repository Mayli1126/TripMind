<template>
  <div class="home-container">
    <AgentIntro />

    <section id="planner" class="planner-section">
      <div class="planner-heading">
        <div>
          <span class="planner-kicker">CREATE YOUR TRIP</span>
          <h2>说说你想怎样旅行</h2>
        </div>
        <p>填写越具体，规划越贴近真实需求。常用偏好会自动保存在本机，下次规划无需重复填写。</p>
      </div>

      <router-link to="/guide" class="guide-entry">
        <span class="guide-entry-icon">📖</span>
        <span class="guide-entry-text">
          <strong>有小红书/公众号攻略？</strong>
          粘贴正文或链接，AI 提取地点并核对地图位置，规划时优先安排你想去的
        </span>
        <span class="guide-entry-arrow">去导入 →</span>
      </router-link>

      <div v-if="matchingCandidateCount" class="candidate-hint">
        <span>📌 已勾选 {{ matchingCandidateCount }} 个「{{ formData.city }}」攻略地点，规划时会优先安排</span>
        <router-link to="/guide" class="candidate-hint-link">查看/调整</router-link>
      </div>

      <a-card class="form-card" :bordered="false">
      <a-form
        :model="formData"
        layout="vertical"
        @finish="handleSubmit"
      >
        <!-- 第一步:行程信息 -->
        <div class="form-section">
          <div class="section-header">
            <span class="section-icon">📍</span>
            <span class="section-title">行程信息</span>
          </div>

          <a-row :gutter="24">
            <a-col :span="6">
              <a-form-item name="origin_city" :rules="[{ required: true, message: '请输入出发城市' }]">
                <template #label>
                  <span class="form-label">出发城市</span>
                </template>
                <a-input
                  v-model:value="formData.origin_city"
                  placeholder="例如: 北京"
                  size="large"
                  class="custom-input"
                >
                  <template #prefix>
                    <span style="color: #667eea;">🏠</span>
                  </template>
                </a-input>
              </a-form-item>
            </a-col>
            <a-col :span="2" class="arrow-col">
              <span class="arrow-icon">→</span>
            </a-col>
            <a-col :span="6">
              <a-form-item name="city" :rules="[{ required: true, message: '请输入目的地城市' }]">
                <template #label>
                  <span class="form-label">目的地城市</span>
                </template>
                <a-input
                  v-model:value="formData.city"
                  placeholder="例如: 上海"
                  size="large"
                  class="custom-input"
                >
                  <template #prefix>
                    <span style="color: #764ba2;">🌍</span>
                  </template>
                </a-input>
              </a-form-item>
            </a-col>
            <a-col :span="5">
              <a-form-item name="start_date" extra="最早可选择今天" :rules="[{ required: true, message: '请选择开始日期' }]">
                <template #label>
                  <span class="form-label">出发日期</span>
                </template>
                <a-date-picker
                  v-model:value="formData.start_date"
                  :disabled-date="disabledStartDate"
                  style="width: 100%"
                  size="large"
                  class="custom-input"
                  placeholder="选择日期"
                />
              </a-form-item>
            </a-col>
            <a-col :span="5">
              <a-form-item name="end_date" extra="不得早于出发日期，行程最长 30 天" :rules="[{ required: true, message: '请选择结束日期' }]">
                <template #label>
                  <span class="form-label">结束日期</span>
                </template>
                <a-date-picker
                  v-model:value="formData.end_date"
                  :disabled-date="disabledEndDate"
                  style="width: 100%"
                  size="large"
                  class="custom-input"
                  placeholder="选择日期"
                />
              </a-form-item>
            </a-col>
          </a-row>
          <a-row v-if="formData.origin_city && formData.city && formData.origin_city !== formData.city" :gutter="24" class="transport-time-row">
            <a-col :span="8">
              <a-form-item label="已确认的出发时间（可选）" name="departure_time" extra="购票后填写，系统会优先按实际班次排程">
                <a-time-picker v-model:value="formData.departure_time" format="HH:mm" :minute-step="5" size="large" style="width: 100%" placeholder="例如 08:30" />
              </a-form-item>
            </a-col>
            <a-col :span="8">
              <a-form-item label="已确认的到达时间（可选）" name="arrival_time" extra="早于出发时间时按次日到达处理">
                <a-time-picker v-model:value="formData.arrival_time" format="HH:mm" :minute-step="5" size="large" style="width: 100%" placeholder="例如 14:20" />
              </a-form-item>
            </a-col>
            <a-col :span="8" class="transport-rule-note">
              <b>首日时间会自动扣除</b>
              <span>进站 / 值机、跨城乘坐、取行李与抵达接驳</span>
            </a-col>
          </a-row>
          <a-row v-if="formData.origin_city && formData.city && formData.origin_city !== formData.city" :gutter="24" class="transport-time-row">
            <a-col :span="8">
              <a-form-item label="行程类型" name="trip_type">
                <a-select v-model:value="formData.trip_type" size="large">
                  <a-select-option value="round_trip">往返旅行</a-select-option>
                  <a-select-option value="one_way">单程旅行</a-select-option>
                </a-select>
              </a-form-item>
            </a-col>
            <a-col v-if="formData.trip_type === 'round_trip'" :span="8">
              <a-form-item label="已确认的返程出发时间（可选）" name="return_departure_time" extra="默认从旅行结束日的目的地出发">
                <a-time-picker v-model:value="formData.return_departure_time" format="HH:mm" :minute-step="5" size="large" style="width: 100%" placeholder="例如 16:30" />
              </a-form-item>
            </a-col>
            <a-col v-if="formData.trip_type === 'round_trip'" :span="8">
              <a-form-item label="已确认的返程到达时间（可选）" name="return_arrival_time" extra="早于返程出发时间时按次日到达处理">
                <a-time-picker v-model:value="formData.return_arrival_time" format="HH:mm" :minute-step="5" size="large" style="width: 100%" placeholder="例如 21:20" />
              </a-form-item>
            </a-col>
          </a-row>
        </div>

        <!-- 第二步:偏好设置 -->
        <!-- 高级偏好：低频选项默认收起，保持首屏清爽 -->
        <div class="advanced-toggle" @click="showAdvanced = !showAdvanced">
          <span>🧩 更多偏好（住宿档次、预算上限、行程节奏等）</span>
          <b>{{ showAdvanced ? '收起 ▲' : '展开 ▼' }}</b>
        </div>
        <div v-show="showAdvanced" class="advanced-sections">
        <div class="form-section">
          <div class="section-header">
            <span class="section-icon">⚙️</span>
            <span class="section-title">偏好设置</span>
          </div>

          <a-row :gutter="24">
            <a-col :span="8">
              <a-form-item name="transportation">
                <template #label>
                  <span class="form-label">通勤交通</span>
                </template>
                <a-select v-model:value="formData.transportation" size="large" class="custom-select">
                  <a-select-option value="high_speed_train">🚄 高铁</a-select-option>
                  <a-select-option value="train">🚂 火车</a-select-option>
                  <a-select-option value="plane">✈️ 飞机</a-select-option>
                  <a-select-option value="bus">🚌 长途汽车</a-select-option>
                  <a-select-option value="自驾">🚗 自驾</a-select-option>
                  <a-select-option value="公共交通">🚇 公共交通</a-select-option>
                </a-select>
              </a-form-item>
            </a-col>
            <a-col :span="8">
              <a-form-item name="accommodation">
                <template #label>
                  <span class="form-label">住宿偏好</span>
                </template>
                <a-select v-model:value="formData.accommodation" size="large" class="custom-select">
                  <a-select-option value="经济型酒店">💰 经济型酒店</a-select-option>
                  <a-select-option value="舒适型酒店">🏨 舒适型酒店</a-select-option>
                  <a-select-option value="豪华酒店">⭐ 豪华酒店</a-select-option>
                  <a-select-option value="民宿">🏡 民宿</a-select-option>
                </a-select>
              </a-form-item>
            </a-col>
            <a-col :span="8">
              <a-form-item name="preferences">
                <template #label>
                  <span class="form-label">旅行偏好</span>
                </template>
                <div class="preference-tags">
                  <a-checkbox-group v-model:value="formData.preferences" class="custom-checkbox-group">
                    <a-checkbox value="历史文化" class="preference-tag">🏛️ 历史文化</a-checkbox>
                    <a-checkbox value="自然风光" class="preference-tag">🏞️ 自然风光</a-checkbox>
                    <a-checkbox value="美食" class="preference-tag">🍜 美食</a-checkbox>
                    <a-checkbox value="购物" class="preference-tag">🛍️ 购物</a-checkbox>
                    <a-checkbox value="艺术" class="preference-tag">🎨 艺术</a-checkbox>
                    <a-checkbox value="休闲" class="preference-tag">☕ 休闲</a-checkbox>
                  </a-checkbox-group>
                </div>
              </a-form-item>
            </a-col>
          </a-row>
        </div>

        <!-- 第三步:真实约束 -->
        <div class="form-section">
          <div class="section-header">
            <span class="section-icon">🎯</span>
            <span class="section-title">预算与行程约束</span>
          </div>

          <a-row :gutter="24">
            <a-col :span="4">
              <a-form-item label="出行人数" name="travelers">
                <a-input-number v-model:value="formData.travelers" :min="1" :max="20" size="large" style="width: 100%" />
              </a-form-item>
            </a-col>
            <a-col :span="4">
              <a-form-item label="房间数" name="rooms">
                <a-input-number v-model:value="formData.rooms" :min="1" :max="formData.travelers" size="large" style="width: 100%" />
              </a-form-item>
            </a-col>
            <a-col :span="6">
              <a-form-item label="总预算（元）" name="budget_limit">
                <a-input-number v-model:value="formData.budget_limit" :min="0" :max="1000000" :step="500" placeholder="可选" size="large" style="width: 100%" />
              </a-form-item>
            </a-col>
            <a-col :span="5">
              <a-form-item label="优化目标" name="optimization_goal">
                <a-select v-model:value="formData.optimization_goal" size="large">
                  <a-select-option value="balanced">综合平衡</a-select-option>
                  <a-select-option value="economy">经济优先</a-select-option>
                  <a-select-option value="fastest">时间优先</a-select-option>
                </a-select>
              </a-form-item>
            </a-col>
            <a-col :span="5">
              <a-form-item label="行程节奏" name="pace">
                <a-select v-model:value="formData.pace" size="large">
                  <a-select-option value="relaxed">轻松</a-select-option>
                  <a-select-option value="balanced">适中</a-select-option>
                  <a-select-option value="intensive">紧凑</a-select-option>
                </a-select>
              </a-form-item>
            </a-col>
            <a-col :span="4">
              <a-form-item name="ask_route_options">
                <template #label>
                  <a-tooltip title="生成前先在 2-3 个行程方向（按当前偏好 / 舒适升级 / 经济轻松）中做选择，选中的方向才会完整生成。需要 Redis 模式支持。">
                    <span>比较多个方案 (？)</span>
                  </a-tooltip>
                </template>
                <a-switch v-model:checked="formData.ask_route_options" />
              </a-form-item>
            </a-col>
          </a-row>
        </div>

        <!-- 第四步:额外要求 -->
        <div class="form-section">
          <div class="section-header">
            <span class="section-icon">💬</span>
            <span class="section-title">额外要求</span>
          </div>

          <a-form-item name="free_text_input">
            <a-textarea
              v-model:value="formData.free_text_input"
              placeholder="请输入您的额外要求,例如:想去看升旗、需要无障碍设施、对海鲜过敏等..."
              :rows="3"
              size="large"
              class="custom-textarea"
            />
          </a-form-item>
        </div>
        </div>

        <!-- 提交按钮 -->
        <a-form-item>
          <a-button
            type="primary"
            html-type="submit"
            :loading="loading"
            size="large"
            block
            class="submit-button"
          >
            <template v-if="!loading">
              <span class="button-icon">🚀</span>
              <span>开始规划我的旅行</span>
            </template>
            <template v-else>
              <span>正在生成中...</span>
            </template>
          </a-button>
        </a-form-item>

        <!-- 加载进度条 -->
        <a-form-item v-if="loading">
          <div class="loading-container">
            <div class="agent-working-head">
              <div class="working-orb"><span></span></div>
              <div>
                <strong>正在为你规划行程</strong>
                <p>{{ loadingStatus }}</p>
              </div>
              <b>{{ loadingProgress }}%</b>
            </div>
            <a-progress
              :percent="loadingProgress"
              :status="loadingProgress === 100 ? 'success' : 'active'"
              :stroke-color="{
                  '0%': '#14213d',
                  '100%': '#ff6b4a',
              }"
              :stroke-width="10"
            />
            <!-- interrupt 决策卡片：等待用户确认（如预算超限是否降级住宿） -->
            <div v-if="waitingInput" class="waiting-card">
              <div class="waiting-head">
                <span class="waiting-icon">🤔</span>
                <strong>需要你的确认</strong>
              </div>
              <p class="waiting-question">{{ waitingInput.question }}</p>
              <div class="waiting-actions">
                <a-button
                  v-for="option in waitingInput.options"
                  :key="option.value"
                  :type="option.value === 'keep' ? 'default' : 'primary'"
                  size="small"
                  :loading="resumingJob"
                  @click="handleResume(option.value)"
                >{{ option.label }}</a-button>
              </div>
            </div>
            <div v-if="parallelAgents.some(agent => agent.status !== 'waiting')" class="parallel-agents">
              <div
                v-for="agent in parallelAgents"
                :key="agent.key"
                class="parallel-agent"
                :class="`is-${agent.status}`"
              >
                <span class="parallel-agent-icon">{{ agent.icon }}</span>
                <div>
                  <strong>{{ agent.label }}</strong>
                  <small>{{ getAgentStatusText(agent.status, agent.durationMs) }}</small>
                </div>
                <i></i>
              </div>
            </div>
            <div class="agent-timeline">
              <div
                v-for="event in orchestrationEvents"
                :key="event.sequence"
                class="timeline-event"
                :class="{ current: event.sequence === orchestrationEvents[orchestrationEvents.length - 1]?.sequence }"
              >
                <span class="event-icon">{{ getStageIcon(event.stage) }}</span>
                <span class="event-message">{{ event.message }}</span>
                <span class="event-progress">
                  {{ event.duration_ms !== undefined ? `${(event.duration_ms / 1000).toFixed(1)}s` : `${event.progress}%` }}
                </span>
              </div>
            </div>
          </div>
        </a-form-item>
      </a-form>
      </a-card>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, reactive, watch, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import {
  Button as AButton,
  Card as ACard,
  Checkbox as ACheckbox,
  CheckboxGroup as ACheckboxGroup,
  Col as ACol,
  DatePicker as ADatePicker,
  TimePicker as ATimePicker,
  Form as AForm,
  FormItem as AFormItem,
  Input as AInput,
  InputNumber as AInputNumber,
  Progress as AProgress,
  Row as ARow,
  Select as ASelect,
  SelectOption as ASelectOption,
  Switch as ASwitch,
  Textarea as ATextarea,
  Tooltip as ATooltip,
  message
} from 'ant-design-vue'
import { createSavedTrip, fetchGuideCandidates, generateTripPlanWithProgress, getPlanningJob, getUserPreferences, resumePlanningJob, saveUserPreferences, waitForExistingJob } from '@/services/api'
import type { CandidatePlace, PlanningJobResponse, PlanningProgressEvent, TripFormData, TripPlan, WaitingInputPayload } from '@/types'
import type { Dayjs } from 'dayjs'
import dayjs from 'dayjs'
import AgentIntro from '@/components/AgentIntro.vue'

const router = useRouter()
const loading = ref(false)
// 高级偏好默认收起：首屏只保留行程信息，降低首次使用门槛
const showAdvanced = ref(false)
const loadingProgress = ref(0)
const loadingStatus = ref('')
const progressEvents = ref<PlanningProgressEvent[]>([])
// interrupt 等待用户确认：图触发 budget_confirm 时任务暂停，展示决策卡片
const waitingInput = ref<WaitingInputPayload | null>(null)
const waitingJobId = ref('')
const resumingJob = ref(false)
// 进行中的规划任务句柄：组件卸载时取消 SSE 监听，防泄漏与幽灵跳转
const activeTask = ref<{ cancel: () => void } | null>(null)
const agentDefinitions = [
  { key: 'attractions', label: '景点研究', icon: '⌖' },
  { key: 'weather', label: '天气分析', icon: '☀' },
  { key: 'hotels', label: '住宿筛选', icon: '▣' },
  { key: 'transportation', label: '交通比较', icon: '↝' }
] as const

const parallelAgents = computed(() => agentDefinitions.map(definition => {
  const events = progressEvents.value.filter(event => event.agent === definition.key)
  return {
    ...definition,
    status: events[events.length - 1]?.agent_status || 'waiting',
    durationMs: events[events.length - 1]?.duration_ms
  }
}))

const orchestrationEvents = computed(() => progressEvents.value.filter(event => !event.agent))

const getAgentStatusText = (status: string, durationMs?: number) => {
  const duration = durationMs === undefined ? '' : ` · ${(durationMs / 1000).toFixed(1)}s`
  return ({
    waiting: '排队中',
    running: '查询中',
    completed: `资料已就绪${duration}`,
    degraded: `暂用参考数据${duration}`
  }[status] || status)
}

interface FormData extends Omit<TripFormData, 'start_date' | 'end_date' | 'departure_time' | 'arrival_time' | 'return_departure_time' | 'return_arrival_time'> {
  start_date?: Dayjs
  end_date?: Dayjs
  departure_time?: Dayjs
  arrival_time?: Dayjs
  return_departure_time?: Dayjs
  return_arrival_time?: Dayjs
}

const formData = reactive<FormData>({
  origin_city: '',
  city: '',
  start_date: undefined,
  end_date: undefined,
  travel_days: 1,
  transportation: 'high_speed_train',
  trip_type: 'round_trip',
  departure_time: undefined,
  arrival_time: undefined,
  return_departure_time: undefined,
  return_arrival_time: undefined,
  accommodation: '经济型酒店',
  preferences: [],
  free_text_input: '',
  travelers: 1,
  rooms: 1,
  budget_limit: undefined,
  optimization_goal: 'balanced',
  pace: 'balanced',
  ask_route_options: false
})

const disabledStartDate = (current: Dayjs) => current.isBefore(dayjs().startOf('day'))
const disabledEndDate = (current: Dayjs) => {
  const earliest = formData.start_date?.startOf('day') || dayjs().startOf('day')
  const latest = formData.start_date?.startOf('day').add(29, 'day')
  return current.isBefore(earliest) || Boolean(latest && current.isAfter(latest))
}

// ---------------------------------------------------------------------------
// 攻略候选接入：提交规划时自动携带"攻略导入"页确认过的候选地点
// 暂存键与 GuideImport 页面共享；excluded 不携带，跨城市候选按目的地过滤
// ---------------------------------------------------------------------------
const CANDIDATE_STORAGE_KEY = 'tripmind.candidate_pool.v1'

const matchingCandidateCount = ref(0)

// 本地快照：保存用户在确认页做过的 user_choice（candidate_id 跨设备稳定）
function readLocalCandidates(): CandidatePlace[] {
  try {
    const raw = localStorage.getItem(CANDIDATE_STORAGE_KEY)
    return raw ? (JSON.parse(raw) as CandidatePlace[]) : []
  } catch {
    return []
  }
}

// 候选云端化：服务端聚合候选为准，本地 user_choice 覆盖；网络失败回退本地快照
async function loadConfirmedCandidates(destinationCity: string): Promise<CandidatePlace[]> {
  const localChoices = readLocalCandidates()
  const choiceById = new Map(localChoices.map((item) => [item.candidate_id, item.user_choice]))
  const filterUsable = (list: CandidatePlace[]) =>
    list.filter(
      (item) => item.user_choice !== 'excluded' && (!item.city || item.city === destinationCity)
    )

  try {
    const serverCandidates = await fetchGuideCandidates()
    if (serverCandidates.length) {
      return filterUsable(
        serverCandidates.map((item) => ({
          ...item,
          user_choice: choiceById.get(item.candidate_id) ?? item.user_choice
        }))
      )
    }
  } catch {
    // 后端不可达时回退本地快照，不阻塞规划主流程
  }
  return filterUsable(localChoices)
}

// 刷新提示条数量（watch 触发 + onMounted 兜底）
async function refreshCandidateCount() {
  if (!formData.city) {
    matchingCandidateCount.value = 0
    return
  }
  matchingCandidateCount.value = (await loadConfirmedCandidates(formData.city)).length
}

watch(() => formData.city, () => {
  void refreshCandidateCount()
})

const preferenceRevision = ref(0)
onMounted(async () => {
  try {
    const preferences = await getUserPreferences()
    preferenceRevision.value = preferences.revision || 0
    Object.assign(formData, preferences)
  } catch {
    // 首次使用或后端尚未启动时继续使用安全默认值。
  }
  // 偏好回填可能带入目的地城市，手动算一次候选数量
  void refreshCandidateCount()
  applyGuidePrefill()
  // 页面刷新后若有进行中的规划任务，恢复进度监听（放在最前，恢复态下用户先看进度）
  void resumeActiveJob()
})

// 从攻略导入页"去规划"跳转过来：预填目的城市并滚动到表单
function applyGuidePrefill() {
  const city = sessionStorage.getItem('tripmind.guide_prefill_city')
  if (!city) return
  sessionStorage.removeItem('tripmind.guide_prefill_city')
  formData.city = city
  message.success(`已带入攻略候选的目的地：${city}，候选将优先安排`)
  void refreshCandidateCount()
  document.getElementById('planner')?.scrollIntoView({ behavior: 'smooth' })
}

// 监听日期变化,自动计算旅行天数
watch([() => formData.start_date, () => formData.end_date], ([start, end]) => {
  if (start?.isBefore(dayjs().startOf('day'))) {
    message.warning('出发日期不能早于今天')
    formData.start_date = undefined
    formData.end_date = undefined
    return
  }
  if (start && end) {
    const days = end.diff(start, 'day') + 1
    if (days > 0 && days <= 30) {
      formData.travel_days = days
    } else if (days > 30) {
      message.warning('旅行天数不能超过30天')
      formData.end_date = undefined
    } else {
      message.warning('结束日期不能早于开始日期')
      formData.end_date = undefined
    }
  }
})

watch(() => formData.travelers, (travelers) => {
  if (formData.rooms > travelers) formData.rooms = travelers
})

// 进行中规划任务的本机记录键：页面刷新后据此恢复监听
const ACTIVE_JOB_KEY = 'tripmind.active_job'

// 生成完成后的统一收尾：存行程库、记偏好、写入会话并跳转结果页
// （正常流程与"刷新后恢复监听"两条路径共用，避免行为不一致）
const finalizePlan = async (plan: TripPlan, requestData: TripFormData) => {
  loadingProgress.value = 100
  loadingStatus.value = '旅行计划已经准备完成'
  const savedTrip = await createSavedTrip(requestData, plan)
  try {
    const preferences = await saveUserPreferences({
      revision: preferenceRevision.value,
      origin_city: requestData.origin_city,
      transportation: requestData.transportation,
      accommodation: requestData.accommodation,
      preferences: requestData.preferences,
      travelers: requestData.travelers,
      rooms: requestData.rooms,
      budget_limit: requestData.budget_limit,
      optimization_goal: requestData.optimization_goal,
      pace: requestData.pace
    })
    preferenceRevision.value = preferences.revision || 0
  } catch {
    // 偏好保存是辅助能力，不应阻止用户查看已经生成并保存的行程。
  }
  sessionStorage.setItem('tripPlan', JSON.stringify(plan))
  message.success(plan.is_fallback ? '已生成参考计划，请留意核实提醒' : '旅行计划生成成功!')
  setTimeout(() => router.push(`/trips/${savedTrip.id}`), 450)
}

// 页面刷新后恢复进行中的规划任务：
// queued/running 直接续听进度，waiting_input 恢复决策卡片，completed/failed/过期则清记录
const resumeActiveJob = async () => {
  const jobId = sessionStorage.getItem(ACTIVE_JOB_KEY)
  if (!jobId) return
  let job: PlanningJobResponse
  try {
    job = await getPlanningJob(jobId)
  } catch {
    sessionStorage.removeItem(ACTIVE_JOB_KEY)  // 任务已过期或不存在
    return
  }
  if (job.status === 'completed' || job.status === 'failed') {
    sessionStorage.removeItem(ACTIVE_JOB_KEY)
    message.info(job.status === 'completed' ? '上次的规划已完成，请在"我的行程"中查看' : '上次的规划未成功，可重新提交')
    return
  }

  // 恢复监听：请求快照在提交时已存入 sessionStorage
  const rawRequest = sessionStorage.getItem('tripRequest')
  if (!rawRequest) {
    sessionStorage.removeItem(ACTIVE_JOB_KEY)
    return
  }
  let requestData: TripFormData
  try {
    requestData = JSON.parse(rawRequest) as TripFormData
  } catch {
    sessionStorage.removeItem(ACTIVE_JOB_KEY)
    return
  }

  loading.value = true
  loadingProgress.value = job.progress
  loadingStatus.value = `已恢复上次进行中的规划任务：${job.message || '正在生成行程'}`
  if (job.waiting_input) {
    waitingJobId.value = jobId
    waitingInput.value = job.waiting_input
  }

  const task = waitForExistingJob(jobId, (event) => {
    loadingProgress.value = event.progress
    loadingStatus.value = event.message
    const previous = progressEvents.value[progressEvents.value.length - 1]
    if (!previous || previous.stage !== event.stage || previous.message !== event.message) {
      progressEvents.value.push(event)
    }
  }, (_jobId, payload) => {
    waitingJobId.value = jobId
    waitingInput.value = payload
  })
  activeTask.value = task
  try {
    const plan = await task
    waitingInput.value = null
    sessionStorage.removeItem(ACTIVE_JOB_KEY)
    await finalizePlan(plan, requestData)
  } catch (error: any) {
    if (error?.name !== 'AbortError') {
      message.error(error.message || '恢复的任务执行失败，可重新提交')
      sessionStorage.removeItem(ACTIVE_JOB_KEY)
    }
  } finally {
    waitingInput.value = null
    activeTask.value = null
    setTimeout(() => {
      loading.value = false
      loadingProgress.value = 0
      loadingStatus.value = ''
    }, 1000)
  }
}

const handleSubmit = async () => {
  if (!formData.origin_city) {
    message.error('请输入出发城市')
    return
  }
  if (!formData.start_date || !formData.end_date) {
    message.error('请选择日期')
    return
  }
  if (formData.start_date.isBefore(dayjs().startOf('day'))) {
    message.error('出发日期不能早于今天')
    return
  }
  if (Boolean(formData.departure_time) !== Boolean(formData.arrival_time)) {
    message.error('跨城出发时间和到达时间需要同时填写')
    return
  }
  if (formData.trip_type === 'round_trip' && Boolean(formData.return_departure_time) !== Boolean(formData.return_arrival_time)) {
    message.error('返程出发时间和到达时间需要同时填写')
    return
  }

  loading.value = true
  loadingProgress.value = 0
  loadingStatus.value = '正在创建规划任务...'
  progressEvents.value = []

  try {
    const requestData: TripFormData = {
      origin_city: formData.origin_city,
      city: formData.city,
      start_date: formData.start_date.format('YYYY-MM-DD'),
      end_date: formData.end_date.format('YYYY-MM-DD'),
      travel_days: formData.travel_days,
      transportation: formData.transportation,
      trip_type: formData.trip_type,
      departure_time: formData.departure_time?.format('HH:mm'),
      arrival_time: formData.arrival_time?.format('HH:mm'),
      return_departure_time: formData.trip_type === 'round_trip' ? formData.return_departure_time?.format('HH:mm') : undefined,
      return_arrival_time: formData.trip_type === 'round_trip' ? formData.return_arrival_time?.format('HH:mm') : undefined,
      accommodation: formData.accommodation,
      preferences: formData.preferences,
      free_text_input: formData.free_text_input,
      travelers: formData.travelers,
      rooms: formData.rooms,
      budget_limit: formData.budget_limit,
      optimization_goal: formData.optimization_goal,
      pace: formData.pace,
      // 开启后生成前先在 2-3 个行程方向中选一个（interrupt，需 Redis 模式）
      ask_route_options: formData.ask_route_options,
      // 攻略候选：必去/想去的地点会优先安排进行程（后端渲染进规划约束并回填核对坐标）
      candidate_places: await loadConfirmedCandidates(formData.city)
    }

    // 提交即存请求快照与任务 ID：页面刷新后可恢复监听进行中的任务
    sessionStorage.setItem('tripRequest', JSON.stringify(requestData))

    const task = generateTripPlanWithProgress(requestData, (event) => {
      loadingProgress.value = event.progress
      loadingStatus.value = event.message
      const previous = progressEvents.value[progressEvents.value.length - 1]
      if (!previous || previous.stage !== event.stage || previous.message !== event.message) {
        progressEvents.value.push(event)
      }
    }, (jobId, payload) => {
      // 图 interrupt：展示决策卡片等用户选择，选择后 Worker 从断点继续
      waitingJobId.value = jobId
      waitingInput.value = payload
    }, (jobId) => {
      sessionStorage.setItem(ACTIVE_JOB_KEY, jobId)
    })
    activeTask.value = task
    const plan = await task
    waitingInput.value = null
    sessionStorage.removeItem(ACTIVE_JOB_KEY)
    await finalizePlan(plan, requestData)
  } catch (error: any) {
    // 页面离开触发的取消是预期行为，不打扰用户
    if (error?.name !== 'AbortError') {
      message.error(error.message || '生成旅行计划失败,请稍后重试')
      // 失败的任务不再可恢复（后端重试机制会自行处理），清掉恢复入口
      sessionStorage.removeItem(ACTIVE_JOB_KEY)
    }
  } finally {
    waitingInput.value = null
    activeTask.value = null
    setTimeout(() => {
      loading.value = false
      loadingProgress.value = 0
      loadingStatus.value = ''
    }, 1000)
  }
}

// 组件卸载（用户导航离开）时取消任务监听：关闭 SSE 连接，
// 防止连接泄漏与完成后的"幽灵跳转"把用户从其他页面强行拉走
onUnmounted(() => {
  activeTask.value?.cancel()
})

// 提交用户对 interrupt（预算确认等）的选择：任务重新入队，Worker 从断点继续，
// EventSource 连接保持，后续 completed 事件照常收
const handleResume = async (value: string) => {
  if (!waitingJobId.value || resumingJob.value) return
  resumingJob.value = true
  try {
    await resumePlanningJob(waitingJobId.value, value)
    waitingInput.value = null
    loadingStatus.value = '已收到你的选择，正在继续生成行程…'
    progressEvents.value.push({
      sequence: progressEvents.value.length + 1,
      progress: loadingProgress.value,
      stage: 'resumed',
      message: loadingStatus.value,
      status: 'running',
      timestamp: new Date().toISOString()
    })
  } catch (error: any) {
    message.error(error.response?.data?.detail || error.message || '提交选择失败，请重试')
  } finally {
    resumingJob.value = false
  }
}

const getStageIcon = (stage: string): string => ({
  queued: '◷',
  starting: '✦',
  initializing: '◎',
  attractions: '⌖',
  weather: '☀',
  hotels: '▣',
  transportation: '↝',
  parallel_research: '⋯',
  research_joined: '⇥',
  planning: '✎',
  validating: '✓',
  replanning: '↻',
  optimizing: '◇',
  degraded: '!',
  completed: '●'
}[stage] || '·')
</script>

<style scoped>
.home-container {
  min-height: 100vh;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  padding: 60px 20px;
  position: relative;
  overflow: hidden;
}

/* 背景装饰 */
.bg-decoration {
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
  overflow: hidden;
}

.circle {
  position: absolute;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.1);
  animation: float 20s infinite ease-in-out;
}

.circle-1 {
  width: 300px;
  height: 300px;
  top: -100px;
  left: -100px;
  animation-delay: 0s;
}

.circle-2 {
  width: 200px;
  height: 200px;
  top: 50%;
  right: -50px;
  animation-delay: 5s;
}

.circle-3 {
  width: 150px;
  height: 150px;
  bottom: -50px;
  left: 30%;
  animation-delay: 10s;
}

@keyframes float {
  0%, 100% {
    transform: translateY(0) rotate(0deg);
  }
  50% {
    transform: translateY(-30px) rotate(180deg);
  }
}

/* 页面标题 */
.page-header {
  text-align: center;
  margin-bottom: 50px;
  animation: fadeInDown 0.8s ease-out;
  position: relative;
  z-index: 1;
}

.icon-wrapper {
  margin-bottom: 20px;
}

.icon {
  font-size: 80px;
  display: inline-block;
  animation: bounce 2s infinite;
}

@keyframes bounce {
  0%, 100% {
    transform: translateY(0);
  }
  50% {
    transform: translateY(-20px);
  }
}

.page-title {
  font-size: 56px;
  font-weight: 800;
  color: #ffffff;
  margin-bottom: 16px;
  text-shadow: 3px 3px 6px rgba(0, 0, 0, 0.3);
  letter-spacing: 2px;
}

.page-subtitle {
  font-size: 20px;
  color: rgba(255, 255, 255, 0.95);
  margin: 0;
  font-weight: 300;
}

/* 攻略导入入口提示条 */
.guide-entry {
  max-width: 1400px;
  margin: 0 auto 16px;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 18px;
  border-radius: 14px;
  border: 1px solid rgba(255, 139, 115, 0.35);
  background: rgba(255, 246, 242, 0.92);
  text-decoration: none;
  animation: fadeInUp 0.8s ease-out;
  position: relative;
  z-index: 1;
  transition: border-color 0.2s ease, transform 0.2s ease;
}
.guide-entry:hover { border-color: #ff6b4a; transform: translateY(-1px); }
.guide-entry-icon { font-size: 20px; }
.guide-entry-text { flex: 1; font-size: 13px; color: #7a5c50; line-height: 1.5; }
.guide-entry-text strong { color: #d4502e; margin-right: 6px; }
.guide-entry-arrow { font-size: 12px; font-weight: 700; color: #ff6b4a; white-space: nowrap; }

/* 已确认候选提示条 */
.candidate-hint {
  max-width: 1400px;
  margin: 0 auto 16px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 18px;
  border-radius: 14px;
  border: 1px solid rgba(33, 161, 121, 0.3);
  background: rgba(244, 251, 248, 0.95);
  font-size: 13px;
  color: #39755e;
  animation: fadeInUp 0.8s ease-out;
  position: relative;
  z-index: 1;
}
.candidate-hint-link { font-size: 12px; font-weight: 700; color: #21a179; text-decoration: none; }
.candidate-hint-link:hover { text-decoration: underline; }

/* interrupt 决策卡片 */
.waiting-card {
  margin-top: 12px;
  padding: 14px 16px;
  border-radius: 12px;
  border: 1px solid rgba(255, 139, 115, 0.4);
  background: #fff8f5;
}
.waiting-head { display: flex; align-items: center; gap: 8px; color: #d4502e; font-size: 13px; }
.waiting-question { margin: 8px 0 12px; font-size: 13px; color: #4a3f3a; line-height: 1.6; }
.waiting-actions { display: flex; gap: 10px; }

/* 表单卡片 */
.form-card {
  max-width: 1400px;
  margin: 0 auto;
  border-radius: 24px;
  box-shadow: 0 30px 80px rgba(0, 0, 0, 0.4);
  animation: fadeInUp 0.8s ease-out;
  position: relative;
  z-index: 1;
  backdrop-filter: blur(10px);
  background: rgba(255, 255, 255, 0.98) !important;
}

/* 表单分区 */
.form-section {
  margin-bottom: 32px;
  padding: 24px;
  background: linear-gradient(135deg, #f5f7fa 0%, #ffffff 100%);
  border-radius: 16px;
  border: 1px solid #e8e8e8;
  transition: all 0.3s ease;
}

.form-section:hover {
  box-shadow: 0 8px 24px rgba(102, 126, 234, 0.15);
  transform: translateY(-2px);
}

.section-header {
  display: flex;
  align-items: center;
  margin-bottom: 20px;
  padding-bottom: 12px;
  border-bottom: 2px solid #667eea;
}

.section-icon {
  font-size: 24px;
  margin-right: 12px;
}

.section-title {
  font-size: 18px;
  font-weight: 600;
  color: #333;
}

/* 表单标签 */
.form-label {
  font-size: 15px;
  font-weight: 500;
  color: #555;
}

/* 自定义输入框 */
.custom-input :deep(.ant-input),
.custom-input :deep(.ant-picker) {
  border-radius: 12px;
  border: 2px solid #e8e8e8;
  transition: all 0.3s ease;
}

.custom-input :deep(.ant-input:hover),
.custom-input :deep(.ant-picker:hover) {
  border-color: #667eea;
}

.custom-input :deep(.ant-input:focus),
.custom-input :deep(.ant-picker-focused) {
  border-color: #667eea;
  box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
}

/* 自定义选择框 */
.custom-select :deep(.ant-select-selector) {
  border-radius: 12px !important;
  border: 2px solid #e8e8e8 !important;
  transition: all 0.3s ease;
}

.custom-select:hover :deep(.ant-select-selector) {
  border-color: #667eea !important;
}

.custom-select :deep(.ant-select-focused .ant-select-selector) {
  border-color: #667eea !important;
  box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1) !important;
}

/* 天数显示 - 紧凑版 */
.days-display-compact {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 40px;
  padding: 8px 16px;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  border-radius: 12px;
  color: white;
}

.days-display-compact .days-value {
  font-size: 24px;
  font-weight: 700;
  margin-right: 4px;
}

.days-display-compact .days-unit {
  font-size: 14px;
}

/* 偏好标签 */
.preference-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.custom-checkbox-group {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  width: 100%;
}

.preference-tag :deep(.ant-checkbox-wrapper) {
  margin: 0 !important;
  padding: 8px 16px;
  border: 2px solid #e8e8e8;
  border-radius: 20px;
  transition: all 0.3s ease;
  background: white;
  font-size: 14px;
}

.preference-tag :deep(.ant-checkbox-wrapper:hover) {
  border-color: #667eea;
  background: #f5f7ff;
}

.preference-tag :deep(.ant-checkbox-wrapper-checked) {
  border-color: #667eea;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
}

/* 自定义文本域 */
.custom-textarea :deep(.ant-input) {
  border-radius: 12px;
  border: 2px solid #e8e8e8;
  transition: all 0.3s ease;
}

.custom-textarea :deep(.ant-input:hover) {
  border-color: #667eea;
}

.custom-textarea :deep(.ant-input:focus) {
  border-color: #667eea;
  box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
}

/* 提交按钮 */
.submit-button {
  height: 56px;
  border-radius: 28px;
  font-size: 18px;
  font-weight: 600;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  border: none;
  box-shadow: 0 8px 24px rgba(102, 126, 234, 0.4);
  transition: all 0.3s ease;
}

.submit-button:hover {
  transform: translateY(-2px);
  box-shadow: 0 12px 32px rgba(102, 126, 234, 0.5);
}

.submit-button:active {
  transform: translateY(0);
}

.button-icon {
  margin-right: 8px;
  font-size: 20px;
}

/* 加载容器 */
.loading-container {
  text-align: center;
  padding: 24px;
  background: linear-gradient(135deg, #f5f7fa 0%, #ffffff 100%);
  border-radius: 16px;
  border: 2px dashed #667eea;
}

.loading-status {
  margin-top: 16px;
  color: #667eea;
  font-size: 18px;
  font-weight: 500;
}

/* 动画 */
@keyframes fadeInDown {
  from {
    opacity: 0;
    transform: translateY(-30px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@keyframes fadeInUp {
  from {
    opacity: 0;
    transform: translateY(30px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

/* 箭头列 */
.arrow-col {
  display: flex;
  align-items: center;
  justify-content: center;
  padding-top: 28px;
}

.arrow-icon {
  color: #ff6b4a;
  font-size: 30px;
  font-weight: 800;
}

/* 新版产品化首页 */
.home-container {
  min-height: 100vh;
  padding: 0 0 90px;
  overflow: visible;
  background:
    radial-gradient(circle at 84% 9%, rgba(255, 116, 87, .09), transparent 24%),
    linear-gradient(180deg, #fbfcfe 0%, #f5f7fa 100%);
}

.planner-section {
  max-width: 1240px;
  margin: 0 auto;
  padding: 70px 24px 0;
  scroll-margin-top: 82px;
}

.planner-heading {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: 40px;
  margin-bottom: 30px;
}

.planner-kicker {
  color: #e65334;
  font-size: 12px;
  font-weight: 800;
  letter-spacing: 1.8px;
}

.planner-heading h2 {
  margin: 8px 0 0;
  color: #14213d;
  font-size: 38px;
  line-height: 1.2;
  letter-spacing: -1px;
}

.planner-heading p {
  max-width: 470px;
  margin: 0;
  color: #6b778c;
  line-height: 1.7;
}

.planner-heading b { color: #ff6b4a; }

.form-card {
  max-width: none;
  margin: 0;
  border: 1px solid #e4eaf0;
  border-radius: 22px;
  background: #fff !important;
  box-shadow: 0 26px 70px rgba(30, 48, 76, .09);
  backdrop-filter: none;
}

.form-card :deep(.ant-card-body) { padding: 34px; }

.form-section {
  margin-bottom: 22px;
  padding: 26px;
  border: 1px solid #e9edf2;
  border-radius: 16px;
  background: #fbfcfd;
}

.form-section:hover {
  transform: none;
  border-color: #d8e0e9;
  box-shadow: 0 12px 30px rgba(35, 53, 79, .06);
}

.section-header {
  margin-bottom: 23px;
  padding-bottom: 14px;
  border-bottom: 1px solid #e5eaf0;
}

.section-icon {
  width: 36px;
  height: 36px;
  margin-right: 11px;
  display: grid;
  place-items: center;
  border-radius: 10px;
  background: #fff0ec;
  font-size: 18px;
}

.section-title { color: #192640; font-size: 17px; font-weight: 800; }
.form-label { color: #3f4b5f; font-size: 14px; font-weight: 700; }

.custom-select :deep(.ant-select-selector),
.custom-textarea :deep(.ant-input),
.form-section :deep(.ant-input-affix-wrapper),
.form-section :deep(.ant-picker),
.form-section :deep(.ant-input-number) {
  border-color: #dfe5ec !important;
  border-radius: 10px !important;
  box-shadow: none !important;
}

.form-section :deep(.ant-input-affix-wrapper:hover),
.form-section :deep(.ant-picker:hover),
.form-section :deep(.ant-input-number:hover),
.custom-select:hover :deep(.ant-select-selector) { border-color: #ff8b73 !important; }

.preference-tag {
  margin: 0 !important;
  padding: 8px 13px;
  border: 1px solid #dfe5ec;
  border-radius: 10px;
  background: white;
  transition: .2s ease;
}

.preference-tag:hover { border-color: #ff8b73; background: #fff8f6; }
.preference-tag.ant-checkbox-wrapper-checked { border-color: #ff8b73; color: #d94e31; background: #fff0ec; }

.submit-button {
  height: 58px;
  border-radius: 12px;
  background: #14213d;
  box-shadow: 0 12px 28px rgba(20, 33, 61, .2);
}

.submit-button:hover { background: #ff6b4a !important; box-shadow: 0 14px 30px rgba(255, 107, 74, .24); }

.transport-time-row { margin-top: 8px; }
.transport-rule-note {
  align-self: center;
  display: flex;
  flex-direction: column;
  gap: 5px;
  margin-top: 2px;
  padding: 14px 16px;
  color: #536071;
  background: #f7f9fc;
  border: 1px solid #e6ebf2;
  border-radius: 10px;
}
.transport-rule-note b { color: #182234; }

.loading-container {
  border: 1px solid #ffd2c8;
  background: #fff8f6;
}

.loading-status { color: #d95437; }

.agent-working-head {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 14px;
  align-items: center;
  margin-bottom: 18px;
  text-align: left;
}

.agent-working-head strong { color: #14213d; font-size: 16px; }
.agent-working-head p { margin: 3px 0 0; color: #7a879a; font-size: 13px; }
.agent-working-head b { color: #e65334; font-size: 22px; }
.working-orb { width: 42px; height: 42px; display: grid; place-items: center; border-radius: 13px; background: #14213d; }
.working-orb span { width: 10px; height: 10px; border-radius: 50%; background: #ff7e63; box-shadow: 0 0 0 7px rgba(255,126,99,.18); animation: workingPulse 1.5s ease-in-out infinite; }

.parallel-agents {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 9px;
  margin-top: 18px;
}

.parallel-agent {
  position: relative;
  display: grid;
  grid-template-columns: 34px 1fr;
  gap: 9px;
  align-items: center;
  min-height: 62px;
  padding: 10px;
  overflow: hidden;
  border: 1px solid #e7ebf1;
  border-radius: 12px;
  background: #fff;
}

.parallel-agent-icon {
  width: 34px;
  height: 34px;
  display: grid;
  place-items: center;
  border-radius: 10px;
  color: #6f7c90;
  background: #f0f3f7;
  font-size: 16px;
}

.parallel-agent strong,
.parallel-agent small { display: block; }
.parallel-agent strong { color: #25334a; font-size: 12px; }
.parallel-agent small { margin-top: 2px; color: #9aa4b2; font-size: 10px; }
.parallel-agent i { position: absolute; inset: auto 0 0; height: 3px; background: #e7ebf1; }
.parallel-agent.is-running { border-color: #ffd0c6; background: #fff9f7; }
.parallel-agent.is-running .parallel-agent-icon { color: #fff; background: #ff6b4a; animation: workingPulse 1.5s ease-in-out infinite; }
.parallel-agent.is-running i { width: 60%; background: #ff6b4a; animation: agentSweep 1.5s ease-in-out infinite alternate; }
.parallel-agent.is-completed { border-color: #ccebdd; background: #f8fffb; }
.parallel-agent.is-completed .parallel-agent-icon { color: #fff; background: #21a179; }
.parallel-agent.is-completed i { background: #21a179; }
.parallel-agent.is-degraded { border-color: #f2d89b; background: #fffbf2; }
.parallel-agent.is-degraded .parallel-agent-icon { color: #fff; background: #d99b20; }
.parallel-agent.is-degraded i { background: #d99b20; }

.agent-timeline {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin-top: 18px;
}

.timeline-event {
  display: grid;
  grid-template-columns: 26px 1fr auto;
  gap: 8px;
  align-items: center;
  min-height: 40px;
  padding: 7px 10px;
  border: 1px solid #edf0f4;
  border-radius: 9px;
  color: #7f8b9c;
  background: white;
  font-size: 12px;
}

.timeline-event.current { color: #14213d; border-color: #ffb8a8; background: #fff5f2; }
.event-icon { width: 25px; height: 25px; display: grid; place-items: center; border-radius: 7px; color: white; background: #98a4b6; }
.timeline-event.current .event-icon { background: #ff6b4a; }
.event-progress { color: #a1aab8; font-size: 10px; }

@keyframes workingPulse { 50% { transform: scale(.7); opacity: .7; } }
@keyframes agentSweep { to { transform: translateX(65%); } }

@media (max-width: 900px) {
  .planner-section { padding-top: 40px; }
  .planner-heading { display: block; }
  .planner-heading p { margin-top: 14px; }
  .planner-heading h2 { font-size: 32px; }
  .form-card :deep(.ant-card-body) { padding: 20px; }
  .form-section { padding: 20px; }
  .form-section :deep(.ant-row) { display: block; }
  .form-section :deep(.ant-col) { max-width: 100% !important; flex: 0 0 100% !important; }
  .arrow-col { display: none !important; }
  .agent-timeline { grid-template-columns: 1fr; }
  .parallel-agents { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}

@media (max-width: 520px) {
  .planner-section { padding-inline: 14px; }
  .planner-heading h2 { font-size: 28px; }
  .form-card :deep(.ant-card-body) { padding: 12px; }
  .form-section { padding: 17px; border-radius: 13px; }
  .parallel-agents { grid-template-columns: 1fr; }
}

.advanced-toggle {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 13px 16px;
  border: 1px dashed #cfd8e3;
  border-radius: 12px;
  cursor: pointer;
  color: #4a5a72;
  font-size: 13px;
  transition: all .15s;
}
.advanced-toggle:hover { border-color: #ff6b4a; color: #14213d; }
.advanced-toggle b { color: #ff6b4a; font-size: 12px; }
</style>
