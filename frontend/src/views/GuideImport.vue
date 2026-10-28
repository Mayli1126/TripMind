<template>
  <div class="guide-import">
    <section class="page-heading">
      <span class="page-kicker">GUIDE IMPORT</span>
      <h1>攻略导入</h1>
      <p>粘贴旅游攻略正文或填写公开链接，AI 会提取其中值得去的地点并到高德地图逐一核对位置。你确认后的候选地点将作为后续规划的重要参考。</p>
    </section>

    <!-- 输入区 -->
    <a-card class="input-card" :bordered="false">
      <a-radio-group v-model:value="inputType" class="type-switch" button-style="solid">
        <a-radio-button value="text">粘贴正文</a-radio-button>
        <a-radio-button value="url">攻略链接</a-radio-button>
        <a-radio-button value="image">攻略截图</a-radio-button>
      </a-radio-group>

      <a-form layout="vertical" class="input-form">
        <template v-if="inputType === 'text'">
          <a-form-item label="攻略标题（可选）">
            <a-input v-model:value="title" placeholder="例如：北京 3 天 2 晚人均 1500 攻略" allow-clear />
          </a-form-item>
          <a-form-item label="攻略正文" required>
            <a-textarea
              v-model:value="content"
              :rows="10"
              :maxlength="50000"
              show-count
              placeholder="把攻略全文粘贴到这里：景点、美食、路线安排、注意事项都可以"
            />
          </a-form-item>
        </template>
        <template v-else-if="inputType === 'url'">
          <a-form-item label="攻略链接" required>
            <a-input v-model:value="sourceUrl" placeholder="https://…（公开可访问的攻略页面）" allow-clear />
          </a-form-item>
        </template>
        <template v-else>
          <a-form-item label="攻略文件（可多选）" required>
            <div
              class="image-upload-area"
              :class="{ 'is-dragover': dragover }"
              @dragover.prevent="dragover = true"
              @dragleave.prevent="dragover = false"
              @drop.prevent="handleDrop"
            >
              <img v-if="imagePreview" :src="imagePreview" class="image-preview" alt="攻略截图预览" />
              <a-upload
                :before-upload="handleFileSelected"
                :show-upload-list="false"
                multiple
                accept="image/png,image/jpeg,image/jpg,image/webp,.txt,.md"
              >
                <a-button>{{ pendingFiles.length ? `已选 ${pendingFiles.length} 个文件，可继续添加` : '选择截图或 txt/md 文件（可多选，≤6MB/个）' }}</a-button>
              </a-upload>
              <a-button v-if="pendingFiles.length" size="small" @click="clearPending">清空</a-button>
            </div>
            <p class="parse-tip">
              支持小红书/公众号长图截图（可一次选多张拼成一篇完整攻略）与 txt/md 攻略文件，也可以直接拖拽到这里。
              AI 会逐篇识别内容并到高德核对地点，失败的单篇可单独重试。
            </p>
            <ul v-if="pendingFiles.length" class="pending-list">
              <li v-for="(item, index) in pendingFiles" :key="index">
                {{ item.name }}（{{ item.inputType === 'image' ? '截图' : '文本' }}）
                <a @click.prevent="removePending(index)">移除</a>
              </li>
            </ul>
          </a-form-item>
        </template>

        <a-form-item label="目的地城市（可选，帮助更准地核对地点）">
          <a-input v-model:value="city" placeholder="例如：北京" style="max-width: 280px" allow-clear />
        </a-form-item>

        <a-button type="primary" size="large" :loading="parsing" :disabled="!canSubmit" @click="handleParse">
          {{ parseButtonText }}
        </a-button>
        <p v-if="parsing" class="parse-tip">{{ parseProgressText }}</p>
      </a-form>
    </a-card>

    <!-- 解析结果 -->
    <section v-if="records.length" class="result-section">
      <h2 class="section-title">已导入攻略（{{ records.length }}）</h2>

      <a-card v-for="record in records" :key="record.id" class="source-card" :bordered="false">
        <div class="source-head">
          <div class="source-title">
            <strong>{{ record.result?.title || record.title || '未命名攻略' }}</strong>
            <a-tag v-if="record.parse_status === 'parsed'" color="green">解析成功</a-tag>
            <a-tag v-else-if="record.parse_status === 'failed'" color="red">解析失败</a-tag>
            <a-tag v-else color="blue">解析中</a-tag>
          </div>
          <div class="source-actions">
            <a-button
              v-if="record.parse_status === 'failed' && record.input_type === 'url'"
              size="small"
              :loading="retryingId === record.id"
              @click="handleRetry(record)"
            >重试</a-button>
            <a-popconfirm
              title="删除这篇攻略？其候选地点也会一并移除"
              ok-text="删除"
              cancel-text="取消"
              @confirm="handleDelete(record)"
            >
              <a-button size="small" danger :loading="deletingId === record.id">删除</a-button>
            </a-popconfirm>
          </div>
        </div>

        <a-alert
          v-if="record.parse_status === 'failed'"
          type="warning"
          show-icon
          class="source-error"
          :message="record.result?.error_message || record.error_message || '解析失败'"
          :description="retryHint(record)"
        />

        <p v-else-if="record.result" class="source-meta">
          城市：{{ record.result.city || '未识别' }}
          · 提取 {{ record.result.candidates.length }} 个地点
          · 正文 {{ record.result.document_chars }} 字
        </p>
      </a-card>

      <!-- 候选确认板：聚合所有成功攻略的候选 -->
      <a-card class="board-card" :bordered="false">
        <template #title>
          <span>候选地点确认</span>
          <span class="board-subtitle">先勾选要去的地点（右侧可再调优先级），确认后才用于规划</span>
        </template>
        <template #extra>
          <a-button v-if="mappableCandidates.length >= 2" size="small" @click="toggleCandidateMap">
            {{ candidateMapOpen ? '收起分布图' : '看地图分布 📍' }}
          </a-button>
        </template>
        <div v-show="candidateMapOpen && mappableCandidates.length >= 2" class="candidate-map">
          <div id="candidate-map-container"></div>
          <p class="candidate-map-tip">每个点是一个候选地点，颜色代表你的选择（橙=必去、蓝=想去、灰=不去）</p>
        </div>
        <CandidatePlaceBoard
          :candidates="candidates"
          @change="() => persistCandidates(true)"
          @bulk-change="() => persistCandidates(true)"
        />
        <div class="board-actions">
          <a-button
            type="primary"
            size="large"
            :disabled="!activeCount"
            @click="goPlanWithCandidates"
          >用勾选的 {{ activeCount }} 个地点开始规划 →</a-button>
          <a-button @click="persistCandidates()">保存选择</a-button>
          <span v-if="savedTip" class="saved-tip">{{ savedTip }}</span>
        </div>
      </a-card>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  Alert as AAlert,
  Button as AButton,
  Card as ACard,
  Form as AForm,
  FormItem as AFormItem,
  Input as AInput,
  Popconfirm as APopconfirm,
  RadioGroup as ARadioGroup,
  RadioButton as ARadioButton,
  Tag as ATag,
  Textarea as ATextarea,
  Upload as AUpload,
  message
} from 'ant-design-vue'
import CandidatePlaceBoard from '@/components/CandidatePlaceBoard.vue'
import { deleteGuideSource, listGuideSources, parseGuideSource, retryGuideSource, updateCandidateChoices } from '@/services/api'
import type { CandidatePlace, GuideInputType, GuideSourceRecord } from '@/types'

