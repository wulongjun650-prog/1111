import threading
import time

import pytest
from fastapi.testclient import TestClient

from ablab import web
from ablab.store import Store
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
    with admin.app.state.store.connect() as db:
        paths = [row[0] for row in db.execute("SELECT path FROM access_log WHERE path='/api/collect'")]
    assert paths == ['/api/collect', '/api/collect']
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


def test_analysis_panel_combines_every_domain(tmp_path):
    admin, target = clients(tmp_path)
    one = admin.post('/api/sites', json={'domain': 'one.example.com'}).json()['site']['id']
    two = admin.post('/api/sites', json={'domain': 'two.example.com'}).json()['site']['id']
    opened = target.get('/?gclid=from-default', headers={'User-Agent': 'HK-Default'})
    assert opened.status_code == 503
    assert '数据深度分析' not in opened.text
    posted = target.post('/api/collect', content='{"event":"whatsapp_click","number":"85200000000"}', headers={'Content-Type': 'text/plain'})
    assert posted.status_code == 204
    web.flush_access_log()
    for site_id, ua, body in ((one, 'HK-One', '{"event":"from-one"}'), (two, 'HK-Two', '{"event":"from-two"}')):
        store = admin.app.state.registry.store(site_id)
        store.record_access(ua, '', '', '"14.0.0"', '?1', 'https://ads.example', 'placement=feed', '85211112222', 'MISS', 'HK', 'Hong Kong', 200, 3, '/')
        store.record_client_event(body)
    page = admin.get('/')
    assert 'data-tab="analysis"' in page.text
    assert '数据深度分析' in page.text
    assert page.text.index('id="panel-analysis"') < page.text.index('id="panel-logs"')
    data = admin.get('/api/analysis?days=7')
    assert data.status_code == 200
    payload = data.json()
    domains = {item['domain'] for item in payload['requests']['items']}
    assert domains == {'本地站点', 'one.example.com', 'two.example.com'}
    assert payload['requests']['total'] >= 3
    one_row = next(item for item in payload['requests']['items'] if item['domain'] == 'one.example.com')
    assert one_row['ua'] == 'HK-One'
    assert one_row['ch_platform_version'] == '"14.0.0"'
    assert one_row['query'] == 'placement=feed'
    assert one_row['wa_number'] == '85211112222'
    bodies = {item['domain']: item['body'] for item in payload['events']['items']}
    assert bodies['one.example.com'] == '{"event":"from-one"}'
    assert bodies['two.example.com'] == '{"event":"from-two"}'
    assert '{"event":"whatsapp_click","number":"85200000000"}' in bodies['本地站点']
    report = admin.get('/api/analysis/report?days=7')
    assert report.headers['content-type'].startswith('text/plain')
    assert '===== one.example.com =====' in report.text
    assert '===== two.example.com =====' in report.text
    assert 'HK-One' in report.text and 'HK-Two' in report.text


def test_wa_events_keeps_the_raw_json_for_any_landing_domain(tmp_path):
    admin, target = clients(tmp_path)
    created = admin.post('/api/sites', json={'domain': 'one.example.com'})
    assert created.status_code == 200, created.text
    site_id = created.json()['site']['id']
    body = '{"event":"whatsapp_click","transaction_id":"t-9","wa_env":"ios_safari","link_type":"universal","placement_app":"feed","future_field":{"n":1}}'
    foreign = TestClient(admin.app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 11))
    posted = foreign.post('/wa-events', content=body, headers={
        'Content-Type': 'text/plain;charset=UTF-8',
        'Origin': 'https://one.example.com',
        'Sec-Fetch-Site': 'cross-site',
    })
    assert posted.status_code == 204
    assert posted.text == ''
    assert posted.headers['access-control-allow-origin'] == '*'
    referred = target.post('/wa-events', content='{"event":"wa_app_opened","transaction_id":"t-9"}', headers={
        'Referer': 'https://one.example.com/landing?gclid=1',
    })
    assert referred.status_code == 204
    assert referred.headers['access-control-allow-origin'] == '*'
    other = target.post('/wa-events', content='not-json', headers={'Origin': 'null', 'Content-Type': 'text/plain'})
    assert other.status_code == 204
    web.flush_access_log()
    with admin.app.state.registry.store(site_id).connect() as db:
        rows = db.execute('SELECT body, origin FROM client_events ORDER BY id').fetchall()
    assert [row['body'] for row in rows] == [body, '{"event":"wa_app_opened","transaction_id":"t-9"}']
    assert {row['origin'] for row in rows} == {'one.example.com'}
    with admin.app.state.store.connect() as db:
        kept = db.execute('SELECT body, origin FROM client_events').fetchone()
        assert kept['body'] == 'not-json' and kept['origin'] == ''
        assert db.execute("SELECT COUNT(*) FROM access_log WHERE path='/wa-events'").fetchone()[0] == 0
    huge = foreign.post('/wa-events', content='x' * 8193, headers={'Content-Type': 'text/plain', 'Origin': 'https://one.example.com'})
    assert huge.status_code == 413
    assert huge.headers['access-control-allow-origin'] == '*'
    page = target.get('/')
    policy = page.headers['content-security-policy']
    flags = policy.split(';')[0].split()
    assert flags == ['sandbox', 'allow-scripts', 'allow-forms', 'allow-same-origin', 'allow-popups', 'allow-popups-to-escape-sandbox', 'allow-top-navigation-to-custom-protocols']
    assert 'allow-top-navigation' not in flags


