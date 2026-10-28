import axios from 'axios'
import type {
  CandidatePlace,
  GuideSourcePayload,
  GuideSourceRecord,
  PlanningJobResponse,
  PlanningProgressEvent,
  TripFormData,
  TripPlan,
  TripPlanResponse,
  TripRevisionRequest,
  SavedTripDetail,
  SavedTripSummary,
  UserPreferences,
  TripProgressResponse,
  TransportationRequest,
  TransportationResponse,
  WaitingInputPayload
} from '@/types'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'

const apiClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: 300000, // 5分钟超时；规划进度本身通过SSE实时返回
  headers: {
    'Content-Type': 'application/json'
  }
})

// 请求拦截器
apiClient.interceptors.request.use(
  (config) => {
    console.log('发送请求:', config.method?.toUpperCase(), config.url)
    return config
  },
  (error) => {
    console.error('请求错误:', error)
    return Promise.reject(error)
  }
)

// 响应拦截器
apiClient.interceptors.response.use(
  (response) => {
    console.log('收到响应:', response.status, response.config.url)
    return response
  },
  (error) => {
    console.error('响应错误:', error.response?.status, error.message)
    return Promise.reject(error)
  }
)

/**
 * 生成旅行计划
 */
export async function generateTripPlan(formData: TripFormData): Promise<TripPlanResponse> {
  try {
    const response = await apiClient.post<TripPlanResponse>('/api/trip/plan', formData)
    return response.data
  } catch (error: any) {
    console.error('生成旅行计划失败:', error)
    throw new Error(error.response?.data?.detail || error.message || '生成旅行计划失败')
  }
}

async function createPlanningJob(formData: TripFormData): Promise<PlanningJobResponse> {
  const response = await apiClient.post<PlanningJobResponse>('/api/trip/jobs', formData)
  return response.data
}

async function createRevisionJob(request: TripRevisionRequest): Promise<PlanningJobResponse> {
  const response = await apiClient.post<PlanningJobResponse>('/api/trip/revision-jobs', request)
  return response.data
}

// 查询任务状态（含 waiting_input 载荷）——恢复监听与决策卡片拉详情共用
export async function getPlanningJob(jobId: string): Promise<PlanningJobResponse> {
  const response = await apiClient.get<PlanningJobResponse>(`/api/trip/jobs/${jobId}`)
  return response.data
}

// 恢复等待用户确认的任务（interrupt）：Worker 从 Checkpointer 断点继续
export async function resumePlanningJob(jobId: string, resumeValue: string): Promise<PlanningJobResponse> {
  const response = await apiClient.post<PlanningJobResponse>(`/api/trip/jobs/${jobId}/resume`, {
    resume_value: resumeValue
  })
  return response.data
}

function waitForPlanningJob(
  jobId: string,
  onProgress: (event: PlanningProgressEvent) => void,
  onWaitingInput?: (jobId: string, payload: WaitingInputPayload) => void
): Promise<TripPlan> & { cancel: () => void } {
  let settled = false
  let disconnectCount = 0
  let rejectFn: (reason: unknown) => void = () => {}
  const events = new EventSource(`${API_BASE_URL}/api/trip/jobs/${jobId}/events`)

  const promise = new Promise<TripPlan>((resolve, reject) => {
    rejectFn = reject

    const finish = async (status: PlanningJobResponse['status']) => {
      if (settled) return
      try {
        const job = await getPlanningJob(jobId)
        if (status === 'completed' && job.data) {
          settled = true
          events.close()
          resolve(job.data)
        } else if (status === 'failed') {
          settled = true
          events.close()
          reject(new Error(job.error || job.message || '规划失败'))
        }
      } catch (error) {
        settled = true
        events.close()
        reject(error)
      }
    }

    events.addEventListener('progress', (rawEvent) => {
      disconnectCount = 0
      const event = JSON.parse((rawEvent as MessageEvent).data) as PlanningProgressEvent
      onProgress(event)
      if (event.status === 'waiting_input') {
        // interrupt 事件只带问题文本，完整载荷（选项/金额）从任务详情拉取；
        // 不关闭 EventSource，用户 resume 后继续在此连接上收 completed
        void getPlanningJob(jobId)
          .then((job) => {
            if (!settled && job.waiting_input) onWaitingInput?.(jobId, job.waiting_input)
          })
          .catch(() => {/* 详情拉取失败不打断监听，下次事件会再触发 */})
        return
      }
      if (event.status === 'completed' || event.status === 'failed') {
        void finish(event.status)
      }
    })

    events.onerror = async () => {
      if (settled) return
      disconnectCount += 1
      try {
        const job = await getPlanningJob(jobId)
        if (job.status === 'completed' && job.data) {
          settled = true
          events.close()
          resolve(job.data)
        } else if (job.status === 'failed') {
          settled = true
          events.close()
          reject(new Error(job.error || job.message || '规划失败'))
        } else if (disconnectCount >= 3) {
          settled = true
          events.close()
          reject(new Error('进度连接多次中断，请重新提交'))
        } else {
          // EventSource 会按服务端 retry 指令自动重连，并携带 Last-Event-ID 续传。
          return
        }
      } catch (error) {
        if (disconnectCount >= 3) {
          settled = true
          events.close()
          reject(error)
        }
      }
    }
  }) as Promise<TripPlan> & { cancel: () => void }

  // 组件卸载时调用：关连接、置终态，防止后台连接泄漏与迟到回调（幽灵导航）
  promise.cancel = () => {
    if (settled) return
    settled = true
    events.close()
    rejectFn(new DOMException('页面已离开，任务监听已取消', 'AbortError'))
  }
  return promise
}

