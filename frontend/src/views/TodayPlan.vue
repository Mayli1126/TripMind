<template>
  <div class="today-page">
    <header class="today-header">
      <div class="header-bar">
        <router-link :to="`/trips/${tripId}`">← 查看完整行程</router-link>
        <span>TRIP EXECUTION MODE</span>
      </div>
      <div v-if="trip" class="hero-content">
        <div>
          <p>{{ trip.city }} · {{ trip.start_date }} — {{ trip.end_date }}</p>
          <h1>今天走到哪一步了？</h1>
          <span>按自己的节奏旅行。计划是助手，不是必须完成的任务。</span>
        </div>
        <div class="overall-progress">
          <strong>{{ overallPercent }}%</strong>
          <span>整趟旅程</span>
          <div><i :style="{ width: `${overallPercent}%` }"></i></div>
        </div>
      </div>
    </header>

    <main v-if="trip" class="today-main">
      <nav class="day-tabs" aria-label="选择行程日期">
        <button
          v-for="(day, index) in trip.plan.days"
          :key="day.date"
          :class="{ active: selectedDay === index }"
          @click="selectedDay = index"
        >
          <span>DAY {{ index + 1 }}</span>
          <b>{{ shortDate(day.date) }}</b>
          <small>{{ dayProgress(index) }}% 完成</small>
        </button>
      </nav>

      <section class="day-layout">
        <aside class="day-summary">
          <span class="summary-kicker">{{ dayRelation(currentDay.date) }}</span>
          <h2>第 {{ selectedDay + 1 }} 天</h2>
          <p>{{ currentDay.description }}</p>

          <div v-if="isTodaySelected && currentStepIndex >= 0" class="now-block">
            <span class="now-kicker">▶ 现在</span>
            <b>{{ currentSteps[currentStepIndex].title }}</b>
            <small>{{ currentStepTiming }}</small>
          </div>
          <div v-else-if="isTodaySelected" class="now-block done">
            <span class="now-kicker">▶ 现在</span>
            <b>今天的安排都完成了</b>
            <small>好好休息，或看看明天的计划</small>
          </div>

          <div v-if="todayWeather" class="weather-chip">
            <span>{{ weatherIcon(todayWeather.day_weather) }}</span>
            <div>
              <b>{{ todayWeather.day_temp }}°</b>
              <small>{{ todayWeather.day_weather }} · {{ todayWeather.wind_direction }} {{ todayWeather.wind_power }}</small>
              <div v-if="weatherTips.length" class="weather-tips">
                <span v-for="tip in weatherTips" :key="tip">{{ tip }}</span>
              </div>
            </div>
          </div>

          <div class="day-progress-ring" :style="{ '--progress': `${dayProgress(selectedDay) * 3.6}deg` }">
            <div><b>{{ completedCount }}</b><span>/ {{ currentSteps.length }}</span><small>今日步骤</small></div>
          </div>

          <div v-if="dayProgress(selectedDay) === 100" class="day-complete">🎉 今天的计划已全部完成</div>
        </aside>

        <div class="plan-board">
          <div class="board-heading">
            <div><span>DAILY CHECKLIST</span><h2>今日计划表</h2></div>
            <b>{{ completedCount }} / {{ currentSteps.length }}</b>
          </div>

          <div class="step-list">
            <article
              v-for="(step, index) in currentSteps"
              :key="step.key"
              class="plan-step"
              :class="{ completed: isCompleted(step.key), saving: savingKey === step.key, now: index === currentStepIndex }"
            >
              <button class="check-button" :aria-label="isCompleted(step.key) ? '标记为未完成' : '标记为完成'" @click="toggleStep(step)">
                {{ isCompleted(step.key) ? '✓' : index + 1 }}
              </button>
              <div class="step-icon">{{ step.icon }}</div>
              <div class="step-content">
                <div class="step-meta">
                  <span>{{ step.category }}</span>
                  <small>{{ step.hint }}</small>
                  <em v-if="index === currentStepIndex" class="now-chip">▶ 现在 · {{ currentStepTiming }}</em>
                </div>
                <h3>{{ step.title }}</h3>
                <p v-if="step.description">{{ step.description }}</p>
                <span v-if="step.address" class="address">⌖ {{ step.address }}</span>
              </div>
              <a v-if="step.address" class="nav-button" :href="navigationUrl(step)" target="_blank" rel="noreferrer">导航 ↗</a>
            </article>
          </div>

          <div class="board-note">
            <span>✦</span>
            <p><b>旅行中的变化很正常</b>如果天气、体力或营业情况变化，可以回到完整行程使用 AI 修改；未变化的步骤会继续保留完成状态。</p>
          </div>
        </div>
      </section>
    </main>

    <div v-else class="loading-state"><a-spin size="large" /><span>正在打开旅行计划表…</span></div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Spin as ASpin, message } from 'ant-design-vue'
