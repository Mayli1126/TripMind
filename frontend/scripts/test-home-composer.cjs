// 隔离检查页面与交互，不访问真实账号、模型、地图或业务数据。
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const {chromium} = require('playwright')
;(async () => {
  const browser = await chromium.launch({headless:true,channel:'msedge'})
  const folder = fs.mkdtempSync(path.join(os.tmpdir(),'tripmind-composer-'))
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000}})
    const errors=[]; let writes=0
    page.on('pageerror', e=>errors.push(e.message))
    await page.route('**/api/**',async route=>{
      const req=route.request(), url=new URL(req.url())
      if(req.method()!=='GET')writes++
      let data=[]
      if(url.pathname==='/api/auth/me')data={id:'fixture',username:'preview',role:'user'}
      if(url.pathname==='/api/library/trips')data=[{id:'fixture-trip',city:'青岛',title:'青岛四天 · 海风与城市漫步',days:4,start_date:'2026-11-20',end_date:'2026-11-23',current_version:1}]
      await route.fulfill({json:data})
    })
    await page.goto('http://127.0.0.1:5175/')
    const input=page.getByRole('textbox',{name:'描述你的旅行想法'})
    await input.waitFor()
    const glow=()=>page.locator('.chat-home').evaluate(el=>Number(getComputedStyle(el,'::before').opacity))
    await page.screenshot({path:path.join(folder,'desktop-idle.png'),fullPage:true})
    const idle=await glow()
    await input.focus(); await page.waitForTimeout(450)
    assert.ok((await glow())>idle)
    assert.equal(await page.locator('.recent-section').evaluate(el=>getComputedStyle(el).opacity),'0.5')
    await page.screenshot({path:path.join(folder,'desktop-focus.png'),fullPage:true})
    await input.fill('和朋友去威海看海四天')
    assert.equal(await page.getByRole('button',{name:'开始对话'}).isEnabled(),true)
    await input.press('Shift+Enter')
    assert.equal(writes,0)
    await page.getByRole('heading',{level:1}).click(); await page.waitForTimeout(450)
    assert.equal(await glow(),idle)
    assert.ok((await input.inputValue()).includes('威海'))
    await page.setViewportSize({width:390,height:844})
    await input.focus(); await page.waitForTimeout(450)
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth))
    const box=await input.boundingBox(), send=await page.getByRole('button',{name:'开始对话'}).boundingBox()
    assert.ok(box.x+box.width<=send.x)
    await page.screenshot({path:path.join(folder,'mobile-focus.png'),fullPage:true})
    assert.deepEqual(errors,[])
    console.log('PASS: focus glow/recovery, retained text, enabled send, Shift+Enter, mobile fit')
    console.log('SCREENSHOTS '+folder)
  } finally {await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1})
