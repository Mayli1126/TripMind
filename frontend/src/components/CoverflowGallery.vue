<template>
  <div
    class="coverflow"
    @pointerdown="onPointerDown"
    @pointerup="onPointerUp"
    @pointercancel="onPointerUp"
  >
    <!-- 舞台：中间卡完全清晰置顶，两侧依次缩小、降透明度并压在后面 -->
    <div class="cover-stage">
      <div
        v-for="(item, index) in items"
        :key="item.key"
        class="cover-card"
        :class="{ 'is-active': index === current, 'is-hidden': Math.abs(index - current) > 2 }"
        :style="cardStyle(index)"
        :data-attraction-name="item.name"
        @click="onCardClick(index)"
      >
        <img class="cover-img" :src="item.image" :alt="item.name" loading="lazy" draggable="false" />
        <div class="cover-shade"></div>
        <div class="cover-info">
          <h3 class="cover-name">{{ item.name }}</h3>
          <p class="cover-desc">{{ item.description }}</p>
        </div>
        <span class="cover-index">{{ index + 1 }}/{{ items.length }}</span>
      </div>
    </div>

    <!-- 左右切换箭头：圆形半透明 -->
    <button
      v-if="items.length > 1"
      class="cover-arrow cover-arrow-left"
      aria-label="上一个景点"
      @click.stop="prev"
    >‹</button>
    <button
      v-if="items.length > 1"
      class="cover-arrow cover-arrow-right"
      aria-label="下一个景点"
      @click.stop="next"
    >›</button>

    <!-- 位置指示圆点 -->
    <div v-if="items.length > 1" class="cover-dots">
      <button
        v-for="(_, index) in items"
        :key="index"
        class="cover-dot"
        :class="{ 'is-current': index === current }"
        :aria-label="`查看第 ${index + 1} 个景点`"
        @click="goTo(index)"
      ></button>
    </div>
  </div>
</template>

<script setup lang="ts">
// 行程概览的 Coverflow 轮播：纯手写实现（零第三方依赖）
// 规格：竖向 3:4 大卡，中间 1.0 / 两侧 0.82、0.68 递减，层叠纵深，300ms ease-out
import { onMounted, onUnmounted, ref } from 'vue'

export interface CoverflowItem {
  key: string | number
  name: string
  image: string
  description?: string
}

const props = defineProps<{ items: CoverflowItem[] }>()
const emit = defineEmits<{ (e: 'select', index: number): void }>()

const current = ref(0)

// 缩放/透明度/层级按与中间卡的偏移取值；超过两格隐藏
const SCALES = [1, 0.82, 0.68]
const OPACITIES = [1, 0.72, 0.38]

const cardStyle = (index: number) => {
  const offset = index - current.value
  const distance = Math.abs(offset)
  const scale = SCALES[Math.min(distance, SCALES.length - 1)]
  const opacity = OPACITIES[Math.min(distance, OPACITIES.length - 1)]
  return {
    transform: `translateX(${offset * 62}%) scale(${scale})`,
    opacity: String(opacity),
    zIndex: String(100 - distance)
  }
}

const goTo = (index: number) => {
  if (index >= 0 && index < props.items.length) current.value = index
}
const next = () => goTo(current.value + 1)
const prev = () => goTo(current.value - 1)

// 点击侧边卡转到中间；点击中间卡视为选中（父页面跳到对应行程日）
const onCardClick = (index: number) => {
  if (index === current.value) emit('select', index)
  else goTo(index)
}

// 键盘左右方向键切换（输入框聚焦时不响应）
const onKeydown = (event: KeyboardEvent) => {
  const tag = (event.target as HTMLElement | null)?.tagName
  if (tag === 'INPUT' || tag === 'TEXTAREA') return
  if (event.key === 'ArrowRight') next()
  else if (event.key === 'ArrowLeft') prev()
}

