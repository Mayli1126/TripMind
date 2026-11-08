// Isolated browser fixtures only; no real account, model, map or database writes.
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { chromium } = require('playwright')

async function main() {
  const browser = await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL || 'msedge'})
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000}})
    const errors=[]; page.on('pageerror',e=>errors.push(e.message))
    const items=[]
    const summary={confirmed:[],rejected:[{text:'不安排爬山',quote:'不要爬山'}],open_questions:[],processed_count:4,method:'model'}
    const messages=Array.from({length:20},(_,i)=>({role:i%2?'assistant':'user',content:i===0?'不要爬山':`旅行消息${i}`}))
    let record={id:'memory-chat',kind:'planner',title:'青岛旅行',trip_id:null,revision:1,updated_at:1,snapshot:{messages,draft:{city:'青岛'}},summary:null}
    await page.route(/webapi\.amap\.com/,r=>r.abort())
    await page.route('**/api/**',async route=>{
      const req=route.request(),url=new URL(req.url()),method=req.method()
      let data=[],status=200
      if(url.pathname==='/api/auth/me')data={id:'fixture',username:'preview',role:'user'}
      else if(url.pathname==='/api/library/memories' && method==='GET') data=items
      else if(url.pathname.startsWith('/api/library/memories/')) {
        const id=url.pathname.split('/').pop(),body=req.postDataJSON(),index=items.findIndex(x=>x.id===id)
        const matched=items.filter(x=>x.id!==id && x.enabled && x.category===body.category && x.city===body.city)
        if(index>=0 && body.revision!==items[index].revision){status=409;data={detail:'偏好已更新，请刷新后重试'}}
        else if(matched.length&&(!body.replace_conflicts || matched.some(x=>body.conflict_revisions?.[x.id]!==x.revision))){status=409;data={detail:{message:'conflict',conflicts:matched}}}
        else {
          for(const item of matched){item.enabled=item.active=false;item.revision++}
          const row={id,...body,revision:(body.revision||0)+1,content:body.category==='pace' ? ({relaxed:'轻松慢游',intensive:'充实打卡'})[body.value] : body.content,expired:false,active:body.enabled}
          if(index<0)items.push(row);else items[index]=row
          data={saved:true}
        }
      } else if(url.pathname==='/api/library/conversations/memory-chat') {
        if(method==='PUT'){const body=req.postDataJSON();record={...record,...body,revision:body.revision+1};data={revision:record.revision}}
        else data=record
      } else if(url.pathname==='/api/planner-chat/messages') {
        assert.equal(req.postDataJSON().conversation_id,'memory-chat')
        record.summary=summary
        data={reply:'记得，你之前说不要爬山。接下来想哪天出发？',draft:{city:'青岛'},suggestions:[],ready:false,conversation_summary:summary}
      }
      await route.fulfill({status,json:data})
    })
    await page.goto('http://127.0.0.1:5175/memories')
    await page.getByLabel('偏好分类').selectOption('pace')
    await page.getByLabel('偏好内容',{exact:true}).selectOption('relaxed')
    await page.getByRole('button',{name:'保存偏好',exact:true}).click()
    await page.getByRole('heading',{name:'轻松慢游',exact:true}).waitFor()
    await page.getByLabel('偏好分类').selectOption('pace')
    await page.getByLabel('偏好内容',{exact:true}).selectOption('intensive')
    await page.getByRole('button',{name:'保存偏好',exact:true}).click()
    await page.getByRole('alert').waitFor()
    assert.equal(items.length,1)
    await page.getByRole('button',{name:'暂停旧记录并保存'}).click()
    await page.getByRole('heading',{name:'充实打卡',exact:true}).waitFor()
    assert.equal(items.filter(x=>x.enabled).length,1)
    const folder=fs.mkdtempSync(path.join(os.tmpdir(),'tripmind-memory-'))
    await page.screenshot({path:path.join(folder,'preferences-desktop.png'),fullPage:true})
    await page.setViewportSize({width:390,height:844})
    await page.screenshot({path:path.join(folder,'preferences-mobile.png'),fullPage:true})
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth))
    await page.setViewportSize({width:1440,height:1000})
    await page.goto('http://127.0.0.1:5175/?conversation=memory-chat')
    await page.getByLabel('发送旅行需求').fill('还记得我之前的要求吗')
    await page.getByRole('button',{name:'发送消息',exact:true}).click()
    await page.getByText('记得，你之前说不要爬山。接下来想哪天出发？',{exact:true}).waitFor()
    await page.locator('.conversation-summary summary').waitFor()
    await page.locator('.conversation-summary summary').click()
    await page.getByText('原文：不要爬山',{exact:true}).waitFor()
    await page.locator('.thread').evaluate(element=>{element.scrollTop=element.scrollHeight})
    await page.screenshot({path:path.join(folder,'conversation-summary.png'),fullPage:true})
    await page.reload()
    await page.locator('.conversation-summary summary').waitFor()
    await page.setViewportSize({width:390,height:844})
    await page.locator('.conversation-summary summary').click()
    await page.screenshot({path:path.join(folder,'conversation-summary-mobile.png'),fullPage:true})
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth))
    assert.deepEqual(errors,[])
    console.log('PASS: structured preference save/conflict confirmation, mobile fit, summary display and restore')
    console.log('SCREENSHOTS',folder)
  } finally {await browser.close()}
}
main().catch(e=>{console.error(e);process.exitCode=1})