def test_wa_events_returns_before_the_write(tmp_path, monkeypatch):
    _admin, target = clients(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def slow(self, body, origin=''):
        started.set()
        release.wait(2)

    monkeypatch.setattr(Store, 'record_client_event', slow)
    try:
        began = time.perf_counter()
        posted = target.post('/wa-events', content='{"event":"whatsapp_click"}', headers={'Content-Type': 'text/plain', 'Origin': 'https://shop.example'})
        elapsed = time.perf_counter() - began
        assert posted.status_code == 204
        assert elapsed < 0.5
        assert started.wait(1)
    finally:
        release.set()
        web.flush_access_log()


def test_old_event_rows_gain_a_source_domain_column(tmp_path):
    import sqlite3
    path = tmp_path / 'app.db'
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE client_events(id INTEGER PRIMARY KEY, created REAL NOT NULL, body TEXT NOT NULL)')
    db.execute('INSERT INTO client_events(created, body) VALUES(?, ?)', (time.time(), '{"event":"whatsapp_click","transaction_id":"old","wa_env":"ios","link_type":"api"}'))
    db.commit()
    db.close()
    store = Store(tmp_path)
    report = store.access_report(7, None, '本地站点')
    assert '本地站点\tios\tapi\t1\t0\t0' in report
    assert '{"event":"whatsapp_click","transaction_id":"old","wa_env":"ios","link_type":"api"}' in report


def test_weekly_section_three_counts_domain_env_and_link(tmp_path):
    admin, _target = clients(tmp_path)
    store = admin.app.state.store
    rows = [
        ('{"event":"whatsapp_click","transaction_id":"b","wa_env":"ios","link_type":"universal","placement_app":"feed","future":1}', 'one.example'),
        ('{"event":"wa_app_opened","transaction_id":"a","wa_env":"ios","link_type":"universal"}', 'one.example'),
        ('{"event":"whatsapp_click","transaction_id":"a","wa_env":"ios","link_type":"universal"}', 'one.example'),
        ('{"event":"wa_fallback_shown","wa_env":"android","link_type":"scheme"}', 'two.example'),
        ('{"event":"wa_copy_number","transaction_id":"a","wa_env":"ios","link_type":"universal","note":"keep me"}', 'one.example'),
        ('not-json', 'two.example'),
    ]
    for body, origin in rows:
        store.record_client_event(body, origin)
    report = store.access_report(7, 'HK', '本地站点')
    assert 'one.example\tios\tuniversal\t2\t1\t0' in report
    assert 'two.example\tandroid\tscheme\t0\t0\t1' in report
    assert '"future":1' in report
    assert 'wa_copy_number' in report
    raw = report.split('# 3.1 全部原始 JSON（按 transaction_id 排序）', 1)[1]
    markers = [
        '"event":"wa_app_opened","transaction_id":"a"',
        '"event":"whatsapp_click","transaction_id":"a"',
        'wa_copy_number',
        '"transaction_id":"b"',
        'wa_fallback_shown',
        'not-json',
    ]
    positions = [raw.index(item) for item in markers]
    assert positions == sorted(positions)
    downloaded = admin.get('/api/analysis/report?days=7')
    assert 'one.example\tios\tuniversal\t2\t1\t0' in downloaded.text


def test_jump_events_outside_hong_kong_are_still_stored(tmp_path, monkeypatch):
    monkeypatch.setattr(web, 'geo_country', lambda ip: 'US')
    admin, target = clients(tmp_path)
    posted = target.post('/wa-events', content='{"event":"whatsapp_click","transaction_id":"us"}', headers={'Content-Type': 'text/plain', 'Origin': 'https://shop.example'})
    assert posted.status_code == 204
    collect = target.post('/api/collect', content='{"event":"whatsapp_click"}', headers={'Content-Type': 'text/plain'})
    assert collect.status_code == 204
    web.flush_access_log()
    with admin.app.state.store.connect() as db:
        rows = db.execute('SELECT body, origin FROM client_events').fetchall()
        assert db.execute('SELECT COUNT(*) FROM access_log').fetchone()[0] == 0
    assert [(row['body'], row['origin']) for row in rows] == [('{"event":"whatsapp_click","transaction_id":"us"}', 'shop.example')]


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
