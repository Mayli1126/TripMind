<template>
  <div class="intro-wrap">
    <section class="hero-section">
      <div class="hero-copy">
        <div class="eyebrow"><span class="pulse-dot"></span> AI TRAVEL PLANNER · 可解释旅行规划</div>
        <h1>把一句旅行想法，<br><span>变成真正能出发的行程</span></h1>
        <p class="hero-description">
          输入目的地和偏好， TripMind 会核对景点、天气、酒店与交通信息，在预算和时间约束内
          排出能真正出发的行程：每个地点都有真实位置，每笔费用都算得清。
        </p>
        <div class="hero-actions">
          <a href="#planner" class="primary-cta">开始规划 <span>↗</span></a>
          <a href="#how-it-works" class="secondary-cta">了解工作方式 <span>↓</span></a>
        </div>
        <div class="trust-row">
          <span>✓ 多源信息核对</span>
          <span>✓ 预算可解释</span>
          <span>✓ 查不到就明说</span>
        </div>
      </div>

      <div class="agent-visual" aria-label="旅行智能规划工作示意图">
        <div class="visual-glow"></div>
        <div class="orbit orbit-one"></div>
        <div class="orbit orbit-two"></div>
        <div class="agent-core">
          <div class="core-icon">✦</div>
          <strong>TripMind</strong>
          <small>智能规划</small>
        </div>
        <div class="agent-node node-poi"><i>⌖</i><span>景点检索</span><small>地图核对</small></div>
        <div class="agent-node node-weather"><i>☀</i><span>天气感知</span><small>天气</small></div>
        <div class="agent-node node-budget"><i>¥</i><span>预算计算</span><small>预算</small></div>
        <div class="agent-node node-route"><i>↝</i><span>路线优化</span><small>路线</small></div>
        <div class="route-line line-one"></div>
        <div class="route-line line-two"></div>
      </div>
    </section>

    <section class="signal-strip" aria-label="核心能力指标">
      <div><strong>4</strong><span>类出行信息一次核对</span></div>
      <div><strong>30秒</strong><span>平均生成一份行程</span></div>
      <div><strong>100%</strong><span>费用逐项算得清</span></div>
      <div><strong>可放心</strong><span>每个地点都在地图上</span></div>
    </section>

    <section id="how-it-works" class="capability-section">
      <div class="section-kicker">WHY TRIPMIND</div>
      <div class="section-heading">
        <h2>不只是生成一段好看的文字</h2>
        <p>从需求理解到结果校验，每一步都有明确职责与质量边界。</p>
      </div>
      <div class="capability-grid">
        <article class="capability-card coral">
          <div class="card-index">01</div>
          <div class="capability-icon">◎</div>
          <h3>理解真实约束</h3>
          <p>同时考虑日期、人数、预算、住宿、旅行节奏和自然语言补充要求。</p>
          <span>Intent & Constraints →</span>
        </article>
        <article class="capability-card blue">
          <div class="card-index">02</div>
          <div class="capability-icon">⌁</div>
          <h3>规划后再次验证</h3>
          <p>检查每日景点、三餐、日期连续性与预算，结构性失败最多自动修复一次。</p>
          <span>Validate & Replan →</span>
        </article>
        <article class="capability-card green">
          <div class="card-index">03</div>
          <div class="capability-icon">◇</div>
          <h3>解释数据与结果</h3>
          <p>区分实时检索、模型生成、估算和静态降级，让用户知道哪些信息需要核实。</p>
          <span>Evidence & Quality →</span>
        </article>
      </div>
    </section>
  </div>
</template>

<style scoped>
.intro-wrap {
  --ink: #14213d;
  --muted: #64748b;
  --coral: #ff6b4a;
  max-width: 1240px;
  margin: 0 auto;
  padding: 72px 24px 24px;
}

