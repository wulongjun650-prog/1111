import pytest
from fastapi.testclient import TestClient

from ablab import web
from ablab.web import create_admin, create_target


@pytest.fixture(autouse=True)
def hong_kong(monkeypatch):
    monkeypatch.setattr(web, 'geo_country', lambda ip: 'HK')


def clients(tmp_path):
    target = TestClient(create_target(tmp_path), base_url='http://127.0.0.1:8766', client=('127.0.0.1', 9))
    admin = TestClient(create_admin(tmp_path), base_url='http://127.0.0.1:8765', client=('127.0.0.1', 9))
    admin.headers.update({'Origin': 'http://127.0.0.1:8765', 'X-CSRF-Token': admin.app.state.csrf})
    return admin, target


def test_each_request_keeps_the_raw_ua_query_and_client_hints(tmp_path):
    admin, target = clients(tmp_path)
    ua = 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 SamsungBrowser/30.0 Chrome/120.0.0.0 Mobile Safari/537.36'
    response = target.get('/?gclid=abc123&placement=feed&x=1', headers={
        'User-Agent': ua,
        'Sec-CH-UA': '"Chromium";v="120"',
        'Sec-CH-UA-Platform': '"Android"',
        'Sec-CH-UA-Platform-Version': '"10.0.0"',
        'Sec-CH-UA-Mobile': '?1',
        'Referer': 'https://ads.example/landing',
        'CF-Cache-Status': 'MISS',
        'CF-IPCity': 'Hong%20Kong',
    })
    assert response.status_code == 503
    web.flush_access_log()
    report = admin.get('/api/access-report').text
    assert response.headers['content-type'].startswith('text/html')
    assert report.startswith('访问日志周报')
    assert admin.get('/api/access-report').headers['content-type'].startswith('text/plain')
    assert f'1\t{ua}' in report
    assert '含wv)\tiOS且不含Safari/\t带gclid\t带placement\t请求数' in report
    assert '\t0\t0\t1\t1\t1' in report
    with admin.app.state.store.connect() as db:
        row = db.execute('SELECT * FROM access_log').fetchone()
    assert row['ua'] == ua
    assert row['ch_ua'] == '"Chromium";v="120"'
    assert row['ch_platform'] == '"Android"'
    assert row['ch_platform_version'] == '"10.0.0"'
    assert row['ch_mobile'] == '?1'
    assert row['referer'] == 'https://ads.example/landing'
    assert row['query'] == 'gclid=abc123&placement=feed&x=1'
    assert row['cf_cache_status'] == 'MISS'
    assert row['city'] == 'Hong Kong'
    assert row['status'] == 503
    assert row['duration_ms'] >= 0


def test_ios_webview_without_safari_is_its_own_bucket(tmp_path):
    admin, target = clients(tmp_path)
    webview = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148'
    target.get('/?placement=reels', headers={'User-Agent': webview})
    target.get('/', headers={'User-Agent': webview + ' Safari/604.1'})
    target.get('/app?gclid=1', headers={'User-Agent': 'Mozilla/5.0 (Linux; Android 10; wv) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36'})
    web.flush_access_log()
    report = admin.get('/api/access-report').text
    assert f'1\t{webview}' in report
    assert '\t0\t1\t0\t1\t1' in report
    assert '\t1\t0\t1\t0\t1' in report


def test_beacon_stores_the_json_as_received_and_ignores_origin(tmp_path):
    admin, target = clients(tmp_path)
    body = '{"event":"whatsapp_click","ts":1,"transaction_id":"t-1","wa_env":"ios_safari","link_type":"universal","cta":"hero","placement":"feed","app":"whatsapp","gclid":"abc","number":"85211112222","wa_msg":"你好","tag_ready":true,"ua":"Mozilla/5.0"}'
    posted = target.post('/api/collect', content=body, headers={'Origin': 'https://evil.example', 'Content-Type': 'text/plain'})
    assert posted.status_code == 204
    assert posted.text == ''
    other = target.post('/api/collect', content='not-json', headers={'Origin': 'https://other.example', 'Content-Type': 'text/plain'})
    assert other.status_code == 204
    web.flush_access_log()
    report = admin.get('/api/access-report').text
    assert body in report
    assert 'not-json' in report
    assert '_raw' not in report
    huge = target.post('/api/collect', content='{' + ('"a":1,' * 2000) + '"z":1}')
    assert huge.status_code == 413


def test_assigned_whatsapp_number_is_recorded_with_the_request(tmp_path):
    admin, target = clients(tmp_path)
    page = '<!doctype html><html><body><p id="reception-text">本次由助理 Chloe 接待</p><script>var CONFIG={whatsappNumber:"85200000000",receptionist:"Chloe"};</script></body></html>'
    uploaded = admin.post('/api/upload/B?name=index.html', content=page.encode())
    assert uploaded.status_code == 200, uploaded.text
    version = uploaded.json()['version']['id']
    assert admin.post('/api/publish/B', json={'version_id': version}).status_code == 200
    number = admin.post('/api/b-redirects/numbers', json={'phones': ['85211112222'], 'note': '', 'display_name': 'Vivian'}).json()['numbers'][0]
    assert admin.put('/api/b-redirects/numbers/split', json={'enabled': True, 'mode': 'random', 'members': [{'number_id': number['id'], 'weight': 1}]}).status_code == 200
    opened = target.get('/')
    assert opened.status_code == 200
    web.flush_access_log()
    assert '85211112222' in opened.text
    with admin.app.state.store.connect() as db:
        row = db.execute('SELECT wa_number, status, country FROM access_log ORDER BY id DESC LIMIT 1').fetchone()
    assert row['wa_number'] == '85211112222'
    assert row['status'] == 200
    assert row['country'] == 'HK'


def test_visitors_outside_hong_kong_are_not_stored(tmp_path, monkeypatch):
    monkeypatch.setattr(web, 'geo_country', lambda ip: 'US')
    admin, target = clients(tmp_path)
    assert target.get('/').status_code == 503
    posted = target.post('/api/collect', content='{"event":"whatsapp_click"}', headers={'Content-Type': 'text/plain'})
    assert posted.status_code == 204
    web.flush_access_log()
    with admin.app.state.store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM access_log').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM client_events').fetchone()[0] == 0
