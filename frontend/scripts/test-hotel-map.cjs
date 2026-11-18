// 使用本地 SDK 替身，不使用真实地图密钥、地理编码或上游调用。
const assert = require('node:assert/strict')
const { chromium } = require('playwright')
async function main() {
  const browser = await chromium.launch({ headless:true, channel:process.env.PLAYWRIGHT_CHANNEL || 'msedge' })
  try {
    const page = await browser.newPage()
    await page.route('**/api/**', route => route.fulfill({ json:route.request().url().includes('/auth/me') ? {id:'hotel-fixture',role:'user'} : [] }))
    await page.route('**/webapi.amap.com/**', async route => {
      await new Promise(resolve => setTimeout(resolve, 800))
      return route.fulfill({contentType:'application/javascript',body:`
      window.AMap = {version:'2.0', plugin:(_,done)=>done(),
        Map:class{setFitView(){window.fitCount=(window.fitCount||0)+1;window.camera={type:'fit'}}setZoomAndCenter(z,p){window.hotelCenter=p;window.camera={type:'focus',zoom:z,center:p}}clearMap(){}destroy(){}},
        Polyline:class{setMap(){}},
        Marker:class{constructor(options){this.options=options;this.el=document.createElement('button');this.el.title=options.title;this.el.innerHTML=options.content}
          setMap(map){if(map)document.querySelector('#amap-container').append(this.el);else this.el.remove()}
          on(event,handler){this.el.addEventListener(event,handler)}getContent(){return this.el.firstElementChild}}
      };window.___onAPILoaded();`})
    })
    const hotel={name:'测试海边酒店',type:'舒适型酒店',address:'测试地址',price_range:'约¥400/晚',rating:'',distance:'',location:{longitude:120.35,latitude:36.07}}
    const plan={city:'青岛',start_date:'2026-11-07',end_date:'2026-11-09',days:[
      {day_index:0,date:'2026-11-07',attractions:[{name:'栈桥',location:{longitude:120.3,latitude:36.05},visit_duration:90,ticket_price:0}],meals:[],hotel},
      {day_index:1,date:'2026-11-08',attractions:[],meals:[],hotel:{...hotel,name:'仅住宿日酒店'}},
      {day_index:2,date:'2026-11-09',attractions:[],meals:[],hotel:{...hotel,name:'位置待确认酒店',location:{longitude:0,latitude:0}}}
    ],weather_info:[],overall_suggestions:''}
    await page.addInitScript(plan=>{sessionStorage.setItem('tripmind.storage_owner','hotel-fixture');localStorage.setItem('tripmind.account','hotel-fixture');sessionStorage.setItem('tripPlan',JSON.stringify(plan))},plan)
    await page.goto('http://127.0.0.1:5175/result', {waitUntil:'domcontentloaded'})
    await page.locator('.stop-row').click()
    const marker=page.locator('#amap-container button[title="测试海边酒店"]')
    await marker.waitFor()
    assert.deepEqual(await page.evaluate(()=>window.camera),{type:'focus',zoom:15,center:[120.3,36.05]}, 'a place selected before SDK initialization remains focused')
    assert.equal(await marker.textContent(),'住')
    await marker.click()
    assert.match(await page.locator('.hotel-detail').textContent(),/测试海边酒店/)
    assert.deepEqual(await page.evaluate(()=>window.hotelCenter),[120.35,36.07])
    assert.equal(await page.evaluate(()=>window.camera.zoom),15)
    await page.locator('.stop-row').click()
    assert.equal(await page.locator('.hotel-detail').count(),0)
    assert.deepEqual(await page.evaluate(()=>window.camera),{type:'focus',zoom:15,center:[120.3,36.05]})
    await page.getByRole('button',{name:'后一天',exact:true}).click()
    await page.locator('#amap-container button[title="仅住宿日酒店"]').waitFor()
    assert.equal(await page.evaluate(()=>window.camera.type),'fit', 'day switching restores the whole route view')
    assert.equal(await page.locator('#amap-container button[title="测试海边酒店"]').count(),0)
    await page.getByRole('button',{name:'后一天',exact:true}).click()
    await page.locator('.hotel-row').click()
    assert.equal(await page.locator('#amap-container button').count(),0)
    assert.match(await page.locator('.hotel-location-note').textContent(),/暂无有效坐标/)
    console.log('PASS: hotel marker/list/detail linking, date switching, hotel-only day and missing coordinates')
  } finally {await browser.close()}
}
main().catch(error=>{console.error(error);process.exitCode=1})
