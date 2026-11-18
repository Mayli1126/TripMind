import { ref } from 'vue'
import { apiClient } from './api'

export interface ConversationSummary { confirmed: {text: string; quote: string}[]; rejected: {text: string; quote: string}[]; open_questions: {text: string; quote: string}[]; processed_count: number; method?: string; pending_notes?: string[] }
export interface Conversation { id: string; kind: 'planner' | 'trip'; trip_id: string | null; title: string; revision: number; updated_at: number; snapshot?: any; summary?: ConversationSummary | null }
export const listConversations = async (): Promise<Conversation[]> => (await apiClient.get('/api/library/conversations')).data
export const getConversation = async (id: string): Promise<Conversation> => (await apiClient.get(`/api/library/conversations/${id}`)).data
export const deleteConversation = async (id: string) => apiClient.delete(`/api/library/conversations/${id}`)

// 浏览器的 randomUUID 仅适用于安全上下文，当前 IP 部署使用 HTTP，需要兼容处理。
export function newConversationId(): string {
  if (typeof globalThis.crypto?.randomUUID === 'function') return globalThis.crypto.randomUUID()
  const bytes = new Uint8Array(16)
  globalThis.crypto.getRandomValues(bytes)
  bytes[6] = (bytes[6] & 15) | 64
  bytes[8] = (bytes[8] & 63) | 128
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

export function useConversation(kind: 'planner' | 'trip') {
  let id: string = newConversationId(), revision = 0, tail = Promise.resolve(), blocked = false, synced = false
  const syncMessage = ref('对话将保存到你的账号')
  function reset(record?: Conversation) { id = record?.id || newConversationId(); revision = record?.revision || 0; blocked = false; synced = !!record; syncMessage.value = record ? '已恢复账号中的对话' : '新对话' }
  async function save(snapshot: any, title: string, tripId: string | null = null) {
    const target = id, copy = JSON.parse(JSON.stringify(snapshot))
    tail = tail.then(async () => {
      if (target !== id || blocked) return
      syncMessage.value = '正在同步对话…'
      try {
        const result = await apiClient.put(`/api/library/conversations/${target}`, { kind, trip_id: tripId, title: title.slice(0, 100), snapshot: copy, revision })
        if (target === id) { revision = result.data.revision; synced = true; syncMessage.value = '对话已保存到账号' }
      } catch (error: any) {
        if (target === id) { synced = false; blocked = error.response?.status === 409; syncMessage.value = error.response?.data?.detail || '同步失败，当前内容仍在本页；可继续操作后重试' }
      }
    })
    await tail
  }
  return { save, reset, syncMessage, identity: () => ({ id, revision }), syncedId: () => synced && !blocked ? id : undefined }
}