.hero-section {
  min-height: 560px;
  display: grid;
  grid-template-columns: 1.18fr .82fr;
  gap: 38px;
  align-items: center;
}

.eyebrow, .section-kicker {
  color: #e65334;
  font-size: 12px;
  font-weight: 800;
  letter-spacing: 1.8px;
}

.eyebrow { display: flex; align-items: center; gap: 10px; }
.pulse-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--coral); box-shadow: 0 0 0 7px rgba(255,107,74,.12); }

h1 {
  margin: 24px 0;
  color: var(--ink);
  font-size: clamp(44px, 4.8vw, 62px);
  line-height: 1.08;
  letter-spacing: -3px;
  font-weight: 900;
}

h1 span { color: var(--coral); white-space: nowrap; }
.hero-description { max-width: 650px; color: var(--muted); font-size: 17px; line-height: 1.9; }
.hero-actions { display: flex; gap: 14px; margin: 34px 0 24px; }
.hero-actions a { display: inline-flex; align-items: center; gap: 14px; padding: 15px 22px; border-radius: 12px; font-weight: 700; text-decoration: none; transition: .25s ease; }
.primary-cta { color: white; background: var(--ink); box-shadow: 0 12px 30px rgba(20,33,61,.2); }
.primary-cta:hover { color: white; background: var(--coral); transform: translateY(-2px); }
.secondary-cta { color: var(--ink); border: 1px solid #dbe3ec; background: white; }
.secondary-cta:hover { color: var(--coral); border-color: #ffb4a4; }
.trust-row { display: flex; flex-wrap: wrap; gap: 22px; color: #728096; font-size: 13px; }

.agent-visual { position: relative; height: 480px; isolation: isolate; }
.visual-glow { position: absolute; width: 360px; height: 360px; left: 50%; top: 50%; transform: translate(-50%,-50%); border-radius: 50%; background: radial-gradient(circle, rgba(255,107,74,.2), rgba(255,255,255,0) 68%); filter: blur(5px); }
.orbit { position: absolute; left: 50%; top: 50%; border: 1px dashed rgba(35,55,85,.2); border-radius: 50%; transform: translate(-50%,-50%) rotate(-10deg); }
.orbit-one { width: 320px; height: 320px; }
.orbit-two { width: 440px; height: 250px; transform: translate(-50%,-50%) rotate(22deg); }
.agent-core { position: absolute; left: 50%; top: 50%; width: 160px; height: 160px; transform: translate(-50%,-50%); border-radius: 42px; display: flex; flex-direction: column; align-items: center; justify-content: center; color: white; background: linear-gradient(145deg, #172643, #0c1528); box-shadow: 0 28px 60px rgba(15,31,57,.28), inset 0 1px rgba(255,255,255,.2); z-index: 3; }
.core-icon { color: #ff8a70; font-size: 34px; margin-bottom: 7px; }
.agent-core strong { font-size: 20px; }
.agent-core small { margin-top: 5px; color: #a9b7cd; }
.agent-node { position: absolute; width: 132px; padding: 14px 16px; border: 1px solid rgba(219,227,236,.9); border-radius: 16px; background: rgba(255,255,255,.92); box-shadow: 0 18px 45px rgba(49,65,91,.13); backdrop-filter: blur(12px); z-index: 2; animation: hover 5s ease-in-out infinite; }
.agent-node i { float: left; width: 34px; height: 34px; margin-right: 9px; border-radius: 10px; display: grid; place-items: center; font-style: normal; font-weight: 800; color: white; background: var(--coral); }
.agent-node span { display: block; color: var(--ink); font-size: 13px; font-weight: 800; }
.agent-node small { color: #8a97a9; font-size: 10px; }
.node-poi { left: 0; top: 78px; }
.node-weather { right: 4px; top: 54px; animation-delay: -1.4s; }
.node-budget { left: 10px; bottom: 70px; animation-delay: -2.8s; }
.node-route { right: 0; bottom: 88px; animation-delay: -4s; }
.node-weather i { background: #f2ae35; }
.node-budget i { background: #21a179; }
.node-route i { background: #4776e6; }
.route-line { position: absolute; height: 2px; border-top: 2px dotted rgba(255,107,74,.35); z-index: 1; }
.line-one { width: 280px; left: 90px; top: 155px; transform: rotate(15deg); }
.line-two { width: 300px; right: 70px; bottom: 155px; transform: rotate(-15deg); }

.signal-strip { margin: 26px 0 104px; display: grid; grid-template-columns: repeat(4, 1fr); border: 1px solid #e6ebf1; border-radius: 18px; background: white; box-shadow: 0 20px 50px rgba(37,55,82,.07); }
.signal-strip div { padding: 22px 26px; border-right: 1px solid #edf0f4; }
.signal-strip div:last-child { border: 0; }
.signal-strip strong { display: block; color: var(--ink); font-size: 24px; }
.signal-strip span { color: #7d899a; font-size: 12px; }

.capability-section { scroll-margin-top: 90px; padding-bottom: 84px; }
.section-heading { display: flex; align-items: end; justify-content: space-between; margin: 12px 0 34px; }
.section-heading h2 { margin: 0; color: var(--ink); font-size: 36px; letter-spacing: -1px; }
.section-heading p { max-width: 430px; margin: 0; color: var(--muted); line-height: 1.7; }
.capability-grid { display: grid; grid-template-columns: repeat(3,1fr); gap: 20px; }
.capability-card { position: relative; overflow: hidden; min-height: 250px; padding: 30px; border: 1px solid #e7ecf2; border-radius: 20px; background: white; transition: .25s ease; }
.capability-card:hover { transform: translateY(-5px); box-shadow: 0 24px 55px rgba(39,58,86,.1); }
.card-index { position: absolute; right: 20px; top: 12px; color: #edf1f5; font-size: 52px; font-weight: 900; }
.capability-icon { width: 48px; height: 48px; margin-bottom: 36px; display: grid; place-items: center; border-radius: 14px; color: white; background: var(--coral); font-size: 24px; }
.capability-card.blue .capability-icon { background: #4776e6; }
.capability-card.green .capability-icon { background: #21a179; }
.capability-card h3 { color: var(--ink); font-size: 20px; }
.capability-card p { min-height: 66px; color: var(--muted); line-height: 1.7; }
.capability-card > span { color: #8793a4; font-size: 12px; font-weight: 700; letter-spacing: .6px; }

@keyframes hover { 0%,100% { transform: translateY(0); } 50% { transform: translateY(-8px); } }

@media (max-width: 900px) {
  .intro-wrap { padding-top: 38px; }
  .hero-section { min-height: auto; grid-template-columns: 1fr; gap: 24px; text-align: center; }
  h1 span { white-space: normal; }
  .eyebrow, .trust-row, .hero-actions { justify-content: center; }
  .hero-description { margin-inline: auto; }
  .agent-visual { transform: scale(.9); margin-inline: -20px; }
  .signal-strip { grid-template-columns: repeat(2,1fr); margin-bottom: 72px; }
  .signal-strip div:nth-child(2) { border-right: 0; }
  .signal-strip div:nth-child(-n+2) { border-bottom: 1px solid #edf0f4; }
  .section-heading { display: block; }
  .section-heading p { margin-top: 12px; }
  .capability-grid { grid-template-columns: 1fr; }
}

@media (max-width: 540px) {
  h1 { font-size: 40px; letter-spacing: -2px; }
  .hero-actions { flex-direction: column; }
  .agent-visual { height: 390px; transform: scale(.74); margin: -35px -75px; }
  .signal-strip { grid-template-columns: 1fr; }
  .signal-strip div { border-right: 0; border-bottom: 1px solid #edf0f4; }
  .section-heading h2 { font-size: 30px; }
}
</style>
