<template>
  <div class="trips-page">
    <section class="trips-hero">
      <div>
        <span class="eyebrow">PERSONAL TRIP LIBRARY</span>
        <h1>我的行程</h1>
        <p>计划会保存在这台设备上。随时回来查看、继续修改，或者恢复到之前的版本。</p>
      </div>
      <router-link to="/#planner" class="new-trip">＋ 规划新旅行</router-link>
    </section>

    <section class="trip-content">
      <div v-if="loading" class="state-card"><a-spin /><span>正在整理你的行程…</span></div>
      <div v-else-if="!trips.length" class="empty-card">
        <div class="empty-mark">⌁</div>
        <h2>还没有保存的行程</h2>
        <p>完成第一次 AI 规划后，行程会自动出现在这里。</p>
        <router-link to="/#planner">开始规划</router-link>
      </div>
      <div v-else class="trip-grid">
        <article v-for="trip in trips" :key="trip.id" class="trip-card">
          <router-link :to="`/trips/${trip.id}`" class="trip-link">
            <div class="card-visual" :class="{ 'has-photo': trip.cover_image_url }">
              <img v-if="trip.cover_image_url" :src="trip.cover_image_url" :alt="trip.city" loading="lazy" @error="trip.cover_image_url = null" />
              <span v-else>{{ cityInitial(trip.city) }}</span>
              <div class="trip-status" :class="tripPhase(trip).tone">{{ tripPhase(trip).label }}</div>
              <div class="quality-dot" :class="trip.data_quality">{{ qualityLabel(trip.data_quality) }}</div>
            </div>
            <div class="card-body">
              <div class="card-kicker">{{ trip.start_date }} — {{ trip.end_date }}</div>
              <h2>{{ trip.title }}</h2>
              <div class="card-city">📍 {{ trip.city }}</div>
              <div class="card-stats">
                <span><b>{{ trip.days }}</b> 天</span>
                <span v-if="trip.estimated_budget !== undefined"><b>¥{{ trip.estimated_budget }}</b> 预估</span>
                <span><b>v{{ trip.current_version }}</b> 当前版本</span>
              </div>
              <div class="card-footer">
                <span>更新于 {{ formatDate(trip.updated_at) }}</span>
                <strong>继续查看 →</strong>
              </div>
            </div>
          </router-link>
          <button class="delete-button" aria-label="删除行程" @click="removeTrip(trip.id, trip.title)">删除</button>
        </article>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { Modal, Spin as ASpin, message } from 'ant-design-vue'
import { deleteSavedTrip, listSavedTrips } from '@/services/api'
import type { SavedTripSummary } from '@/types'

const loading = ref(true)
const trips = ref<SavedTripSummary[]>([])

onMounted(async () => {
  try {
    trips.value = sortTrips(await listSavedTrips())
  } catch (error: any) {
    message.error(error.message || '暂时无法读取行程')
  } finally {
    loading.value = false
  }
})

const cityInitial = (city: string) => city.slice(0, 1)

// 行程阶段：进行中最醒目、即将出发带倒计时、已结束置后
const localToday = () => {
  const date = new Date()
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 10)
}
const tripPhase = (trip: SavedTripSummary) => {
  const today = localToday()
  if (today > trip.end_date) return { tone: 'past', label: '已结束' }
  if (today >= trip.start_date) return { tone: 'ongoing', label: '旅行中' }
  const daysLeft = Math.round((new Date(`${trip.start_date}T00:00:00`).getTime() - new Date(`${today}T00:00:00`).getTime()) / 86400000)
  return { tone: 'upcoming', label: daysLeft <= 1 ? '明天出发' : `${daysLeft} 天后出发` }
}
// 列表排序：旅行中 > 即将出发（近的在前）> 未来 > 已结束（近的在前）
const sortTrips = (list: SavedTripSummary[]) => {
  const rank = (t: SavedTripSummary) => {
    const phase = tripPhase(t).tone
    return phase === 'ongoing' ? 0 : phase === 'upcoming' ? 1 : 2
  }
  return [...list].sort((a, b) => {
    const ra = rank(a), rb = rank(b)
    if (ra !== rb) return ra - rb
    return ra === 2 ? b.start_date.localeCompare(a.start_date) : a.start_date.localeCompare(b.start_date)
  })
}
const qualityLabel = (quality: string) => ({ live: '实时数据', mixed: '混合数据', fallback: '参考计划' }[quality] || '已保存')
const formatDate = (value: string) => new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric' }).format(new Date(value))

const removeTrip = (id: string, title: string) => {
  Modal.confirm({
    title: `删除“${title}”？`,
    content: '行程和它的所有历史版本都会被删除，此操作无法撤销。',
    okText: '确认删除', cancelText: '保留', okType: 'danger',
    async onOk() {
      await deleteSavedTrip(id)
      trips.value = trips.value.filter(item => item.id !== id)
      message.success('行程已删除')
    }
  })
}
</script>

