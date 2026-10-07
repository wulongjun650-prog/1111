// Run with playwright on NODE_PATH and Jinja2 on PYTHON, like source-editor.browser.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import fs from 'node:fs/promises';
import {execFileSync} from 'node:child_process';
import {createRequire} from 'node:module';
import path from 'node:path';

const {chromium} = createRequire(import.meta.url)('playwright');
const root = path.resolve(import.meta.dirname,'..');
const document = execFileSync(process.env.PYTHON || 'python',['-X','utf8','-c',
  'from jinja2 import Environment,FileSystemLoader; print(Environment(loader=FileSystemLoader("templates")).get_template("index.html").render(account={"role":"agent"},username="viewer",deployment=False,csrf_token="csrf",target_url="http://127.0.0.1:9000"))'],{cwd:root,encoding:'utf8'});
const siteId = 'a'.repeat(32);
const site = {id:siteId,domain:'mine.example.com',enabled:true,stage:'waiting_dns'};
const version = (slot,id) => ({slot,id,name:`${slot}-${id}.zip`,created:1,files:3,bytes:300,sha256:'abc'});
const check = (status='unchecked') => ({status,platform:'google',detail:status==='unknown'?'查询暂不可用':status==='abnormal'?'检测到风险':'未发现风险',checked_at:status==='unchecked'?null:2,http_status:status==='unchecked'?null:200,final_url:'https://safe.example/landing'});
const occurrences = suffix => [
  {id:`href-${suffix}`,key:'index.html:href:1',path:'index.html',line:1,kind:'anchor',url:'https://old.example/landing'},
  {id:`script-${suffix}`,key:'assets/main.js:location:1',path:'assets/main.js',line:1,kind:'js_location',url:'https://old.example/landing'}
];

