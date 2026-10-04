import sqlite3

from fastapi.testclient import TestClient

from ablab.auth import Auth
from ablab.settings import Deployment
from ablab.sites import Registry
from ablab.store import Store
from ablab.web import create_admin, create_target


def local_client(app, port):
    return TestClient(app, base_url=f'http://127.0.0.1:{port}', client=('127.0.0.1', 10000))


def test_old_registry_gains_notes_without_losing_created_or_stage(tmp_path):
    with sqlite3.connect(tmp_path / 'registry.db') as db:
        db.execute('''CREATE TABLE sites(
            id TEXT PRIMARY KEY, domain TEXT UNIQUE NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
            stage TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
            next_attempt REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
            panel_id INTEGER, managed_path TEXT NOT NULL DEFAULT '', generation INTEGER NOT NULL DEFAULT 0)''')
        db.execute("INSERT INTO sites(id,domain,stage,created) VALUES('default','','legacy',123.5)")
    registry = Registry(tmp_path)
    assert registry.get('default')['note'] == ''
    assert registry.get('default')['created'] == 123.5
    assert registry.get('default')['stage'] == 'legacy'
    assert Registry(tmp_path).get('default')['note'] == ''


def test_note_roundtrip_validation_and_site_isolation(tmp_path):
    app = create_admin(tmp_path)
    admin = local_client(app, 8765)
    admin.headers.update({'Origin': 'http://127.0.0.1:8765', 'X-CSRF-Token': app.state.csrf})
    registry = app.state.registry
    first = registry.add('first.example.com')
    second = registry.add('second.example.com')
    path = f"/api/sites/{first['id']}/metadata"
    note = '<script>alert("x")</script> & 复核记录'
    response = admin.patch(path, json={'note': note})
    assert response.status_code == 200, response.text
    assert response.json()['site']['note'] == note
    assert Registry(tmp_path).get(first['id'])['note'] == note
    assert registry.get(second['id'])['note'] == ''
    assert next(site for site in admin.get('/api/sites').json()['sites'] if site['id'] == first['id'])['note'] == note
    assert response.json()['site']['created'] == first['created']
    events = registry.events(first['id'])
    assert events[0]['stage'] == 'unconfigured' and events[0]['detail'] == '备注已更新'
    assert note not in str(events)
    assert admin.patch(path, json={'note': note}).status_code == 200
    assert registry.events(first['id']) == events
    for body in [{}, {'note': 42}, {'note': 'x' * 201}, {'note': 'ok', 'unexpected': 1}]:
        assert admin.patch(path, json=body).status_code == 422
    assert registry.get(first['id'])['note'] == note
    assert admin.patch('/api/sites/unknown/metadata', json={'note': ''}).status_code == 404
    assert admin.patch(path, json={'note': ''}).json()['site']['note'] == ''


def test_availability_preserves_verified_stage_and_gates_default_and_scoped_visitors(tmp_path):
    app = create_admin(tmp_path)
    admin = local_client(app, 8765)
    admin.headers.update({'Origin': 'http://127.0.0.1:8765', 'X-CSRF-Token': app.state.csrf})
    registry = app.state.registry
    site = registry.add('live.example.com')
    assert registry.transition(site['id'], site['generation'], 'active', detail='已验收')
    for site_id, suffix in [('default', 'old'), (site['id'], 'new')]:
        store = registry.store(site_id)
        store.add_links('B', [f'https://example.com/{suffix}'])
        config, revision = store.config()
        store.save_config(config.model_copy(update={'content_mode': 'LINK'}), revision)
    target = local_client(create_target(tmp_path), 8766)
    default_path = '/api/sites/default/availability/'
    site_path = f"/api/sites/{site['id']}/availability/"
    assert target.get('/', follow_redirects=False).status_code == 302
    assert target.get(f"/_sites/{site['id']}/", follow_redirects=False).status_code == 302
    assert admin.post(default_path + 'offline', json={}).json()['site']['enabled'] == 0
    offline = admin.post(site_path + 'offline', json={}).json()['site']
    assert offline['enabled'] == 0 and offline['stage'] == 'active' and offline['error'] == '已验收'
    assert registry.events(site['id'])[0]['stage'] == 'active'
    assert registry.events(site['id'])[0]['detail'] == '用户下线'
    offline_events = registry.events(site['id'])
    assert admin.post(site_path + 'offline', json={}).json()['site']['generation'] == offline['generation']
    assert registry.events(site['id']) == offline_events
    assert target.get('/').status_code == 503
    assert target.get(f"/_sites/{site['id']}/").status_code == 503
    assert admin.post(default_path + 'online', json={}).json()['site']['stage'] == 'legacy'
    online = admin.post(site_path + 'online', json={}).json()['site']
    assert online['enabled'] == 1 and online['stage'] == 'active' and online['error'] == '已验收'
    assert registry.events(site['id'])[0]['stage'] == 'active'
    assert registry.events(site['id'])[0]['detail'] == '用户上线'
    online_events = registry.events(site['id'])
    assert admin.post(site_path + 'online', json={}).json()['site']['generation'] == online['generation']
    assert registry.events(site['id']) == online_events
    assert target.get('/', follow_redirects=False).headers['location'] == 'https://example.com/old'
    assert target.get(f"/_sites/{site['id']}/", follow_redirects=False).headers['location'] == 'https://example.com/new'
    assert admin.post(site_path + 'later', json={}).status_code == 422
    assert admin.post('/api/sites/unknown/availability/offline', json={}).status_code == 404


