import hashlib
import time
import pytest
from fastapi.testclient import TestClient
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


def test_observer_reads_every_domain_and_account_but_cannot_change_anything(accounts):
    app, auth, owner, a, b, one, two = accounts
    first, second = add(a, 'one.test'), add(b, 'two.test')
    with app.state.registry.store(first['id']).connect() as db:
        db.execute('INSERT INTO events(created,ip,country,device,slot,reason,path,mode) VALUES(?,?,?,?,?,?,?,?)',
                   (time.time()-60, '203.0.113.0/24', 'KR', 'mobile', 'B', 'allowed', '/', 'RULES'))
    created = owner.post('/api/accounts', json={'username': 'watcher', 'password': 'watch-password-ok', 'role': 'observer'})
    assert created.status_code == 200, created.text
    assert created.json()['account']['role'] == 'observer'
    assert 'password' not in created.json()['account']
    assert a.post('/api/accounts', json={'username': 'forged-watch', 'password': 'watch-password-ok', 'role': 'observer'}).status_code == 403
    watcher = sign_in(app, 'watcher', 'watch-password-ok')
    page = watcher.get('/')
    assert page.status_code == 200
    assert 'data-tab="accounts"' in page.text and '观察账号' in page.text
    catalog = watcher.get('/api/sites')
    assert catalog.status_code == 200, catalog.text
    assert {site['domain'] for site in catalog.json()['sites']} >= {'one.test', 'two.test'}
    assert catalog.json()['sites'][-1]['owner_username'] == 'agent-two'
    listed = watcher.get('/api/accounts')
    assert listed.status_code == 200, listed.text
    assert {item['username'] for item in listed.json()['items']} >= {'admin888', 'agent-one', 'agent-two', 'watcher'}
    assert all('password' not in item for item in listed.json()['items'])
    assert a.get('/api/accounts').status_code == 403
    assert watcher.get(f"/api/sites/{second['id']}/state").status_code == 200
    assert watcher.get(f"/api/sites/{first['id']}/logs").status_code == 200
    assert watcher.get(f"/api/sites/{first['id']}/audit").status_code == 200
    analytics = watcher.get('/api/analytics?scope=all')
    assert analytics.status_code == 200, analytics.text
    assert analytics.json()['summary']['total'] == 1
    before = app.state.registry.store(first['id']).logs(7, '', 1)['total']
    writes = [
        ('POST', '/api/sites', {'domain': 'watcher.test'}),
        ('PUT', f"/api/sites/{first['id']}/config", {}),
        ('POST', f"/api/sites/{first['id']}/publish/A", {}),
        ('POST', f"/api/sites/{first['id']}/simulate", {}),
        ('POST', f"/api/sites/{first['id']}/logs/clear", {}),
        ('POST', f"/api/sites/{first['id']}/logs/clear-foreign", {}),
        ('POST', f"/api/sites/{first['id']}/desk/review", {'tickets': [], 'phone': ''}),
        ('POST', f"/api/sites/{first['id']}/desk/quote", {}),
        ('POST', f"/api/sites/{first['id']}/desk/sheet", {}),
        ('POST', f"/api/sites/{first['id']}/desk/screen/click", {'x': 1, 'y': 1}),
        ('POST', f"/api/sites/{first['id']}/reputation/check", {}),
        ('POST', f"/api/sites/{first['id']}/b-redirects/numbers/apply", {}),
        ('POST', '/api/accounts', {'username': 'another', 'password': 'watch-password-ok', 'role': 'agent'}),
        ('PATCH', f"/api/accounts/{one['id']}", {'enabled': False}),
        ('PUT', f"/api/sites/{first['id']}/owner", {'owner_id': 'admin'}),
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
