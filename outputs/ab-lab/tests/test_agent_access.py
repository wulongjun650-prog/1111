import hashlib
import time
import httpx
import pytest
from fastapi.testclient import TestClient
from ablab.archives import write_bundle
from ablab.auth import Auth
from ablab.settings import Deployment
from ablab.sites import Registry
from ablab.store import Store
from ablab.web import create_admin, create_target


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    # These tests verify API isolation; full scrypt is covered by test_accounts.
    monkeypatch.setattr('ablab.auth.password_hash', lambda password, salt: hashlib.sha256((salt + password).encode()).hexdigest())
    auth = Auth(Store(tmp_path))
    auth.set_password('admin888', 'test-password-for-owner')
    one = auth.create_agent('agent-one', 'test-password-for-one')
    two = auth.create_agent('agent-two', 'test-password-for-two')
    settings = Deployment(admin_origin='https://admin.test', target_origin='https://visitor.test')
    app = create_admin(tmp_path, deployment=settings)
    def login(username, password):
        client = TestClient(app, base_url=settings.admin_origin, client=('127.0.0.1', 4000))
        client.headers.update({'Origin': settings.admin_origin, 'X-Real-IP': '203.0.113.10', 'X-Forwarded-Proto': 'https', 'X-CSRF-Token': app.state.csrf})
        result = client.post('/api/login', json={'username': username, 'password': password})
        assert result.status_code == 200, result.text
        client.headers['X-CSRF-Token'] = result.json()['csrf']
        return client
    owner = login('admin888', 'test-password-for-owner')
    a = login('agent-one', 'test-password-for-one')
    b = login('agent-two', 'test-password-for-two')
    return app, auth, owner, a, b, one, two


def add(client, domain):
    result = client.post('/api/sites', json={'domain': domain})
    assert result.status_code == 200, result.text
    return result.json()['site']


def test_agent_catalog_and_new_domain_owner_are_isolated(accounts):
    app, auth, owner, a, b, one, two = accounts
    a_site, b_site = add(a, 'one.test'), add(b, 'two.test')
    assert a_site['owner_id'] == one['id'] and b_site['owner_id'] == two['id']
    first = a.get('/api/sites').json()
    assert [s['id'] for s in first['sites']] == [a_site['id']]
    assert first['account']['role'] == 'agent'
    assert 'agent-two' not in str(first) and 'two.test' not in str(first)
    assert a.get('/api/me').json()['account']['id'] == one['id']
    assert {s['id'] for s in owner.get('/api/sites').json()['sites']} == {'default', a_site['id'], b_site['id']}
    assert b.get(f"/api/sites/{b_site['id']}/state").status_code == 200
    assert owner.get(f"/api/sites/{a_site['id']}/state").status_code == 200
    assert a.post('/api/sites', json={'domain': 'forged.test', 'owner_id': two['id']}).status_code == 400


@pytest.mark.parametrize('method,suffix,body', [
    ('GET', 'state', None), ('GET', 'analytics?scope=all', None),
    ('GET', 'logs', None), ('GET', 'logs.csv', None), ('GET', 'logs/rate', None), ('GET', 'audit', None),
    ('GET', 'provision/events', None), ('POST', 'reputation/check', {}),
    ('PATCH', 'metadata', {'note': 'bad'}), ('POST', 'availability/offline', {}),
    ('POST', 'provision/retry', {}), ('PUT', 'config', {}),
    ('POST', 'upload/A?name=bad.html', None), ('POST', 'publish/A', {}),
    ('POST', 'preview', {}), ('POST', 'links', {}), ('DELETE', 'links/1', None),
    ('POST', 'counters/reset', {}), ('POST', 'simulate', {}),
    ('PUT', 'owner', {'owner_id': 'admin'}),
])
def test_every_site_api_rejects_other_agent_before_work(accounts, method, suffix, body):
    app, auth, owner, a, b, one, two = accounts
    site = add(b, 'private.test')
    path = f"/api/sites/{site['id']}/{suffix}"
    result = a.request(method, path, json=body)
    assert result.status_code == 404, result.text
    assert not (app.state.registry.root / 'sites' / site['id']).exists()
    assert app.state.registry.get(site['id'])['enabled'] == 1


