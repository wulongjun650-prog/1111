import io
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from ablab.web import create_admin
from test_agent_access import accounts, add


@pytest.fixture
def console(tmp_path):
    app = create_admin(tmp_path)
    client = TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 5000))
    client.headers.update({'Origin': 'http://127.0.0.1:8765', 'X-CSRF-Token': app.state.csrf})
    return client, app


def upload(client, prefix='/api', slot='B', publish=True):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr('index.html', '''<!doctype html>
<a href="https://old.test/a?x=1&amp;y=2">go</a>
<a href='https://old.test/a'>go2</a><img src="https://old.test/p.png">
<meta http-equiv="refresh" content="0; url=https://old.test/refresh">
<button onclick="window.open(&quot;https://old.test/inline&quot;)">go</button>
<script>location.href = "https://old.test/script";</script>
<!-- <a href="https://old.test/comment">no</a> -->
<script src="app.js"></script>''')
        bundle.writestr('app.js', '\ufeffconst target = "https://old.test/variable";\nfetch(target);\nlocation.assign(target);\nwindow.open("https://old.test/open");\n// location.href="https://old.test/comment";\nconst image="https://old.test/image";')
        bundle.writestr('more.htm', '<a href="https://old.test/other">other</a>')
        bundle.writestr('style.css', 'body{color:red}')
    result = client.post(prefix+'/upload/'+slot+'?name=links.zip', content=archive.getvalue())
    assert result.status_code == 200, result.text
    version = result.json()['version']['id']
    if publish:
        assert client.post(prefix+'/publish/'+slot, json={'version_id':version}).status_code == 200
    return version


class Checker:
    def __init__(self, status='normal', callback=None):
        self.calls, self.status, self.callback = [], status, callback

    def check(self, url):
        self.calls.append(url)
        if self.callback:
            self.callback()
        return {'status':self.status, 'platform':'网站', 'detail':'检测结果', 'checked_at':123.5,
                'http_status':404 if self.status == 'abnormal' else 200, 'final_url':url}


def preset(client, prefix='/api', url='https://example.com/new?x=1&y=2'):
    result = client.post(prefix+'/b-redirects/presets', json={'urls':[url], 'note':'备用一'})
    assert result.status_code == 200, result.text
    return result.json()['presets'][0]


def apply(client, scan, link, prefix='/api', ids=None):
    return client.post(prefix+'/b-redirects/apply', json={
        'version_id':scan['version']['id'], 'preset_id':link['id'],
        'occurrence_ids': ids if ids is not None else [o['id'] for o in scan['occurrences']],
        'expected_published':scan['published_version']})


def test_scans_all_static_navigation_and_apply_keeps_a_assets_history(console):
    client, app = console
    original_a, original_b = upload(client, slot='A'), upload(client)
    store = app.state.store
    app.state.link_checker = checker = Checker()
    root = store.pages/original_b
    before = {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}
    response = client.get('/api/b-redirects')
    assert response.status_code == 200, response.text
    scan = response.json()
    assert len(scan['occurrences']) == 8
    assert len({o['id'] for o in scan['occurrences']}) == 8
    assert len({o['key'] for o in scan['occurrences']}) == 8
    assert all(o['line'] > 0 and 'comment' not in o['url'] for o in scan['occurrences'])
    link = preset(client)
    assert checker.calls == []
    result = apply(client, scan, link)
    assert result.status_code == 200, result.text
    new = result.json()['version']
    assert new['slot'] == 'B' and new['id'] != original_b and result.json()['changed'] == 8
    assert store.slots() == {'A':original_a, 'B':new['id']}
    assert {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()} == before
    updated = store.pages/new['id']
    assert (updated/'style.css').read_bytes() == before['style.css']
    text = (updated/'app.js').read_text(encoding='utf-8-sig')
    assert 'fetch(target)' in text and 'const target = "https://old.test/variable"' in text
    assert (updated/'app.js').read_bytes().startswith(b'\xef\xbb\xbf')
    rescanned = client.get('/api/b-redirects').json()
    assert {o['url'] for o in rescanned['occurrences']} == {link['url']}
    assert [o['key'] for o in rescanned['occurrences']] == [o['key'] for o in scan['occurrences']]
    assert rescanned['active']['version_id'] == new['id'] and rescanned['active']['preset_id'] == link['id']
    assert checker.calls == [link['url']]