// 已确认候选项在本机的暂存键；阶段 2 候选池接入主规划图后由此读取
const STORAGE_KEY = 'tripmind.candidate_pool.v1'

const inputType = ref<GuideInputType>('text')
const content = ref('')
const sourceUrl = ref('')
const title = ref('')
const city = ref('')
// 批量文件队列：截图与 txt/md 攻略文件混合，逐篇解析
interface PendingFile {
  name: string
  inputType: GuideInputType
  mimeType: string
  base64?: string    // 截图：base64（不含 data: 前缀）
  text?: string      // 文本文件内容
  preview?: string   // 截图缩略
}
const pendingFiles = ref<PendingFile[]>([])
const imagePreview = ref('')
const dragover = ref(false)
// 批量解析进度
const batchTotal = ref(0)
const batchDone = ref(0)

const MAX_FILE_BYTES = 6 * 1024 * 1024

// antd Upload 的 before-upload：返回 false 阻止自动上传，转为加入本地队列
const handleFileSelected = (file: File) => {
  if (file.size > MAX_FILE_BYTES) {
    message.error(`「${file.name}」超过 6MB，请压缩后重试`)
    return false
  }
  if (file.type.startsWith('image/')) {
    const reader = new FileReader()
    reader.onload = () => {
      const result = String(reader.result || '')
      const commaIndex = result.indexOf(',')
      pendingFiles.value.push({
        name: file.name,
        inputType: 'image',
        mimeType: result.slice(0, commaIndex).match(/data:([^;]+)/)?.[1] || 'image/jpeg',
        base64: result.slice(commaIndex + 1),
        preview: result,
      })
      // 预览区显示最后一张图
      imagePreview.value = result
    }
    reader.readAsDataURL(file)
  } else if (/\.(txt|md)$/i.test(file.name)) {
    const reader = new FileReader()
    reader.onload = () => {
      pendingFiles.value.push({
        name: file.name,
        inputType: 'text',
        mimeType: 'text/plain',
        text: String(reader.result || ''),
      })
    }
    reader.readAsText(file)
  } else {
    message.error(`「${file.name}」不是支持的类型（截图或 txt/md）`)
  }
  return false
}

