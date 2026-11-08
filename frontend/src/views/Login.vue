<template>
  <section class="login-page">
    <div class="login-story"><span class="eyebrow">TRIPMIND · 属于你的旅行</span><h1>旅行有很多种样子，<br />你的计划只属于你。</h1><p>登录后继续对话、收藏灵感，<br />在不同设备间找回自己的旅行计划。</p><div class="story-stamp"><span>✧</span><small>下一站，从这里开始。</small></div></div>
    <form class="login-card" @submit.prevent="submit">
      <div class="login-tabs"><button type="button" :class="{ active: !register }" @click="register = false">欢迎回来</button><button type="button" :class="{ active: register }" @click="register = true">创建账号</button></div>
      <h2>{{ register ? '开启你的旅行空间' : '继续你的旅行故事' }}</h2><p class="subtitle">{{ register ? '你的行程、攻略和偏好将独立保存。' : '登录 TripMind，找回所有旅行灵感。' }}</p>
      <label>用户名<input v-model.trim="username" required minlength="3" maxlength="32" pattern="[A-Za-z0-9_\-]+" autocomplete="username" placeholder="3–32 位字母、数字、下划线" /></label>
      <label>密码<input v-model="password" required type="password" minlength="8" maxlength="128" :autocomplete="register ? 'new-password' : 'current-password'" placeholder="至少 8 位" /></label>
      <label v-if="register">确认密码<input v-model="confirmation" required type="password" minlength="8" maxlength="128" autocomplete="new-password" placeholder="再输入一次密码" /></label>
      <p v-if="error" class="login-error" role="alert">{{ error }}</p>
      <div v-if="adminReady === false" class="setup-note"><strong>首次使用：先创建管理员</strong><p>请在后端目录激活虚拟环境后运行：</p><code>python -m app.services.auth_service create-admin --username admin</code><p>密码在终端输入，不会出现在网页或日志里。创建好后直接登录。</p><button type="button" @click="loadStatus">我已创建，重新检查</button></div>
      <button class="submit-login" type="submit" :disabled="busy || adminReady === false">{{ busy ? '正在进入…' : register ? '创建账号并开始旅行 →' : '登录 →' }}</button>
      <small class="privacy-note">普通账号只可访问自己的记录。管理员在管理中心查看全部记录。</small>
    </form>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import apiClient from '@/services/api'
import { signIn } from '@/services/auth'

const router = useRouter()
const route = useRoute()
const register = ref(false)
const username = ref('')
const password = ref('')
const confirmation = ref('')
const busy = ref(false)
const error = ref('')
const adminReady = ref<boolean | null>(null)

async function loadStatus() {
  try { adminReady.value = (await apiClient.get('/api/auth/status')).data.admin_ready }
  catch { error.value = '暂时无法连接服务，请确认后端地址和服务状态。' }
}

async function submit() {
  if (busy.value) return
  if (register.value && password.value !== confirmation.value) { error.value = '两次密码不一致，请再确认一下。'; return }
  error.value = ''
  busy.value = true
  try {
    await signIn(username.value, password.value, register.value)
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
    await router.replace(redirect.startsWith('/') && !redirect.startsWith('//') && !redirect.startsWith('/login') ? redirect : '/')
  } catch (cause: any) {
    const detail = cause.response?.data?.detail
    error.value = typeof detail === 'string' ? detail : '暂时没能登录，请检查输入或稍后重试。'
  } finally { busy.value = false }
}

onMounted(loadStatus)
</script>

<style scoped>
.login-page { min-height: calc(100dvh - 166px); padding: 70px max(28px,calc((100vw - 1100px)/2)); display: grid; grid-template-columns: 1.1fr 1fr; gap: 90px; align-items: center; background: radial-gradient(ellipse at 5% 0,#d7f4fc,transparent 65%),radial-gradient(ellipse at 100% 0,#e4edfd,transparent 65%),#fff; }
.eyebrow { font-size: 10px; letter-spacing: 2px; color: #76939d; }
.login-story h1 { font-size: clamp(28px,3vw,42px); letter-spacing: -1.5px; line-height: 1.6; color: #233a43; margin: 24px 0; }
.login-story > p { color: #879da6; font-size: 14px; line-height: 2; }
.story-stamp { display: flex; align-items: center; gap: 16px; color: #83b9bf; margin-top: 60px; }.story-stamp span { font-size: 46px; }.story-stamp small { font-size: 11px; letter-spacing: 1px; }
.login-card { background: #fff; border-radius: 28px; padding: 34px; box-shadow: 0 20px 80px #29495310; border: 1px solid #f0f5f5; }
.login-tabs { display: flex; gap: 18px; margin-bottom: 27px; }.login-tabs button { padding: 0 0 10px; border: 0; border-bottom: 2px solid transparent; background: none; color: #a4b2b8; cursor: pointer; font-size: 12px; }.login-tabs .active { border-color: #4eafb9; color: #365c68; font-weight: 700; }
h2 { font-size: 22px; color: #29444e; margin: 0 0 10px; }.subtitle { color: #95a6ae; font-size: 12px; margin: 0 0 28px; }
label { display: flex; flex-direction: column; gap: 10px; margin: 18px 0; font-size: 11px; color: #6b8591; }input { width: 100%; border: 1px solid #e2eaed; border-radius: 12px; padding: 13px 14px; color: #29444e; outline-color: #61b7c1; font-size: 13px; }input::placeholder { color: #b2bfc5; }
.submit-login { width: 100%; margin-top: 12px; padding: 14px; border: 0; border-radius: 13px; background: #213b45; color: white; font-size: 13px; cursor: pointer; }.submit-login:disabled { opacity: .5; cursor: default; }.privacy-note { display: block; color: #a4b2b8; font-size: 10px; line-height: 1.9; margin-top: 20px; }.login-error { font-size: 12px; color: #ae654c; line-height: 1.8; }.setup-note { padding: 14px; border-radius: 12px; background: #f1f8f9; color: #71909c; font-size: 11px; line-height: 1.8; }.setup-note p { margin: 5px 0; }.setup-note code { overflow-wrap: anywhere; }.setup-note button { border: 0; background: none; padding: 8px 0 0; color: #348f9e; cursor: pointer; }
@media(max-width:800px) { .login-page { padding: 35px 20px; grid-template-columns: 1fr; gap: 28px; }.login-story { text-align: center; }.login-story h1 { margin: 14px 0; font-size: 27px; }.login-story > p,.story-stamp { display: none; }.login-card { width: 100%; max-width: 440px; margin: auto; padding: 27px; } }
</style>
