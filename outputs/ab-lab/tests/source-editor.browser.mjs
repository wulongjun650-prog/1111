// Run with playwright on NODE_PATH and Jinja2 on PYTHON, like account-console.browser.mjs.
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
const source = '<a href="https://old.example">链接</a><script>window.sourceExecuted=true</script>';

async function withConsole(run) {
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
  const fixture={sites:[site],published:{A:'live-a',B:'live-b'},versions:[version('A','live-a'),version('A','import-a'),version('B','live-b')],files:{'index.html':source,'assets/main.js':'location.href = "https://old.example";','assets/style.css':'body { color: navy; }'},writes:[],reads:[],saveStatus:200,readStatus:200,delayPath:null,releaseRead:null};
  page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/api/**',async route=>{
    const request=route.request(),url=new URL(request.url()),pathname=url.pathname;
    let response,status=200;
    if(pathname==='/api/sites') response={account:{role:'agent'},sites:fixture.sites,google_reputation_configured:false};
    else if(pathname===`/api/sites/${siteId}/state`) response={site,config:{routing:'RULES',allowed_slot:'B',protection:true,content_mode:'PAGE',distribution:'random',rules:{}},versions:fixture.versions,slots:fixture.published,links:[],health:{geoip:true},target_url:'http://127.0.0.1:9000',revision:1};
    else if(pathname.endsWith('/analytics')) response={period:'today',end:new Date().toISOString(),summary:{total:0,allowed:0,blocked:0,rate:0},trend:[],domains:[],countries:[],reasons:[],recent:[]};
    else if(pathname===`/api/sites/${siteId}/b-redirects`) response={version:fixture.versions.find(item=>item.id===(url.searchParams.get('version_id')||fixture.published.B))||null,published_version:fixture.published.B,occurrences:[],warnings:[],presets:[],active:null};
    else if(pathname.startsWith(`/api/sites/${siteId}/source/`)) {
      const [,slot,id]=pathname.match(/\/source\/([^/]+)\/([^/]+)$/);
      if(request.method()==='POST') {
        fixture.writes.push({slot,id,body:request.postDataJSON(),csrf:request.headers()['x-csrf-token']});
        status=fixture.saveStatus;
        if(status===200) {
          const saved=version(slot,`saved-${fixture.writes.length}`);
          fixture.versions.push(saved); fixture.published[slot]=saved.id;
          fixture.files[request.postDataJSON().path]=request.postDataJSON().content;
          response={version:saved,published_version:saved.id};
        } else response={detail:'save rejected'};
      } else {
        fixture.reads.push({slot,id,path:url.searchParams.get('path')});
        if(url.searchParams.has('path')) {
          if(fixture.delayPath===url.searchParams.get('path')) await new Promise(resolve=>{fixture.releaseRead=resolve;});
          status=fixture.readStatus;
          response={path:url.searchParams.get('path'),content:fixture.files[url.searchParams.get('path')],published_version:fixture.published[slot]};
        } else response={files:Object.entries(fixture.files).map(([path,content])=>({path,bytes:content.length})),published_version:fixture.published[slot]};
      }
    } else throw new Error(`Unexpected endpoint ${pathname}`);
    await route.fulfill({status,json:response});
  });
  try {
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.waitForFunction(()=>document.querySelector('#rules-fields').disabled===false);
    await page.locator('[data-tab="content"]').click();
    await run(page,fixture);
    assert.deepEqual(errors,[]);
  } finally {fixture.releaseRead?.();await browser.close();await new Promise(resolve=>server.close(resolve));}
}

async function openLive(page,slot='A') {
  const entry=page.locator(`#slot-live-${slot}`).getByRole('button',{name:'编辑源码',exact:true});
  assert.equal(await entry.count(),1,'published content exposes the source editor');
  await entry.click();
  await page.waitForFunction(()=>document.querySelector('#source-editor-content')?.readOnly===false);
}

test('published A/B source stays text, saves one file immediately, and supports repeated keyboard saves',async()=>{
  await withConsole(async(page,fixture)=>{
    await openLive(page);
    const editor=page.locator('#source-editor-content');
    assert.equal(await editor.inputValue(),source);
    assert.equal(await page.evaluate(()=>window.sourceExecuted),undefined);
    assert.match(await page.locator('#source-editor-context').innerText(),/mine\.example\.com.*A/);
    const bounds=await editor.boundingBox();assert.ok(bounds.width>900 && bounds.height>400);
    assert.equal(await page.getByRole('button',{name:'保存并发布',exact:true}).isDisabled(),true);
    await editor.fill(source.replace('old.example','new.example'));
    await editor.press('Control+s');
    await page.waitForFunction(()=>document.querySelector('#source-editor-status').textContent.includes('已保存并发布'));
    assert.deepEqual(fixture.writes[0],{slot:'A',id:'live-a',body:{path:'index.html',content:source.replace('old.example','new.example'),expected_published:'live-a'},csrf:'csrf'});
    assert.match(await page.locator('#slot-live-A').innerText(),/saved-1/);
    await editor.fill(source.replace('old.example','second.example'));
    await page.getByRole('button',{name:'保存并发布',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#source-editor-status').textContent.includes('已保存并发布'));
    assert.equal(fixture.writes[1].id,'saved-1');
    assert.equal(fixture.writes[1].body.expected_published,'saved-1');
    await page.locator('#source-editor').getByRole('button',{name:'取消',exact:true}).click();
    await openLive(page,'B');
    assert.match(await page.locator('#source-editor-context').innerText(),/B/);
    assert.equal(fixture.reads.at(-1).slot,'B');
  });
});

test('imported versions retain initial published revision and confirm before discarding a file draft',async()=>{
  await withConsole(async(page,fixture)=>{
    const row=page.locator('#versions-A .version-row').filter({hasText:'A-import-a.zip'});
    await row.getByRole('button',{name:'编辑源码',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#source-editor-content')?.readOnly===false);
    const editor=page.locator('#source-editor-content'),file=page.locator('#source-editor-file');
    await editor.fill('private draft');
    await file.selectOption('assets/main.js');
    await page.locator('.confirm-dialog').getByRole('button',{name:'取消',exact:true}).click();
    assert.equal(await editor.inputValue(),'private draft');
    assert.equal(await file.inputValue(),'index.html');
    fixture.published.A='another-published';
    await file.selectOption('assets/main.js');
    await page.locator('.confirm-dialog').getByRole('button',{name:'确认',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#source-editor-content').value.startsWith('location.href'));
    await editor.fill('location.href = "https://new.example";');
    await page.getByRole('button',{name:'保存并发布',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#source-editor-status').textContent.includes('已保存并发布'));
    assert.equal(fixture.writes[0].id,'import-a');
    assert.equal(fixture.writes[0].body.expected_published,'live-a');
    assert.equal(fixture.writes[0].body.path,'assets/main.js');
    await editor.fill('unsaved again');
    await editor.press('Escape');
    await page.locator('.confirm-dialog').getByRole('button',{name:'取消',exact:true}).click();
    assert.equal(await editor.inputValue(),'unsaved again');
    await editor.press('Escape');
    await page.locator('.confirm-dialog').getByRole('button',{name:'确认',exact:true}).click();
    await page.locator('#source-editor').waitFor({state:'hidden'});
    assert.equal(await editor.inputValue(),'');
  });
});

test('read failures and conflicting or failed saves leave the draft editable and unpublished',async()=>{
  await withConsole(async(page,fixture)=>{
    await openLive(page);
    const editor=page.locator('#source-editor-content');
    await editor.fill('keep this draft');
    fixture.readStatus=500;
    await page.locator('#source-editor-file').selectOption('assets/style.css');
    await page.locator('.confirm-dialog').getByRole('button',{name:'确认',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#source-editor-error').textContent.length>0);
    assert.equal(await editor.inputValue(),'keep this draft');
    assert.equal(await page.locator('#source-editor-file').inputValue(),'index.html');
    for(const status of [409,500]) {
      fixture.saveStatus=status;
      await page.getByRole('button',{name:'保存并发布',exact:true}).click();
      await page.waitForFunction(()=>document.querySelector('#source-editor-content').readOnly===false && document.querySelector('#source-editor-error').textContent.length>0);
      assert.equal(await editor.inputValue(),'keep this draft');
      if(status===409)assert.match(await page.locator('#source-editor-error').innerText(),/重新打开|最新版本/);
    }
    assert.equal(fixture.published.A,'live-a');
    assert.equal(await page.getByRole('button',{name:'保存并发布',exact:true}).isDisabled(),false);
  });
});

test('ownership loss clears an open source editor and a late file response cannot restore site data',async()=>{
  await withConsole(async(page,fixture)=>{
    await openLive(page);
    fixture.delayPath='assets/main.js';
    await page.locator('#source-editor-file').selectOption('assets/main.js');
    while(!fixture.releaseRead)await new Promise(resolve=>setTimeout(resolve,10));
    fixture.sites=[];
    await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));
    await page.locator('#no-sites').waitFor({state:'visible'});
    assert.equal(await page.locator('#source-editor').isVisible(),false);
    fixture.releaseRead();
    await page.waitForFunction(()=>document.querySelector('#site-selector').disabled);
    assert.equal(await page.locator('#source-editor-content').inputValue(),'');
    assert.equal(await page.locator('#source-editor-file option').count(),0);
    assert.equal(await page.locator('#source-editor-context').innerText(),'');
  });
});
