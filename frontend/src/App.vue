<template>
  <a-config-provider :theme="theme">
  <div id="app">
    <header class="app-header">
      <router-link to="/" class="brand" aria-label="TripMind 首页">
        <span class="brand-mark">T</span>
        <span class="brand-text"><strong>TripMind</strong><small>智能旅行规划</small></span>
      </router-link>

      <nav class="main-nav" aria-label="主导航">
        <router-link to="/">首页</router-link>
        <router-link to="/guide">攻略导入</router-link>
        <router-link to="/trips">我的行程</router-link>
        <a href="/#planner">开始规划</a>
      </nav>

    </header>

    <main class="app-main"><router-view /></main>

    <footer class="app-footer">
      <div>
        <span class="footer-brand">TripMind</span>
        <span>让旅行计划有依据，也有温度。</span>
      </div>
      <div class="footer-meta">每个地点都在地图上核对 · 每笔费用都算得清</div>
    </footer>
  </div>
  </a-config-provider>
</template>

<script setup lang="ts">
import { ConfigProvider as AConfigProvider } from 'ant-design-vue'

const theme = {
  token: {
    colorPrimary: '#ff6b4a',
    colorInfo: '#4776e6',
    colorSuccess: '#21a179',
    borderRadius: 10,
    fontFamily: 'Inter, PingFang SC, Microsoft YaHei, sans-serif'
  }
}
</script>

<style>
:root {
  color: #14213d;
  background: #f8fafc;
  font-family: Inter, "PingFang SC", "Microsoft YaHei", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-synthesis: none;
  text-rendering: optimizeLegibility;
}

* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body { margin: 0; min-width: 320px; background: #f8fafc; }
button, input, textarea, select { font: inherit; }

#app { min-height: 100vh; }

.app-header {
  position: sticky;
  top: 0;
  z-index: 1000;
  height: 72px;
  padding: 0 max(28px, calc((100vw - 1240px) / 2));
  display: flex;
  align-items: center;
  gap: 44px;
  border-bottom: 1px solid rgba(220, 227, 235, .85);
  background: rgba(255, 255, 255, .88);
  backdrop-filter: blur(16px);
}

.brand { display: inline-flex; align-items: center; gap: 11px; color: #14213d; text-decoration: none; }
.brand-mark { width: 38px; height: 38px; display: grid; place-items: center; border-radius: 11px; color: white; background: #14213d; font-weight: 900; box-shadow: 5px 5px 0 #ff8b73; }
.brand-text { display: flex; flex-direction: column; line-height: 1.05; }
.brand-text strong { font-size: 17px; letter-spacing: -.3px; }
.brand-text small { margin-top: 5px; color: #8a96a8; font-size: 9px; letter-spacing: 1.2px; text-transform: uppercase; }

.main-nav { display: flex; gap: 30px; margin-left: auto; }
.main-nav a { position: relative; padding: 26px 0 23px; color: #667287; font-size: 13px; font-weight: 700; text-decoration: none; }
.main-nav a:hover, .main-nav .router-link-active { color: #14213d; }
.main-nav a:hover::after, .main-nav .router-link-active::after { content: ""; position: absolute; left: 0; right: 0; bottom: 17px; height: 2px; background: #ff6b4a; }

.header-status { display: flex; align-items: center; gap: 8px; padding: 8px 11px; border: 1px solid #dfeae5; border-radius: 999px; color: #39755e; background: #f4fbf8; font-size: 10px; font-weight: 800; letter-spacing: .7px; }
.header-status span { width: 7px; height: 7px; border-radius: 50%; background: #21a179; box-shadow: 0 0 0 4px rgba(33, 161, 121, .12); }

.app-main { min-height: calc(100vh - 150px); }

.app-footer { min-height: 94px; padding: 26px max(28px, calc((100vw - 1240px) / 2)); display: flex; align-items: center; justify-content: space-between; gap: 20px; color: #8490a2; background: #111d34; font-size: 12px; }
.app-footer > div:first-child { display: flex; flex-direction: column; gap: 5px; }
.footer-brand { color: white; font-size: 18px; font-weight: 900; }
.footer-meta { color: #697992; letter-spacing: .7px; }

@media (max-width: 720px) {
  .app-header { height: 64px; padding-inline: 18px; }
  .main-nav { display: none; }
  .header-status { margin-left: auto; }
  .app-footer { align-items: flex-start; flex-direction: column; padding-inline: 20px; }
}

@media (max-width: 420px) {
  .brand-text small { display: none; }
  .header-status { font-size: 0; padding: 10px; }
}
</style>