def test_legacy_default_aliases_do_not_expose_owner_to_empty_agent(accounts):
    app, auth, owner, a, b, one, two = accounts
    assert a.get('/api/sites').json()['sites'] == []
    for suffix in ('state', 'logs', 'logs.csv', 'audit', 'analytics?scope=current'):
        assert a.get('/api/' + suffix).status_code == 404
    assert a.post('/api/counters/reset', json={}).status_code == 404
    data = a.get('/api/analytics?scope=all').json()
    assert data['domains'] == [] and data['summary']['total'] == 0
    assert a.get('/').status_code == 200
    assert 'data-tab="accounts"' not in a.get('/').text


def test_analysis_panel_hides_another_agents_domains(accounts):
    app, auth, owner, a, b, one, two = accounts
    mine, theirs = add(a, 'mine.test'), add(b, 'theirs.test')
    for site, marker in ((mine, 'mine-only'), (theirs, 'their-secret')):
        store = app.state.registry.store(site['id'])
        store.record_access(marker, '', '', '"18.0.0"', '?1', '', 'gclid=1', '85299998888', '', 'HK', '', 200, 1, '/')
        store.record_client_event('{"event":"' + marker + '"}')
    data = a.get('/api/analysis?days=7')
    assert data.status_code == 200, data.text
    payload = data.json()
    assert {item['domain'] for item in payload['requests']['items']} == {'mine.test'}
    assert payload['requests']['total'] == 1
    assert payload['events']['items'][0]['body'] == '{"event":"mine-only"}'
    assert 'their-secret' not in data.text and 'theirs.test' not in data.text
    report = a.get('/api/analysis/report?days=7').text
    assert '===== mine.test =====' in report
    assert 'theirs.test' not in report and 'their-secret' not in report
    owner_domains = {item['domain'] for item in owner.get('/api/analysis?days=7').json()['requests']['items']}
    assert {'mine.test', 'theirs.test'} <= owner_domains
    assert '数据深度分析' in a.get('/').text
    stolen = a.get('/api/analysis?days=7&domain=theirs.test')
    assert stolen.status_code == 200
    assert stolen.json()['domains'] == 0
    assert stolen.json()['requests']['items'] == [] and stolen.json()['events']['items'] == []
    assert 'their-secret' not in stolen.text
    assert a.get('/api/analysis/report?days=7&domain=theirs.test').text == '没有可查看的域名。\n'
    assert a.post('/api/analysis/clear?domain=theirs.test').status_code == 404
    assert a.post('/api/campaign/clear?domain=theirs.test').status_code == 404
    own = a.post('/api/analysis/clear?domain=mine.test')
    assert own.status_code == 200, own.text
    assert own.json()['domain'] == 'mine.test'
    with app.state.registry.store(theirs['id']).connect() as db:
        assert db.execute('SELECT COUNT(*) FROM access_log').fetchone()[0] == 1
        assert db.execute("SELECT body FROM client_events").fetchone()[0] == '{"event":"their-secret"}'


def test_campaign_table_hides_another_agents_events(accounts):
    app, auth, owner, a, b, one, two = accounts
    mine, theirs = add(a, 'mine-campaign.test'), add(b, 'their-campaign.test')
    app.state.registry.store(mine['id']).record_client_event('{"event":"whatsapp_click","transaction_id":"mine","wa_env":"ios","link_type":"api","placement":"news.example"}', 'mine-campaign.test')
    app.state.registry.store(theirs['id']).record_client_event('{"event":"whatsapp_click","transaction_id":"secret","wa_env":"ios","link_type":"api","placement":"news.example"}', 'their-campaign.test')
    data = a.get('/api/campaign?range=today')
    assert data.status_code == 200, data.text
    assert 'secret' not in data.text and 'their-campaign.test' not in data.text
    assert 'mine' in data.text


def test_all_analytics_never_reads_another_agents_data(accounts):
    app, auth, owner, a, b, one, two = accounts
    first, second = add(a, 'one.test'), add(b, 'two.test')
    for sid, country in [('default', 'JP'), (first['id'], 'KR'), (second['id'], 'US')]:
        with app.state.registry.store(sid).connect() as db:
            db.execute('INSERT INTO events(created,ip,country,device,slot,reason,path,mode) VALUES(?,?,?,?,?,?,?,?)',
                       (time.time()-60, '203.0.113.0/24', country, 'mobile', 'B', 'allowed', '/', 'RULES'))
    for path in ['/api/analytics?scope=all', f"/api/sites/{first['id']}/analytics?scope=all"]:
        response = a.get(path)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data['summary']['total'] == 1
        assert [item['code'] for item in data['countries']] == ['KR']
        assert [item['id'] for item in data['domains']] == [first['id']]
        assert data['recent'][0]['site_id'] == first['id']
        assert 'two.test' not in str(data) and 'visitor.test' not in str(data)
    assert owner.get('/api/analytics?scope=all').json()['summary']['total'] == 3


