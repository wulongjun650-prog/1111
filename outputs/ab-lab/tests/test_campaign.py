import json

from fastapi.testclient import TestClient

from ablab.campaign import build_campaign, device_from_ua, placement_label, window_bounds
from ablab.web import create_admin, create_target


def test_placement_and_phone_labels():
    assert placement_label('') == '未知'
    assert placement_label('mobileapp::2-com.example.app') == 'Android App · com.example.app'
    assert placement_label('mobileapp::1-12345') == 'iOS App · 12345'
    assert placement_label('news.example') == 'news.example'
    assert device_from_ua('Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) Version/18.7 Mobile/15E148 Safari/604.1') == ('iPhone', 'iOS 18.7')
    assert device_from_ua('Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36') == ('Pixel 7', 'Android 13')


def test_campaign_counts_visits_clicks_and_red_rows():
    def visit(domain, query, ip, ua='Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X)', path='/'):
        return {'domain': domain, 'query': query, 'ip': ip, 'ua': ua, 'path': path, 'created': 1}
    def event(domain, body, row_id):
        return {'domain': domain, 'body': json.dumps(body, ensure_ascii=False), 'id': row_id, 'created': row_id, 'origin': domain}

    visits = [
        visit('one.test', 'gclid=g1&placement=mobileapp::2-com.foo', '1.1.1.1'),
        visit('one.test', 'gclid=g1&placement=mobileapp::2-com.foo', '9.9.9.9'),
        visit('one.test', 'placement=mobileapp::2-com.foo', '2.2.2.2'),
        visit('one.test', 'placement=mobileapp::2-com.foo', '2.2.2.2'),
        visit('one.test', 'placement=news.example', '3.3.3.3', ua='Mozilla/5.0 (Linux; Android 13; Pixel 7) Chrome/120.0.0.0 Mobile'),
        visit('one.test', 'gclid=asset', '4.4.4.4', path='/app.js'),
    ]
    events = [
        event('one.test', {'event': 'whatsapp_click', 'transaction_id': 't1', 'wa_env': 'ios_safari', 'link_type': 'universal', 'device': 'mobile', 'placement': 'mobileapp::2-com.foo', 'gclid': 'g1', 'ua': 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X)'}, 1),
        event('one.test', {'event': 'whatsapp_click', 'transaction_id': 't1', 'wa_env': 'ios_safari', 'link_type': 'universal', 'device': 'mobile', 'placement': 'mobileapp::2-com.foo', 'gclid': 'g1', 'ua': 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X)'}, 2),
        event('one.test', {'event': 'wa_app_opened', 'transaction_id': 't1', 'wa_env': 'ios_safari', 'link_type': 'universal', 'device': 'mobile', 'placement': 'mobileapp::2-com.foo', 'gclid': 'g1', 'future': {'keep': True}}, 3),
        event('one.test', {'event': 'wa_fallback_shown', 'transaction_id': 't2', 'wa_env': 'ios_safari', 'link_type': 'scheme', 'device': 'mobile', 'placement': 'news.example', 'ua': 'Mozilla/5.0 (Linux; Android 13; Pixel 7) Chrome/120.0.0.0 Mobile'}, 4),
        event('one.test', {'event': 'wa_fallback_click', 'transaction_id': 't2', 'wa_env': 'ios_safari', 'link_type': 'scheme', 'device': 'mobile', 'placement': 'news.example'}, 5),
        event('one.test', {'event': 'wa_copy_number', 'transaction_id': 't2', 'wa_env': 'ios_safari', 'link_type': 'scheme', 'device': 'mobile', 'placement': 'news.example'}, 6),
        event('one.test', {'event': 'wa_copy_message', 'transaction_id': 't2', 'wa_env': 'ios_safari', 'link_type': 'scheme', 'device': 'mobile', 'placement': 'news.example'}, 7),
    ]
    report = build_campaign(visits, events, {})
    android = next(row for row in report['rows'] if row['placement'] == 'mobileapp::2-com.foo' and row['clicks'])
    assert android['placement_label'] == 'Android App · com.foo'
    assert android['clicks'] == 1
    assert android['opens'] == 1
    assert android['visits'] == 1
    assert report['totals']['visits'] == 3
    assert android['model'] == 'iPhone'
    news = next(row for row in report['rows'] if row['placement'] == 'news.example' and row['fallbacks'])
    assert news['fallbacks'] == 1
    assert news['fallback_click'] == 1 and news['copy_number'] == 1 and news['copy_message'] == 1
    assert news['model'] == 'Pixel 7'
    assert report['totals']['clicks'] == 1
    assert all(row['path'] != '/app.js' for row in report['rows'] if 'path' in row)

    low, high = [], []
    for index in range(30):
        low.append(event('one.test', {'event': 'whatsapp_click', 'transaction_id': f'low-{index}', 'wa_env': 'android_chrome', 'link_type': 'universal', 'device': 'mobile', 'placement': 'slow.example'}, 100 + index))
        if index < 15:
            low.append(event('one.test', {'event': 'wa_app_opened', 'transaction_id': f'low-{index}', 'wa_env': 'android_chrome', 'link_type': 'universal', 'device': 'mobile', 'placement': 'slow.example'}, 200 + index))
    for index in range(100):
        high.append(event('one.test', {'event': 'whatsapp_click', 'transaction_id': f'high-{index}', 'wa_env': 'android_chrome', 'link_type': 'universal', 'device': 'mobile', 'placement': 'fast.example'}, 300 + index))
        high.append(event('one.test', {'event': 'wa_app_opened', 'transaction_id': f'high-{index}', 'wa_env': 'android_chrome', 'link_type': 'universal', 'device': 'mobile', 'placement': 'fast.example'}, 400 + index))
    flagged = build_campaign([], low + high, {})
    slow = next(row for row in flagged['rows'] if row['placement'] == 'slow.example')
    fast = next(row for row in flagged['rows'] if row['placement'] == 'fast.example')
    assert slow['clicks'] == 30 and slow['opens'] == 15 and slow['alert'] is True
    assert '低于 50%' not in slow['alert_reason']
    assert '60%' in slow['alert_reason']
    assert fast['alert'] is False
    absolute = build_campaign([], [
        event('one.test', {'event': 'whatsapp_click', 'transaction_id': f'abs-{index}', 'wa_env': 'ios_safari', 'link_type': 'api', 'placement': 'abs.example', 'device': 'mobile'}, 500 + index)
        for index in range(30)
    ] + [
        event('one.test', {'event': 'wa_app_opened', 'transaction_id': f'abs-{index}', 'wa_env': 'ios_safari', 'link_type': 'api', 'placement': 'abs.example', 'device': 'mobile'}, 600 + index)
        for index in range(10)
    ], {})
    assert absolute['rows'][0]['alert'] is True and '低于 50%' in absolute['rows'][0]['alert_reason']
    small = build_campaign([], [
        event('one.test', {'event': 'whatsapp_click', 'transaction_id': 'only', 'wa_env': 'ios_safari', 'link_type': 'api', 'placement': 'abs.example', 'device': 'mobile'}, 1)
    ], {})
    assert small['rows'][0]['alert'] is False
    joined = build_campaign([], [
        event('one.test', {'event': 'whatsapp_click', 'transaction_id': 'join', 'wa_env': 'ios_safari', 'link_type': 'universal', 'device': 'mobile', 'placement': 'mobileapp::2-com.foo', 'ua': 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X)'}, 1),
        event('one.test', {'event': 'wa_app_opened', 'transaction_id': 'join'}, 2),
    ], {'model': 'iPhone'})
    assert len(joined['rows']) == 1
    assert joined['rows'][0]['clicks'] == 1 and joined['rows'][0]['opens'] == 1
    assert joined['rows'][0]['model'] == 'iPhone' and joined['rows'][0]['wa_env'] == 'ios_safari'


def test_campaign_api_filters_and_exports(tmp_path):
    admin = TestClient(create_admin(tmp_path), base_url='http://127.0.0.1:8765', client=('127.0.0.1', 9))
    admin.headers.update({'Origin': 'http://127.0.0.1:8765', 'X-CSRF-Token': admin.app.state.csrf})
    store = admin.app.state.store
    store.record_access('Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X)', '', '', '', '', '', 'gclid=abc&placement=mobileapp::2-com.foo', '', '', 'HK', '', 200, 1, '/', '203.0.113.8')
    store.record_access('Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X)', '', '', '', '', '', 'gclid=abc&placement=mobileapp::2-com.foo', '', '', 'HK', '', 200, 1, '/', '203.0.113.9')
    store.record_access('Mozilla/5.0', '', '', '', '', '', '', '', '', 'HK', '', 200, 1, '/app.js', '203.0.113.8')
    body = '{"event":"whatsapp_click","transaction_id":"same","wa_env":"ios_safari","link_type":"universal","device":"mobile","placement":"mobileapp::2-com.foo","gclid":"abc","ua":"Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X)","extra":1}'
    store.record_client_event(body, 'shop.example')
    store.record_client_event('{"event":"wa_app_opened","transaction_id":"same","wa_env":"ios_safari","link_type":"universal","device":"mobile","placement":"mobileapp::2-com.foo","gclid":"abc"}', 'shop.example')
    store.record_client_event('{"event":"whatsapp_click","transaction_id":"other","wa_env":"ios_safari","link_type":"universal","device":"mobile","placement":"news.example"}', 'shop.example')
    data = admin.get('/api/campaign?range=today&placement=com.foo')
    assert data.status_code == 200, data.text
    payload = data.json()
    assert payload['endpoint'] == 'https://hhucuq.top/wa-events'
    assert payload['totals']['visits'] == 1
    assert payload['totals']['clicks'] == 1
    assert payload['totals']['opens'] == 1
    assert payload['rows'][0]['placement_label'] == 'Android App · com.foo'
    assert payload['rows'][0]['open_rate'] == 1
    exported = admin.get('/api/campaign/events?range=today')
    bodies = [item['body'] for item in exported.json()]
    assert [item['transaction_id'] for item in exported.json()] == ['other', 'same', 'same']
    assert '"extra":1' in ''.join(bodies)
    csv_text = admin.get('/api/campaign.csv?range=today').text
    assert '点击数' in csv_text and 'Android App · com.foo' in csv_text
    assert admin.get('/api/campaign?range=custom&start=2026-01-02&end=2026-01-01').status_code == 400
    store.record_client_event('{"event":"whatsapp_click","transaction_id":"blank","wa_env":"ios_safari","device":"mobile"}', 'shop.example')
    exact = admin.get('/api/campaign/events?range=today&domain=shop.example&placement=&placement_exact=true&wa_env=ios_safari')
    assert [item['transaction_id'] for item in exact.json()] == ['blank']
    narrowed = admin.get('/api/campaign?range=today&model=iPhone')
    assert narrowed.json()['totals']['opens'] == 1
    opened, closed = window_bounds('today', '', '', 1_700_000_000)
    assert closed - opened == 86400


def test_served_page_fills_only_the_empty_event_endpoint(tmp_path):
    admin = TestClient(create_admin(tmp_path), base_url='http://127.0.0.1:8765', client=('127.0.0.1', 9))
    target = TestClient(create_target(tmp_path), base_url='http://127.0.0.1:8766', client=('127.0.0.1', 9))
    admin.headers.update({'Origin': 'http://127.0.0.1:8765', 'X-CSRF-Token': admin.app.state.csrf})
    page = '''<!doctype html><html><body><script>
const CONFIG = { whatsappNumber: "85200000000", eventEndpoint: "", receptionist: "Chloe" };
function goWhatsApp(){ location.href = "https://wa.me/" + CONFIG.whatsappNumber; }
</script></body></html>'''
    uploaded = admin.post('/api/upload/B?name=page.html', content=page.encode())
    assert uploaded.status_code == 200, uploaded.text
    assert admin.post('/api/publish/B', json={'version_id': uploaded.json()['version']['id']}).status_code == 200
    served = target.get('/')
    assert served.status_code == 200
    url = 'https://hhucuq.top/wa-events'
    assert served.text == page.replace('eventEndpoint: ""', f'eventEndpoint: "{url}"')
    flags = served.headers['content-security-policy'].split(';')[0].split()
    assert 'allow-popups' in flags and 'allow-popups-to-escape-sandbox' in flags
    assert 'allow-top-navigation-to-custom-protocols' in flags and 'allow-top-navigation' not in flags
    stored = next((admin.app.state.store.pages / admin.app.state.store.slots()['B']).rglob('*.html')).read_text(encoding='utf-8')
    assert stored == page
    kept = page.replace('eventEndpoint: ""', 'eventEndpoint: "https://other.example/off"')
    uploaded = admin.post('/api/upload/B?name=page.html', content=kept.encode())
    admin.post('/api/publish/B', json={'version_id': uploaded.json()['version']['id']})
    assert target.get('/').text == kept