const handleDrop = (event: DragEvent) => {
  dragover.value = false
  for (const file of event.dataTransfer?.files || []) {
    handleFileSelected(file)
  }
}

const removePending = (index: number) => {
  pendingFiles.value.splice(index, 1)
}

const clearPending = () => {
  pendingFiles.value = []
  imagePreview.value = ''
}

const parsing = ref(false)
const retryingId = ref('')
const deletingId = ref('')
const savedTip = ref('')
const records = ref<GuideSourceRecord[]>([])
const router = useRouter()

// 参与规划的候选数（不含用户排除的），供"去规划"按钮文案
const activeCount = computed(() => candidates.value.filter((item) => item.user_choice !== 'excluded').length)

// 页面挂载时回显历史攻略：确认板聚合的候选跨会话/跨设备一致（服务端为准）
onUnmounted(() => {
  candidateMap?.destroy?.()
  candidateMap = null
})

onMounted(async () => {
  try {
    records.value = await listGuideSources()
  } catch {
    // 历史记录拉取失败不阻塞新导入
  }
})

// 聚合所有解析成功攻略的候选地点（后端已按 poi_id 跨来源去重合并）
const candidates = computed<CandidatePlace[]>(() =>
  records.value
    .filter((record) => record.parse_status === 'parsed' && record.result)
    .flatMap((record) => record.result!.candidates)
)

const canSubmit = computed(() => {
  if (inputType.value === 'text') return content.value.trim().length > 0
  if (inputType.value === 'url') return sourceUrl.value.trim().startsWith('http')
  return pendingFiles.value.length > 0
})

const parseButtonText = computed(() => {
  if (!parsing.value) {
    return inputType.value === 'image' && pendingFiles.value.length > 1
      ? `批量解析 ${pendingFiles.value.length} 个文件`
      : '解析攻略'
  }
  return batchTotal.value > 1 ? `正在解析 ${batchDone.value}/${batchTotal.value} 篇…` : '正在解析攻略…'
})

const parseProgressText = computed(() =>
  batchTotal.value > 1
    ? `多篇攻略逐篇提取地点并到高德核对，已处理 ${batchDone.value}/${batchTotal.value} 篇。`
    : '正在提取地点并到高德地图核对位置，一般需要 30 秒到 1 分钟。'
)

function retryHint(record: GuideSourceRecord): string {
  if (record.input_type === 'url') return record.result?.error_hint || '可点击"重试"再次尝试，或复制正文粘贴导入。'
  if (record.input_type === 'image') return record.result?.error_hint || '截图不会被保存，请重新选择后再试；也可粘贴正文导入。'
  return record.result?.error_hint || '粘贴的正文不会被保存，请重新粘贴后再试。'
}