def test_admin_accounts_and_transfer_revoke_previous_owner_access(accounts):
    app, auth, owner, a, b, one, two = accounts
    site = add(a, 'transfer.test')
    result = owner.put(f"/api/sites/{site['id']}/owner", json={'owner_id': two['id']})
    assert result.status_code == 200, result.text
    assert a.get(f"/api/sites/{site['id']}/state").status_code == 404
    assert a.get('/api/sites').json()['sites'] == []
    assert b.get(f"/api/sites/{site['id']}/state").status_code == 200
    own = owner.get('/api/sites').json()['sites'][-1]
    assert own['owner_username'] == 'agent-two'
    assert owner.get('/api/accounts').status_code == 200
    assert a.get('/api/accounts').status_code == 403
    assert a.post('/api/accounts', json={'username':'forged', 'password':'valid-test-password'}).status_code == 403
    assert a.put(f"/api/sites/{site['id']}/owner", json={'owner_id':one['id']}).status_code == 404
    assert b.put(f"/api/sites/{site['id']}/owner", json={'owner_id':one['id']}).status_code == 403
    for path, method, data in [(f"/api/accounts/{two['id']}", 'PATCH', {'enabled':False}),
                              (f"/api/accounts/{two['id']}/password", 'POST', {'password':'other-valid-password'})]:
        assert a.request(method, path, json=data).status_code == 403
    assert owner.put(f"/api/sites/{site['id']}/owner", json={'owner_id':'missing'}).status_code == 404
    assert app.state.registry.get(site['id'])['owner_id'] == two['id']
    result = owner.post('/api/accounts', json={'username':'new-agent','password':'new-valid-password'})
    assert result.status_code == 200, result.text
    assert result.json()['account']['role'] == 'agent'
    assert 'password' not in str(result.json())
    assert owner.patch(f"/api/accounts/{one['id']}", json={'enabled':False}).status_code == 200
    assert a.get('/api/sites').status_code == 401
    assert owner.get('/api/sites').status_code == 200


def test_admin_management_still_requires_origin_csrf_and_admin_cannot_be_disabled(accounts):
    app, auth, owner, a, b, one, two = accounts
    assert owner.post('/api/accounts', json={'username':'bad', 'password':'valid-test-password'}, headers={'X-CSRF-Token':'bad'}).status_code == 403
    assert owner.post('/api/accounts', json={'username':'bad', 'password':'valid-test-password', 'role':'admin'}).status_code == 422
    assert owner.patch('/api/accounts/admin', json={'enabled':False}).status_code == 400
    assert owner.post('/api/accounts/admin/password', json={'password':'new-valid-password'}).status_code == 400


def sign_in(app, username, password):
    settings = Deployment(admin_origin='https://admin.test', target_origin='https://visitor.test')
    client = TestClient(app, base_url=settings.admin_origin, client=('127.0.0.1', 4000))
    client.headers.update({'Origin': settings.admin_origin, 'X-Real-IP': '203.0.113.10', 'X-Forwarded-Proto': 'https', 'X-CSRF-Token': app.state.csrf})
    result = client.post('/api/login', json={'username': username, 'password': password})
    assert result.status_code == 200, result.text
    client.headers['X-CSRF-Token'] = result.json()['csrf']
    return client