async function withConsole(run, options = {}) {
  const browser = await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
  const server = http.createServer(async (req,res)=>{
    if(req.url==='/') {res.setHeader('Content-Type','text/html; charset=utf-8');res.end(document);return;}
    if(req.url.startsWith('/static/')) {
      const file=path.join(root,req.url);
      try {res.setHeader('Content-Type',/\.(js|mjs)$/.test(file)?'text/javascript':file.endsWith('.css')?'text/css':'image/png');res.end(await fs.readFile(file));}
      catch {res.writeHead(404);res.end();}
      return;
    }
    res.writeHead(404);res.end();
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  if(options.clock) await page.clock.install();
  const fixture={sites:[site],published:{A:'live-a',B:'live-b'},versions:[version('A','live-a'),version('B','live-b'),version('B','import-b')],
    scans:[],byVersion:{'live-b':occurrences('live-b'),'import-b':occurrences('import-b')},
    presets:[{id:1,url:'https://safe.example/landing',note:'<img src=x onerror="window.presetExecuted=true">',created:1,check:check('normal')},
      {id:2,url:'https://blocked.example/landing',note:'风险地址',created:1,check:check('abnormal')},
      {id:3,url:'https://unknown.example/landing',note:'待确认地址',created:1,check:check('unknown')}],
    active:null,numbers:[],adds:[],numberAdds:[],checks:[],applies:[],numberApplies:[],uploads:[],deletes:[],numberDeletes:[],trustChecks:[],trustResults:{},trust_poll:false,deskReviews:[],deskSwitch:'',checkResults:{},activeChecks:0,maxChecks:0,
    applyStatus:200,scanStatus:200,stateStatus:200,delayCheck:null,releaseCheck:null,delayVersion:null,releaseScan:null};
  page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/api/**',async route=>{
    const request=route.request(),url=new URL(request.url()),pathname=url.pathname;
    let response,status=200;
    if(pathname==='/api/sites') response={account:{role:'agent'},sites:fixture.sites,google_reputation_configured:false};
    else if(pathname===`/api/sites/${siteId}/state`) {
      status=fixture.stateStatus;
      response=status===200?{site,config:{routing:'RULES',allowed_slot:'B',protection:true,content_mode:'PAGE',distribution:'random',rules:{}},versions:fixture.versions,slots:fixture.published,links:[],health:{geoip:true},target_url:'http://127.0.0.1:9000',revision:1}:{detail:'读取状态失败'};
    }
    else if(pathname.endsWith('/analytics')) response={period:'today',end:new Date().toISOString(),summary:{total:0,allowed:0,blocked:0,rate:0},trend:[],domains:[],countries:[],reasons:[],recent:[]};
    else if(pathname===`/api/sites/${siteId}/tracking`) response={snippets:[], published:{A:{version_id:fixture.published.A, ga4:'', conversion:'', ga4_body:'', conversion_body:''}, B:{version_id:fixture.published.B, ga4:'', conversion:'', ga4_body:'', conversion_body:''}}};
    else if(pathname===`/api/sites/${siteId}/b-redirects`) {
      fixture.scans.push(url.searchParams.get('version_id'));
      const selected=url.searchParams.get('version_id')||fixture.published.B;
      response=structuredClone({version:fixture.versions.find(item=>item.id===selected),published_version:fixture.published.B,occurrences:fixture.byVersion[selected]||[],warnings:[],presets:fixture.presets,numbers:fixture.numbers,trust_poll:Boolean(fixture.trust_poll),active:fixture.active});
      status=fixture.scanStatus;
      if(status!==200) response={detail:'无法读取此版本'};
      if(selected===fixture.delayVersion) await new Promise(resolve=>{fixture.releaseScan=resolve;});
    } else if(pathname===`/api/sites/${siteId}/b-redirects/presets`) {
      const body=request.postDataJSON();fixture.adds.push({body,csrf:request.headers()['x-csrf-token']});
      if(body.urls.some(item=>!/^https?:\/\//.test(item))) {status=400;response={detail:'仅支持 HTTP/HTTPS 网址'};}
      else {
        const added=body.urls.map((url,index)=>({id:fixture.adds.length*100+index+1,url,note:body.note,created:3,check:check()}));
        fixture.presets.push(...added);
        response={presets:added};
      }
    } else if(/\/b-redirects\/presets\/[^/]+\/check$/.test(pathname)) {
      const id=Number(pathname.split('/').at(-2)),preset=fixture.presets.find(item=>item.id===id);
      fixture.checks.push(id);fixture.activeChecks++;fixture.maxChecks=Math.max(fixture.maxChecks,fixture.activeChecks);
      if(id===fixture.delayCheck) await new Promise(resolve=>{fixture.releaseCheck=resolve;});
      preset.check=check(fixture.checkResults[id]||'normal');fixture.activeChecks--;
      response={preset};
    } else if(/\/b-redirects\/presets\/[^/]+$/.test(pathname) && request.method()==='DELETE') {
      const id=Number(pathname.split('/').at(-1));fixture.deletes.push(id);
      fixture.presets=fixture.presets.filter(item=>item.id!==id);response={presets:fixture.presets};
    } else if(pathname===`/api/sites/${siteId}/b-redirects/numbers` && request.method()==='POST') {
      const body=request.postDataJSON();fixture.numberAdds.push(body);
      const added=body.phones.map((phone,index)=>({id:fixture.numbers.length+index+20,phone,note:body.note||'',created:3}));
      fixture.numbers.push(...added);response={numbers:added};
    } else if(/\/b-redirects\/numbers\/\d+\/trust$/.test(pathname) && request.method()==='POST') {
      const id=Number(pathname.split('/').at(-2));
      const number=fixture.numbers.find(item=>item.id===id);
      fixture.trustChecks.push(id);
      if(number) number.trust={status:fixture.trustResults[id]||'unconfirmed', detail:'这次没看清', checked_at:4};
      const statuses=new Set(fixture.numbers.map(item=>item.trust?.status));
      response={number, busy:false, poll_enabled:Boolean(fixture.trust_poll) || (statuses.has('trust') && statuses.has('clear'))};
    } else if(pathname===`/api/sites/${siteId}/b-redirects/numbers/apply`) {
      const body=request.postDataJSON(),number=fixture.numbers.find(item=>item.id===body.number_id);
      fixture.numberApplies.push({body,csrf:request.headers()['x-csrf-token']});
      const saved=version('B',`wa-${fixture.numberApplies.length}`);fixture.versions.push(saved);fixture.published.B=saved.id;
      fixture.byVersion[saved.id]=(fixture.byVersion[body.version_id]||[]).map(item=>({...item,id:`${item.kind}-${saved.id}`,url:body.occurrence_ids.includes(item.id)&&item.kind==='whatsapp_number'?number.phone:item.url}));
      response={version:saved,changed:body.occurrence_ids.length};
    } else if(/\/b-redirects\/numbers\/[^/]+$/.test(pathname) && request.method()==='DELETE') {
      const id=Number(pathname.split('/').at(-1));fixture.numberDeletes.push(id);
      fixture.numbers=fixture.numbers.filter(item=>item.id!==id);response={ok:true};
    } else if(pathname===`/api/sites/${siteId}/b-redirects/apply`) {
      const body=request.postDataJSON(),preset=fixture.presets.find(item=>item.id===body.preset_id);
      fixture.applies.push({body,csrf:request.headers()['x-csrf-token']});
      status=preset.check.status==='abnormal'?400:fixture.applyStatus;
      if(status!==200) response={detail:status===409?'B 发布版本已改变，请重新扫描':'检测到风险，不能发布',check:preset.check};
      else {
        const saved=version('B',`saved-${fixture.applies.length}`);fixture.versions.push(saved);fixture.published.B=saved.id;
        fixture.byVersion[saved.id]=fixture.byVersion[body.version_id].map((item,index)=>({...item,id:`${index?'script':'href'}-${saved.id}`,url:body.occurrence_ids.includes(item.id)?preset.url:item.url}));
        fixture.active={url:preset.url,preset_id:preset.id,version_id:saved.id,updated:4};
        response={version:saved,check:preset.check,changed:body.occurrence_ids.length};
      }
    } else if(pathname===`/api/sites/${siteId}/desk/review` && request.method()==='POST') {
      const body=request.postDataJSON();
      fixture.deskReviews.push(body);
      const phone=body.phone;
      const leads=phone==='85299990000'?2:0;
      response={rows:[{name:'鳄鱼-梵高', code:'sampleTicket', phone:phone||'85299990000', leads:String(leads)}], total:String(leads), switch:fixture.deskSwitch||'', online:fixture.deskSwitch==='online'?4:1};
    } else if(pathname===`/api/sites/${siteId}/desk/quote` && request.method()==='POST') {
      response={text:'10/06\nHK项目\nAJ\n消耗：33.29\n进线：0.8\n成本：41.61', adjusted:'33.29', cost:'41.61', row:[]};
    } else if(pathname===`/api/sites/${siteId}/upload/B`) {
      const uploaded=version('B','upload-b');fixture.versions.push(uploaded);fixture.byVersion[uploaded.id]=occurrences(uploaded.id);
      fixture.uploads.push({name:url.searchParams.get('name'),body:request.postData(),csrf:request.headers()['x-csrf-token']});response={version:uploaded};
    } else throw new Error(`Unexpected endpoint ${pathname}`);
    await route.fulfill({status,json:response});
  });
  try {
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.waitForFunction(()=>document.querySelector('#rules-fields').disabled===false);
    await run(page,fixture);
    assert.deepEqual(errors,[]);
  } finally {fixture.releaseCheck?.();fixture.releaseScan?.();await browser.close();await new Promise(resolve=>server.close(resolve));}
}

async function openRedirects(page) {
  await page.locator('[data-tab="content"]').click();
  assert.equal(await page.locator('#b-redirects').count(),1,'content exposes the B redirect panel');
  await page.waitForFunction(()=>document.querySelectorAll('#b-redirect-occurrences input[type=checkbox]').length===2);
}

const presetRow = (page,id) => page.locator(`#b-redirect-presets [data-preset-id="${id}"]`);
async function waitForFixture(predicate,message) {
  const deadline=Date.now()+5000;
  while(!predicate()) {
    assert.ok(Date.now()<deadline,message);
    await new Promise(resolve=>setTimeout(resolve,10));
  }
}

test('B redirect selection applies the chosen version and preserves exclusions after repeated replacement',async()=>{
  await withConsole(async(page,fixture)=>{
    assert.deepEqual(fixture.scans,[],'the inactive content panel does not scan');
    await openRedirects(page);
    assert.equal(await page.locator('#b-redirect-version').inputValue(),'live-b');
    assert.match(await page.locator('#b-redirect-count').innerText(),/2/);
    assert.deepEqual(await page.locator('#b-redirect-occurrences input[type=checkbox]').evaluateAll(items=>items.map(item=>item.checked)),[true,true]);
    assert.match(await page.locator('#b-redirect-occurrences').innerText(),/index\.html/);
    assert.match(await page.locator('#b-redirect-occurrences').innerText(),/assets\/main\.js/);
    assert.match(await page.locator('#b-redirect-presets').innerText(),/window\.presetExecuted/);
    assert.equal(await page.evaluate(()=>window.presetExecuted),undefined);
    assert.equal(await page.locator('#b-redirect-version option[value="live-a"]').count(),0,'A content is never a B redirect choice');
    await page.locator('#b-redirect-version').selectOption('import-b');
    await page.waitForFunction(()=>document.querySelector('#b-redirect-occurrences input[type=checkbox]')?.value==='href-import-b');
    await page.locator('#b-redirect-occurrences input[type=checkbox]').nth(1).uncheck();
    await presetRow(page,1).getByRole('button',{name:'换成这条',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#b-redirect-version').value==='saved-1' && document.querySelector('#b-redirect-occurrences input[type=checkbox]')?.value==='href-saved-1');
    assert.deepEqual(fixture.applies[0],{body:{version_id:'import-b',preset_id:1,occurrence_ids:['href-import-b'],expected_published:'live-b'},csrf:'csrf'});
    assert.deepEqual(await page.locator('#b-redirect-occurrences input[type=checkbox]').evaluateAll(items=>items.map(item=>item.checked)),[true,false]);
    assert.match(await page.locator('#slot-live-B').innerText(),/saved-1/);
    assert.match(await page.locator('#b-redirect-current').innerText(),/safe\.example/);
    await presetRow(page,1).locator('.b-redirect-active-badge').waitFor({state:'visible'});
    assert.match(await presetRow(page,1).innerText(),/当前使用中/,'the live redirect preset is flagged after a successful swap');
    assert.equal(await presetRow(page,2).locator('.b-redirect-active-badge').count(),0,'only the live redirect preset is flagged');
    await presetRow(page,1).getByRole('button',{name:'再换一次',exact:true}).click();
    await page.locator('#b-redirect-message').getByText('这些位置已经是这条链接，没有新版本。').waitFor();
    assert.equal(fixture.applies.length,1,'the same link on the selected spots does not publish another version');
    assert.equal(await page.locator('#b-redirect-version').inputValue(),'saved-1');
    assert.deepEqual(await page.locator('#b-redirect-occurrences input[type=checkbox]').evaluateAll(items=>items.map(item=>item.checked)),[true,false]);
    assert.equal(fixture.published.A,'live-a');
    fixture.scanStatus=500;
    await page.locator('#b-redirect-version').selectOption('import-b');
    await page.locator('#b-redirect-message[role="alert"]').waitFor({state:'visible'});
    assert.equal(await page.locator('#b-redirect-version').inputValue(),'import-b');
    assert.equal(await page.locator('#b-redirect-occurrences input[type=checkbox]').count(),0,'failed scans cannot apply positions from the previously selected version');
    assert.equal(await presetRow(page,1).getByRole('button',{name:'再换一次',exact:true}).isDisabled(),true);
    fixture.scanStatus=200;
    await page.locator('#b-redirects').getByRole('button',{name:'重新扫描',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#b-redirect-occurrences input[type=checkbox]')?.value==='href-import-b');
    assert.deepEqual(await page.locator('#b-redirect-occurrences input[type=checkbox]').evaluateAll(items=>items.map(item=>item.checked)),[true,false]);
    fixture.stateStatus=500;
    await presetRow(page,1).getByRole('button',{name:'再换一次',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#b-redirects').getAttribute('aria-busy')==='false');
    assert.match(await page.locator('#b-redirect-message').innerText(),/已.*发布|发布.*成功/,'the confirmed publication is acknowledged even when refreshing its state fails');
    assert.match(await page.locator('#b-redirect-message').innerText(),/刷新|状态/);
    assert.equal(fixture.published.B,'saved-2');
    assert.equal(await page.locator('#b-redirect-version').inputValue(),'saved-2');
    assert.equal(fixture.applies.length,2,'a failed status refresh never repeats the successful apply');
  });
});

test('adding more presets during a check keeps checks serial and leaving content stops the queue',async()=>{
  await withConsole(async(page,fixture)=>{
    await openRedirects(page);
    await page.locator('#b-redirect-urls').fill('javascript:alert(1)');
    await page.locator('#b-redirect-add').click();
    await page.getByRole('alert').filter({hasText:/HTTP|网址|链接/}).first().waitFor({state:'visible'});
    assert.equal(fixture.adds.length,0,'unsafe URLs never reach the API');
    fixture.delayCheck=101;fixture.checkResults={102:'unknown',201:'abnormal'};
    await page.locator('#b-redirect-urls').fill('https://first.example/path\nhttps://second.example/path');
    await page.locator('#b-redirect-note').fill('新增备注');
    await page.locator('#b-redirect-add').click();
    await waitForFixture(()=>fixture.releaseCheck,'first automatic check starts');
    assert.equal(await page.locator('#b-redirect-add').isDisabled(),false,'a pending check does not block adding more presets');
    assert.equal(await presetRow(page,101).getByRole('button',{name:'换成这条',exact:true}).isDisabled(),true);
    assert.equal(await presetRow(page,1).getByRole('button',{name:'换成这条',exact:true}).isDisabled(),false);
    await page.locator('#b-redirect-urls').fill('https://third.example/path');
    await page.locator('#b-redirect-add').click();
    await presetRow(page,201).waitFor({state:'visible'});
    assert.deepEqual(fixture.checks,[101],'the next batch waits behind the current batch');
    fixture.releaseCheck();
    await page.waitForFunction(()=>document.querySelector('[data-preset-id="201"] .b-redirect-check')?.textContent==='不正常');
    assert.deepEqual(fixture.checks,[101,102,201]);
    assert.equal(fixture.maxChecks,1);
    assert.match(await presetRow(page,101).innerText(),/正常/);
    assert.match(await presetRow(page,102).innerText(),/状态未知/);
    assert.deepEqual(fixture.adds[0],{body:{urls:['https://first.example/path','https://second.example/path'],note:'新增备注'},csrf:'csrf'});
    assert.equal(await page.locator('#b-redirect-urls').inputValue(),'');
    fixture.delayCheck=301;fixture.releaseCheck=null;
    await page.locator('#b-redirect-urls').fill('https://fourth.example/path\nhttps://fifth.example/path');
    await page.locator('#b-redirect-add').click();
    await waitForFixture(()=>fixture.releaseCheck,'third batch starts');
    await page.locator('[data-tab="rules"]').click();
    fixture.releaseCheck();
    await waitForFixture(()=>fixture.activeChecks===0,'in-flight check finishes after leaving content');
    await page.locator('[data-tab="content"]').click();
    await presetRow(page,302).waitFor({state:'visible'});
    assert.deepEqual(fixture.checks,[101,102,201,301],'leaving the tab cancels remaining automatic checks');
    assert.match(await presetRow(page,302).innerText(),/未检测/);
  });
});

test('abnormal links stay unpublished, unknown links show a notice, and conflicts retain selection',async()=>{
  await withConsole(async(page,fixture)=>{
    await openRedirects(page);
    await page.locator('#b-redirect-occurrences input[type=checkbox]').nth(1).uncheck();
    await presetRow(page,2).getByRole('button',{name:'换成这条',exact:true}).click();
    await page.locator('#b-redirect-message[role="alert"]').waitFor({state:'visible'});
    assert.match(await page.locator('#b-redirect-message').innerText(),/风险|不能发布/);
    assert.match(await presetRow(page,2).innerText(),/不正常/);
    assert.equal(fixture.published.B,'live-b');
    assert.deepEqual(await page.locator('#b-redirect-occurrences input[type=checkbox]').evaluateAll(items=>items.map(item=>item.checked)),[true,false]);
    await presetRow(page,3).getByRole('button',{name:'换成这条',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#b-redirect-message').textContent.includes('状态未知') && document.querySelector('#b-redirect-version').value==='saved-2');
    assert.equal(await page.locator('#b-redirect-message').getAttribute('role'),'status');
    assert.equal(fixture.published.B,'saved-2');
    assert.equal(fixture.applies[1].body.occurrence_ids.length,1);
    fixture.applyStatus=409;fixture.published.B='other-published';
    const scansBeforeConflict=fixture.scans.length;
    await presetRow(page,1).getByRole('button',{name:'换成这条',exact:true}).click();
    await page.locator('#b-redirect-message[role="alert"]').waitFor({state:'visible'});
    assert.match(await page.locator('#b-redirect-message').innerText(),/发布版本.*变化|重新扫描|刷新状态/);
    assert.equal(await page.locator('#b-redirect-version').inputValue(),'saved-2');
    assert.deepEqual(await page.locator('#b-redirect-occurrences input[type=checkbox]').evaluateAll(items=>items.map(item=>item.checked)),[true,false]);
    assert.equal(fixture.scans.length,scansBeforeConflict,'a conflict keeps the original publication expectation until an explicit scan');
    await presetRow(page,1).getByRole('button',{name:'换成这条',exact:true}).click();
    await waitForFixture(()=>fixture.applies.length===4,'conflict can be retried with the retained selection');
    assert.deepEqual(fixture.applies[3].body,{version_id:'saved-2',preset_id:1,occurrence_ids:['href-saved-2'],expected_published:'saved-2'});
    await presetRow(page,3).getByRole('button',{name:'删除',exact:true}).click();
    await page.locator('.confirm-dialog').getByRole('button',{name:'确认',exact:true}).click();
    await presetRow(page,3).waitFor({state:'detached'});
    assert.deepEqual(fixture.deletes,[3]);
    assert.equal(fixture.active.url,'https://unknown.example/landing','deleting a preset retains the published target');
  });
});

test('a newly uploaded B version is selected for scanning without publishing it',async()=>{
  await withConsole(async(page,fixture)=>{
    await openRedirects(page);
    const html='<a href="https://old.example/landing">目标</a><script>window.uploadExecuted=true</script>';
    await page.locator('#slot-B input[type=file]').setInputFiles({name:'new-b.html',mimeType:'text/html',buffer:Buffer.from(html)});
    await page.locator('#slot-B').getByRole('button',{name:'上传并创建版本',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#b-redirect-version').value==='upload-b' && document.querySelector('#b-redirect-occurrences input[type=checkbox]')?.value==='href-upload-b');
    assert.deepEqual(fixture.uploads,[{name:'new-b.html',body:html,csrf:'csrf'}]);
    assert.equal(fixture.published.B,'live-b');
    assert.equal(fixture.scans.at(-1),'upload-b');
    assert.equal(await page.evaluate(()=>window.uploadExecuted),undefined);
    assert.match(await page.locator('#b-redirect-current').innerText(),/未发布版本/);
  });
});

test('losing site ownership clears the panel and ignores a late redirect scan',async()=>{
  await withConsole(async(page,fixture)=>{
    await openRedirects(page);
    await page.locator('#b-redirect-urls').fill('https://private-draft.example');
    await page.locator('#b-redirect-note').fill('私人备注');
    fixture.delayVersion='import-b';
    await page.locator('#b-redirect-version').selectOption('import-b');
    await waitForFixture(()=>fixture.releaseScan,'the old version scan is in flight');
    fixture.sites=[];
    await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));
    await page.locator('#no-sites').waitFor({state:'visible'});
    const lateResponse=page.waitForResponse(response=>response.url().includes('/b-redirects?version_id=import-b'));
    fixture.releaseScan();
    await (await lateResponse).finished();
    await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(resolve)));
    await page.waitForFunction(()=>document.querySelector('#site-selector').disabled);
    assert.equal(await page.locator('#b-redirect-urls').inputValue(),'');
    assert.equal(await page.locator('#b-redirect-note').inputValue(),'');
    assert.equal(await page.locator('#b-redirect-version option').count(),0);
    assert.equal(await page.locator('#b-redirect-occurrences input').count(),0);
    assert.equal(await page.locator('#b-redirect-presets [data-preset-id]').count(),0);
    assert.equal(await page.locator('#b-redirect-current').innerText(),'');
    assert.equal(await page.locator('#b-redirect-message').innerText(),'');
    assert.equal(await page.locator('#b-redirect-add').isDisabled(),true);
  });
});

test('a publication refresh invalidates an older same-site scan and preserves B draft exclusions',async()=>{
  await withConsole(async(page,fixture)=>{
    await openRedirects(page);
    await page.locator('#b-redirect-occurrences input[type=checkbox]').nth(1).uncheck();
    await page.locator('#b-redirect-urls').fill('https://draft.example/');
    await page.locator('#b-redirect-note').fill('保留草稿');
    fixture.delayVersion='import-b';
    await page.locator('#b-redirect-version').selectOption('import-b');
    await waitForFixture(()=>fixture.releaseScan,'the old scan starts');
    fixture.published.B='new-live';
    fixture.versions.push(version('B','new-live'));
    fixture.byVersion['new-live']=occurrences('new-live');
    await page.locator('#refresh-state').click();
    await page.waitForFunction(()=>document.querySelector('#slot-live-B').textContent.includes('new-live'));
    const lateResponse=page.waitForResponse(response=>response.url().includes('/b-redirects?version_id=import-b'));
    fixture.releaseScan();
    await (await lateResponse).finished();
    await page.waitForFunction(()=>document.querySelector('#b-redirects').getAttribute('aria-busy')==='false');
    assert.equal(await page.locator('#b-redirect-version').inputValue(),'new-live');
    assert.deepEqual(await page.locator('#b-redirect-occurrences input[type=checkbox]').evaluateAll(items=>items.map(item=>[item.value,item.checked])),[['href-new-live',true],['script-new-live',false]]);
    assert.equal(await page.locator('#b-redirect-urls').inputValue(),'https://draft.example/');
    assert.equal(await page.locator('#b-redirect-note').inputValue(),'保留草稿');
    await presetRow(page,1).getByRole('button',{name:'换成这条',exact:true}).click();
    await waitForFixture(()=>fixture.applies.length===1,'the refreshed B version is applied');
    assert.deepEqual(fixture.applies[0].body,{version_id:'new-live',preset_id:1,occurrence_ids:['href-new-live'],expected_published:'new-live'});
    assert.equal(fixture.published.A,'live-a');
  });
});

test('a late scan retains presets saved during scanning and their automatic check results',async()=>{
  await withConsole(async(page,fixture)=>{
    await openRedirects(page);
    await page.locator('#b-redirect-occurrences input[type=checkbox]').nth(1).uncheck();
    fixture.delayVersion='live-b';fixture.delayCheck=101;
    await page.locator('#b-redirects').getByRole('button',{name:'重新扫描',exact:true}).click();
    await waitForFixture(()=>fixture.releaseScan,'the old preset snapshot is pending');
    await page.locator('#b-redirect-urls').fill('https://new.example/');
    await page.locator('#b-redirect-add').click();
    await presetRow(page,101).waitFor({state:'visible'});
    await waitForFixture(()=>fixture.releaseCheck,'the new preset automatic check starts');
    await page.locator('#b-redirect-urls').fill('https://next-draft.example/');
    await page.locator('#b-redirect-note').fill('下一条草稿');
    const lateResponse=page.waitForResponse(response=>response.url().includes('/b-redirects?version_id=live-b'));
    fixture.releaseScan();
    await (await lateResponse).finished();
    await page.waitForFunction(()=>document.querySelector('#b-redirects').getAttribute('aria-busy')==='false');
    assert.equal(await page.locator('#b-redirect-presets [data-preset-id]').count(),4);
    assert.equal(await presetRow(page,101).count(),1);
    assert.equal(await page.locator('#b-redirect-urls').inputValue(),'https://next-draft.example/');
    assert.equal(await page.locator('#b-redirect-note').inputValue(),'下一条草稿');
    assert.deepEqual(await page.locator('#b-redirect-occurrences input[type=checkbox]').evaluateAll(items=>items.map(item=>item.checked)),[true,false]);
    fixture.releaseCheck();
    await page.waitForFunction(()=>document.querySelector('[data-preset-id="101"] .b-redirect-check')?.textContent==='正常');
    assert.equal(fixture.presets.length,4);
  });
});

test('a WhatsApp jump is labeled WhatsApp beside the other page link', async () => {
  await withConsole(async (page, fixture) => {
    fixture.byVersion['live-b'] = [
      {id:'yt-live-b', key:'index.html:0', path:'index.html', line:225, kind:'anchor', url:'https://www.youtube.com/@EconManBlog/shorts'},
      {id:'wa-live-b', key:'index.html:1', path:'index.html', line:512, kind:'js_variable', url:'https://api.whatsapp.com/send?phone=85257980601&text=你好，黑马'}
    ];
    await openRedirects(page);
    const text = await page.locator('#b-redirect-occurrences').innerText();
    assert.match(text, /页面链接/);
    assert.match(text, /WhatsApp/);
    assert.match(text, /85257980601/);
    assert.match(text, /你好，黑马/);
    assert.doesNotMatch(text, /跳转变量/);
  });
});

test('WhatsApp number box replaces only the checked phone and leaves link apply alone', async () => {
  await withConsole(async (page, fixture) => {
    fixture.byVersion['live-b'] = [
      ...occurrences('live-b'),
      {id:'wa-live-b', key:'index.html:wa', path:'index.html', line:12, kind:'whatsapp_number', url:'85264150954'}
    ];
    fixture.numbers = [{id:9, phone:'85299990000', note:'一线', created:1}];
    await page.locator('[data-tab="content"]').click();
    await page.waitForFunction(() => document.querySelectorAll('#b-redirect-occurrences input[type=checkbox]').length === 3);
    assert.match(await page.locator('#b-wa').innerText(), /WS 号码/);
    assert.match(await page.locator('#b-redirect-numbers').innerText(), /85299990000/);
    assert.match(await page.locator('#b-redirect-occurrences').innerText(), /WhatsApp 号码/);
    assert.match(await page.locator('#b-redirect-occurrences').innerText(), /85264150954/);
    assert.equal(await page.locator('#b-redirect-numbers .b-redirect-active-badge').count(), 0);
    await page.locator('#b-wa-numbers').fill('85211112222\n+852 3333 4444');
    await page.locator('#b-wa-add').click();
    await page.locator('#b-redirect-numbers').getByText('85233334444', {exact:true}).waitFor();
    await page.locator('#b-redirect-numbers .b-redirect-check', {hasText:'未确认'}).nth(1).waitFor();
    assert.deepEqual(fixture.numberAdds[0].phones, ['85211112222', '85233334444']);
    assert.deepEqual(fixture.trustChecks, [21, 22]);
    const numberApply = page.waitForResponse(response => response.url().includes('/b-redirects/numbers/apply'));
    await page.locator('#b-redirect-numbers [data-number-id="9"]').getByRole('button', {name:'换成这个号码', exact:true}).click();
    await numberApply;
    assert.deepEqual(fixture.numberApplies[0].body.occurrence_ids, ['wa-live-b']);
    assert.equal(fixture.numberApplies[0].body.number_id, 9);
    await page.locator('#b-redirect-numbers [data-number-id="9"] .b-redirect-active-badge').waitFor();
    const linkApply = page.waitForResponse(response => response.url().includes('/b-redirects/apply') && !response.url().includes('/numbers'));
    await page.locator('#b-redirect-presets [data-preset-id="1"]').getByRole('button', {name:'换成这条', exact:true}).click();
    await linkApply;
    assert.deepEqual(fixture.applies.at(-1).body.occurrence_ids, ['anchor-wa-1', 'js_location-wa-1']);
    assert.equal(fixture.applies.at(-1).body.occurrence_ids.includes('whatsapp_number-wa-1'), false);
  });
});

test('clicking another number moves the current-number mark onto it', async () => {
  await withConsole(async (page, fixture) => {
    fixture.byVersion['live-b'] = [
      ...occurrences('live-b'),
      {id:'wa-live-b', key:'index.html:wa', path:'index.html', line:12, kind:'whatsapp_number', url:'85264150954'}
    ];
    fixture.numbers = [
      {id:8, phone:'85264150954', note:'旧号', created:1},
      {id:9, phone:'85299990000', note:'新号', created:1}
    ];
    await page.locator('[data-tab="content"]').click();
    await page.locator('#b-redirect-numbers [data-number-id="8"] .b-redirect-active-badge').waitFor();
    const numberApply = page.waitForResponse(response => response.url().includes('/b-redirects/numbers/apply'));
    await page.locator('#b-redirect-numbers [data-number-id="9"]').getByRole('button', {name:'换成这个号码', exact:true}).click();
    await page.locator('#b-redirect-numbers [data-number-id="9"] .b-redirect-active-badge').waitFor();
    assert.equal(await page.locator('#b-redirect-numbers [data-number-id="8"] .b-redirect-active-badge').count(), 0);
    await numberApply;
    assert.equal(fixture.numberApplies.at(-1).body.number_id, 9);
    assert.deepEqual(fixture.numberApplies.at(-1).body.occurrence_ids, ['wa-live-b']);
    await page.locator('.b-wa-status').getByText('当前号码已换成 85299990000').waitFor();
  });
});

test('clicking the number already on the page keeps the mark and does not publish', async () => {
  await withConsole(async (page, fixture) => {
    fixture.byVersion['live-b'] = [
      ...occurrences('live-b'),
      {id:'wa-live-b', key:'index.html:wa', path:'index.html', line:12, kind:'whatsapp_number', url:'85264150954'}
    ];
    fixture.numbers = [
      {id:8, phone:'85264150954', note:'旧号', created:1},
      {id:9, phone:'85299990000', note:'新号', created:1}
    ];
    await page.locator('[data-tab="content"]').click();
    await page.locator('#b-redirect-numbers [data-number-id="8"] .b-redirect-active-badge').waitFor();
    await page.locator('#b-redirect-numbers [data-number-id="8"]').getByRole('button', {name:'换成这个号码', exact:true}).click();
    await page.locator('#b-redirect-message').getByText('这个号码已经是当前号码。').waitFor();
    await page.locator('.b-wa-status').getByText('这个号码已经是当前号码。').waitFor();
    assert.equal(fixture.numberApplies.length, 0);
    assert.equal(await page.locator('#b-redirect-numbers [data-number-id="8"] .b-redirect-active-badge').count(), 1);
    assert.equal(await page.locator('#b-redirect-numbers [data-number-id="9"] .b-redirect-active-badge').count(), 0);
  });
});

test('current number shows trust, clear, and unconfirmed without polling until both exist', async () => {
  const base = [
    ...occurrences('live-b'),
    {id:'wa-live-b', key:'index.html:wa', path:'index.html', line:12, kind:'whatsapp_number', url:'85299990000'}
  ];
  await withConsole(async (page, fixture) => {
    fixture.byVersion['live-b'] = base;
    fixture.trust_poll = false;
    fixture.numbers = [{id:9, phone:'85299990000', note:'', created:1, trust:{status:'trust', detail:'出现信任弹窗', checked_at:2}}];
    await page.locator('[data-tab="content"]').click();
    await page.locator('#b-redirect-numbers [data-number-id="9"] .b-redirect-check.abnormal').getByText('出现信任弹窗').waitFor();
    await page.locator('#wa-trust-alarm').getByText('当前号码出现信任弹窗。').waitFor();
    assert.equal(await page.locator('#b-wa').getAttribute('data-trust-poll'), '0');
    assert.deepEqual(fixture.trustChecks, []);
  });
  await withConsole(async (page, fixture) => {
    fixture.byVersion['live-b'] = base;
    fixture.trust_poll = true;
    fixture.numbers = [{id:9, phone:'85299990000', note:'', created:1, trust:{status:'clear', detail:'正常', checked_at:2}}];
    await page.locator('[data-tab="content"]').click();
    await page.locator('#b-redirect-numbers [data-number-id="9"] .b-redirect-check.normal').getByText('正常').waitFor();
    assert.equal(await page.locator('#wa-trust-alarm').count(), 1);
    assert.equal(await page.locator('#wa-trust-alarm').isHidden(), true);
    assert.equal(await page.locator('#b-wa').getAttribute('data-trust-poll'), '1');
  });
  await withConsole(async (page, fixture) => {
    fixture.byVersion['live-b'] = base;
    fixture.numbers = [{id:9, phone:'85299990000', note:'', created:1, trust:{status:'unconfirmed', detail:'这次没看清', checked_at:2}}];
    await page.locator('[data-tab="content"]').click();
    await page.locator('#b-redirect-numbers [data-number-id="9"] .b-redirect-check.unknown').getByText('未确认').waitFor();
    assert.equal(await page.locator('#wa-trust-alarm').isHidden(), true);
    assert.equal(await page.locator('#b-wa').getAttribute('data-trust-poll'), '0');
    const recheck = page.waitForResponse(response => response.url().includes('/numbers/9/trust'));
    await page.locator('#b-redirect-numbers [data-number-id="9"]').getByRole('button', {name:'再测一次', exact:true}).click();
    await recheck;
    assert.deepEqual(fixture.trustChecks, [9]);
  });
});

test('trust recheck interval accepts 10 seconds or longer and waits that long', async () => {
  const base = [
    ...occurrences('live-b'),
    {id:'wa-live-b', key:'index.html:wa', path:'index.html', line:12, kind:'whatsapp_number', url:'85299990000'}
  ];
  await withConsole(async (page, fixture) => {
    fixture.byVersion['live-b'] = base;
    fixture.trust_poll = true;
    fixture.numbers = [{id:9, phone:'85299990000', note:'', created:1, trust:{status:'clear', detail:'正常', checked_at:2}}];
    await page.locator('[data-tab="content"]').click();
    await page.locator('#b-wa-trust-note').getByText('每 10 秒复查').waitFor();
    assert.equal(await page.locator('#b-wa-interval').inputValue(), '10');
    await page.locator('#b-wa-interval').fill('9');
    await page.getByRole('button', {name:'按这个时间', exact:true}).click();
    await page.locator('#b-redirect-message').getByText('最短 10 秒。').waitFor();
    assert.equal(await page.locator('#b-wa').getAttribute('data-trust-seconds'), '10');
    await page.locator('#b-wa-interval').fill('30');
    await page.getByRole('button', {name:'按这个时间', exact:true}).click();
    await page.locator('#b-wa-trust-note').getByText('每 30 秒复查').waitFor();
    await page.locator('#b-redirect-message').getByText('已改为每 30 秒复查。').waitFor();
    assert.equal(await page.locator('#b-wa').getAttribute('data-trust-seconds'), '30');
    await page.clock.fastForward(10000);
    assert.deepEqual(fixture.trustChecks, []);
    const seen = page.waitForResponse(response => response.url().includes('/numbers/9/trust'));
    await page.clock.fastForward(20000);
    await seen;
    assert.deepEqual(fixture.trustChecks, [9]);
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#rules-fields').disabled === false);
    await page.locator('[data-tab="content"]').click();
    await page.locator('#b-wa-trust-note').getByText('每 30 秒复查').waitFor();
    assert.equal(await page.locator('#b-wa-interval').inputValue(), '30');
  }, {clock:true});
});

test('a trusted current number switches to the next spare, and a lone number alarms', async () => {
  const wa = phone => ({id:'wa-live-b', key:'index.html:wa', path:'index.html', line:12, kind:'whatsapp_number', url:phone});
  await withConsole(async (page, fixture) => {
    fixture.byVersion['live-b'] = [...occurrences('live-b'), wa('85299990000')];
    fixture.numbers = [
      {id:9, phone:'85299990000', note:'', created:1, trust:{status:'clear', detail:'正常', checked_at:2}},
      {id:10, phone:'85264150954', note:'', created:1, trust:{status:'clear', detail:'正常', checked_at:2}}
    ];
    fixture.trustResults[9] = 'trust';
    await page.locator('[data-tab="content"]').click();
    await page.locator('#b-redirect-numbers [data-number-id="9"] .b-redirect-active-badge').waitFor();
    const applied = page.waitForResponse(response => response.url().includes('/numbers/apply'));
    await page.locator('#b-redirect-numbers [data-number-id="9"]').getByRole('button', {name:'再测一次', exact:true}).click();
    await applied;
    await page.locator('.b-wa-status').getByText('已自动换成下一个').waitFor();
    assert.equal(fixture.numberApplies.at(-1).body.number_id, 10);
    assert.deepEqual(fixture.numberApplies.at(-1).body.occurrence_ids, ['wa-live-b']);
    await page.locator('#b-redirect-numbers [data-number-id="10"] .b-redirect-active-badge').waitFor();
    assert.equal(await page.locator('#wa-trust-alarm').isHidden(), true);
  });
  await withConsole(async (page, fixture) => {
    fixture.byVersion['live-b'] = [...occurrences('live-b'), wa('85299990000')];
    fixture.numbers = [
      {id:9, phone:'85299990000', note:'', created:1, trust:{status:'clear', detail:'正常', checked_at:2}},
      {id:10, phone:'85264150954', note:'', created:1, trust:{status:'trust', detail:'出现信任弹窗', checked_at:2}},
      {id:11, phone:'85200003333', note:'', created:1, trust:{status:'clear', detail:'正常', checked_at:2}}
    ];
    fixture.trustResults[9] = 'trust';
    await page.locator('[data-tab="content"]').click();
    const applied = page.waitForResponse(response => response.url().includes('/numbers/apply'));
    await page.locator('#b-redirect-numbers [data-number-id="9"]').getByRole('button', {name:'再测一次', exact:true}).click();
    await applied;
    assert.equal(fixture.numberApplies.at(-1).body.number_id, 11);
    assert.equal(await page.locator('#wa-trust-alarm').isHidden(), true);
  });
  await withConsole(async (page, fixture) => {
    fixture.byVersion['live-b'] = [...occurrences('live-b'), wa('85299990000')];
    fixture.numbers = [{id:9, phone:'85299990000', note:'', created:1, trust:{status:'clear', detail:'正常', checked_at:2}}];
    fixture.trustResults[9] = 'trust';
    await page.locator('[data-tab="content"]').click();
    const recheck = page.waitForResponse(response => response.url().includes('/numbers/9/trust'));
    await page.locator('#b-redirect-numbers [data-number-id="9"]').getByRole('button', {name:'再测一次', exact:true}).click();
    await recheck;
    await page.locator('#wa-trust-alarm').getByText('当前号码出现信任弹窗。').waitFor();
    assert.equal(fixture.numberApplies.length, 0);
  });
});

test('work order over three online switches the published number and shows lead detail', async () => {
  await withConsole(async (page, fixture) => {
    fixture.byVersion['live-b'] = [
      ...occurrences('live-b'),
      {id:'wa-live-b', key:'index.html:wa', path:'index.html', line:12, kind:'whatsapp_number', url:'85299990000'}
    ];
    fixture.numbers = [
      {id:9, phone:'85299990000', note:'', created:1, trust:{status:'clear', detail:'正常', checked_at:2}},
      {id:10, phone:'85200002222', note:'', created:1, trust:{status:'clear', detail:'正常', checked_at:2}}
    ];
    fixture.deskSwitch = 'online';
    await page.locator('[data-tab="content"]').click();
    await page.locator('#b-redirect-numbers [data-number-id="9"] .b-redirect-active-badge').waitFor();
    await page.locator('[data-tab="desk"]').click();
    await page.locator('#desk-url').fill('https://admin.haiwangweb.com/web#/accountshow/sampleTicket');
    await page.locator('#desk-name').fill('鳄鱼-梵高');
    await page.locator('#desk-password').fill('secret');
    await page.getByRole('button', {name:'添加工单', exact:true}).click();
    await page.locator('#desk-date').fill('10/06');
    await page.locator('#desk-project').fill('HK项目');
    await page.locator('#desk-buyer').fill('AJ');
    await page.locator('#desk-phone').fill('85299990000');
    await page.locator('#desk-spend').fill('28.95');
    await page.locator('#desk-leads').fill('0.8');
    await page.getByRole('button', {name:'生成金额', exact:true}).click();
    await page.locator('#desk-preview').getByText('成本：41.61').waitFor();
    const applied = page.waitForResponse(response => response.url().includes('/numbers/apply'));
    await page.locator('#desk-check').click();
    await applied;
    await page.locator('.desk-message').getByText('已自动换成下一个预存号码').waitFor();
    await page.locator('.desk-lead').getByText('鳄鱼-梵高').waitFor();
    assert.equal(fixture.deskReviews.at(-1).phone, '85299990000');
    assert.equal(fixture.deskReviews.at(-1).tickets[0].password, 'secret');
    assert.equal(fixture.numberApplies.at(-1).body.number_id, 10);
    assert.equal(await page.locator('#b-redirect-numbers [data-number-id="10"] .b-redirect-active-badge').count(), 1);
  });
  await withConsole(async (page, fixture) => {
    fixture.byVersion['live-b'] = [
      ...occurrences('live-b'),
      {id:'wa-live-b', key:'index.html:wa', path:'index.html', line:12, kind:'whatsapp_number', url:'85299990000'}
    ];
    fixture.numbers = [{id:9, phone:'85299990000', note:'', created:1, trust:{status:'clear', detail:'正常', checked_at:2}}];
    fixture.deskSwitch = 'offline';
    await page.locator('[data-tab="content"]').click();
    await page.locator('#b-redirect-numbers [data-number-id="9"] .b-redirect-active-badge').waitFor();
    await page.locator('[data-tab="desk"]').click();
    await page.locator('#desk-url').fill('https://admin.haiwangweb.com/web#/accountshow/sampleTicket');
    await page.getByRole('button', {name:'添加工单', exact:true}).click();
    await page.locator('#desk-check').click();
    await page.locator('.desk-alarm').getByText('当前号码离线').waitFor();
    assert.equal(fixture.numberApplies.length, 0);
  });
});