// 恢复监听一个已存在的任务（页面刷新后接续进度/等待确认/完成事件），
// SSE 自动携带 Last-Event-ID 续传，不重算后端任何步骤
export function waitForExistingJob(
  jobId: string,
  onProgress: (event: PlanningProgressEvent) => void,
  onWaitingInput?: (jobId: string, payload: WaitingInputPayload) => void
): Promise<TripPlan> & { cancel: () => void } {
  return waitForPlanningJob(jobId, onProgress, onWaitingInput)
}

export function generateTripPlanWithProgress(
  formData: TripFormData,
  onProgress: (event: PlanningProgressEvent) => void,
  onWaitingInput?: (jobId: string, payload: WaitingInputPayload) => void,
  onJobCreated?: (jobId: string) => void
): Promise<TripPlan> & { cancel: () => void } {
  // 任务 ID 创建后才建立 SSE 监听；cancel 需穿透到最终连接
  let cancelInner: (() => void) | null = null
  const promise = createPlanningJob(formData)
    .then((job) => {
      onJobCreated?.(job.job_id)  // 供前端记录 active_job，页面刷新后可恢复监听
      const waiter = waitForPlanningJob(job.job_id, onProgress, onWaitingInput)
      cancelInner = waiter.cancel
      return waiter
    })
  const wrapped = promise as Promise<TripPlan> & { cancel: () => void }
  wrapped.cancel = () => cancelInner?.()
  return wrapped
}

export function reviseTripWithProgress(
  request: TripRevisionRequest,
  onProgress: (event: PlanningProgressEvent) => void,
  onWaitingInput?: (jobId: string, payload: WaitingInputPayload) => void
): Promise<TripPlan> & { cancel: () => void } {
  let cancelInner: (() => void) | null = null
  const promise = createRevisionJob(request)
    .then((job) => {
      const waiter = waitForPlanningJob(job.job_id, onProgress, onWaitingInput)
      cancelInner = waiter.cancel
      return waiter
    })
  const wrapped = promise as Promise<TripPlan> & { cancel: () => void }
  wrapped.cancel = () => cancelInner?.()
  return wrapped
}

export async function createSavedTrip(request: TripFormData, plan: TripPlan): Promise<SavedTripDetail> {
  const response = await apiClient.post<SavedTripDetail>('/api/library/trips', { request, plan })
  return response.data
}

export async function listSavedTrips(): Promise<SavedTripSummary[]> {
  const response = await apiClient.get<SavedTripSummary[]>('/api/library/trips')
  return response.data
}

export async function getSavedTrip(tripId: string): Promise<SavedTripDetail> {
  const response = await apiClient.get<SavedTripDetail>(`/api/library/trips/${tripId}`)
  return response.data
}