def test_observer_reads_one_domain_and_the_rows_look_like_that_domain(accounts):
    app, auth, owner, a, b, one, two = accounts
    first, second = add(a, 'one.test'), add(b, 'two.test')
    source = app.state.registry.store(first['id'])
    with source.connect() as db:
        db.execute('INSERT INTO events(created,ip,country,device,slot,reason,path,mode) VALUES(?,?,?,?,?,?,?,?)',
                   (time.time()-60, '203.0.113.0/24', 'KR', 'mobile', 'B', 'allowed', '/', 'RULES'))
    source.record_client_event('{"event":"whatsapp_click","transaction_id":"watch-me","page":"https://one.test/go","wa_env":"ios","link_type":"api"}', 'one.test')
    source.record_access('watch-ua', '', '', '"18.0.0"', '?1', '', 'gclid=1', '85290001111', '', 'HK', '', 200, 1, '/')
    created = owner.post('/api/accounts', json={'username': 'watcher', 'password': 'watch-password-ok', 'role': 'observer'})
    assert created.status_code == 200, created.text
    assert created.json()['account']['role'] == 'observer'
    assert 'password' not in created.json()['account']
    watcher_id = created.json()['account']['id']
    assert a.post('/api/accounts', json={'username': 'forged-watch', 'password': 'watch-password-ok', 'role': 'observer'}).status_code == 403
    watcher = sign_in(app, 'watcher', 'watch-password-ok')
    page = watcher.get('/')
    assert page.status_code == 200
    assert 'data-tab="accounts"' not in page.text and '观察账号' in page.text
    assert '只能看指定的这一个域名' in page.text
    assert 'id="analysis-clear"' not in page.text
    assert 'id="campaign-clear"' not in page.text
    assert '导出数据' in page.text
    assert watcher.get('/api/sites').json()['sites'] == []
    assert watcher.get('/api/accounts').status_code == 403
    assert a.get('/api/accounts').status_code == 403
    assert watcher.get(f"/api/sites/{first['id']}/state").status_code == 404
    assert watcher.get(f"/api/sites/{second['id']}/logs").status_code == 404
    mirror = add(owner, 'watch.test')
    bound = owner.put(f"/api/sites/{mirror['id']}/mirror", json={'source_id': first['id'], 'observer_id': watcher_id})
    assert bound.status_code == 200, bound.text
    assert bound.json()['site']['mirror_domain'] == 'one.test'
    catalog = watcher.get('/api/sites')
    assert catalog.status_code == 200, catalog.text
    assert [site['domain'] for site in catalog.json()['sites']] == ['watch.test']
    assert 'mirror_source' not in catalog.json()['sites'][0]
    assert 'one.test' not in catalog.text and 'two.test' not in catalog.text
    admin_row = next(site for site in owner.get('/api/sites').json()['sites'] if site['id'] == mirror['id'])
    assert admin_row['mirror_source'] == first['id'] and admin_row['mirror_domain'] == 'one.test'
    assert watcher.get(f"/api/sites/{mirror['id']}/state").status_code == 200
    assert watcher.get(f"/api/sites/{mirror['id']}/audit").status_code == 200
    logs = watcher.get(f"/api/sites/{mirror['id']}/logs")
    assert logs.status_code == 200, logs.text
    assert logs.json()['total'] == 1
    assert app.state.registry.store(mirror['id']).logs(7, '', 1)['total'] == 0
    analytics = watcher.get(f"/api/sites/{mirror['id']}/analytics?scope=current")
    assert analytics.status_code == 200, analytics.text
    assert analytics.json()['summary']['total'] == 1
    assert analytics.json()['recent'][0]['domain'] == 'watch.test'
    assert analytics.json()['recent'][0]['site_id'] == mirror['id']
    assert 'one.test' not in analytics.text
    assert owner.get('/api/analytics?scope=all').json()['summary']['total'] == 1
    campaign = watcher.get('/api/campaign?range=today&domain=watch.test')
    assert campaign.status_code == 200, campaign.text
    assert 'one.test' not in campaign.text and 'watch.test' in campaign.text
    assert campaign.json()['totals']['clicks'] == 1
    source_campaign = owner.get('/api/campaign?range=today&domain=one.test')
    assert source_campaign.json()['totals']['clicks'] == 1
    assert 'one.test' in source_campaign.text
    analysis = watcher.get('/api/analysis?days=7&domain=watch.test')
    assert analysis.status_code == 200, analysis.text
    assert {item['domain'] for item in analysis.json()['events']['items']} == {'watch.test'}
    assert {item['domain'] for item in analysis.json()['requests']['items']} == {'watch.test'}
    assert 'one.test' not in analysis.text
    assert owner.post('/api/analysis/clear?domain=watch.test').status_code == 400
    assert owner.post(f"/api/sites/{mirror['id']}/logs/clear").status_code == 400
    assert source.logs(7, '', 1)['total'] == 1
    with source.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM client_events').fetchone()[0] == 1
    added = owner.post(f"/api/sites/{first['id']}/b-redirects/numbers", json={'phones': ['85290001111'], 'note': ''})
    assert added.status_code == 200, added.text
    assert [item['phone'] for item in app.state.registry.store(mirror['id']).whatsapp_numbers()] == ['85290001111']
    app.state.reputation.key = 'test-key'
    app.state.reputation.transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    checked = watcher.post(f"/api/sites/{mirror['id']}/reputation/check", json={})
    assert checked.status_code == 200, checked.text
    assert checked.json()['result']['status'] == 'clean'
    shown = watcher.get('/api/sites').json()['sites'][0]
    assert shown['google_reputation']['status'] == 'clean'
    assert app.state.registry.get(first['id'])['owner_id'] == one['id']
    assert app.state.registry.get(first['id'])['enabled'] == 1
    before = source.logs(7, '', 1)['total']
    writes = [
        ('POST', '/api/sites', {'domain': 'watcher.test'}),
        ('PUT', f"/api/sites/{mirror['id']}/config", {}),
        ('POST', f"/api/sites/{mirror['id']}/publish/A", {}),
        ('POST', f"/api/sites/{mirror['id']}/simulate", {}),
        ('POST', '/api/analysis/clear?domain=watch.test', {}),
        ('POST', '/api/campaign/clear?domain=watch.test', {}),
        ('POST', f"/api/sites/{mirror['id']}/logs/clear", {}),
        ('POST', f"/api/sites/{mirror['id']}/logs/clear-foreign", {}),
        ('POST', f"/api/sites/{mirror['id']}/desk/review", {'tickets': [], 'phone': ''}),
        ('POST', f"/api/sites/{mirror['id']}/desk/quote", {}),
        ('POST', f"/api/sites/{mirror['id']}/desk/sheet", {}),
        ('POST', f"/api/sites/{mirror['id']}/desk/screen/click", {'x': 1, 'y': 1}),
        ('POST', f"/api/sites/{mirror['id']}/cloudflare/status", {}),
        ('POST', f"/api/sites/{mirror['id']}/cloudflare/purge", {}),
        ('POST', f"/api/sites/{mirror['id']}/b-redirects/numbers/apply", {}),
        ('POST', f"/api/sites/{mirror['id']}/b-redirects/links", {'app': 'band', 'urls': ['https://band.us/n/example']}),
        ('POST', f"/api/sites/{mirror['id']}/b-redirects/links/apply", {}),
        ('DELETE', f"/api/sites/{mirror['id']}/b-redirects/links/1", None),
        ('POST', f"/api/sites/{mirror['id']}/b-redirects/numbers/reception", {'phone': '85211112222', 'display_name': 'Vivian', 'version_id': 'a' * 32}),
        ('POST', f"/api/sites/{mirror['id']}/b-redirects/direct-entry", {'version_id': 'a' * 32, 'expected_published': 'a' * 32}),
        ('PUT', f"/api/sites/{mirror['id']}/b-redirects/numbers/split", {'enabled': True, 'mode': 'random', 'members': []}),
        ('POST', '/api/accounts', {'username': 'another', 'password': 'watch-password-ok', 'role': 'agent'}),
        ('PATCH', f"/api/accounts/{one['id']}", {'enabled': False}),
        ('PUT', f"/api/sites/{mirror['id']}/owner", {'owner_id': 'admin'}),
        ('DELETE', f"/api/sites/{mirror['id']}", None),
    ]
    for method, path, body in writes:
        result = watcher.request(method, path, json=body)
        assert result.status_code == 403, (method, path, result.text)
        assert result.json()['detail'] == '观察号只能查看'
    assert app.state.registry.store(first['id']).logs(7, '', 1)['total'] == before
    assert watcher.get('/api/sites').status_code == 200
    refused = owner.put(f"/api/sites/{first['id']}/owner", json={'owner_id': created.json()['account']['id']})
    assert refused.status_code == 400, refused.text
    assert app.state.registry.get(first['id'])['owner_id'] == one['id']
    assert watcher.post('/api/logout').status_code == 200
    assert watcher.get('/api/sites').status_code == 401