def test_selection_only_and_repeated_replacement(console):
    client, app = console
    upload(client)
    app.state.link_checker = Checker('unknown')
    first = client.get('/api/b-redirects').json()
    link = preset(client)
    response = apply(client, first, link, ids=[first['occurrences'][0]['id']])
    assert response.status_code == 200 and response.json()['check']['status'] == 'unknown'
    second = client.get('/api/b-redirects').json()
    assert len([o for o in second['occurrences'] if o['url'] == link['url']]) == 1
    next_link = preset(client, url='https://example.org/different')
    assert apply(client, second, next_link).status_code == 200
    assert {o['url'] for o in client.get('/api/b-redirects').json()['occurrences']} == {next_link['url']}


def test_abnormal_does_not_publish_and_delete_active_does_not_change_code(console):
    client, app = console
    base = upload(client)
    app.state.link_checker = checker = Checker('abnormal')
    scan = client.get('/api/b-redirects').json()
    link = preset(client)
    denied = apply(client, scan, link)
    assert denied.status_code == 400 and '此链接不正常' in denied.json()['detail']
    assert app.state.store.slots()['B'] == base and len(app.state.store.versions()) == 1
    assert client.get('/api/b-redirects').json()['presets'][0]['check']['status'] == 'abnormal'
    checker.status = 'normal'
    assert apply(client, scan, link).status_code == 200
    current = app.state.store.slots()['B']
    assert client.delete('/api/b-redirects/presets/'+str(link['id'])).status_code == 200
    state = client.get('/api/b-redirects').json()
    assert state['presets'] == [] and state['active']['preset_id'] is None
    assert state['active']['url'] == link['url'] and app.state.store.slots()['B'] == current
    assert client.post('/api/b-redirects/presets/'+str(link['id'])+'/check',json={}).status_code == 404


def test_stale_and_invalid_occurrences_rejected_before_network(console):
    client, app = console
    upload(client)
    app.state.link_checker = checker = Checker()
    scan = client.get('/api/b-redirects').json()
    link = preset(client)
    assert apply(client, scan, link, ids=['0'*64]).status_code == 409
    assert apply(client, scan, link, ids=[]).status_code == 422
    upload(client)
    assert apply(client, scan, link).status_code == 409
    assert checker.calls == []


def test_concurrent_apply_has_one_winner_no_orphans(console):
    client, app = console
    base = upload(client)
    app.state.link_checker = Checker()
    scan, link = client.get('/api/b-redirects').json(), preset(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _:apply(client, scan, link),range(2)))
    assert sorted(r.status_code for r in results) == [200,409]
    versions = {v['id'] for v in app.state.store.versions()}
    assert len(versions) == 2 and base in versions
    assert {p.name for p in app.state.store.pages.iterdir()} == versions


@pytest.mark.parametrize('mutation', ['delete', 'publish'])
def test_changes_during_network_check_do_not_publish(console, mutation):
    client, app = console
    upload(client)
    scan, link = client.get('/api/b-redirects').json(), preset(client)
    def change():
        if mutation == 'delete':
            app.state.store.delete_redirect_preset(link['id'])
        else:
            upload(client)
    app.state.link_checker = Checker(callback=change)
    response = apply(client, scan, link)
    assert response.status_code in (404,409)
    assert len(app.state.store.versions()) == (1 if mutation == 'delete' else 2)


def test_unpublished_empty_wrong_slot_and_strict_bodies(console):
    client, app = console
    assert client.get('/api/b-redirects').json()['version'] is None
    b = upload(client, publish=False)
    a = upload(client, slot='A')
    assert client.get('/api/b-redirects',params={'version_id':b}).json()['version']['id'] == b
    for version in (a,'0'*32):
        assert client.get('/api/b-redirects',params={'version_id':version}).status_code == 404
    assert client.get('/api/b-redirects',params={'version_id':'../x'}).status_code == 422
    for body in ({'urls':['https://example.org'],'note':'','slot':'A'}, {'urls':['http://127.0.0.1/'],'note':''},
                 {'urls':['https://example.org'],'note':'x'*301}, {'urls':[],'note':''}):
        assert client.post('/api/b-redirects/presets',json=body).status_code in (400,422)
    original = preset(client)
    assert preset(client)['id'] == original['id']
    assert len(client.get('/api/b-redirects').json()['presets']) == 1