import { getSavedTrip, getTripProgress, updateTripProgress } from '@/services/api'
import type { DayPlan, SavedTripDetail } from '@/types'

interface ExecutionStep {
  key: string
  category: string
  title: string
  description?: string
  address?: string
  hint: string
  icon: string
  location?: { longitude: number; latitude: number }
  startMinutes?: number   // 当日开始分钟数（"现在"定位用）
  endMinutes?: number
}

const route = useRoute()
const router = useRouter()
const tripId = String(route.params.id || '')
const trip = ref<SavedTripDetail | null>(null)
const selectedDay = ref(0)
const progress = ref<Record<string, boolean>>({})
const savingKey = ref('')

onMounted(async () => {
  try {
    const [saved, savedProgress] = await Promise.all([getSavedTrip(tripId), getTripProgress(tripId)])
    trip.value = saved
    progress.value = Object.fromEntries(savedProgress.items.map(item => [item.item_key, item.completed]))
    const today = localDate()
    const exactDay = saved.plan.days.findIndex(day => day.date === today)
    const nextDay = saved.plan.days.findIndex(day => day.date > today)
    selectedDay.value = exactDay >= 0 ? exactDay : nextDay >= 0 ? nextDay : Math.max(saved.plan.days.length - 1, 0)
  } catch (error: any) {
    message.error(error.response?.status === 404 ? '旅行计划不存在' : '暂时无法加载计划表')
    router.push('/trips')
  }
})

const localDate = () => {
  const date = new Date()
  const offset = date.getTimezoneOffset() * 60000
  return new Date(date.getTime() - offset).toISOString().slice(0, 10)
}