def test_only_the_admin_can_delete_a_domain(accounts):
    app, auth, owner, a, b, one, two = accounts
    site = add(a, 'gone.test')
    refused = a.delete(f"/api/sites/{site['id']}")
    assert refused.status_code == 403
    assert app.state.registry.get(site['id'])['domain'] == 'gone.test'
    assert owner.delete('/api/sites/default').status_code == 400
    result = owner.delete(f"/api/sites/{site['id']}")
    assert result.status_code == 200, result.text
    assert result.json()['domain'] == 'gone.test'
    assert all(row['domain'] != 'gone.test' for row in owner.get('/api/sites').json()['sites'])
    assert a.get('/api/sites').json()['sites'] == []


def test_registry_owner_migration_preserves_existing_sites_and_worker_fields(tmp_path):
    registry = Registry(tmp_path)
    site = registry.add('old.test')
    with registry.connect() as db:
        db.execute('ALTER TABLE sites DROP COLUMN owner_id')
        db.execute("UPDATE sites SET note='keep',generation=42,panel_id=28 WHERE id=?", (site['id'],))
    migrated = Registry(tmp_path)
    assert migrated.get(site['id'])['owner_id'] == 'admin'
    assert migrated.get(site['id'])['note'] == 'keep'
    assert migrated.get(site['id'])['generation'] == 42
    assert migrated.get(site['id'])['panel_id'] == 28
    assert migrated.get(site['id'])['mirror_source'] == ''
    assert migrated.get(site['id'])['observer_account'] == ''
    assert len(migrated.list('unassigned-agent')) == 0