def test_agent_all_new_routes_isolated_and_owner_change_during_check(accounts):
    app, auth, owner, a, b, one, two = accounts
    owned, other = add(a,'one.test'),add(b,'two.test')
    prefix = f"/api/sites/{owned['id']}"
    upload(a,prefix)
    link = preset(a,prefix)
    scan = a.get(prefix+'/b-redirects').json()
    app.state.link_checker = checker = Checker()
    for bad_prefix in ('/api',f"/api/sites/{other['id']}"):
        assert a.get(bad_prefix+'/b-redirects').status_code == 404
        assert a.post(bad_prefix+'/b-redirects/presets',json={'urls':['https://example.org'],'note':''}).status_code == 404
        assert a.post(bad_prefix+f"/b-redirects/presets/{link['id']}/check",json={}).status_code == 404
        assert a.delete(bad_prefix+f"/b-redirects/presets/{link['id']}").status_code == 404
        assert a.post(bad_prefix+'/b-redirects/numbers',json={'phones':['85211112222'],'note':''}).status_code == 404
        assert a.delete(bad_prefix+'/b-redirects/numbers/1').status_code == 404
        assert apply(a,scan,link,bad_prefix).status_code == 404
    other_state = b.get(f"/api/sites/{other['id']}/b-redirects").json()
    assert checker.calls == [] and other_state['presets'] == [] and other_state['numbers'] == []
    def transfer():
        assert owner.put(prefix+'/owner',json={'owner_id':two['id']}).status_code == 200
    app.state.link_checker = Checker(callback=transfer)
    assert apply(a,scan,link,prefix).status_code == 404
    assert app.state.registry.store(owned['id']).slots()['B'] == scan['version']['id']


def test_disabled_agent_during_check_cannot_publish(accounts):
    app, auth, owner, a, b, one, two = accounts
    owned = add(a, 'one.test')
    prefix = f"/api/sites/{owned['id']}"
    base = upload(a, prefix)
    link, scan = preset(a, prefix), a.get(prefix + '/b-redirects').json()
    store = app.state.registry.store(owned['id'])
    app.state.link_checker = Checker(callback=lambda: auth.set_enabled(one['id'], False))
    response = apply(a, scan, link, prefix)
    assert response.status_code == 401, response.text
    assert store.slots()['B'] == base
    assert [version['id'] for version in store.versions()] == [base]
    assert {path.name for path in store.pages.iterdir()} == {base}


def test_owner_transfer_during_bundle_creation_cannot_publish(accounts, monkeypatch):
    from ablab import redirects

    app, auth, owner, a, b, one, two = accounts
    owned = add(a, 'one.test')
    prefix = f"/api/sites/{owned['id']}"
    base = upload(a, prefix)
    link, scan = preset(a, prefix), a.get(prefix + '/b-redirects').json()
    store = app.state.registry.store(owned['id'])
    actual_replace = redirects.replace_bundle

    def transfer_during_bundle(*args, **kwargs):
        app.state.registry.assign_owner(owned['id'], two['id'])
        return actual_replace(*args, **kwargs)

    monkeypatch.setattr(redirects, 'replace_bundle', transfer_during_bundle)
    app.state.link_checker = Checker()
    response = apply(a, scan, link, prefix)
    assert response.status_code == 404, response.text
    assert store.slots()['B'] == base
    assert [version['id'] for version in store.versions()] == [base]
    assert {path.name for path in store.pages.iterdir()} == {base}


def upload_whatsapp(client):
    archive = io.BytesIO()
    page = '''<!doctype html><script>
const CONFIG = { whatsappNumber: '85264150954' };
const VARIANTS = { blackhorse: { message: '你好，我想免費領取今日潛力黑馬名單，麻煩發給我，謝謝。' } };
function buildWhatsAppUrl(message){
  const phone = String(CONFIG.whatsappNumber || '').replace(/\\D/g,'');
  const params = new URLSearchParams();
  params.set('phone', phone);
  params.set('text', message);
  params.set('type', 'phone_number');
  params.set('app_absent', '0');
  return `https://api.whatsapp.com/send?${params.toString()}`;
}
function go(){ window.location.assign(buildWhatsAppUrl(VARIANTS.blackhorse.message)); }
</script><a href="https://www.youtube.com/@EconManBlog/shorts">YouTube</a>'''
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr('index.html', page)
    result = client.post('/api/upload/B?name=wa.zip', content=archive.getvalue())
    assert result.status_code == 200, result.text
    version = result.json()['version']['id']
    assert client.post('/api/publish/B', json={'version_id':version}).status_code == 200
    return version