// 拖拽滑动：位移超过阈值（约 40px）切换一格
const dragStartX = ref<number | null>(null)
const onPointerDown = (event: PointerEvent) => {
  dragStartX.value = event.clientX
}
const onPointerUp = (event: PointerEvent) => {
  const startX = dragStartX.value
  dragStartX.value = null
  if (startX === null) return
  const delta = event.clientX - startX
  if (delta < -40) next()
  else if (delta > 40) prev()
}

onMounted(() => window.addEventListener('keydown', onKeydown))
onUnmounted(() => window.removeEventListener('keydown', onKeydown))

defineExpose({ goTo })
</script>

<style scoped>
.coverflow {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 18px;
  user-select: none;
  touch-action: pan-y;
}

/* 舞台高度由卡宽 3:4 推出，卡宽自适应视口 */
.cover-stage {
  position: relative;
  height: calc(min(64vw, 320px) * 4 / 3);
}
.cover-card {
  position: absolute;
  top: 0;
  left: 50%;
  width: min(64vw, 320px);
  height: 100%;
  margin-left: calc(min(64vw, 320px) / -2);
  overflow: hidden;
  border-radius: 20px;
  cursor: pointer;
  background: #22314f;
  box-shadow: 0 18px 44px rgba(0, 0, 0, 0.45);
  transition: transform 0.3s ease-out, opacity 0.3s ease-out;
  will-change: transform, opacity;
}
.cover-card.is-hidden { visibility: hidden; pointer-events: none; }
.cover-card.is-active .cover-img { filter: none; }
.cover-img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  filter: saturate(0.9) brightness(0.94);
}
.cover-shade {
  position: absolute;
  inset: auto 0 0 0;
  height: 46%;
  background: linear-gradient(to top, rgba(6, 12, 26, 0.88), rgba(6, 12, 26, 0));
  pointer-events: none;
}
.cover-info {
  position: absolute;
  right: 0;
  bottom: 0;
  left: 0;
  padding: 16px 18px 18px;
  pointer-events: none;
}
.cover-name {
  margin: 0 0 5px;
  color: #fff;
  font-size: 16px;
  font-weight: 800;
  letter-spacing: 0.4px;
}
.cover-desc {
  margin: 0;
  color: rgba(255, 255, 255, 0.68);
  font-size: 12px;
  line-height: 1.55;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.cover-index {
  position: absolute;
  left: 14px;
  bottom: 12px;
  padding: 3px 9px;
  border-radius: 999px;
  color: rgba(255, 255, 255, 0.85);
  background: rgba(6, 12, 26, 0.5);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.5px;
}

/* 圆形半透明箭头 */
.cover-arrow {
  position: absolute;
  top: calc(min(64vw, 320px) * 4 / 6);
  transform: translateY(-50%);
  z-index: 200;
  width: 42px;
  height: 42px;
  border: 0;
  border-radius: 50%;
  color: #fff;
  background: rgba(255, 255, 255, 0.14);
  backdrop-filter: blur(4px);
  font-size: 26px;
  line-height: 1;
  cursor: pointer;
  transition: background 0.15s ease, transform 0.15s ease;
}
.cover-arrow:hover { background: rgba(255, 255, 255, 0.26); }
.cover-arrow-left { left: max(10px, calc(50% - min(38vw, 250px))); }
.cover-arrow-right { right: max(10px, calc(50% - min(38vw, 250px))); }

/* 位置圆点 */
.cover-dots {
  display: flex;
  justify-content: center;
  gap: 7px;
}
.cover-dot {
  width: 7px;
  height: 7px;
  padding: 0;
  border: 0;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.24);
  cursor: pointer;
  transition: all 0.2s ease;
}
.cover-dot:hover { background: rgba(255, 255, 255, 0.45); }
.cover-dot.is-current { background: #ff6b4a; transform: scale(1.35); }

@media (max-width: 620px) {
  .cover-stage { height: calc(66vw * 4 / 3); }
  .cover-card { width: 66vw; margin-left: -33vw; }
  .cover-arrow { top: calc(66vw * 4 / 6); }
  .cover-arrow-left { left: 6px; }
  .cover-arrow-right { right: 6px; }
}
</style>
