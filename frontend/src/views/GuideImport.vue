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
          <a-form-item label="攻略截图" required>
            <div class="image-upload-area">
              <img v-if="imagePreview" :src="imagePreview" class="image-preview" alt="攻略截图预览" />
              <a-upload
                :before-upload="handleImageSelected"
                :show-upload-list="false"
                accept="image/png,image/jpeg,image/jpg,image/webp"
              >
                <a-button>{{ imagePreview ? '重新选择' : '选择截图（PNG/JPG/WebP，≤6MB）' }}</a-button>
              </a-upload>
              <a-button v-if="imagePreview" size="small" @click="clearImage">清除</a-button>
            </div>
            <p class="parse-tip">支持小红书/公众号等长图截图；AI 会识别图中的行程文字并到高德核对地点。</p>
          </a-form-item>
        </template>

        <a-form-item label="目的地城市（可选，帮助更准地核对地点）">
          <a-input v-model:value="city" placeholder="例如：北京" style="max-width: 280px" allow-clear />
        </a-form-item>

        <a-button type="primary" size="large" :loading="parsing" :disabled="!canSubmit" @click="handleParse">
          {{ parsing ? '正在解析攻略…' : '解析攻略' }}
        </a-button>
        <p v-if="parsing" class="parse-tip">正在提取地点并到高德地图核对位置，一般需要 30 秒到 1 分钟。</p>
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
          <span class="board-subtitle">选择哪些"必去"、哪些"不去"，后续规划会尊重你的选择</span>
        </template>
        <CandidatePlaceBoard :candidates="candidates" @change="() => persistCandidates(true)" />
        <div class="board-actions">
          <a-button
            type="primary"
            size="large"
            :disabled="!candidates.length"
            @click="goPlanWithCandidates"
          >用这 {{ activeCount }} 个候选去规划行程 →</a-button>
          <a-button @click="persistCandidates()">保存选择</a-button>
          <span v-if="savedTip" class="saved-tip">{{ savedTip }}</span>
        </div>
      </a-card>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
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
import { deleteGuideSource, listGuideSources, parseGuideSource, retryGuideSource } from '@/services/api'
import type { CandidatePlace, GuideInputType, GuideSourceRecord } from '@/types'

// 已确认候选项在本机的暂存键；阶段 2 候选池接入主规划图后由此读取
const STORAGE_KEY = 'tripmind.candidate_pool.v1'

const inputType = ref<GuideInputType>('text')
const content = ref('')
const sourceUrl = ref('')
const title = ref('')
const city = ref('')
// 截图导入：base64（不含 data: 前缀）+ 预览地址
const imageData = ref('')
const imageMime = ref('image/jpeg')
const imagePreview = ref('')

// 截图选择：读取文件转 base64（≤6MB），返回 false 阻止 antd 自动上传
const handleImageSelected = (file: File) => {
  if (file.size > 6 * 1024 * 1024) {
    message.error('截图不能超过 6MB，请压缩后重试')
    return false
  }
  const reader = new FileReader()
  reader.onload = () => {
    const result = String(reader.result || '')
    const commaIndex = result.indexOf(',')
    // data:image/png;base64,xxxx -> 拆出 mime 与纯 base64
    const header = result.slice(0, commaIndex)
    imageMime.value = header.match(/data:([^;]+)/)?.[1] || 'image/jpeg'
    imageData.value = result.slice(commaIndex + 1)
    imagePreview.value = result
  }
  reader.readAsDataURL(file)
  return false
}

const clearImage = () => {
  imageData.value = ''
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
  return imageData.value.length > 0
})

function retryHint(record: GuideSourceRecord): string {
  if (record.input_type === 'url') return record.result?.error_hint || '可点击"重试"再次尝试，或复制正文粘贴导入。'
  if (record.input_type === 'image') return record.result?.error_hint || '截图不会被保存，请重新选择后再试；也可粘贴正文导入。'
  return record.result?.error_hint || '粘贴的正文不会被保存，请重新粘贴后再试。'
}

async function handleParse() {
  parsing.value = true
  savedTip.value = ''
  try {
    const record = await parseGuideSource({
      input_type: inputType.value,
      content: inputType.value === 'text' ? content.value.trim() : undefined,
      source_url: inputType.value === 'url' ? sourceUrl.value.trim() : undefined,
      image_data: inputType.value === 'image' ? imageData.value : undefined,
      image_mime: inputType.value === 'image' ? imageMime.value : undefined,
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
      clearImage()
    }
  } catch (error) {
    message.error(error instanceof Error ? error.message : '攻略解析失败')
  } finally {
    parsing.value = false
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

// 候选选择持久化到本机：作为 user_choice 覆盖层，服务端聚合候选为主数据
function persistCandidates(silent = false) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(candidates.value))
    if (silent) return
    savedTip.value = `已保存 ${candidates.value.length} 个候选地点的选择`
    message.success('候选地点选择已保存')
  } catch (error) {
    if (!silent) {
      message.error('保存失败：浏览器本地存储不可用')
      console.error('候选地点保存失败:', error)
    }
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
.image-upload-area { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.image-preview {
  max-width: 320px;
  max-height: 220px;
  border-radius: 10px;
  border: 1px solid #e6eaf1;
  object-fit: contain;
}

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
</style>
