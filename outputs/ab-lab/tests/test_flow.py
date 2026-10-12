import json

from fastapi.testclient import TestClient

from ablab import web
from ablab.archives import write_bundle
from ablab.flow import build_flow, normalize_placement
from ablab.web import create_admin


IPHONE = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 Version/18.7 Mobile/15E148 Safari/604.1'
ANDROID = 'Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36'
WINDOWS = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'


def visit(created, query, ua, ip='203.0.113.10', number='', domain='one.test', path='/'):
    return {'created': created, 'domain': domain, 'query': query, 'ua': ua, 'ip': ip, 'wa_number': number, 'path': path, 'referer': ''}


def event(created, body, domain='one.test', row_id=1):
    return {'id': row_id, 'created': created, 'domain': domain, 'origin': domain, 'body': json.dumps(body, ensure_ascii=False)}


def test_placement_names_keep_a_platform_and_drop_the_version():
    assert normalize_placement('mobileapp::2-com.etnet.android.iq') == 'Android · com.etnet.android.iq'
    assert normalize_placement('Android App · com.etnet.android.iq') == 'Android · com.etnet.android.iq'
    assert normalize_placement('132.android.com.etnet.android.iq.adsenseformobileapps.com') == 'Android · com.etnet.android.iq'
    assert normalize_placement('3.4.19.iphone.com.etnet.quote.iq-streaming.adsenseformobileapps.com') == 'iOS · com.etnet.quote.iq-streaming'
    assert normalize_placement('3.4.20.iphone.com.etnet.quote.iq-streaming.adsenseformobileapps.com') == 'iOS · com.etnet.quote.iq-streaming'
    assert normalize_placement('mobileapp::1-123456789') == 'iOS · 123456789'
    assert normalize_placement('iOS App · 123456789 · ETNet') == 'iOS · 123456789 · ETNet'
    assert normalize_placement('https://news.example/story') == '网站 · news.example'
    assert normalize_placement('news.example') == '网站 · news.example'
    assert flow_system_of(IPHONE) == 'iOS'
    assert flow_system_of(ANDROID) == 'Android'
    assert flow_system_of(WINDOWS) == '桌面'


def flow_system_of(ua):
    from ablab.flow import flow_system
    return flow_system(ua)


def test_one_visit_keeps_its_chain_and_versions_share_a_row():
    raw = '3.4.19.iphone.com.etnet.quote.iq-streaming.adsenseformobileapps.com'
    other = '3.4.20.iphone.com.etnet.quote.iq-streaming.adsenseformobileapps.com'
    visits = [visit(100, f'gclid=person&placement={raw}', IPHONE, number='85211112222')]
    clicks = [event(100 + index, {
        'event': 'whatsapp_click', 'transaction_id': f't-{index}', 'gclid': 'person', 'placement': raw if index % 2 == 0 else other,
        'ua': IPHONE, 'link_type': 'universal', 'since_load_ms': 8000,
    }, row_id=index) for index in range(1, 6)]
    opened = event(106, {'event': 'wa_app_opened', 'transaction_id': 't-1', 'gclid': 'person', 'placement': raw, 'ua': IPHONE, 'link_type': 'universal', 'since_load_ms': 9000}, row_id=9)
    report = build_flow(visits, clicks + [opened], [])
    row = report['visits'][0]
    assert row['gclid'] == 'person'
    assert row['placement_raw'] == raw
    assert row['placement_norm'] == 'iOS · com.etnet.quote.iq-streaming'
    assert row['ua'] == IPHONE
    assert row['system'] == 'iOS'
    assert row['browser'] == 'Safari'
    assert row['number'] == '85211112222'
    assert row['align'] == 'gclid'
    assert row['chain'].startswith('whatsapp_click tx=t-1 wa_ms=8000 link_type=universal')
    assert 'wa_app_opened' in row['chain']
    assert row['test'] is False
    assert report['systems'][0]['clicks'] == 1
    assert report['systems'][0]['click_times'] == 5
    assert report['placements'][0]['name'] == 'iOS · com.etnet.quote.iq-streaming'
    assert len([item for item in report['placements'] if item['name'].startswith('iOS · com.etnet')]) == 1


def test_a_visit_without_events_stays_and_ip_ua_joins_within_thirty_minutes():
    quiet = visit(100, 'gclid=quiet&placement=news.example', IPHONE)
    plain = visit(200, 'placement=news.example', ANDROID, ip='203.0.113.20')
    body = {'event': 'whatsapp_click', 'transaction_id': 'late', 'placement': 'news.example', 'ua': ANDROID, 'link_type': 'scheme', 'since_load_ms': 10000}
    matched = event(200 + 20 * 60, body, row_id=2)
    missed = event(200 + 40 * 60, dict(body, transaction_id='too-late'), row_id=3)
    intake = [{'created': matched['created'], 'status': 204, 'body': matched['body'], 'ip': '203.0.113.20', 'ua': ANDROID, 'domain': 'one.test'}]
    report = build_flow([quiet, plain], [matched, missed], intake)
    assert report['visits'][2]['chain'] == '无事件' or any(item['gclid'] == 'quiet' and item['chain'] == '无事件' for item in report['visits'])
    joined = next(item for item in report['visits'] if item['align'] == 'ipua')
    assert 'whatsapp_click tx=late' in joined['chain']
    lost = next(item for item in report['visits'] if 'too-late' in item['chain'])
    assert lost['align'] == '未对齐'