async function handleParse() {
  parsing.value = true
  savedTip.value = ''
  try {
    // 批量模式：截图/txt 文件队列逐篇解析（每篇独立，失败不影响其他篇）
    if (inputType.value === 'image') {
      batchTotal.value = pendingFiles.value.length
      batchDone.value = 0
      let failed = 0
      for (const file of pendingFiles.value) {
        try {
          const record = await parseGuideSource({
            input_type: file.inputType,
            content: file.inputType === 'text' ? (file.text || '').trim() : undefined,
            image_data: file.inputType === 'image' ? file.base64 : undefined,
            image_mime: file.inputType === 'image' ? file.mimeType : undefined,
            title: file.name,
            city: city.value.trim() || undefined
          })
          records.value = [record, ...records.value.filter((item) => item.id !== record.id)]
          if (record.parse_status === 'failed') failed += 1
        } catch (error) {
          failed += 1
          message.warning(`${file.name}：${error instanceof Error ? error.message : '解析失败'}`)
        }
        batchDone.value += 1
      }
      const ok = batchTotal.value - failed
      if (ok > 0) {
        message.success(`批量解析完成：成功 ${ok} 篇${failed ? `，失败 ${failed} 篇（可单独重试或移除）` : ''}`)
        clearPending()
      } else {
        message.error('全部解析失败，请检查文件内容或稍后重试')
      }
      return
    }

    // 单篇：正文粘贴 / 链接
    const record = await parseGuideSource({
      input_type: inputType.value,
      content: inputType.value === 'text' ? content.value.trim() : undefined,
      source_url: inputType.value === 'url' ? sourceUrl.value.trim() : undefined,
      title: title.value.trim() || undefined,
      city: city.value.trim() || undefined
    })
    // 按 id 去重合并：历史回显与新解析可能包含同一篇
    records.value = [record, ...records.value.filter((item) => item.id !== record.id)]
    if (record.parse_status === 'parsed' && record.result?.candidates.length) {
      message.success(`解析完成：提取 ${record.result.candidates.length} 个候选地点`)
    } else if (record.parse_status === 'parsed') {
      message.info('攻略解析完成，但没有提取到可前往的地点')
    }
    // 解析成功后清空输入，方便连续导入多篇
    if (record.parse_status === 'parsed') {
      content.value = ''
      sourceUrl.value = ''
      title.value = ''
    }
  } catch (error) {
    message.error(error instanceof Error ? error.message : '攻略解析失败')
  } finally {
    parsing.value = false
    batchTotal.value = 0
    batchDone.value = 0
  }
}

async function handleRetry(record: GuideSourceRecord) {
  retryingId.value = record.id
  try {
    const updated = await retryGuideSource(record.id)
    records.value = records.value.map((item) => (item.id === updated.id ? updated : item))
    if (updated.parse_status === 'parsed') message.success('重试成功')
  } catch (error) {
    message.error(error instanceof Error ? error.message : '重试失败')
  } finally {
    retryingId.value = ''
  }
}

// ---- 候选地图分布：让用户看一眼这些地点在城市里的位置再决定 ----
const candidateMapOpen = ref(false)
const mappableCandidates = computed(() =>
  candidates.value.filter(item => item.location?.longitude && item.location?.latitude)
)
let candidateMap: any = null

const toggleCandidateMap = async () => {
  candidateMapOpen.value = !candidateMapOpen.value
  if (!candidateMapOpen.value) return
  await nextTick()
  try {
    const AMapLoader = (await import('@amap/amap-jsapi-loader')).default
    const AMap = await AMapLoader.load({
      key: import.meta.env.VITE_AMAP_WEB_JS_KEY,
      version: '2.0',
      plugins: ['AMap.Marker']
    })
    candidateMap?.destroy?.()
    candidateMap = new AMap.Map('candidate-map-container', { zoom: 11, viewMode: '2D' })
    const choiceColor: Record<string, string> = { required: '#ff6b4a', preferred: '#4776e6', optional: '#9aa7bb', excluded: '#d3dae4' }
    const markers = mappableCandidates.value.map(item =>
      new AMap.Marker({
        position: [item.location!.longitude, item.location!.latitude],
        title: item.canonical_name,
        anchor: 'center',
        content: `<div title="${item.canonical_name}" style="width:12px;height:12px;border-radius:50%;background:${choiceColor[item.user_choice] || '#9aa7bb'};border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.3);cursor:pointer;"></div>`,
      })
    )
    candidateMap.add(markers)
    candidateMap.setFitView(markers, false, [40, 40, 40, 40])
  } catch (error) {
    console.error('候选地图加载失败:', error)
    message.warning('地图暂时打不开，可稍后再试')
    candidateMapOpen.value = false
  }
}

