// Run with playwright on NODE_PATH and a Python environment containing Jinja2 on PYTHON.
import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import fs from 'node:fs/promises';
import {execFileSync} from 'node:child_process';
import {createRequire} from 'node:module';
import path from 'node:path';

const {chromium} = createRequire(import.meta.url)('playwright');
const root = path.resolve(import.meta.dirname,'..');
const render = role => execFileSync(process.env.PYTHON || 'python',['-X','utf8','-c',
  'from jinja2 import Environment,FileSystemLoader; import sys; print(Environment(loader=FileSystemLoader("templates")).get_template("index.html").render(account={"role":sys.argv[1]},username="viewer",deployment=False,csrf_token="csrf",target_url="http://127.0.0.1:9000"))',role],{cwd:root,encoding:'utf8'});
const documents = {admin:render('admin'),agent:render('agent'),observer:render('observer')};
const siteId = 'a'.repeat(32);
const agent = {id:'agent-id',username:'viewer',role:'agent',enabled:true};
const admin = {id:'admin',username:'boss',role:'admin',enabled:true};
const site = {id:siteId,domain:'mine.example.com',owner_id:agent.id,owner_username:agent.username,enabled:true,stage:'waiting_dns',note:'保留备注'};
const siteState = {site,config:{routing:'RULES',allowed_slot:'B',protection:true,content_mode:'PAGE',distribution:'random',rules:{}},versions:[],slots:{A:null,B:null},links:[],health:{geoip:true},target_url:'http://127.0.0.1:9000',revision:1};
const analytics = {period:'today',end:new Date().toISOString(),tz_offset:0,summary:{total:0,allowed:0,blocked:0,rate:0,a:0,b:0,other:0},trend:[],domains:[],countries:[],reasons:[],recent:[]};