def test_test_traffic_is_kept_out_of_the_totals():
    fast = visit(1000, 'gclid=fast&placement=news.example', IPHONE)
    click = event(1001, {'event': 'whatsapp_click', 'transaction_id': 'fast', 'gclid': 'fast', 'placement': 'news.example', 'ua': IPHONE, 'since_load_ms': 1500}, row_id=1)
    report = build_flow([fast], [click], [])
    assert report['visits'][0]['test'] is True
    assert '打开后 3 秒内点击' in report['visits'][0]['kind']
    assert report['systems'] == []
    assert report['test']['click_times'] == 1

    repeated = []
    clicks = []
    for index in range(3):
        repeated.append(visit(2000 + index * 30, f'gclid=r{index}&placement=news.example', ANDROID, ip='203.0.113.30'))
        clicks.append(event(2000 + index * 30 + 10, {
            'event': 'whatsapp_click', 'transaction_id': f'r-{index}', 'gclid': f'r{index}', 'placement': 'news.example', 'ua': ANDROID, 'since_load_ms': 10000,
        }, row_id=10 + index))
    again = build_flow(repeated, clicks, [])
    assert all(item['test'] for item in again['visits'])
    assert again['test']['click_times'] == 3
    assert again['systems'] == []


def test_zero_clicks_wake_rate_and_a_quiet_quarter_hour_are_flagged():
    base = 1_700_000_000
    quiet = [visit(base + index, f'gclid=v{index}&placement=3.4.19.iphone.com.etnet.quote.iq-streaming.adsenseformobileapps.com', IPHONE, ip=f'198.51.100.{index % 200}') for index in range(50)]
    flagged = build_flow(quiet, [], [])
    assert flagged['systems'][0]['alert'] is True
    assert '疑似点击或上报失效' in flagged['systems'][0]['alert_reason']
    assert build_flow(quiet[:49], [], [])['systems'][0]['alert'] is False

    def clicks(prefix, placement, count, opened=0):
        rows = []
        for index in range(count):
            rows.append(event(base + index, {
                'event': 'whatsapp_click', 'transaction_id': f'{prefix}-{index}', 'gclid': f'{prefix}-{index}',
                'placement': placement, 'ua': IPHONE, 'since_load_ms': 9000,
            }, row_id=100 + index))
        for index in range(opened):
            rows.append(event(base + index, {
                'event': 'wa_app_opened', 'transaction_id': f'{prefix}-{index}', 'gclid': f'{prefix}-{index}',
                'placement': placement, 'ua': IPHONE,
            }, row_id=200 + index))
        return rows
    wake = build_flow([], clicks('bad', 'mobileapp::1-111', 10, 0) + clicks('good', 'mobileapp::1-222', 10, 10), [])
    bad = next(row for row in wake['placements'] if row['name'] == 'iOS · 111')
    good = next(row for row in wake['placements'] if row['name'] == 'iOS · 222')
    assert bad['alert'] is True and '疑似唤醒失败' in bad['alert_reason']
    assert good['alert'] is False

    started = base
    walking = [visit(started + index * 300, f'gclid=w{index}&placement=news.example', ANDROID, ip=f'203.0.113.{index}') for index in range(5)]
    first = event(started, {'event': 'whatsapp_click', 'transaction_id': 'only', 'gclid': 'w0', 'placement': 'news.example', 'ua': ANDROID, 'since_load_ms': 9000}, row_id=1)
    gap = build_flow(walking, [first], [])
    assert any('接收中断' in item['reason'] for item in gap['alerts'])
    assert any(item['alert'] for item in gap['trend'])
    short = build_flow(walking[:2], [first], [])
    assert not any('接收中断' in item['reason'] for item in short['alerts'])