def test_offline_cancels_worker_generation_and_online_queues_incomplete_site(tmp_path):
    registry = Registry(tmp_path)
    site = registry.add('pending.example.com')
    assert registry.transition(site['id'], site['generation'], 'failed', detail='DNS 失败', delay=3600)
    failed = registry.get(site['id'])
    offline = registry.set_availability(site['id'], 'offline')
    assert offline['enabled'] == 0 and offline['stage'] == 'failed' and offline['error'] == 'DNS 失败'
    assert registry.events(site['id'])[0]['stage'] == 'failed'
    assert registry.events(site['id'])[0]['detail'] == '用户下线'
    assert offline['generation'] == failed['generation'] + 1
    assert not registry.transition(site['id'], failed['generation'], 'active')
    online = registry.set_availability(site['id'], 'online')
    assert online['enabled'] == 1 and online['stage'] == 'waiting_dns' and online['next_attempt'] == 0
    assert registry.events(site['id'])[0]['stage'] == 'waiting_dns'
    assert registry.events(site['id'])[0]['detail'] == '用户开启接入'
    online_events = registry.events(site['id'])
    assert registry.set_availability(site['id'], 'online')['generation'] == online['generation']
    assert registry.events(site['id']) == online_events
    assert online['generation'] == offline['generation'] + 1
    assert not registry.transition(site['id'], offline['generation'], 'active')
    assert registry.transition(site['id'], online['generation'], 'active')
    assert registry.get(site['id'])['stage'] == 'active'


def test_metadata_and_availability_require_production_auth_and_csrf(tmp_path):
    settings = Deployment('https://admin.example.com', 'https://old.example.com')
    Auth(Store(tmp_path)).set_password('owner', 'a-long-test-password-123')
    app = create_admin(tmp_path, deployment=settings)
    admin = TestClient(app, base_url=settings.admin_origin, client=('127.0.0.1', 1000))
    admin.headers.update({'X-Real-IP': '203.0.113.9', 'X-Forwarded-Proto': 'https', 'Origin': settings.admin_origin})
    assert admin.patch('/api/sites/default/metadata', json={'note': 'private'}).status_code == 401
    assert admin.post('/api/sites/default/availability/offline', json={}).status_code == 401
    admin.headers['X-CSRF-Token'] = app.state.csrf
    login = admin.post('/api/login', json={'username': 'owner', 'password': 'a-long-test-password-123'})
    assert login.status_code == 200
    assert admin.patch('/api/sites/default/metadata', json={'note': 'private'}).status_code == 403
    assert admin.post('/api/sites/default/availability/offline', json={}).status_code == 403
    admin.headers['X-CSRF-Token'] = login.json()['csrf']
    assert admin.patch('/api/sites/default/metadata', json={'note': 'private'}).status_code == 200
    assert admin.post('/api/sites/default/availability/offline', json={}).status_code == 200
    target = TestClient(create_target(tmp_path, deployment=settings), base_url=settings.target_origin, client=('127.0.0.1', 1001))
    target.headers.update({'X-Real-IP': '203.0.113.9', 'X-Forwarded-Proto': 'https'})
    assert target.get('/').status_code == 403
    assert admin.post('/api/sites/default/availability/online', json={}).status_code == 200
    assert target.get('/').status_code == 503  # Host is open; no content has been published.