def test_whatsapp_number_pool_rewrites_only_the_phone(console):
    client, app = console
    base = upload_whatsapp(client)
    app.state.link_checker = checker = Checker()
    scan = client.get('/api/b-redirects').json()
    numbers = [item for item in scan['occurrences'] if item['kind'] == 'whatsapp_number']
    links = [item for item in scan['occurrences'] if item['kind'] != 'whatsapp_number']
    assert [item['url'] for item in numbers] == ['85264150954']
    assert links and links[0]['url'].startswith('https://www.youtube.com/')
    added = client.post('/api/b-redirects/numbers', json={'phones':['85211112222', '+852 3333 4444', '852-5555-6666', '85211112222'], 'note':'一线'})
    assert added.status_code == 200, added.text
    pool = added.json()['numbers']
    assert [item['phone'] for item in pool] == ['85211112222', '85233334444', '85255556666']
    assert client.get('/api/b-redirects').json()['active'] is None
    denied = apply(client, scan, preset(client), ids=[numbers[0]['id']])
    assert denied.status_code == 400 and '号码预设' in denied.json()['detail']
    assert checker.calls == [] and app.state.store.slots()['B'] == base
    chosen = pool[0]
    response = client.post('/api/b-redirects/numbers/apply', json={
        'version_id':scan['version']['id'], 'number_id':chosen['id'],
        'occurrence_ids':[numbers[0]['id']], 'expected_published':scan['published_version']})
    assert response.status_code == 200, response.text
    assert checker.calls == [] and response.json()['changed'] == 1
    published = app.state.store.slots()['B']
    text = (app.state.store.pages/published/'index.html').read_text(encoding='utf-8')
    assert 'api.whatsapp.com' in text and '你好，我想免費領取今日潛力黑馬名單' in text
    assert '85264150954' not in text and '85211112222' in text
    assert 'youtube.com' in text
    state = client.get('/api/b-redirects').json()
    assert state['active'] is None
    assert [item['phone'] for item in state['numbers']] == ['85211112222', '85233334444', '85255556666']
    assert [item['url'] for item in state['occurrences'] if item['kind'] == 'whatsapp_number'] == ['85211112222']
    before = [item['id'] for item in app.state.store.versions()]
    repeated = client.post('/api/b-redirects/numbers/apply', json={
        'version_id': state['version']['id'], 'number_id': chosen['id'],
        'occurrence_ids': [item['id'] for item in state['occurrences'] if item['kind'] == 'whatsapp_number'],
        'expected_published': state['published_version']})
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()['changed'] == 0
    assert repeated.json()['version']['id'] == published
    assert [item['id'] for item in app.state.store.versions()] == before
    youtube = next(item for item in state['occurrences'] if item['kind'] != 'whatsapp_number')
    link = preset(client, url='https://example.com/watch')
    swapped = apply(client, state, link, ids=[youtube['id']])
    assert swapped.status_code == 200, swapped.text
    assert checker.calls == ['https://example.com/watch']
    after = (app.state.store.pages/app.state.store.slots()['B']/'index.html').read_text(encoding='utf-8')
    assert '85211112222' in after and 'api.whatsapp.com' in after and 'https://example.com/watch' in after
    assert client.post('/api/b-redirects/numbers', json={'phones':['12'], 'note':''}).status_code in (400, 422)
    assert client.delete('/api/b-redirects/numbers/'+str(pool[1]['id'])).status_code == 200
    remaining = client.get('/api/b-redirects').json()['numbers']
    assert [item['phone'] for item in remaining] == ['85211112222', '85255556666']