// 候选选择持久化：本地快照（user_choice 覆盖层）+ 服务端回写（各攻略候选主数据）
// 回写失败不阻塞——本地覆盖层仍能保证本机规划行为正确
async function persistCandidates(silent = false) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(candidates.value))
  } catch (error) {
    if (!silent) {
      message.error('本地保存失败：浏览器存储不可用')
      console.error('候选地点保存失败:', error)
    }
  }

  // 按攻略分组回写服务端（PATCH /guide-sources/{id}/candidates/choices）
  const bySource = new Map<string, Record<string, string>>()
  for (const record of records.value) {
    if (record.parse_status !== 'parsed' || !record.result) continue
    for (const candidate of record.result.candidates) {
      bySource.set(record.id, {
        ...(bySource.get(record.id) || {}),
        [candidate.candidate_id]: candidate.user_choice
      })
    }
  }
  const writes = [...bySource.entries()].map(([sourceId, choices]) =>
    updateCandidateChoices(sourceId, choices).catch((error) => {
      console.error('候选选择云端回写失败:', sourceId, error)
    })
  )
  await Promise.allSettled(writes)
  if (!silent) {
    savedTip.value = `已保存 ${candidates.value.length} 个候选地点的选择`
    message.success('候选地点选择已保存')
  }
}

// 删除攻略：服务端删除记录与事实，前端同步移除（聚合候选随之消失）
async function handleDelete(record: GuideSourceRecord) {
  deletingId.value = record.id
  try {
    await deleteGuideSource(record.id)
    records.value = records.value.filter((item) => item.id !== record.id)
    persistCandidates()
    message.success('已删除该攻略')
  } catch (error) {
    message.error(error instanceof Error ? error.message : '删除失败')
  } finally {
    deletingId.value = ''
  }
}

// 确认完成后衔接规划：保存选择 -> 预填目的城市 -> 跳首页定位到表单
function goPlanWithCandidates() {
  persistCandidates(true)
  const destination = candidates.value.find((item) => item.city)?.city || ''
  if (destination) {
    sessionStorage.setItem('tripmind.guide_prefill_city', destination)
  }
  router.push('/')
}
</script>

<style scoped>
.guide-import {
  max-width: 900px;
  margin: 0 auto;
  padding: 40px 20px 60px;
  display: flex;
  flex-direction: column;
  gap: 24px;
}
.page-heading { display: flex; flex-direction: column; gap: 8px; }
.page-kicker { font-size: 11px; font-weight: 800; letter-spacing: 2px; color: #ff6b4a; }
.page-heading h1 { margin: 0; font-size: 30px; color: #14213d; }
.page-heading p { margin: 0; color: #778398; font-size: 14px; line-height: 1.7; max-width: 640px; }

.type-switch { margin-bottom: 18px; }
.input-form :deep(.ant-form-item) { margin-bottom: 16px; }
.parse-tip { margin: 10px 0 0; font-size: 12px; color: #8a96a8; }

/* 截图上传区 */
.image-upload-area {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  padding: 10px;
  border: 1px dashed #d6dee9;
  border-radius: 10px;
}
.image-upload-area.is-dragover { border-color: #ff6b4a; background: #fff8f5; }
.image-preview {
  max-width: 320px;
  max-height: 220px;
  border-radius: 10px;
  border: 1px solid #e6eaf1;
  object-fit: contain;
}
.pending-list {
  margin: 8px 0 0;
  padding: 0;
  list-style: none;
  font-size: 12px;
  color: #4a5a72;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.pending-list a { color: #ff6b4a; margin-left: 8px; }

.result-section { display: flex; flex-direction: column; gap: 14px; }
.section-title { margin: 8px 0 0; font-size: 18px; color: #14213d; }
.source-card, .board-card { border-radius: 14px; box-shadow: 0 6px 24px rgba(20, 33, 61, .06); }
.source-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.source-title { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.source-title strong { font-size: 14px; color: #14213d; }
.source-error { margin-top: 10px; }
.source-actions { display: flex; align-items: center; gap: 8px; }
.source-meta { margin: 8px 0 0; font-size: 12px; color: #8a96a8; }
.board-subtitle { margin-left: 10px; font-size: 12px; font-weight: 400; color: #8a96a8; }
.board-actions { margin-top: 16px; display: flex; align-items: center; gap: 12px; }
.saved-tip { font-size: 12px; color: #21a179; }

.candidate-map { margin-bottom: 14px; }
#candidate-map-container { height: 300px; border-radius: 12px; border: 1px solid #e6eaf1; }
.candidate-map-tip { margin: 8px 0 0; font-size: 11px; color: #97a2b4; }
</style>