<style scoped>
.trips-page { min-height: calc(100vh - 166px); background: #f6f8fb; }
.trips-hero { min-height: 280px; padding: 64px max(28px, calc((100vw - 1180px) / 2)); display: flex; align-items: end; justify-content: space-between; gap: 30px; color: white; background: radial-gradient(circle at 80% 20%, rgba(255,126,99,.32), transparent 28%), #14213d; }
.eyebrow { color: #ff9a83; font-size: 11px; font-weight: 900; letter-spacing: 2px; }
h1 { margin: 10px 0 8px; font-size: clamp(38px, 5vw, 58px); letter-spacing: -2px; }
.trips-hero p { max-width: 600px; margin: 0; color: #aeb9ca; line-height: 1.8; }
.new-trip { padding: 13px 18px; border-radius: 11px; color: #14213d; background: white; font-weight: 800; text-decoration: none; white-space: nowrap; }
.trip-content { max-width: 1236px; margin: 0 auto; padding: 44px 28px 80px; }
.trip-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 22px; }
.trip-card { position: relative; overflow: hidden; border: 1px solid #e3e9f0; border-radius: 18px; background: white; box-shadow: 0 14px 34px rgba(30,47,72,.07); transition: transform .2s ease, box-shadow .2s ease; }
.trip-card:hover { transform: translateY(-4px); box-shadow: 0 20px 45px rgba(30,47,72,.12); }
.trip-link { color: inherit; text-decoration: none; }
.card-visual { height: 126px; padding: 18px; display: flex; align-items: center; justify-content: center; position: relative; color: rgba(255,255,255,.92); background: linear-gradient(135deg, #263858, #476483); }
.card-visual > span { font-size: 54px; font-weight: 900; opacity: .9; }
.quality-dot { position: absolute; top: 14px; right: 14px; padding: 5px 9px; border-radius: 999px; color: #244735; background: #dff7e9; font-size: 10px; font-weight: 800; }
.quality-dot.fallback { color: #7a5520; background: #fff0cf; }
.card-body { padding: 22px; }
.card-kicker { color: #e65334; font-size: 11px; font-weight: 800; letter-spacing: .7px; }
.card-body h2 { margin: 7px 0 18px; color: #14213d; font-size: 22px; }
.card-stats { display: flex; gap: 15px; padding-bottom: 19px; border-bottom: 1px solid #edf0f4; color: #8994a5; font-size: 11px; }
.card-stats span { display: flex; flex-direction: column; }
.card-stats b { color: #344258; font-size: 14px; }
.card-footer { display: flex; justify-content: space-between; margin-top: 17px; color: #98a2b1; font-size: 11px; }
.card-footer strong { color: #e65334; }
.delete-button { position: absolute; top: 14px; left: 14px; padding: 5px 9px; border: 0; border-radius: 999px; color: white; background: rgba(20,33,61,.6); opacity: 0; cursor: pointer; transition: opacity .2s; }
.trip-card:hover .delete-button { opacity: 1; }
.state-card, .empty-card { min-height: 320px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 14px; border: 1px dashed #ccd5e0; border-radius: 20px; color: #7b8799; background: white; text-align: center; }
.empty-mark { width: 64px; height: 64px; display: grid; place-items: center; border-radius: 18px; color: white; background: #14213d; font-size: 34px; }
.empty-card h2 { margin: 5px 0 -8px; color: #14213d; }
.empty-card p { margin: 0; }
.empty-card a { margin-top: 8px; padding: 10px 18px; border-radius: 9px; color: white; background: #ff6b4a; text-decoration: none; font-weight: 800; }
@media (max-width: 900px) { .trip-grid { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 620px) { .trips-hero { min-height: 300px; padding-block: 44px; align-items: flex-start; flex-direction: column; } .trip-content { padding-inline: 16px; } .trip-grid { grid-template-columns: 1fr; } .delete-button { opacity: 1; } }

.card-visual.has-photo > span { display: none; }
.card-visual img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
.card-visual.has-photo::after { content: ''; position: absolute; inset: 0; background: linear-gradient(180deg, rgba(20,33,61,.05) 40%, rgba(20,33,61,.55)); }
.card-visual > span, .quality-dot, .trip-status { position: relative; z-index: 1; }
.trip-status { position: absolute; top: 14px; left: 14px; z-index: 2; padding: 5px 10px; border-radius: 999px; font-size: 11px; font-weight: 800; }
.trip-status.ongoing { color: #fff; background: #ff6b4a; box-shadow: 0 3px 10px rgba(255,107,74,.4); }
.trip-status.upcoming { color: #14532d; background: #d9f7e5; }
.trip-status.past { color: #e8edf3; background: rgba(20,33,61,.55); }
.card-city { margin: -12px 0 14px; color: #8792a3; font-size: 12px; }
</style>
