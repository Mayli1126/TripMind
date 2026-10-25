import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import 'ant-design-vue/dist/reset.css'
import App from './App.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      name: 'Home',
      component: () => import('./views/Home.vue')
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
    }
  ]
})

const app = createApp(App)

app.use(router)
app.mount('#app')
