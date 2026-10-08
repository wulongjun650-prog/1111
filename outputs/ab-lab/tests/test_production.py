import pytest
from fastapi.testclient import TestClient
from ablab.settings import Deployment
from ablab.auth import Auth
from ablab.store import Store
from ablab.web import create_admin, create_target


@pytest.fixture
def prod(tmp_path):
    settings = Deployment(admin_origin='https://admin.lab.example', target_origin='https://lab.example')
    auth = Auth(Store(tmp_path))
    auth.set_password('owner', 'a-long-test-password-123')
    app = create_admin(tmp_path, deployment=settings)
    client = TestClient(app, base_url=settings.admin_origin, client=('127.0.0.1', 32100))
    client.headers.update({'X-Real-IP': '203.0.113.9', 'X-Forwarded-Proto': 'https', 'Origin': settings.admin_origin, 'X-CSRF-Token': app.state.csrf})
    return tmp_path, settings, auth, client


def login(client):
    response = client.post('/api/login', json={'username': 'owner', 'password': 'a-long-test-password-123'})
    assert response.status_code == 200, response.text
    client.headers['X-CSRF-Token'] = response.json()['csrf']
    return response


def test_public_mode_requires_https_separate_hosts_and_credentials(tmp_path):
    for admin, target in [('http://admin.test', 'https://target.test'), ('https://same.test', 'https://same.test:8443'), ('https://a.test/path', 'https://b.test')]:
        with pytest.raises(ValueError):
            Deployment(admin_origin=admin, target_origin=target)
    with pytest.raises(ValueError):
        create_admin(tmp_path, deployment=Deployment(admin_origin='https://a.test', target_origin='https://b.test'))


def test_auth_secure_cookie_and_logout_revoke(prod):
    _, _, auth, client = prod
    assert client.get('/api/state').status_code == 401
    assert client.get('/', follow_redirects=False).headers['location'] == '/login'
    assert client.get('/login').status_code == 200
    assert 'autocomplete="current-password"' in client.get('/login').text
    assert client.get('/static/login.js').status_code == 200
    assert client.get('/static/app.js').status_code == 401
    assert client.get('/static/angel-v2.png').status_code == 401
    assert client.get('/static/demon-v2.png').status_code == 401
    assert client.get('/static/demon-rest-v2.png').status_code == 401
    assert client.get('/static/theme.mjs').status_code == 401
    assert client.get('/static/dashboard.css').status_code == 401
    response = login(client)
    cookie = response.headers['set-cookie']
    assert '__Host-ab_session=' in cookie and 'Secure' in cookie and 'HttpOnly' in cookie and 'SameSite=strict' in cookie
    assert 'Domain=' not in cookie
    assert client.get('/api/state').status_code == 200
    assert client.get('/api/state').json()['health']['local_only'] is False
    page = client.get('/')
    assert '双子星' in page.text and 'id="logout"' in page.text
    assert '自有服务器 / 数据自主保存' in page.text
    assert '本地运行 / 数据留在本机' not in page.text
    assert client.headers['X-CSRF-Token'] in page.text
    token = client.cookies.get('__Host-ab_session')
    assert auth.session(token) is not None
    assert client.post('/api/logout', json={}).status_code == 200
    assert auth.session(token) is None
    assert client.get('/api/state').status_code == 401


def test_wrong_origin_session_csrf_host_and_untrusted_proxy_blocked(prod):
    data, settings, _, client = prod
    login(client)
    assert client.post('/api/counters/reset', headers={'X-CSRF-Token': 'bad'}, json={}).status_code == 403
    assert client.post('/api/counters/reset', headers={'Origin': settings.target_origin}, json={}).status_code == 403
    assert client.get('/api/state', headers={'Host': 'evil.example'}).status_code == 403
    assert client.get('/api/state', headers={'X-Forwarded-Proto': 'http'}).status_code == 403
    assert client.get('/api/state', headers={'X-Real-IP': '1.2.3.4, 5.6.7.8'}).status_code == 403
    remote = TestClient(create_admin(data, deployment=settings), base_url=settings.admin_origin, client=('198.51.100.1', 1))
    assert remote.get('/login', headers={'X-Real-IP': '127.0.0.1', 'X-Forwarded-Proto': 'https'}).status_code == 403


def test_target_uses_real_proxy_ip_and_cannot_serve_admin(prod):
    data, settings, _, admin = prod
    login(admin)
    for slot in ('A', 'B'):
        vid = admin.post(f'/api/upload/{slot}?name=site.html', content=f'<h1>{slot}</h1>'.encode()).json()['version']['id']
        admin.post(f'/api/publish/{slot}', json={'version_id': vid})
    state = admin.get('/api/state').json()
    state['config']['rules']['blacklist'] = ['203.0.113.9']
    admin.put('/api/config', json={'config': state['config'], 'revision': state['revision']})
    target = TestClient(create_target(data, deployment=settings), base_url=settings.target_origin, client=('127.0.0.1', 1))
    target.headers.update({'X-Real-IP': '203.0.113.9', 'X-Forwarded-Proto': 'https'})
    assert '<h1>A</h1>' in target.get('/').text
    assert '<h1>B</h1>' in target.get('/', headers={'X-Real-IP': '198.51.100.2'}).text
    assert target.get('/api/state').status_code == 404
    assert settings.admin_origin in target.get('/').headers['content-security-policy']


def test_password_reset_revokes_session_and_login_stays_open(prod):
    _, _, auth, client = prod
    login(client)
    old_token = client.cookies.get('__Host-ab_session')
    auth.set_password('owner', 'another-long-password-456')
    assert auth.session(old_token) is None
    client.headers['X-CSRF-Token'] = client.app.state.csrf
    for _ in range(8):
        response = client.post('/api/login', json={'username': 'owner', 'password': 'wrong'})
        assert response.status_code == 401
        assert response.json()['detail'] == '账号或密码错误'


def test_password_never_stored_and_sessions_expire(prod):
    _, _, auth, client = prod
    login(client)
    token = client.cookies.get('__Host-ab_session')
    with auth.store.connect() as db:
        password = db.execute('SELECT password_hash FROM admin_account').fetchone()[0]
        assert 'a-long-test-password' not in password
        stored = db.execute('SELECT token_hash FROM account_sessions').fetchone()[0]
        assert stored != token
        db.execute('UPDATE account_sessions SET expires=0')
    assert client.get('/api/state').status_code == 401