async function withConsole(role, run) {
  const browser = await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
  const server = http.createServer(async (req,res)=>{
    if(req.url==='/') {
      res.setHeader('Content-Type','text/html; charset=utf-8');
      res.setHeader('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'");
      res.end(documents[role]);
      return;
    }
    if(req.url.startsWith('/static/')) {
      const file=path.join(root,req.url);
      try {res.setHeader('Content-Type',/\.(js|mjs)$/.test(file)?'text/javascript':file.endsWith('.css')?'text/css':'image/png');res.end(await fs.readFile(file));}
      catch {res.writeHead(404);res.end();}
      return;
    }
    res.writeHead(404);res.end();
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const page=await browser.newPage({viewport:{width:1440,height:1000}}), errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  try {await run(page,`http://127.0.0.1:${server.address().port}`,errors);}
  finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
}

test('empty agent starts with catalog only, can add a site, and clears lost-site data', async ()=>{
  await withConsole('agent',async(page,url,errors)=>{
    let sites=[];
    const requests=[];
    await page.route('**/api/**',async route=>{
      const request=route.request(),pathname=new URL(request.url()).pathname;
      requests.push(pathname);
      let response;
      if(pathname==='/api/sites') {if(request.method()==='POST')sites=[site];response={account:agent,sites,google_reputation_configured:false,server_ip:'203.0.113.1'};}
      else if(pathname===`/api/sites/${siteId}/state`)response=siteState;
      else if(pathname===`/api/sites/${siteId}/analytics`)response=analytics;
      else throw new Error(`Unexpected endpoint ${pathname}`);
      await route.fulfill({json:response});
    });
    await page.goto(url);await page.locator('#no-sites').waitFor({state:'visible'});
    assert.equal(await page.locator('#cf-choice').isHidden(),true);
    assert.deepEqual(requests,['/api/sites']);
    assert.equal(await page.locator('[data-tab="accounts"]').count(),0);
    assert.equal(await page.locator('[data-tab="rules"]').isDisabled(),true);
    assert.equal(await page.locator('#site-selector').isDisabled(),true);
    assert.equal(await page.locator('#panel-overview').evaluate(node=>node.inert),true);
    assert.doesNotMatch(await page.locator('body').innerText(),/API 密钥|后台密钥/);
    await page.locator('#empty-add-domain').click();
    await page.locator('#domain-form input').fill('mine.example.com');
    await page.locator('#domain-form button').click();
    await page.waitForFunction(()=>document.querySelector('#no-sites').hidden && document.querySelector('#rules-fields').disabled===false);
    assert.ok(requests.includes(`/api/sites/${siteId}/state`));
    assert.equal(await page.locator('#site-selector').inputValue(),siteId);
    await page.locator('[data-tab="overview"]').click();
    await page.waitForFunction(()=>document.querySelector('#analytics-status').textContent.includes('刚刚更新'));
    assert.match(await page.locator('#analytics-status').innerText(),/^我的域名/);
    await page.evaluate(()=>{
      document.querySelector('#links-form').elements.urls.value='https://private.example';
      document.querySelector('#rules-form').elements.blacklist.value='1.2.3.4';
      for(const id of ['logs-body','audit-list','links-list','traffic-trend','preview-frame'])document.querySelector(`#${id}`).textContent='PRIVATE';
      document.querySelector('#preview-panel').hidden=false;
    });
    sites=[];
    await page.locator('#refresh-state').click();
    await page.locator('#no-sites').waitFor({state:'visible'});
    assert.doesNotMatch(await page.locator('body').innerText(),/PRIVATE/);
    assert.equal(await page.locator('#links-form textarea').inputValue(),'');
    assert.equal(await page.locator('#rules-form [name="blacklist"]').inputValue(),'');
    assert.equal(await page.locator('#preview-frame').innerHTML(),'');
    assert.equal(await page.locator('#export-logs').getAttribute('href'),null);
    assert.equal(await page.locator('#open-target').getAttribute('href'),null);
    assert.ok(requests.every(value=>!value.includes('/default/')));
    assert.deepEqual(errors,[]);
  });
});

test('clicking the managed domain copies it and leaves the site selected',async()=>{
  await withConsole('admin',async(page,url,errors)=>{
    await page.context().grantPermissions(['clipboard-read','clipboard-write'],{origin:new URL(url).origin});
    await page.route('**/api/**',async route=>{
      const pathname=new URL(route.request().url()).pathname;
      if(pathname==='/api/sites') {await route.fulfill({json:{account:admin,sites:[site],google_reputation_configured:false,server_ip:'203.0.113.1'}});return;}
      if(pathname===`/api/sites/${siteId}/state`) {await route.fulfill({json:siteState});return;}
      if(pathname.endsWith('/analytics')) {await route.fulfill({json:analytics});return;}
      throw new Error(`Unexpected endpoint ${pathname}`);
    });
    await page.goto(url);
    const copy=page.locator('#copy-domain');
    await page.waitForFunction(domain=>document.querySelector('#copy-domain').textContent===domain,'mine.example.com');
    assert.equal(await copy.getAttribute('aria-label'),'复制域名 mine.example.com');
    await copy.click();
    await page.waitForFunction(()=>document.querySelector('.toast')?.textContent.includes('已复制 mine.example.com'));
    assert.equal(await page.evaluate(()=>navigator.clipboard.readText()),'mine.example.com');
    assert.equal(await page.locator('#site-selector').inputValue(),siteId);
    assert.deepEqual(errors,[]);
  });
});

test('admin creates, disables, resets, and assigns an agent without displaying passwords',async()=>{
  await withConsole('admin',async(page,url,errors)=>{
    let items=[admin,agent];const writes=[];
    await page.route('**/api/**',async route=>{
      const request=route.request(),pathname=new URL(request.url()).pathname;
      if(request.method()!=='GET')writes.push({pathname,method:request.method(),body:request.postDataJSON()});
      let response;
      if(pathname==='/api/sites')response={account:admin,sites:[site],google_reputation_configured:false,server_ip:'203.0.113.1'};
      else if(pathname===`/api/sites/${siteId}/state`)response=siteState;
      else if(pathname===`/api/sites/${siteId}/analytics`)response=analytics;
      else if(pathname==='/api/accounts') {
        if(request.method()==='POST')items.push({...agent,id:'new-agent',username:request.postDataJSON().username});
        response={items,account:items.at(-1)};
      }
      else if(pathname==='/api/accounts/agent-id') {items=items.map(item=>item.id===agent.id?{...item,enabled:request.postDataJSON().enabled}:item);response={account:items[1]};}
      else if(pathname==='/api/accounts/agent-id/password')response={ok:true};
      else if(pathname===`/api/sites/${siteId}/owner`)response={site};
      else throw new Error(`Unexpected endpoint ${pathname}`);
      await route.fulfill({json:response});
    });
    await page.goto(url);await page.waitForFunction(()=>document.querySelector('#rules-fields').disabled===false);
    await page.locator('[data-tab="accounts"]').click();
    await page.locator('#account-form [name="username"]').fill('new-agent');
    await page.locator('#account-form [name="password"]').fill('CREATE-secret-123');
    await page.locator('#account-form button').click();
    await page.waitForFunction(()=>document.querySelector('#account-form').elements.password.value==='');
    assert.doesNotMatch(await page.locator('body').innerText(),/CREATE-secret/);
    const row=page.locator('#account-list tr').filter({hasText:'viewer'});
    await row.getByRole('button',{name:'停用',exact:true}).click();
    await page.getByRole('dialog').getByRole('button',{name:'确认',exact:true}).click();
    await page.waitForFunction(()=>[...document.querySelectorAll('#account-list tr')].some(node=>node.textContent.includes('viewer')&&node.textContent.includes('已停用')));
    await row.getByRole('button',{name:'重设密码',exact:true}).click();
    await page.getByRole('dialog').locator('input').fill('RESET-secret-456');
    await page.getByRole('dialog').getByRole('button',{name:'重设密码',exact:true}).click();
    await page.getByRole('dialog').waitFor({state:'detached'});
    assert.doesNotMatch(await page.locator('body').innerText(),/RESET-secret/);
    await page.locator('[data-tab="domains"]').click();
    await page.locator('.domain-owner').getByRole('button',{name:'分配'}).click();
    assert.match(await page.getByRole('dialog').locator('select').innerText(),/viewer.*已停用/);
    await page.getByRole('dialog').locator('select').selectOption('admin');
    await page.getByRole('dialog').getByRole('button',{name:'保存归属'}).click();
    await page.getByRole('dialog').waitFor({state:'detached'});
    assert.ok(writes.some(value=>value.pathname==='/api/accounts'&&value.body.username==='new-agent'));
    assert.ok(writes.some(value=>value.pathname==='/api/accounts/agent-id'&&value.body.enabled===false));
    assert.ok(writes.some(value=>value.pathname==='/api/accounts/agent-id/password'&&value.body.password==='RESET-secret-456'));
    assert.ok(writes.some(value=>value.pathname===`/api/sites/${siteId}/owner`&&value.body.owner_id==='admin'));
    assert.deepEqual(errors,[]);
  });
});

test('404 after ownership loss reloads the catalog and opens the first remaining site',async()=>{
  await withConsole('agent',async(page,url,errors)=>{
    const nextId='b'.repeat(32), nextSite={...site,id:nextId,domain:'remaining.example.com'};
    let lost=false,oldStateCalls=0;
    await page.route('**/api/**',async route=>{
      const pathname=new URL(route.request().url()).pathname;
      if(pathname==='/api/sites') {await route.fulfill({json:{account:agent,sites:lost?[nextSite]:[site,nextSite],google_reputation_configured:false}});return;}
      if(pathname===`/api/sites/${siteId}/state`) {
        if(++oldStateCalls>1) {lost=true;await route.fulfill({status:404,json:{detail:'站点不存在'}});return;}
        await route.fulfill({json:siteState});return;
      }
      if(pathname===`/api/sites/${nextId}/state`) {await route.fulfill({json:{...siteState,site:nextSite}});return;}
      if(pathname.endsWith('/analytics')) {await route.fulfill({json:analytics});return;}
      throw new Error(`Unexpected endpoint ${pathname}`);
    });
    await page.goto(url);await page.waitForFunction(()=>document.querySelector('#rules-fields').disabled===false);
    await page.evaluate(()=>document.querySelector('#links-form').elements.urls.value='https://lost-private.example');
    await page.locator('#refresh-state').click();
    await page.waitForFunction(id=>document.querySelector('#site-selector').value===id && document.querySelector('#rules-fields').disabled===false,nextId);
    assert.equal(await page.locator('#links-form textarea').inputValue(),'');
    assert.match(await page.locator('#site-context').innerText(),/remaining.example.com/);
    assert.equal(await page.locator('#load-error').isVisible(),false);
    assert.deepEqual(errors,[]);
  });
});

test('catalog loss also clears open provision logs for a different domain',async()=>{
  await withConsole('agent',async(page,url,errors)=>{
    const otherId='b'.repeat(32), other={...site,id:otherId,domain:'other.example.com'};
    let sites=[site,other];
    await page.route('**/api/**',async route=>{
      const pathname=new URL(route.request().url()).pathname;
      if(pathname==='/api/sites') {await route.fulfill({json:{account:agent,sites,google_reputation_configured:false}});return;}
      if(pathname===`/api/sites/${siteId}/state`) {await route.fulfill({json:siteState});return;}
      if(pathname.endsWith('/analytics')) {await route.fulfill({json:analytics});return;}
      if(pathname===`/api/sites/${otherId}/provision/events`) {await route.fulfill({json:{items:[{created:1,stage:'failed',detail:'OTHER-PRIVATE-LOG'}]}});return;}
      throw new Error(`Unexpected endpoint ${pathname}`);
    });
    await page.goto(url);await page.waitForFunction(()=>document.querySelector('#rules-fields').disabled===false);
    await page.locator('[data-tab="domains"]').click();
    await page.locator('.domain-table tr').filter({hasText:'other.example.com'}).getByRole('button',{name:'日志',exact:true}).click();
    await page.locator('#provision-events-panel').waitFor({state:'visible'});
    sites=[site];await page.locator('#refresh-domains').click();
    await page.waitForFunction(()=>document.querySelectorAll('.domain-table tbody tr').length===1);
    assert.equal(await page.locator('#provision-events-panel').isVisible(),false);
    assert.equal(await page.locator('#provision-events').innerHTML(),'');
    assert.deepEqual(errors,[]);
  });
});

test('cloudflare stays unchecked unless the operator opts in',async()=>{
  await withConsole('agent',async(page,url,errors)=>{
    const bodies=[];
    let sites=[];
    await page.route('**/api/**',async route=>{
      const request=route.request(),pathname=new URL(request.url()).pathname;
      if(pathname==='/api/sites' && request.method()==='GET') {
        await route.fulfill({json:{account:agent,sites,google_reputation_configured:false,cloudflare_configured:true,cloudflare_template:'rules.example.com',server_ip:'8.8.8.8'}});
        return;
      }
      if(pathname==='/api/sites' && request.method()==='POST') {
        bodies.push(request.postDataJSON());
        sites=[site];
        await route.fulfill({json:{account:agent,sites,server_ip:'8.8.8.8',cloudflare:null,site}});
        return;
      }
      if(pathname===`/api/sites/${siteId}/cloudflare/status` && request.method()==='POST') {
        await route.fulfill({json:{site,cloudflare:{ok:true,status:site.cf_status || 'pending',active:false,nameservers:(site.cf_nameservers || '').split(',').filter(Boolean),detail:'NS 尚未生效'}}});
        return;
      }
      if(pathname===`/api/sites/${siteId}/state`) {await route.fulfill({json:siteState});return;}
      if(pathname.endsWith('/analytics')) {await route.fulfill({json:analytics});return;}
      throw new Error(`Unexpected endpoint ${pathname}`);
    });
    await page.goto(url);
    await page.locator('#empty-add-domain').click();
    await page.locator('#cf-choice').waitFor({state:'visible'});
    assert.match(await page.locator('#cf-choice-note').innerText(),/rules\.example\.com/);
    assert.equal(await page.locator('#use-cloudflare').isChecked(),false);
    await page.locator('#domain-form input').fill('mine.example.com');
    await page.screenshot({path:'/tmp/cf-choice.png'});
    await page.locator('#domain-form button').click();
    await page.waitForFunction(()=>document.querySelector('#no-sites').hidden);
    assert.deepEqual(bodies,[{domain:'mine.example.com'}]);
    await page.locator('#new-domain').click();
    await page.locator('#use-cloudflare').check();
    await page.screenshot({path:'/tmp/cf-choice-on.png'});
    assert.match(await page.locator('#dns-instructions').innerText(),/NS/);
    await page.locator('#domain-form input').fill('cf.example.com');
    await page.locator('#domain-form button').click();
    for (let i = 0; i < 40 && bodies.length < 2; i++) await new Promise(resolve => setTimeout(resolve, 50));
    assert.deepEqual(bodies[1],{domain:'cf.example.com',cloudflare:true});
    Object.assign(site,{cf_zone_id:'c'.repeat(32),cf_status:'pending',cf_nameservers:'ada.ns.cloudflare.com,bob.ns.cloudflare.com'});
    await page.locator('#refresh-domains').click();
    await page.getByRole('button',{name:'清除 CF 缓存'}).waitFor();
    await page.getByText('Cloudflare 待处理').waitFor();
    assert.match(await page.locator('.domain-table').innerText(),/ada\.ns\.cloudflare\.com/);
    assert.deepEqual(errors,[]);
  });
});

test('a site with its own certificate can attach Cloudflare afterwards', async () => {
  await withConsole('agent', async (page, url, errors) => {
    const readyId = 'b'.repeat(32);
    let ready = {id: readyId, domain: 'ready.example.com', owner_id: agent.id, enabled: true, stage: 'active', cf_status: '', cf_zone_id: '', cf_nameservers: '', error: '本机 HTTPS 已接入', note: ''};
    const posts = [];
    await page.route('**/api/**', async route => {
      const request = route.request(), pathname = new URL(request.url()).pathname;
      if (pathname === '/api/sites' && request.method() === 'GET') {
        await route.fulfill({json: {account: agent, sites: [ready], google_reputation_configured: false, cloudflare_configured: true, cloudflare_template: 'rules.example.com', server_ip: '8.8.8.8'}});
        return;
      }
      if (pathname === `/api/sites/${readyId}/cloudflare` && request.method() === 'POST') {
        posts.push(request.postDataJSON());
        ready = {...ready, cf_zone_id: 'c'.repeat(32), cf_status: 'pending', cf_nameservers: 'ada.ns.cloudflare.com,bob.ns.cloudflare.com'};
        await route.fulfill({json: {site: ready, cloudflare: {ok: true, status: 'pending', nameservers: ['ada.ns.cloudflare.com', 'bob.ns.cloudflare.com'], detail: '等待 NS'}}});
        return;
      }
      if (pathname === `/api/sites/${readyId}/cloudflare/status`) {
        await route.fulfill({json: {site: ready, cloudflare: {ok: true, status: ready.cf_status || 'pending', active: false, nameservers: ['ada.ns.cloudflare.com', 'bob.ns.cloudflare.com'], detail: 'NS'}}});
        return;
      }
      if (pathname === `/api/sites/${readyId}/state`) { await route.fulfill({json: {...siteState, site: ready}}); return; }
      if (pathname.endsWith('/analytics')) { await route.fulfill({json: analytics}); return; }
      throw new Error(`Unexpected endpoint ${pathname}`);
    });
    await page.goto(url);
    await page.locator('[data-tab="domains"]').click();
    await page.getByRole('button', {name: '套用 Cloudflare'}).click();
    await page.getByText('NS：ada.ns.cloudflare.com').waitFor();
    assert.deepEqual(posts, [{}]);
    assert.match(await page.locator('.domain-table').innerText(), /Cloudflare 待处理/);
    assert.deepEqual(errors, []);
  });
});

test('observer can read domains and accounts while action buttons stay hidden', async () => {
  const observer = {id:'watch-id', username:'watcher', role:'observer', enabled:true};
  await withConsole('observer', async (page, url, errors) => {
    await page.route('**/api/**', async route => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname === '/api/sites') {
        await route.fulfill({json:{account:observer, sites:[{...site, owner_username:'viewer'}], google_reputation_configured:false, server_ip:'203.0.113.1'}});
        return;
      }
      if (pathname === `/api/sites/${siteId}/state`) { await route.fulfill({json:siteState}); return; }
      if (pathname.endsWith('/analytics')) { await route.fulfill({json:analytics}); return; }
      if (pathname === '/api/accounts') {
        await route.fulfill({json:{items:[
          admin,
          {...agent, domain_count:1},
          {...observer, domain_count:0},
        ]}});
        return;
      }
      if (pathname === `/api/sites/${siteId}/logs`) { await route.fulfill({json:{items:[], total:0, page:1, pages:1}}); return; }
      if (pathname === `/api/sites/${siteId}/provision/events`) { await route.fulfill({json:{items:[]}}); return; }
      throw new Error(`Unexpected endpoint ${pathname}`);
    });
    await page.goto(url);
    const welcome = page.locator('#observer-welcome');
    await welcome.waitFor();
    assert.match(await welcome.innerText(), /欢迎登录/);
    assert.match(await welcome.innerText(), /全网功能最多，最牛B，最强大的/);
    assert.match(await welcome.innerText(), /双子星系统/);
    assert.match(await welcome.innerText(), /此为观察号。只有看，没有更改任何功能选项的权利/);
    await page.locator('#observer-enter').click();
    await welcome.waitFor({state:'detached'});
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#observer-welcome') === null);
    await page.waitForFunction(() => document.querySelector('#site-selector').value === 'a'.repeat(32));
    assert.match(await page.locator('.sidebar-footer').innerText(), /观察账号/);
    assert.equal(await page.locator('#account-form').count(), 0);
    assert.equal(await page.locator('#new-domain').isHidden(), true);
    assert.equal(await page.locator('#clear-logs').isHidden(), true);
    assert.equal(await page.locator('#clear-foreign-logs').isHidden(), true);
    assert.equal(await page.locator('#desk-check').isHidden(), true);
    assert.equal(await page.locator('#rules-form button[type="submit"]').isHidden(), true);
    assert.equal(await page.locator('#logout').count(), 0);
    await page.locator('[data-tab="accounts"]').click();
    await page.getByRole('cell', {name:'watcher'}).waitFor();
    assert.match(await page.locator('#account-list').innerText(), /观察号/);
    assert.match(await page.locator('#account-list').innerText(), /viewer/);
    assert.equal(await page.getByRole('button', {name:'停用'}).count(), 0);
    assert.equal(await page.getByRole('button', {name:'重设密码'}).count(), 0);
    await page.locator('[data-tab="domains"]').click();
    assert.match(await page.locator('.domain-table').innerText(), /归属：viewer/);
    assert.equal(await page.getByRole('button', {name:'分配'}).count(), 0);
    assert.equal(await page.getByRole('button', {name:'下线'}).count(), 0);
    await page.locator('[data-tab="logs"]').click();
    await page.locator('#log-filters button[type="submit"]').waitFor();
    assert.equal(await page.locator('#logs-prev').isHidden(), false);
    assert.deepEqual(errors, []);
  });
});