const buildSteps = (day: DayPlan): ExecutionStep[] => {
  const dayKey = `d${day.day_index}`
  if (day.schedule?.length) {
    const labels = { meal: '餐饮', transport: '交通', attraction: '景点', hotel: '住宿', break: '机动时间' }
    const icons = { meal: '🍜', transport: '↝', attraction: '⌖', hotel: '▣', break: '☕' }
    const toMinutes = (value: string) => {
      const [h, m] = value.split(':').map(Number)
      return (h || 0) * 60 + (m || 0)
    }
    const scheduled: ExecutionStep[] = day.schedule
      .filter(slot => !(slot.item_type === 'transport' && slot.title.startsWith('前往 ')))
      .map((slot, index) => {
      // 景点类步骤回查景点坐标，导航可以直接"到点"而不是按名字搜
      const attraction = slot.item_type === 'attraction' && slot.related_index !== undefined
        ? day.attractions[slot.related_index]
        : undefined
      const isAttraction = slot.item_type === 'attraction'
      return {
        key: slot.stable_id || `${dayKey}-s-${index}`,
        category: labels[slot.item_type],
        title: slot.title,
        description: slot.description,
        address: slot.address,
        hint: isAttraction && attraction
          ? `建议游玩 ${Math.round(attraction.visit_duration / 60 * 10) / 10} 小时`
          : `${slot.start_day_offset ? '次日 ' : ''}${slot.start_time}—${slot.end_day_offset ? '次日 ' : ''}${slot.end_time}`,
        icon: icons[slot.item_type],
        location: attraction?.location,
        startMinutes: isAttraction || slot.start_day_offset ? undefined : toMinutes(slot.start_time),
        endMinutes: isAttraction || slot.end_day_offset ? undefined : toMinutes(slot.end_time),
      }
    })
    const scheduledAttractions = new Set(
      scheduled.filter(step => step.category === '景点').map(step => step.title)
    )
    for (const attraction of day.attractions) {
      if (scheduledAttractions.has(attraction.name)) continue
      scheduled.push({
        key: `${day.date}-attraction-${attraction.poi_id || attraction.name}`,
        category: '景点',
        title: attraction.name,
        description: attraction.description,
        address: attraction.address,
        hint: `建议游玩 ${Math.round(attraction.visit_duration / 60 * 10) / 10} 小时`,
        icon: '⌖',
        location: attraction.location,
      })
    }
    return scheduled
  }
  const meals = Object.fromEntries(day.meals.map(meal => [meal.type, meal]))
  const steps: ExecutionStep[] = []
  const addMeal = (type: 'breakfast' | 'lunch' | 'dinner', label: string, icon: string, hint: string) => {
    const meal = meals[type]
    if (meal) steps.push({ key: `${dayKey}-m-${type}`, category: label, title: meal.name, description: meal.description, address: meal.address, hint, icon })
  }
  addMeal('breakfast', '早餐', '☕', '出发前')
  const lunchAfter = Math.max(1, Math.ceil(day.attractions.length / 2))
  day.attractions.forEach((item, index) => {
    steps.push({ key: `${day.date}-attraction-${item.poi_id || item.name}`, category: `第 ${index + 1} 站`, title: item.name, description: item.description, address: item.address, hint: `建议游玩 ${Math.round(item.visit_duration / 60 * 10) / 10} 小时`, icon: '⌖' })
    if (index + 1 === lunchAfter) addMeal('lunch', '午餐', '🍜', '中途休息')
  })
  addMeal('dinner', '晚餐', '🥢', '结束游览后')
  if (day.hotel) steps.push({ key: `${dayKey}-hotel`, category: '住宿', title: day.hotel.name, description: day.hotel.price_range, address: day.hotel.address, hint: '返回休息', icon: '▣' })
  return steps
}

// 当前时刻（每分钟刷新）：用于"现在该在哪"的高亮与剩余时间
const nowMinutes = ref(-1)
let clockTimer: number | undefined
const refreshNow = () => {
  const now = new Date()
  nowMinutes.value = now.getHours() * 60 + now.getMinutes()
}
refreshNow()
clockTimer = window.setInterval(refreshNow, 60_000)

// 当前步骤：选中日=今天时，找时间上"正在进行或即将开始"的第一个未完成步骤
const isTodaySelected = computed(() => currentDay.value?.date === localDate())
const currentStepIndex = computed(() => {
  if (!isTodaySelected.value || nowMinutes.value < 0) return -1
  const steps = currentSteps.value
  // 未完成里第一个"未结束"的（进行中或还没到）
  for (let i = 0; i < steps.length; i++) {
    if (isCompleted(steps[i].key)) continue
    if (steps[i].startMinutes === undefined && steps[i].endMinutes === undefined) continue
    const end = steps[i].endMinutes ?? steps[i].startMinutes!
    if (end >= nowMinutes.value) return i
  }
  return -1
})
// 当前步骤的"还剩 N 分钟 / N 分钟后开始"
const currentStepTiming = computed(() => {
  const index = currentStepIndex.value
  if (index < 0) return ''
  const step = currentSteps.value[index]
  if (step.startMinutes !== undefined && step.startMinutes > nowMinutes.value) {
    return `${step.startMinutes - nowMinutes.value} 分钟后开始`
  }
  if (step.endMinutes !== undefined) {
    return `还剩约 ${Math.max(1, step.endMinutes - nowMinutes.value)} 分钟`
  }
  return '进行中'
})