def test_default_site_agent_audit_excludes_all_account_records(accounts):
    app, auth, owner, a, b, one, two = accounts
    owner.put('/api/sites/default/owner', json={'owner_id':one['id']})
    with auth.store.connect() as db:
        auth.store._audit(db, 'config_updated', {'before': {}, 'after': {}})
    result = a.get('/api/sites/default/audit')
    assert result.status_code == 200
    assert [row['action'] for row in result.json()['items']] == ['config_updated']
    assert 'agent-two' not in result.text and 'admin888' not in result.text
    assert len(owner.get('/api/audit').json()['items']) > 1


def test_legacy_aliases_require_explicit_site_even_after_default_transfer(accounts):
    app, auth, owner, a, b, one, two = accounts
    owner.put('/api/sites/default/owner', json={'owner_id': one['id']})
    for suffix in ('state', 'logs', 'logs.csv', 'audit', 'analytics?scope=current'):
        assert a.get('/api/' + suffix).status_code == 404
    assert a.post('/api/counters/reset', json={}).status_code == 404
    assert a.get('/api/sites/default/state').status_code == 200
    assert a.get('/api/analytics?scope=all').status_code == 200


@pytest.mark.parametrize('revoke', ['transfer', 'disable', 'password', 'transfer-back', 'disable-enable'])
def test_private_previews_are_revoked_with_account_or_ownership(accounts, revoke):
    app, auth, owner, a, b, one, two = accounts
    site = add(a, 'preview.test')
    prefix = f"/api/sites/{site['id']}"
    upload = a.post(prefix + '/upload/A?name=private.html', content=b'<h1>PRIVATE DRAFT</h1>')
    assert upload.status_code == 200, upload.text
    version_id = upload.json()['version']['id']
    url = a.post(prefix + '/preview', json={'version_id': version_id}).json()['url']
    settings = Deployment(admin_origin='https://admin.test', target_origin='https://visitor.test')
    target = TestClient(create_target(app.state.registry.root, deployment=settings), base_url=settings.target_origin,
                        client=('127.0.0.1', 4000))
    target.headers.update({'X-Real-IP':'203.0.113.10', 'X-Forwarded-Proto':'https'})
    assert target.get(url).status_code == 200
    if revoke in ('transfer', 'transfer-back'):
        owner.put(prefix + '/owner', json={'owner_id': two['id']})
        if revoke == 'transfer-back':
            owner.put(prefix + '/owner', json={'owner_id': one['id']})
    elif revoke in ('disable', 'disable-enable'):
        owner.patch(f"/api/accounts/{one['id']}", json={'enabled': False})
        if revoke == 'disable-enable':
            owner.patch(f"/api/accounts/{one['id']}", json={'enabled': True})
    else:
        owner.post(f"/api/accounts/{one['id']}/password", json={'password': 'replacement-test-password'})
    assert target.get(url).status_code == 404