RECEPTION_PAGE = '''<!doctype html>
<div class="reception" id="reception-text">本次由助理 Chloe 接待</div>
<script>
const CONFIG = { whatsappNumber: '85264150954' };
const VARIANTS = { blackhorse: { message: '你好，我想免費領取今日潛力黑馬名單，麻煩發給我，謝謝。' } };
function buildWhatsAppUrl(message){
  const phone = String(CONFIG.whatsappNumber || '').replace(/\\D/g,'');
  const params = new URLSearchParams();
  params.set('phone', phone);
  params.set('text', message);
  params.set('type', 'phone_number');
  params.set('app_absent', '0');
  return `https://api.whatsapp.com/send?${params.toString()}`;
}
function go(){ window.location.assign(buildWhatsAppUrl(VARIANTS.blackhorse.message)); }
</script>
<a href="https://old.example/landing">go</a>
<a href="https://www.youtube.com/@EconManBlog/shorts">YouTube</a>'''


def upload_reception(client):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr('index.html', RECEPTION_PAGE)
    result = client.post('/api/upload/B?name=wa.zip', content=archive.getvalue())
    assert result.status_code == 200, result.text
    version = result.json()['version']['id']
    assert client.post('/api/publish/B', json={'version_id': version}).status_code == 200
    return version


def published_html(app):
    store = app.state.store
    return (store.pages / store.slots()['B'] / 'index.html').read_text(encoding='utf-8')


def test_reception_line_is_replaced_once_and_not_duplicated():
    from ablab.redirects import rewrite_reception
    page = '<div class="reception" id="reception-text">本次由助理 Chloe 接待</div>'
    updated = rewrite_reception(page, 'Vivian 關詠怡')
    assert updated == '<div class="reception" id="reception-text">本次由助理Vivian 關詠怡 接待</div>'
    assert rewrite_reception(updated, 'Vivian 關詠怡') == updated
    assert updated.count('Vivian') == 1
    assert 'Vivian 關詠怡本次由助理' not in updated
    bare = rewrite_reception('开头本次由助理 Chloe 接待结尾', 'Vivian 關詠怡')
    assert bare == '开头本次由助理Vivian 關詠怡 接待结尾'
    assert rewrite_reception('<h1>没有接待行</h1>', 'Vivian') == '<h1>没有接待行</h1>'


def test_scripted_reception_keeps_the_config_and_shows_the_name_once():
    from ablab.redirects import rewrite_reception
    page = '''<div class="reception" id="reception-text">本次由助理 Chloe 接待</div>
<script>
const CONFIG = { whatsappNumber: "85265492837", receptionist: 'Chloe' };
const receptionText = document.getElementById('reception-text');
receptionText.textContent = `本次由助理 ${CONFIG.receptionist} 接待`;
const message = '你好，我想免費領取今日潛力黑馬名單';
</script>'''
    updated = rewrite_reception(page, 'Vivian 關詠怡')
    assert '<div class="reception" id="reception-text">本次由助理Vivian 關詠怡 接待</div>' in updated
    assert "receptionist: 'Vivian 關詠怡'" in updated
    assert '本次由助理${CONFIG.receptionist} 接待' in updated
    assert '本次由助理 ${CONFIG.receptionist}' not in updated
    assert '85265492837' in updated and '你好，我想免費領取今日潛力黑馬名單' in updated
    assert 'Vivian 關詠怡本次由助理' not in updated
    again = rewrite_reception(updated, 'Vivian 關詠怡')
    assert again.count('Vivian 關詠怡') == updated.count('Vivian 關詠怡')
    shown = '本次由助理' + 'Vivian 關詠怡' + ' 接待'
    assert shown == '本次由助理Vivian 關詠怡 接待'