const allSteps = computed(() => trip.value?.plan.days.map(buildSteps) || [])
const currentDay = computed(() => trip.value!.plan.days[selectedDay.value])
const currentSteps = computed(() => allSteps.value[selectedDay.value] || [])
const todayWeather = computed(() => trip.value?.plan.weather_info.find(item => item.date === currentDay.value.date))
const isCompleted = (key: string) => Boolean(progress.value[key])
const completedCount = computed(() => currentSteps.value.filter(step => isCompleted(step.key)).length)
const dayProgress = (index: number) => {
  const steps = allSteps.value[index] || []
  return steps.length ? Math.round(steps.filter(step => isCompleted(step.key)).length / steps.length * 100) : 0
}
const overallPercent = computed(() => {
  const steps = allSteps.value.flat()
  return steps.length ? Math.round(steps.filter(step => isCompleted(step.key)).length / steps.length * 100) : 0
})

onUnmounted(() => {
  if (clockTimer) window.clearInterval(clockTimer)
})

const toggleStep = async (step: ExecutionStep) => {
  if (savingKey.value) return
  const previous = isCompleted(step.key)
  progress.value = { ...progress.value, [step.key]: !previous }
  savingKey.value = step.key
  try {
    const result = await updateTripProgress(tripId, step.key, !previous)
    progress.value = Object.fromEntries(result.items.map(item => [item.item_key, item.completed]))
  } catch (error: any) {
    progress.value = { ...progress.value, [step.key]: previous }
    message.error(error.message || '完成状态保存失败')
  } finally {
    savingKey.value = ''
  }
}

const shortDate = (value: string) => new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', weekday: 'short' }).format(new Date(`${value}T00:00:00`))
const dayRelation = (value: string) => value === localDate() ? 'TODAY · 今天' : value > localDate() ? 'UPCOMING · 即将到来' : 'PAST · 已经过往'
// 今天出门前的贴身提醒：雨具/防晒/保暖/防风
const weatherTips = computed(() => {
  const weather = todayWeather.value
  if (!weather) return []
  const tips: string[] = []
  const all = `${weather.day_weather}${weather.night_weather}`
  if (all.includes('雨')) tips.push('☂ 带伞')
  if (all.includes('雪')) tips.push('❄ 防滑')
  if ((weather.day_temp || 0) >= 35) tips.push('🥵 高温防晒')
  if ((weather.day_temp || 0) <= 5) tips.push('🧣 保暖')
  const gust = Math.max(0, ...(weather.wind_power || '').match(/\d+/g)?.map(Number) || [0])
  if (gust >= 5) tips.push('💨 大风')
  return tips
})

const weatherIcon = (weather: string) => weather.includes('雨') ? '🌧️' : weather.includes('云') ? '⛅' : weather.includes('雪') ? '❄️' : '☀️'
const navigationUrl = (step: ExecutionStep) => {
  // 有坐标：直接发起"到这个地方"的导航；没有坐标：按名称在城市内搜索
  if (step.location) {
    return `https://uri.amap.com/navigation?to=${step.location.longitude},${step.location.latitude},${encodeURIComponent(step.title)}&mode=car&coordinate=gaode`
  }
  return `https://uri.amap.com/search?keyword=${encodeURIComponent(step.title)}&city=${encodeURIComponent(trip.value?.city || '')}`
}
</script>