def test_follow_button_moves_the_observer_and_keeps_every_record(accounts):
    app, auth, owner, a, b, one, two = accounts
    old = add(owner, 'old-source.test')
    new = add(owner, 'new-source.test')
    watch = add(owner, 'watch.test')
    created = owner.post('/api/accounts', json={'username': 'follow-watcher', 'password': 'watch-password-ok', 'role': 'observer'})
    assert created.status_code == 200, created.text
    watcher_id = created.json()['account']['id']
    bound = owner.put(f"/api/sites/{watch['id']}/mirror", json={'source_id': old['id'], 'observer_id': watcher_id})
    assert bound.status_code == 200, bound.text

    def seed(site_id, phone):
        store = app.state.registry.store(site_id)
        with store.connect() as db:
            db.execute('INSERT INTO events(created,ip,country,device,slot,reason,path,mode) VALUES(?,?,?,?,?,?,?,?)',
                       (time.time(), '203.0.113.0/24', 'KR', 'mobile', 'B', 'allowed', '/', 'RULES'))
        store.record_client_event('{"event":"page_view","page":"https://example.test/"}', 'example.test')
        store.record_access('ua', '', '', '', '', '', '', phone, '', 'HK', '', 200, 1, '/')
        return store

    old_store = seed(old['id'], '852111')
    new_store = seed(new['id'], '852222')
    watch_store = seed(watch['id'], '852333')
    html = b"<html><script>directMode: 'ABCABC'</script></html>"
    version = write_bundle([('index.html', html)], 'page.html', new_store.pages, hashlib.sha256(html).hexdigest())
    new_store.add_version('B', version)
    new_store.publish('B', version['id'])
    new_store.add_whatsapp_numbers(['85290001111'], '值班', 'Vivian')

    def counts(store):
        with store.connect() as db:
            return (
                db.execute('SELECT COUNT(*) FROM events').fetchone()[0],
                db.execute('SELECT COUNT(*) FROM client_events').fetchone()[0],
                db.execute('SELECT COUNT(*) FROM access_log').fetchone()[0],
            )

    before = {name: counts(store) for name, store in (('old', old_store), ('new', new_store), ('watch', watch_store))}
    assert before == {'old': (1, 1, 1), 'new': (1, 1, 1), 'watch': (1, 1, 1)}
    assert 'id="follow-observer"' in owner.get('/').text
    assert '设置更寻' in owner.get('/').text
    watcher = sign_in(app, 'follow-watcher', 'watch-password-ok')
    assert 'id="follow-observer"' not in watcher.get('/').text
    assert 'id="follow-observer"' not in a.get('/').text
    assert a.post(f"/api/sites/{new['id']}/follow", json={}).status_code == 404
    agent_site = add(a, 'agent-source.test')
    assert a.post(f"/api/sites/{agent_site['id']}/follow", json={}).status_code == 403

    moved = owner.post(f"/api/sites/{new['id']}/follow", json={})
    assert moved.status_code == 200, moved.text
    body = moved.json()
    assert body['source_domain'] == 'new-source.test'
    assert body['observers'] == [{'id': watch['id'], 'domain': 'watch.test', 'synced': True}]
    assert body['sync_failed'] is False
    assert '以前的记录都还在' in body['message']
    assert app.state.registry.get(watch['id'])['mirror_source'] == new['id']
    assert app.state.registry.get(watch['id'])['observer_account'] == watcher_id
    assert app.state.registry.get(old['id'])['mirror_source'] == ''
    after = {name: counts(store) for name, store in (('old', old_store), ('new', new_store), ('watch', watch_store))}
    assert after == before
    copied = (watch_store.pages / watch_store.slots()['B'] / 'index.html').read_text()
    assert "directMode: ''" in copied
    assert 'ABCABC' not in copied
    assert [item['phone'] for item in watch_store.whatsapp_numbers()] == ['85290001111']
    assert [item['phone'] for item in old_store.whatsapp_numbers()] == []
    assert [item['phone'] for item in new_store.whatsapp_numbers()] == ['85290001111']

    again = owner.post(f"/api/sites/{new['id']}/follow", json={})
    assert again.status_code == 200, again.text
    assert again.json()['observers'][0]['synced'] is False
    assert counts(watch_store) == before['watch']
    own = owner.post(f"/api/sites/{watch['id']}/follow", json={})
    assert own.status_code == 400
    assert '观察域名自己' in own.json()['detail']
    assert counts(old_store) == before['old']
    cleared = owner.put(f"/api/sites/{watch['id']}/mirror", json={'source_id': '', 'observer_id': ''})
    assert cleared.status_code == 200, cleared.text
    missing = owner.post(f"/api/sites/{new['id']}/follow", json={})
    assert missing.status_code == 400
    assert '还没有观察域名' in missing.json()['detail']
    assert counts(old_store) == before['old']
    assert counts(new_store) == before['new']
    assert counts(watch_store) == before['watch']