export async function saveTripVersion(
  tripId: string,
  plan: TripPlan,
  changeNote: string
): Promise<SavedTripDetail> {
  const response = await apiClient.post<SavedTripDetail>(`/api/library/trips/${tripId}/versions`, {
    plan,
    change_note: changeNote
  })
  return response.data
}

export async function restoreTripVersion(tripId: string, version: number): Promise<SavedTripDetail> {
  const response = await apiClient.post<SavedTripDetail>(`/api/library/trips/${tripId}/versions/${version}/restore`)
  return response.data
}

export async function deleteSavedTrip(tripId: string): Promise<void> {
  await apiClient.delete(`/api/library/trips/${tripId}`)
}

export async function getUserPreferences(): Promise<UserPreferences> {
  const response = await apiClient.get<UserPreferences>('/api/library/preferences')
  return response.data
}

export async function saveUserPreferences(preferences: UserPreferences): Promise<UserPreferences> {
  const response = await apiClient.put<UserPreferences>('/api/library/preferences', preferences)
  return response.data
}

export async function getTripProgress(tripId: string): Promise<TripProgressResponse> {
  const response = await apiClient.get<TripProgressResponse>(`/api/library/trips/${tripId}/progress`)
  return response.data
}

export async function updateTripProgress(
  tripId: string,
  itemKey: string,
  completed: boolean
): Promise<TripProgressResponse> {
  const response = await apiClient.put<TripProgressResponse>(
    `/api/library/trips/${tripId}/progress/${encodeURIComponent(itemKey)}`,
    { completed }
  )
  return response.data
}

/**
 * 健康检查
 */
export async function healthCheck(): Promise<any> {
  try {
    const response = await apiClient.get('/health')
    return response.data
  } catch (error: any) {
    console.error('健康检查失败:', error)
    throw new Error(error.message || '健康检查失败')
  }
}

/**
 * 查询通勤费用
 */
export async function queryTransportation(request: TransportationRequest): Promise<TransportationResponse> {
  try {
    const response = await apiClient.post<TransportationResponse>('/api/trip/transportation', request)
    return response.data
  } catch (error: any) {
    console.error('查询通勤费用失败:', error)
    throw new Error(error.response?.data?.detail || error.message || '查询通勤费用失败')
  }
}

// ---------------------------------------------------------------------------
// 攻略导入（提交后同步解析，单篇一般 1 分钟内；超时给足 5 分钟）
// ---------------------------------------------------------------------------

export async function parseGuideSource(payload: GuideSourcePayload): Promise<GuideSourceRecord> {
  try {
    const response = await apiClient.post<GuideSourceRecord>('/api/guide-sources', payload)
    return response.data
  } catch (error: any) {
    console.error('攻略解析失败:', error)
    throw new Error(error.response?.data?.detail || error.message || '攻略解析失败')
  }
}

export async function listGuideSources(): Promise<GuideSourceRecord[]> {
  const response = await apiClient.get<GuideSourceRecord[]>('/api/guide-sources')
  return response.data
}

export async function getGuideSource(sourceId: string): Promise<GuideSourceRecord> {
  const response = await apiClient.get<GuideSourceRecord>(`/api/guide-sources/${sourceId}`)
  return response.data
}

export async function retryGuideSource(sourceId: string): Promise<GuideSourceRecord> {
  const response = await apiClient.post<GuideSourceRecord>(`/api/guide-sources/${sourceId}/retry`)
  return response.data
}

// 删除攻略及其候选与事实
export async function deleteGuideSource(sourceId: string): Promise<void> {
  await apiClient.delete(`/api/guide-sources/${sourceId}`)
}

// 候选四档选择回写服务端（user_choice 跨设备一致）
export async function updateCandidateChoices(
  sourceId: string,
  choices: Record<string, string>
): Promise<void> {
  await apiClient.patch(`/api/guide-sources/${sourceId}/candidates/choices`, { choices })
}

// 跨来源聚合候选池（候选云端化）：后端已按 poi_id 合并去重
export async function fetchGuideCandidates(city?: string): Promise<CandidatePlace[]> {
  const response = await apiClient.get<CandidatePlace[]>('/api/guide-sources/candidates', {
    params: city ? { city } : undefined
  })
  return response.data
}

export default apiClient