<style scoped>
.today-page { min-height: 100vh; color: #14213d; background: #f5f7fa; }
.today-header { padding: 22px max(24px, calc((100vw - 1180px) / 2)) 48px; color: white; background: radial-gradient(circle at 82% 14%, rgba(73,190,147,.24), transparent 27%), #14213d; }
.header-bar { display: flex; justify-content: space-between; align-items: center; color: #8391a7; font-size: 10px; font-weight: 800; letter-spacing: 1.5px; }
.header-bar a { color: #c9d2df; text-decoration: none; }
.hero-content { display: flex; justify-content: space-between; align-items: end; gap: 40px; margin-top: 48px; }
.hero-content p { margin: 0; color: #5fd0a6; font-size: 12px; font-weight: 800; letter-spacing: .8px; }
.hero-content h1 { margin: 8px 0 6px; font-size: clamp(34px, 5vw, 54px); letter-spacing: -2px; }
.hero-content > div > span { color: #a8b4c6; }
.overall-progress { min-width: 190px; display: grid; grid-template-columns: auto 1fr; align-items: baseline; gap: 0 10px; }
.overall-progress strong { font-size: 34px; }
.overall-progress > span { color: #96a4b8; font-size: 11px; }
.overall-progress > div { grid-column: 1 / -1; height: 5px; overflow: hidden; border-radius: 9px; background: #31415b; }
.overall-progress i { display: block; height: 100%; border-radius: inherit; background: #5fd0a6; transition: width .3s; }
.today-main { max-width: 1236px; margin: 0 auto; padding: 32px 28px 80px; }
.day-tabs { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin-bottom: 24px; }
.day-tabs button { padding: 14px 16px; display: flex; flex-direction: column; align-items: flex-start; border: 1px solid #e1e7ee; border-radius: 12px; color: #758195; background: white; cursor: pointer; transition: .2s; }
.day-tabs button.active { color: white; border-color: #21a179; background: #21a179; box-shadow: 0 10px 24px rgba(33,161,121,.2); }
.day-tabs span { font-size: 9px; font-weight: 900; letter-spacing: 1px; }
.day-tabs b { margin: 3px 0; color: #26344d; font-size: 14px; }
.day-tabs .active b { color: white; }
.day-tabs small { font-size: 10px; opacity: .75; }
.day-layout { display: grid; grid-template-columns: 280px minmax(0, 1fr); gap: 22px; align-items: start; }
.day-summary, .plan-board { border: 1px solid #e2e8ef; border-radius: 18px; background: white; box-shadow: 0 14px 36px rgba(34,51,76,.06); }
.day-summary { padding: 25px; position: sticky; top: 92px; }
.summary-kicker, .board-heading span { color: #21a179; font-size: 9px; font-weight: 900; letter-spacing: 1.3px; }
.day-summary h2 { margin: 5px 0 8px; font-size: 28px; }
.day-summary > p { color: #788497; font-size: 12px; line-height: 1.7; }
.weather-chip { margin: 20px 0; padding: 13px; display: flex; align-items: center; gap: 10px; border-radius: 12px; background: #f3f8f6; }
.weather-chip > span { font-size: 25px; }
.weather-chip div { display: flex; flex-direction: column; }
.weather-chip b { font-size: 18px; }
.weather-chip small { color: #7c8b83; }
.day-progress-ring { --progress: 0deg; width: 142px; height: 142px; margin: 28px auto 12px; padding: 10px; display: grid; place-items: center; border-radius: 50%; background: conic-gradient(#21a179 var(--progress), #e9eef1 0); }
.day-progress-ring::before { content: ''; grid-area: 1/1; width: 118px; height: 118px; border-radius: 50%; background: white; }
.day-progress-ring > div { grid-area: 1/1; z-index: 1; text-align: center; }
.day-progress-ring b { font-size: 30px; }
.day-progress-ring span { color: #8b96a6; }
.day-progress-ring small { display: block; color: #9aa5b4; font-size: 9px; }
.day-complete { padding: 10px; border-radius: 9px; color: #167557; background: #e9f8f2; font-size: 11px; text-align: center; }
.plan-board { padding: 28px; }
.board-heading { display: flex; justify-content: space-between; align-items: end; margin-bottom: 23px; }
.board-heading h2 { margin: 4px 0 0; font-size: 27px; }
.board-heading > b { color: #21a179; font-size: 20px; }
.step-list { display: grid; gap: 10px; }
.plan-step { display: grid; grid-template-columns: 38px 42px 1fr auto; gap: 12px; align-items: center; min-height: 104px; padding: 14px; border: 1px solid #e6ebf0; border-radius: 14px; transition: .2s; }
.plan-step:hover { border-color: #a9dccc; box-shadow: 0 8px 22px rgba(31,76,61,.06); }
.plan-step.completed { opacity: .58; background: #f7faf9; }
.plan-step.completed h3 { text-decoration: line-through; }
.plan-step.saving { pointer-events: none; }
.check-button { width: 34px; height: 34px; border: 2px solid #cdd6df; border-radius: 50%; color: #8793a4; background: white; font-weight: 900; cursor: pointer; }
.completed .check-button { color: white; border-color: #21a179; background: #21a179; }
.step-icon { width: 40px; height: 40px; display: grid; place-items: center; border-radius: 11px; color: #14213d; background: #eef2f6; font-size: 18px; }
.step-meta { display: flex; align-items: center; gap: 10px; color: #21a179; font-size: 9px; font-weight: 900; letter-spacing: .7px; }
.step-meta small { color: #9aa4b3; font-weight: 500; letter-spacing: 0; }
.step-content h3 { margin: 3px 0; font-size: 16px; }
.step-content p { margin: 3px 0; color: #7d8899; font-size: 11px; line-height: 1.5; }
.address { color: #97a1b0; font-size: 10px; }
.nav-button { padding: 8px 10px; border-radius: 8px; color: #334157; background: #f0f3f6; font-size: 11px; font-weight: 800; text-decoration: none; white-space: nowrap; }
.board-note { margin-top: 22px; padding: 15px; display: flex; gap: 12px; border-radius: 12px; color: #738094; background: #fff7f4; }
.board-note > span { color: #ff6b4a; font-size: 20px; }
.board-note p { margin: 0; font-size: 11px; line-height: 1.6; }
.board-note b { display: block; color: #38465b; }
.loading-state { min-height: 60vh; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 15px; color: #8490a1; }
@media (max-width: 820px) { .day-layout { grid-template-columns: 1fr; } .day-summary { position: static; } .day-progress-ring { margin-block: 16px; } }
@media (max-width: 600px) { .today-header { padding-inline: 18px; } .hero-content { align-items: flex-start; flex-direction: column; } .overall-progress { width: 100%; } .today-main { padding-inline: 14px; } .day-tabs { display: flex; overflow-x: auto; } .day-tabs button { min-width: 128px; } .plan-board { padding: 15px; } .plan-step { grid-template-columns: 34px 36px 1fr; } .nav-button { grid-column: 3; justify-self: start; } }

/* "现在"高亮与侧栏块 */
.plan-step.now { border-color: #ff6b4a; box-shadow: 0 10px 26px rgba(255, 107, 74, .18); background: #fffaf8; }
.now-chip { margin-left: auto; padding: 3px 10px; border-radius: 999px; color: #fff; background: #ff6b4a; font-size: 10px; font-weight: 800; font-style: normal; letter-spacing: .5px; animation: nowPulse 2s ease infinite; }
@keyframes nowPulse { 0%, 100% { box-shadow: 0 0 0 0 rgba(255,107,74,.45); } 50% { box-shadow: 0 0 0 5px rgba(255,107,74,.18); } }
.now-block { margin: 18px 0 0; padding: 14px; border-radius: 12px; color: #fff; background: linear-gradient(135deg, #ff6b4a, #ff8b73); display: flex; flex-direction: column; gap: 3px; }
.now-block.done { background: linear-gradient(135deg, #21a179, #43c79b); }
.now-block b { font-size: 15px; }
.now-block small { opacity: .85; font-size: 11px; }
.now-kicker { font-size: 9px; font-weight: 900; letter-spacing: 1.2px; opacity: .9; }
.weather-tips { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 6px; }
.weather-tips span { padding: 2px 8px; border-radius: 999px; background: #ffedd9; color: #b25f0a; font-size: 10px; font-weight: 700; }
</style>