def test_changing_the_reception_name_publishes_that_sentence_only(console):
    client, app = console
    upload_reception(client)
    denied = client.post('/api/b-redirects/numbers/reception', json={
        'phone': '85211112222', 'display_name': '<Vivian>', 'version_id': 'a' * 32, 'expected_published': 'b' * 32})
    assert denied.status_code == 422
    several = client.post('/api/b-redirects/numbers', json={'phones': ['85211112222', '85233334444'], 'note': '', 'display_name': 'Vivian 關詠怡'})
    assert several.status_code == 400
    assert '一次只填一个号码' in several.json()['detail']
    scan = client.get('/api/b-redirects').json()
    wrote = client.post('/api/b-redirects/numbers/reception', json={
        'phone': '85211112222', 'display_name': '  Vivian   關詠怡  ',
        'version_id': scan['version']['id'], 'expected_published': scan['published_version']})
    assert wrote.status_code == 200, wrote.text
    body = wrote.json()
    assert body['changed'] == 1
    assert body['sentence'] == '本次由助理Vivian 關詠怡 接待'
    assert body['number']['display_name'] == 'Vivian 關詠怡'
    text = published_html(app)
    assert text.count('本次由助理Vivian 關詠怡 接待') == 1
    assert text.count('Vivian') == 1
    assert 'Chloe' not in text and '85264150954' in text
    assert '你好，我想免費領取今日潛力黑馬名單' in text and 'youtube.com' in text and 'old.example' in text
    again = client.post('/api/b-redirects/numbers/reception', json={
        'phone': '85211112222', 'display_name': 'Vivian 關詠怡',
        'version_id': body['version']['id'], 'expected_published': body['version']['id']})
    assert again.status_code == 200, again.text
    assert again.json()['changed'] == 0
    assert published_html(app).count('Vivian') == 1


def test_reception_button_refuses_a_page_without_the_sentence(console):
    client, app = console
    base = upload_whatsapp(client)
    scan = client.get('/api/b-redirects').json()
    refused = client.post('/api/b-redirects/numbers/reception', json={
        'phone': '85211112222', 'display_name': 'Vivian',
        'version_id': scan['version']['id'], 'expected_published': scan['published_version']})
    assert refused.status_code == 400, refused.text
    assert '本次由助理' in refused.json()['detail']
    assert app.state.store.slots()['B'] == base
    assert '85264150954' in (app.state.store.pages / base / 'index.html').read_text(encoding='utf-8')


def test_applying_a_named_number_writes_its_sentence_and_only_the_phone(console):
    client, app = console
    upload_reception(client)
    added = client.post('/api/b-redirects/numbers', json={'phones': ['85211112222'], 'note': '一线', 'display_name': 'Vivian 關詠怡'})
    assert added.status_code == 200, added.text
    number = added.json()['numbers'][0]
    assert number['display_name'] == 'Vivian 關詠怡'
    assert 'Chloe' in published_html(app)
    scan = client.get('/api/b-redirects').json()
    phones = [item['id'] for item in scan['occurrences'] if item['kind'] == 'whatsapp_number']
    assert phones
    response = client.post('/api/b-redirects/numbers/apply', json={
        'version_id': scan['version']['id'], 'number_id': number['id'],
        'occurrence_ids': phones, 'expected_published': scan['published_version']})
    assert response.status_code == 200, response.text
    assert response.json()['changed'] >= 1
    text = published_html(app)
    assert text.count('本次由助理Vivian 關詠怡 接待') == 1
    assert 'Vivian 關詠怡本次由助理' not in text and 'Chloe' not in text
    assert '85264150954' not in text and '85211112222' in text
    assert '你好，我想免費領取今日潛力黑馬名單' in text
    assert 'youtube.com' in text and 'old.example' in text
    nameless = client.post('/api/b-redirects/numbers', json={'phones': ['85233334444'], 'note': ''}).json()['numbers'][0]
    refused = client.put('/api/b-redirects/numbers/split', json={'enabled': True, 'mode': 'random', 'members': [{'number_id': nameless['id'], 'weight': 1}]})
    assert refused.status_code == 400 and '接待名' in refused.json()['detail']
    empty = client.put('/api/b-redirects/numbers/split', json={'enabled': True, 'mode': 'random', 'members': []})
    assert empty.status_code == 400
    assert client.put('/api/b-redirects/numbers/split', json={'enabled': False, 'mode': 'random', 'members': [{'number_id': number['id'], 'weight': 0}]}).status_code == 422
    saved = client.put('/api/b-redirects/numbers/split', json={'enabled': True, 'mode': 'weighted', 'members': [{'number_id': number['id'], 'weight': 40}]})
    assert saved.status_code == 200, saved.text
    split = saved.json()['number_split']
    assert split['enabled'] is True and split['mode'] == 'weighted'
    assert split['members'] == [{'number_id': number['id'], 'weight': 40, 'phone': '85211112222', 'display_name': 'Vivian 關詠怡'}]
    assert client.get('/api/b-redirects').json()['split']['enabled'] is False