def test_deleting_a_domain_can_keep_its_visits_on_the_observer(accounts):
    app, auth, owner, a, b, one, two = accounts
    source = add(owner, 'old-source.test')
    watch = add(owner, 'watch.test')
    created = owner.post('/api/accounts', json={'username': 'keep-watcher', 'password': 'watch-password-ok', 'role': 'observer'})
    assert created.status_code == 200, created.text
    watcher_id = created.json()['account']['id']
    bound = owner.put(f"/api/sites/{watch['id']}/mirror", json={'source_id': source['id'], 'observer_id': watcher_id})
    assert bound.status_code == 200, bound.text
    source_store = app.state.registry.store(source['id'])
    watch_store = app.state.registry.store(watch['id'])
    with source_store.connect() as db:
        db.execute('INSERT INTO events(created,ip,country,device,slot,reason,path,mode,device_details) VALUES(?,?,?,?,?,?,?,?,?)',
                   (time.time(), '203.0.113.9', 'HK', 'mobile', 'B', 'allowed', 'https://old-source.test/go', 'RULES', None))
    with watch_store.connect() as db:
        db.execute('INSERT INTO events(created,ip,country,device,slot,reason,path,mode,device_details) VALUES(?,?,?,?,?,?,?,?,?)',
                   (time.time(), '203.0.113.8', 'HK', 'mobile', 'A', 'allowed', 'https://watch.test/own', 'RULES', None))
    source_store.record_access('ua', '', '', '', '', 'https://old-source.test/', 'gclid=1', '852111', '', 'HK', '', 200, 1, '/')
    source_store.record_client_event('{"event":"whatsapp_click","page":"https://old-source.test/go"}', 'old-source.test')
    source_store.record_intake('203.0.113.9', 'ua', 'https://old-source.test', '', 'text/plain', 4, 'body', 200, '')
    source_store.add_whatsapp_numbers(['85290001111'], '值班', 'Vivian')
    assert owner.delete(f"/api/sites/{watch['id']}?keep_records=true").status_code == 400
    assert app.state.registry.get(watch['id'])['domain'] == 'watch.test'

    removed = owner.delete(f"/api/sites/{source['id']}?keep_records=true")
    assert removed.status_code == 200, removed.text
    body = removed.json()
    assert body['kept'][0]['domain'] == 'watch.test'
    assert body['kept'][0]['events'] == 1
    assert body['kept'][0]['access_log'] == 1
    assert body['kept'][0]['client_events'] == 1
    assert body['kept'][0]['intake_log'] == 1
    assert all(row['domain'] != 'old-source.test' for row in owner.get('/api/sites').json()['sites'])
    kept_site = app.state.registry.get(watch['id'])
    assert kept_site['mirror_source'] == ''
    assert kept_site['observer_account'] == watcher_id
    logs = watch_store.logs(7, '', 1, page_size=20)
    assert logs['total'] == 2
    paths = {item['path'] for item in logs['items']}
    assert 'https://watch.test/go' in paths
    assert 'https://watch.test/own' in paths
    assert [item['phone'] for item in watch_store.whatsapp_numbers()] == []
    with watch_store.connect() as db:
        copied = db.execute('SELECT body, origin FROM kept_client_events').fetchone()
        assert 'watch.test' in copied['body'] and 'old-source.test' not in copied['body']
        assert copied['origin'] == 'watch.test'
        assert db.execute('SELECT COUNT(*) FROM kept_access_log').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM kept_intake_log').fetchone()[0] == 1

    nxt = add(owner, 'new-source.test')
    nxt_store = app.state.registry.store(nxt['id'])
    with nxt_store.connect() as db:
        db.execute('INSERT INTO events(created,ip,country,device,slot,reason,path,mode,device_details) VALUES(?,?,?,?,?,?,?,?,?)',
                   (time.time(), '203.0.113.7', 'KR', 'mobile', 'B', 'allowed', 'https://new-source.test/', 'RULES', None))
    rebound = owner.put(f"/api/sites/{watch['id']}/mirror", json={'source_id': nxt['id'], 'observer_id': watcher_id})
    assert rebound.status_code == 200, rebound.text
    shown = owner.get(f"/api/sites/{watch['id']}/logs?days=7")
    assert shown.status_code == 200, shown.text
    shown_paths = {item['path'] for item in shown.json()['items']}
    assert 'https://watch.test/go' in shown_paths
    assert any('new-source.test' not in path and path != 'https://watch.test/go' for path in shown_paths)
    assert shown.json()['total'] == 2

    other = add(owner, 'plain.test')
    owner.put(f"/api/sites/{watch['id']}/mirror", json={'source_id': '', 'observer_id': ''})
    refused = owner.delete(f"/api/sites/{other['id']}?keep_records=true")
    assert refused.status_code == 400
    assert '还没有观察域名' in refused.json()['detail']
    assert app.state.registry.get(other['id'])['domain'] == 'plain.test'