def test_iphone_and_android_show_up_in_the_visit_detail_and_the_raw_log(tmp_path, monkeypatch):
    monkeypatch.setattr(web, 'geo_country', lambda ip: 'HK')
    admin = TestClient(create_admin(tmp_path), base_url='http://127.0.0.1:8765', client=('127.0.0.1', 9))
    admin.headers.update({'Origin': 'http://127.0.0.1:8765', 'X-CSRF-Token': admin.app.state.csrf})
    created = admin.post('/api/sites', json={'domain': 'one.example.com'})
    site_id = created.json()['site']['id']
    store = admin.app.state.registry.store(site_id)
    page = '<!doctype html><body><script>const CONFIG = { eventEndpoint: "", whatsappNumber: "85200000000" }; function buildWhatsAppUrls(){ return {}; }</script></body>'
    version = write_bundle([('index.html', page.encode())], 'landing.html', store.pages, 'land')
    store.add_version('B', version)
    store.publish('B', version['id'])
    store.record_access(IPHONE, '', '', '', '', '', 'gclid=iphone-1&placement=3.4.19.iphone.com.etnet.quote.iq-streaming.adsenseformobileapps.com', '85211110001', '', 'HK', '', 200, 1, '/', '203.0.113.10')
    store.record_access(ANDROID, '', '', '', '', '', 'gclid=android-1&placement=132.android.com.etnet.android.iq.adsenseformobileapps.com', '85211110002', '', 'HK', '', 200, 1, '/', '203.0.113.11')

    def post(ua, body):
        response = admin.post('/wa-events', content=json.dumps(body), headers={
            'Content-Type': 'text/plain', 'Origin': 'https://one.example.com', 'User-Agent': ua, 'Referer': 'https://one.example.com/',
        })
        assert response.status_code == 204
        assert response.headers['access-control-allow-origin'] == '*'

    post(IPHONE, {'event': 'whatsapp_click', 'transaction_id': 'iphone-click', 'gclid': 'iphone-1', 'placement': '3.4.19.iphone.com.etnet.quote.iq-streaming.adsenseformobileapps.com', 'ua': IPHONE, 'link_type': 'universal', 'since_load_ms': 8000, 'number': '85211110001'})
    post(IPHONE, {'event': 'wa_app_opened', 'transaction_id': 'iphone-click', 'gclid': 'iphone-1', 'placement': '3.4.19.iphone.com.etnet.quote.iq-streaming.adsenseformobileapps.com', 'ua': IPHONE, 'link_type': 'universal', 'since_load_ms': 9000})
    post(ANDROID, {'event': 'whatsapp_click', 'transaction_id': 'android-click', 'gclid': 'android-1', 'placement': 'mobileapp::2-com.etnet.android.iq', 'ua': ANDROID, 'link_type': 'intent', 'since_load_ms': 7000, 'number': '85211110002'})
    post(ANDROID, {'event': 'wa_app_opened', 'transaction_id': 'android-click', 'gclid': 'android-1', 'placement': 'mobileapp::2-com.etnet.android.iq', 'ua': ANDROID, 'link_type': 'intent', 'since_load_ms': 7600})
    empty = admin.post('/wa-events', content=b'', headers={'Origin': 'https://one.example.com', 'Content-Type': 'text/plain', 'User-Agent': IPHONE})
    broken = admin.post('/wa-events', content=b'not-json', headers={'Origin': 'https://one.example.com', 'Content-Type': 'text/plain', 'User-Agent': ANDROID})
    huge = admin.post('/wa-events', content=b'x' * 8193, headers={'Origin': 'https://one.example.com', 'Content-Type': 'text/plain', 'User-Agent': IPHONE})
    assert empty.status_code == 204 and broken.status_code == 204 and huge.status_code == 413
    rejected = TestClient(admin.app, base_url='http://127.0.0.1:8765', client=('8.8.8.8', 9))
    denied = rejected.post('/wa-events', content=b'{"event":"whatsapp_click"}', headers={'Origin': 'https://one.example.com', 'Content-Type': 'text/plain', 'User-Agent': IPHONE})
    assert denied.status_code == 403
    web.flush_access_log()

    report = admin.get('/api/campaign/flow?range=today&domain=one.example.com')
    assert report.status_code == 200, report.text
    body = report.json()
    chains = {item['gclid']: item for item in body['visits']}
    assert 'whatsapp_click tx=iphone-click wa_ms=8000 link_type=universal' in chains['iphone-1']['chain']
    assert 'wa_app_opened' in chains['iphone-1']['chain']
    assert chains['iphone-1']['placement_norm'] == 'iOS · com.etnet.quote.iq-streaming'
    assert chains['iphone-1']['number'] == '85211110001'
    assert 'whatsapp_click tx=android-click wa_ms=7000 link_type=intent' in chains['android-1']['chain']
    assert chains['android-1']['placement_norm'] == 'Android · com.etnet.android.iq'
    reasons = {item['reason'] for item in body['intake']}
    assert {'已接收', '空 body', '不是 JSON', '超过大小限制', '来源、域名或代理校验失败'} <= reasons
    assert any(item['ua'] == IPHONE and item['status'] == 204 for item in body['intake'])
    assert any(item['ua'] == ANDROID and item['status'] == 204 for item in body['intake'])
    exported = admin.get('/api/campaign/visits.csv?range=today&domain=one.example.com').text
    assert 'iphone-click' in exported and 'android-click' in exported and '无事件' not in exported.split('iphone-1')[1][:400]
    raw = admin.get('/api/campaign/intake.csv?range=today&domain=one.example.com').text
    assert IPHONE in raw and ANDROID in raw and '不是 JSON' in raw
    summary = admin.get('/api/campaign/flow.csv?range=today&domain=one.example.com').text
    assert '系统' in summary and 'iOS' in summary and 'Android' in summary
    source = admin.get('/api/campaign/page?domain=one.example.com')
    assert source.status_code == 200
    assert 'landing-B.html' in source.headers['content-disposition']
    assert 'https://hhucuq.top/wa-events' in source.text
    assert 'id="ab-dest-app"' in source.text
    assert 'function buildWhatsAppUrls' in source.text
