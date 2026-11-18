// 可选的浏览器冒烟检查，需要 NODE_PATH 中可找到 Playwright，不使用真实账号或接口。
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { chromium } = require('playwright')

async function main() {
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge' })
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } })
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/api/**', route => route.fulfill({
      json: route.request().url().includes('/auth/me') ? { id: 'layout-fixture', username: 'preview', role: 'user' } : [],
      headers: { 'Access-Control-Allow-Origin': 'http://127.0.0.1:5175', 'Access-Control-Allow-Credentials': 'true' }
    }))
    // 单独验证地图加载失败，不消耗真实地图密钥配额。
    let mapRequests = 0
    await page.route('**/webapi.amap.com/**', route => { mapRequests++; return route.abort() })
    const image = 'data:image/svg+xml,' + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="600" height="300"><rect width="600" height="300" fill="#b7d8ca"/><path d="M0 210 Q150 70 300 220T600 170V300H0" fill="#5f9984"/><circle cx="440" cy="65" r="30" fill="#f6e2aa"/></svg>')
    const attraction = (name, i) => ({ name, address: '青岛市 · 测试地址', description: '沿着海岸慢慢散步，感受老城的建筑与海风。这是仅用于布局验证的示例内容。', visit_duration: 90, ticket_price: 0, opening_time: '09:00', closing_time: '18:00', location: { longitude: 120.3 + i * .01, latitude: 36.05 }, image_url: image })
    const plan = { city: '青岛', start_date: '2026-11-02', end_date: '2026-11-03', overall_suggestions: '出发前留意天气变化。', weather_info: [], days: [
      { day_index: 0, date: '2026-11-02', theme: '海风与老城漫步', attractions: ['栈桥', '圣弥厄尔教堂', '大学路', '小鱼山'].map(attraction), meals: [{ type: 'lunch', name: '老城午餐' }], schedule: [], description: '老城慢游', transportation: '步行与公共交通', accommodation: '经济型酒店' },
      { day_index: 1, date: '2026-11-03', theme: '沿海岸去看世界', attractions: ['八大关', '第二海水浴场', '五四广场'].map(attraction), meals: [], schedule: [] }
    ] }
    plan.days[0].hotel = { name: '示例参考民宿', type: '舒适型酒店', estimated_cost: 500, price_range: '约¥500/晚', address: '示例地址', rating: '', distance: '' }
    plan.weather_info = [{ date: '2026-11-02', day_weather: '晴', night_weather: '多云', day_temp: 23, night_temp: 16, wind_direction: '东南风', wind_power: '1-3级' }]
    plan.budget = { total_attractions: 120, total_hotels: 500, total_meals: 200, total_transportation: 180, per_person: 1000, total: 1000, over_budget: false, calculation_note: '用于布局测试的参考费用，非实时价格。' }
    await page.addInitScript(plan => {
      sessionStorage.setItem('tripmind.storage_owner', 'layout-fixture')
      localStorage.setItem('tripmind.account', 'layout-fixture')
      sessionStorage.setItem('tripPlan', JSON.stringify(plan))
    }, plan)
    await page.goto('http://127.0.0.1:5175/result')
    await page.addStyleTag({ content: 'html { scroll-behavior: auto !important } .ant-message { display: none !important }' })
    await page.locator('.stop-row').first().waitFor()
    // 升级 jsPDF 后验证真实导出链路，而不只检查页面可以构建。
    await page.getByRole('button', { name: '导出行程', exact: true }).click()
    const downloadReady = page.waitForEvent('download', { timeout: 60000 })
    await page.getByText('导出为 PDF', { exact: true }).click()
    const download = await downloadReady
    const pdf = fs.readFileSync(await download.path())
    assert.equal(pdf.subarray(0, 5).toString(), '%PDF-')
    assert.ok(pdf.length > 1000, 'PDF contains the rendered itinerary')
    assert.ok((pdf.toString('latin1').match(/\/Type \/Page\b/g) || []).length >= 1)
    assert.equal(await page.locator('.stop-row').count(), 4)
    assert.equal(await page.locator('.coverflow.is-panorama').count(), 0, 'overview uses the restored layered cards')
    assert.ok(await page.locator('.cover-card').nth(1).evaluate(el => getComputedStyle(el).transform !== 'none'))
    assert.equal(await page.locator('.cover-card.is-active').getAttribute('data-attraction-name'), '栈桥')
    await page.getByRole('button', { name: '下一个景点', exact: true }).click()
    assert.equal(await page.locator('.cover-card.is-active').getAttribute('data-attraction-name'), '圣弥厄尔教堂')
    await page.getByRole('button', { name: '上一个景点', exact: true }).click()
    assert.equal(await page.locator('.day-tabs').count(), 0, 'no floating overview/day tab bar')
    assert.equal(await page.locator('#weather').count(), 0, 'no duplicate standalone weather panel')
    assert.equal(await page.locator('.nav-tab').filter({ hasText: '天气信息' }).count(), 0)
    assert.match(await page.locator('.weather-strip').textContent(), /23/)
    assert.equal(await page.locator('.explorer-eyebrow').count(), 0, 'no decorative English heading labels')
    await page.locator('.stop-row').filter({ hasText: '大学路' }).click()
    await page.waitForFunction(() => document.querySelector('.stop-detail h2')?.textContent === '大学路')
    assert.equal(await page.locator('.stop-row[aria-pressed="true"]').count(), 1)
    assert.ok(await page.getByRole('button', { name: '前一天', exact: true }).isDisabled())
    await page.getByRole('button', { name: '后一天', exact: true }).click()
    assert.equal(await page.locator('.stop-row').count(), 3)
    assert.equal(await page.locator('.stop-detail h2').textContent(), '八大关')
    assert.ok(await page.getByRole('button', { name: '后一天', exact: true }).isDisabled())
    await page.getByRole('button', { name: '前一天', exact: true }).click()
    await page.getByRole('button', { name: '编辑行程', exact: true }).click()
    await page.getByRole('button', { name: '↓ 下移', exact: true }).click()
    assert.match(await page.locator('.stop-row').nth(1).textContent(), /栈桥/)
    assert.equal(await page.locator('.stop-detail h2').textContent(), '栈桥')
    await page.getByRole('button', { name: '删除景点', exact: true }).click()
    assert.equal(await page.locator('.stop-row').count(), 3)
    assert.equal(await page.locator('.stop-detail h2').textContent(), '圣弥厄尔教堂')
    await page.getByRole('button', { name: '取消', exact: true }).first().click()
    assert.equal(await page.locator('.stop-row').count(), 4)
    await page.locator('.map-status strong').waitFor()
    assert.equal(await page.getByRole('button', { name: '重新加载', exact: true }).count(), 1)
    await page.getByRole('button', { name: '重新加载', exact: true }).click()
    await page.locator('.map-status strong').waitFor()
    assert.equal(mapRequests, 2, 'map retry must request the SDK again, not reuse its failed promise')
    assert.equal(await page.locator('.itinerary-list .day-support').count(), 0)
    assert.equal(await page.locator('#budget .day-support').count(), 0)
    assert.equal(await page.locator('#budget .daily-timeline').count(), 0)
    assert.equal(await page.locator('#budget .day-info').count(), 0)
    assert.equal(await page.locator('.hotel-row').count(), 1)
    await page.locator('.hotel-row').click()
    assert.equal(await page.locator('#budget .hotel-card').count(), 0)
    assert.match(await page.locator('.hotel-detail .stay-reference').textContent(), /示例参考民宿[\s\S]*约\s*¥500/)
    assert.match(await page.locator('.hotel-location-note').textContent(), /暂无有效坐标/)
    await page.getByRole('button', { name: '后一天', exact: true }).click()
    assert.equal(await page.locator('.hotel-row').count(), 0)
    assert.equal(await page.locator('.hotel-detail').count(), 0)
    await page.getByRole('button', { name: '前一天', exact: true }).click()
    await page.locator('.hotel-row').click()
    const out = fs.mkdtempSync(path.join(os.tmpdir(), 'tripmind-layout-'))
    await page.locator('.itinerary-workspace').screenshot({ path: path.join(out, 'stay-desktop.png') })
    await page.locator('.stop-row').first().click()
    await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'instant' }))
    await page.screenshot({ path: path.join(out, 'top-desktop.png') })
    await page.locator('#overview').screenshot({ path: path.join(out, 'overview.png') })
    await page.locator('#budget').screenshot({ path: path.join(out, 'budget.png') })
    await page.locator('#days').evaluate(el => el.scrollIntoView({ block: 'start' }))
    await page.locator('#days').screenshot({ path: path.join(out, 'desktop.png') })
    const geometry = await page.locator('.itinerary-workspace').evaluate(el => {
      const a = el.children[0].getBoundingClientRect(), b = el.children[1].getBoundingClientRect()
      return { leftWidth: a.width, rightWidth: b.width, separated: a.right <= b.left }
    })
    assert.ok(geometry.separated)
    assert.ok(Math.abs(geometry.leftWidth - 320) < 2, 'desktop route list stays compact at 320px')
    assert.ok(geometry.rightWidth > geometry.leftWidth * 1.5, 'map and detail receive the remaining space')
    const inspectorLayout = await page.locator('.itinerary-inspector').evaluate(el => ({
      overflow: getComputedStyle(el).overflowY,
      mapHeight: el.children[0].getBoundingClientRect().height,
      detailsHeight: el.children[1].getBoundingClientRect().height
    }))
    assert.equal(inspectorLayout.overflow, 'visible', 'inspector should use page scrolling')
    const mapCanvasHeight = await page.locator('.explorer-map .map-section').evaluate(el => el.getBoundingClientRect().height)
    assert.ok(mapCanvasHeight >= 240 && mapCanvasHeight <= 340, 'wide map should still leave room for the introduction')
    for (const height of [768, 900, 1100]) {
      await page.setViewportSize({ width: 1440, height })
      await page.locator('.day-section').evaluate(el => el.scrollIntoView({ block: 'start', behavior: 'instant' }))
      const bounds = await page.locator('.detail-description').evaluate(el => ({ top: el.getBoundingClientRect().top, bottom: el.getBoundingClientRect().bottom, viewport: innerHeight }))
      assert.ok(bounds.top >= 0 && bounds.bottom <= bounds.viewport, `introduction not visible at height ${height}: ${JSON.stringify(bounds)}`)
    }
    const longDetails = await page.locator('.detail-description').evaluate(el => {
      const text = el.textContent
      el.textContent = text.repeat(30)
      const inspector = el.closest('.itinerary-inspector')
      const details = el.closest('.stop-detail')
      const result = { inspectorFits: inspector.scrollHeight <= inspector.clientHeight + 2,
        detailsFit: details.scrollHeight <= details.clientHeight + 2 }
      el.textContent = text
      return result
    })
    assert.ok(longDetails.inspectorFits && longDetails.detailsFit, 'long details should expand without clipping')
    await page.setViewportSize({ width: 390, height: 844 })
    await page.locator('.stop-row').filter({ hasText: '小鱼山' }).click()
    assert.equal(await page.locator('.stop-detail h2').textContent(), '小鱼山')
    await page.locator('.stop-detail').evaluate(el => el.scrollIntoView({ block: 'center' }))
    await page.locator('.detail-content').evaluate(el => Promise.all(el.getAnimations().map(animation => animation.finished)))
    await page.screenshot({ path: path.join(out, 'mobile.png') })
    console.log('SCREENSHOTS', out)
    const overflow = await page.evaluate(() => ({ width: document.documentElement.scrollWidth, viewport: innerWidth,
      elements: [...document.querySelectorAll('body *')].filter(el => { const r = el.getBoundingClientRect(); return r.width && (r.right > innerWidth + 1 || r.left < -1) }).slice(0, 15).map(el => el.className) }))
    assert.ok(overflow.width <= overflow.viewport + 1, 'mobile horizontal overflow: ' + JSON.stringify(overflow))
    // 结果页对话保持在当前页面，确认修改后直接更新当前结果页。
    await page.setViewportSize({ width: 1440, height: 1000 })
    const fixtureRequest = { origin_city: '武汉', city: '青岛', start_date: plan.start_date, end_date: plan.end_date, travel_days: 2, transportation: 'high_speed_train', accommodation: '经济型酒店' }
    await page.evaluate(request => sessionStorage.setItem('tripRequest', JSON.stringify(request)), fixtureRequest)
    let saved = { id: 'chat-fixture', title: '青岛两天', city: '青岛', days: 2, current_version: 1, start_date: plan.start_date, end_date: plan.end_date, request: fixtureRequest, plan, versions: [] }
    const revised = structuredClone(plan)
    revised.days[0].attractions[0].name = '栈桥（调整后）'
    revised.revision_diff = []
    const records = new Map()
    let addedInstruction = ''
    const headers = { 'Access-Control-Allow-Origin': 'http://127.0.0.1:5175', 'Access-Control-Allow-Credentials': 'true' }
    await page.route('**/api/**', async route => {
      const request = route.request(), url = new URL(request.url()), method = request.method()
      let json = []
      if (url.pathname === '/api/auth/me') json = { id: 'layout-fixture', username: 'preview', role: 'user' }
      else if (url.pathname.includes('/conversations')) {
        const id = url.pathname.split('/')[4]
        if (method === 'PUT') {
          const data = request.postDataJSON()
          json = { ...data, id, revision: data.revision + 1 }
          records.set(id, json)
        } else json = id ? records.get(id) : [...records.values()]
      } else if (url.pathname === '/api/map/poi') { assert.equal(url.searchParams.get('city'), '青岛'); json = {success:true,data:[{id:'test-poi',name:'小鱼山公园',address:'青岛市测试地址',location:{longitude:120.33,latitude:36.06}}]} }
      else if (url.pathname === '/api/trip/revision-jobs') { addedInstruction=request.postDataJSON().instruction; json={job_id:'add-job',status:'queued'} }
      else if (url.pathname.endsWith('/add-job/events')) return route.fulfill({headers,contentType:'text/event-stream',body:'event: snapshot\ndata: {"status":"completed","message":"完成"}\n\n'})
      else if (url.pathname.endsWith('/jobs/add-job')) json={status:'completed',data:revised}
      else if (url.pathname.endsWith('/assistant/messages')) json = { answer: '可以减少一天的步行量。', evidence: [], uncertainties: [], proposed_instruction: '第二天轻松一点', can_apply: true, base_version: 1 }
      else if (url.pathname.endsWith('/assistant/revision-jobs')) json = { job_id: 'chat-job', status: 'queued' }
      else if (url.pathname.endsWith('/chat-job/events')) return route.fulfill({ headers, contentType: 'text/event-stream', body: 'event: snapshot\ndata: {"status":"completed","message":"完成"}\n\n' })
      else if (url.pathname.endsWith('/jobs/chat-job')) json = { status: 'completed', data: revised }
      else if (url.pathname.endsWith('/versions')) { saved = { ...saved, current_version: 2, plan: revised }; json = saved }
      else if (url.pathname.startsWith('/api/library/trips')) json = saved
      await route.fulfill({ json, headers })
    })
    // 重新加载仅用于恢复测试请求，不生成新行程。
    await page.reload()
    await page.locator('.assistant-nav').click()
    const panel = page.locator('.assistant-page.embedded')
    await panel.locator('.welcome').waitFor()
    assert.equal(await page.locator('.assistant-entry').count(), 0, 'no duplicate conversation strip')
    assert.equal(await page.locator('.ant-drawer-mask').count(), 0, 'chat must not lock the page')
    await page.evaluate(() => window.scrollTo({ top: 400, behavior: 'instant' }))
    assert.ok(await page.evaluate(() => scrollY > 0))
    await page.screenshot({ path: path.join(out, 'inline-chat-desktop.png') })
    await page.setViewportSize({ width: 390, height: 844 })
    await page.locator('.assistant-dock').screenshot({ path: path.join(out, 'inline-chat-mobile.png') })
    await page.setViewportSize({ width: 1440, height: 1000 })
    const resultUrl = page.url()
    assert.ok(resultUrl.endsWith('/result'), 'opening conversation must not navigate')
    assert.equal(await panel.locator('header').count(), 0)
    assert.equal(await panel.locator('aside').count(), 0)
    await panel.locator('textarea').fill('第二天轻松一点')
    await panel.getByRole('button', { name: '发送', exact: true }).click()
    await panel.getByRole('button', { name: '按此建议生成修改', exact: true }).click()
    await panel.getByRole('button', { name: '确认并保存新版本', exact: true }).waitFor()
    assert.equal(await page.locator('.stop-row').filter({ hasText: '栈桥（调整后）' }).count(), 0, 'draft must not change the visible plan')
    await panel.getByRole('button', { name: '确认并保存新版本', exact: true }).click()
    await page.locator('.stop-row').filter({ hasText: '栈桥（调整后）' }).waitFor()
    assert.equal(page.url(), resultUrl)
    await panel.getByText('已保存为 v2。', { exact: false }).waitFor()
    await page.getByRole('button', { name: '收起行程对话' }).click()
    await panel.waitFor({ state: 'hidden' })
    await page.locator('.assistant-nav').click()
    await page.locator('.assistant-page.embedded .bubble').filter({ hasText: '第二天轻松一点' }).first().waitFor()
    await page.getByRole('button', { name: '收起行程对话' }).click()
    await panel.waitFor({ state: 'hidden' })
    await page.getByRole('button', { name: '更多行程工具' }).click()
    await page.getByRole('menuitem', { name: '用文字修改行程', exact: true }).click()
    await page.getByText('告诉 TripMind 你想调整什么', { exact: true }).waitFor()
    await page.locator('.ant-drawer-open .ant-drawer-close').click()
    await page.getByRole('button', { name: '更多行程工具' }).click()
    await page.getByRole('menuitem', { name: /历史版本/ }).click()
    await page.getByText('每次修改都可以反悔', { exact: true }).waitFor()
    await page.locator('.ant-drawer-open .ant-drawer-close').click()
    await page.locator('.ant-drawer-content:visible').waitFor({ state: 'hidden' })
    await page.getByRole('button', { name: '在栈桥（调整后）后添加地点', exact:true }).click()
    await page.getByRole('textbox', { name:'搜索地点', exact:true }).fill('小鱼山')
    await page.getByRole('button', { name:'搜索', exact:true }).click()
    await page.locator('.place').filter({hasText:'小鱼山公园'}).click()
    await page.getByText('确认 AI 修改内容', {exact:true}).waitFor()
    assert.match(addedInstruction, /小鱼山公园/)
    assert.match(addedInstruction, /第1个景点之后/)
    assert.equal(await page.locator('.stop-name').filter({hasText:'小鱼山公园'}).count(),0,'adding remains a draft until confirmation')
    await page.getByRole('button',{name:'放弃本次修改',exact:true}).click()
    await page.setViewportSize({ width: 390, height: 844 })
    await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'instant' }))
    await page.screenshot({ path: path.join(out, 'top-mobile.png') })
    assert.deepEqual(errors, [])
    console.log('PASS: layered gallery, compact list / wide map, selection, day switching, editing, map retry, long details, mobile layout, inline chat and tools')
    console.log('SCREENSHOTS', out)
  } finally { await browser.close() }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
