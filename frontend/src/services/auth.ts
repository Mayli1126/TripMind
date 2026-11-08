import { reactive } from 'vue'
import apiClient from './api'

export interface Account {
  id: string
  username: string
  role: 'user' | 'admin'
}

export const authState = reactive<{ user: Account | null; initialized: boolean }>({ user: null, initialized: false })
const ACCOUNT_KEY = 'tripmind.account'
const PRIVATE_KEYS = ['tripPlan', 'tripRequest', 'tripmind.active_job', 'tripmind.planner_chat.v1', 'tripmind.guide_prefill_city']
let initializing: Promise<void> | null = null

function clearPrivateData(clearCandidates = true) {
  for (const key of PRIVATE_KEYS) sessionStorage.removeItem(key)
  if (clearCandidates) localStorage.removeItem('tripmind.candidate_pool.v1')
}

function useAccount(user: Account | null) {
  const owner = sessionStorage.getItem('tripmind.storage_owner')
  if (owner !== user?.id) clearPrivateData(localStorage.getItem(ACCOUNT_KEY) !== user?.id)
  authState.user = user
  authState.initialized = true
  if (user) {
    sessionStorage.setItem('tripmind.storage_owner', user.id)
    localStorage.setItem(ACCOUNT_KEY, user.id)
  } else {
    sessionStorage.removeItem('tripmind.storage_owner')
    localStorage.removeItem(ACCOUNT_KEY)
  }
}

export async function initializeAuth() {
  if (authState.initialized) return
  if (!initializing) initializing = (async () => {
    try { useAccount((await apiClient.get<Account>('/api/auth/me')).data) }
    catch (error: any) {
      if (error.response?.status === 401) useAccount(null)
      else throw error
    }
  })().finally(() => { initializing = null })
  await initializing
}

export async function signIn(username: string, password: string, register: boolean) {
  const response = await apiClient.post<Account>(register ? '/api/auth/register' : '/api/auth/login', { username, password })
  useAccount(response.data)
}

export async function signOut() {
  try { await apiClient.post('/api/auth/logout') }
  catch (error: any) { if (error.response?.status !== 401) throw error }
  useAccount(null)
}

export function sessionExpired() {
  clearPrivateData()
  useAccount(null)
}

// Browser cookies are shared across tabs: never leave the previous user's data mounted.
window.addEventListener('storage', event => {
  if (event.key === ACCOUNT_KEY && event.newValue !== authState.user?.id) {
    clearPrivateData()
    sessionStorage.removeItem('tripmind.storage_owner')
    window.location.replace('/login')
  }
})
