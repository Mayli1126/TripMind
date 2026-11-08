import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import 'ant-design-vue/dist/reset.css'
import App from './App.vue'
import { authState, initializeAuth, sessionExpired } from './services/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/conversations', component: () => import('./views/ConversationHistory.vue') },
    { path: '/memories', component: () => import('./views/TravelMemories.vue') },
    { path: '/login', name: 'Login', component: () => import('./views/Login.vue') },
    { path: '/admin', name: 'Admin', component: () => import('./views/Admin.vue') },
    {
      path: '/',
      name: 'Home',
      component: () => import('./views/ChatHome.vue')
    },
    {
      path: '/guide',
      name: 'GuideImport',
      component: () => import('./views/GuideImport.vue')
    },
    {
      path: '/result',
      name: 'Result',
      component: () => import('./views/Result.vue')
    },
    {
      path: '/trips',
      name: 'Trips',
      component: () => import('./views/MyTrips.vue')
    },
    {
      path: '/trips/:id',
      name: 'SavedTrip',
      component: () => import('./views/Result.vue')
    },
    {
      path: '/trips/:id/today',
      name: 'TodayPlan',
      component: () => import('./views/TodayPlan.vue')
    },
    {
      path: '/trips/:id/assistant',
      name: 'TripAssistant',
      component: () => import('./views/TripAssistant.vue')
    }
  ]
})

router.beforeEach(async to => {
  try { await initializeAuth() }
  catch { return to.name === 'Login' ? true : { name: 'Login', query: { redirect: to.fullPath } } }
  if (!authState.user && to.name !== 'Login') return { name: 'Login', query: { redirect: to.fullPath } }
  if (authState.user && to.name === 'Login') return { name: 'Home' }
  if (to.name === 'Admin' && authState.user?.role !== 'admin') return { name: 'Home' }
})

window.addEventListener('tripmind:unauthorized', () => {
  sessionExpired()
  if (router.currentRoute.value.name !== 'Login') void router.replace({ name: 'Login', query: { redirect: router.currentRoute.value.fullPath } })
})

const app = createApp(App)

app.use(router)
app.mount('#app')
