import json
import subprocess

from fastapi.testclient import TestClient

from ablab.campaign import build_campaign, device_from_ua, placement_label, window_bounds
from ablab.web import create_admin, create_target, inject_dest_app


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
    assert android['dest_app'] == 'whatsapp' and android['dest_label'] == 'WhatsApp'
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
    assert '点击数' in csv_text and '去向' in csv_text and 'WhatsApp' in csv_text and 'Android App · com.foo' in csv_text
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


def _event(domain, body, row_id):
    return {'domain': domain, 'body': json.dumps(body, ensure_ascii=False), 'id': row_id, 'created': row_id, 'origin': domain}


def test_band_and_kakao_count_apart_from_whatsapp():
    def click(app, index, placement, opened=False):
        name = {'whatsapp': 'whatsapp_click', 'band': 'band_click', 'kakao': 'kakao_click'}[app]
        opened_name = {'whatsapp': 'wa_app_opened', 'band': 'band_app_opened', 'kakao': 'kakao_app_opened'}[app]
        rows = [_event('one.test', {'event': name, 'transaction_id': f'{app}-{index}', 'wa_env': 'android_chrome', 'link_type': 'universal', 'device': 'mobile', 'placement': placement}, index)]
        if opened:
            rows.append(_event('one.test', {'event': opened_name, 'transaction_id': f'{app}-{index}', 'wa_env': 'android_chrome', 'link_type': 'universal', 'device': 'mobile', 'placement': placement}, 10000 + index))
        return rows

    events = []
    for index in range(30):
        events.extend(click('whatsapp', index, 'wa.example', opened=index < 16))
    for index in range(100):
        events.extend(click('band', 1000 + index, 'band-fast.example', opened=True))
    for index in range(30):
        events.extend(click('band', 2000 + index, 'band-slow.example', opened=index < 10))
    events.extend(click('kakao', 3000, 'kakao.example', opened=True))
    events.append(_event('one.test', {'event': 'band_fallback_shown', 'transaction_id': 'band-2000', 'wa_env': 'android_chrome', 'link_type': 'universal', 'device': 'mobile', 'placement': 'band-slow.example', 'dest_app': 'band'}, 9))
    events.append(_event('one.test', {'event': 'kakao_copy_number', 'transaction_id': 'kakao-3000', 'wa_env': 'android_chrome', 'link_type': 'universal', 'device': 'mobile', 'placement': 'kakao.example'}, 10))
    report = build_campaign([], events, {})
    whatsapp = next(row for row in report['rows'] if row['dest_app'] == 'whatsapp')
    fast = next(row for row in report['rows'] if row['placement'] == 'band-fast.example')
    slow = next(row for row in report['rows'] if row['placement'] == 'band-slow.example')
    kakao = next(row for row in report['rows'] if row['dest_app'] == 'kakao')
    assert whatsapp['clicks'] == 30 and whatsapp['opens'] == 16 and whatsapp['alert'] is False
    assert fast['dest_label'] == 'BAND' and fast['clicks'] == 100 and fast['opens'] == 100 and fast['alert'] is False
    assert slow['clicks'] == 30 and slow['opens'] == 10 and slow['fallbacks'] == 1 and slow['alert'] is True
    assert '低于 50%' in slow['alert_reason']
    assert kakao['dest_label'] == 'Kakao' and kakao['clicks'] == 1 and kakao['opens'] == 1 and kakao['copy_number'] == 1
    assert kakao['alert'] is False
    assert report['totals']['clicks'] == 161 and report['totals']['opens'] == 127
    only_band = build_campaign([], events, {'dest_app': 'BAND'})
    assert {row['dest_app'] for row in only_band['rows']} == {'band'}
    assert only_band['totals']['clicks'] == 130


def _script_body(html):
    marker = '<script id="ab-dest-app">'
    start = html.index(marker) + len(marker)
    return html[start:html.index('</script>', start)]


def _run_destination(script, config):
    program = """
const CONFIG = %s;
function buildWhatsAppUrls() {
  return {phone:'85200000000', universal:'https://wa.me/85200000000?text=hi', scheme:'whatsapp://send?phone=85200000000', intent:'intent://send?phone=85200000000#Intent;scheme=whatsapp;package=com.whatsapp;end'};
}
const sent = [];
function sendEvent(name, extra) { sent.push({name: name, extra: extra}); }
%s
const urls = buildWhatsAppUrls('hi');
sendEvent('whatsapp_click', {transaction_id:'t1', placement:'keep'});
sendEvent('wa_app_opened', {transaction_id:'t1'});
sendEvent('wa_fallback_shown', null);
sendEvent('wa_fallback_click', {});
sendEvent('wa_copy_number', {});
sendEvent('wa_copy_message', {});
process.stdout.write(JSON.stringify({urls: urls, sent: sent}));
""" % (json.dumps(config), script)
    completed = subprocess.run(['node', '-e', program], check=True, capture_output=True, text=True)
    return json.loads(completed.stdout)


def test_served_page_switches_destination_only_after_the_link_is_set(tmp_path):
    admin = TestClient(create_admin(tmp_path), base_url='http://127.0.0.1:8765', client=('127.0.0.1', 9))
    target = TestClient(create_target(tmp_path), base_url='http://127.0.0.1:8766', client=('127.0.0.1', 9))
    admin.headers.update({'Origin': 'http://127.0.0.1:8765', 'X-CSRF-Token': admin.app.state.csrf})
    page = '''<!doctype html><html><body><script>
const CONFIG = { contactApp: "band", bandUrl: "https://band.us/n/example", whatsappNumber: "85200000000", eventEndpoint: "" };
function buildWhatsAppUrls(message) {
  return { phone: "85200000000", universal: "https://wa.me/85200000000", scheme: "whatsapp://send?phone=85200000000", intent: "intent://send" };
}
function sendEvent(name, extra) {}
function goWhatsApp(){ var urls = buildWhatsAppUrls("hi"); location.href = urls.universal; }
</script></body></html>'''
    uploaded = admin.post('/api/upload/B?name=page.html', content=page.encode())
    assert uploaded.status_code == 200, uploaded.text
    assert admin.post('/api/publish/B', json={'version_id': uploaded.json()['version']['id']}).status_code == 200
    served = target.get('/')
    assert served.status_code == 200
    assert 'id="ab-dest-app"' in served.text
    assert 'function buildWhatsAppUrls(message)' in served.text
    assert 'https://wa.me/85200000000' in served.text and 'whatsapp://send?phone=85200000000' in served.text
    assert 'function goWhatsApp(){ var urls = buildWhatsAppUrls("hi"); location.href = urls.universal; }' in served.text
    assert 'eventEndpoint: "https://hhucuq.top/wa-events"' in served.text
    flags = served.headers['content-security-policy'].split(';')[0].split()
    assert 'allow-popups' in flags and 'allow-popups-to-escape-sandbox' in flags
    assert 'allow-top-navigation-to-custom-protocols' in flags and 'allow-top-navigation' not in flags
    stored = next((admin.app.state.store.pages / admin.app.state.store.slots()['B']).rglob('*.html')).read_text(encoding='utf-8')
    assert stored == page
    assert 'ab-dest-app' not in stored
    script = _script_body(served.text)
    band = _run_destination(script, {'contactApp': 'band', 'bandUrl': ' https://band.us/n/example ', 'whatsappNumber': '+852 0000 0000'})
    assert band['urls'] == {'phone': '85200000000', 'universal': 'https://band.us/n/example', 'scheme': 'https://band.us/n/example', 'intent': 'https://band.us/n/example'}
    assert [item['name'] for item in band['sent']] == ['band_click', 'band_app_opened', 'band_fallback_shown', 'band_fallback_click', 'band_copy_number', 'band_copy_message']
    assert band['sent'][0]['extra']['dest_app'] == 'band' and band['sent'][0]['extra']['placement'] == 'keep'
    kakao = _run_destination(script, {'contactApp': 'KAKAO', 'kakaoLink': 'https://open.kakao.com/o/example'})
    assert kakao['urls']['universal'] == 'https://open.kakao.com/o/example'
    assert kakao['sent'][0]['name'] == 'kakao_click' and kakao['sent'][1]['name'] == 'kakao_app_opened'
    whatsapp = _run_destination(script, {'contactApp': 'whatsapp', 'bandUrl': 'https://band.us/n/example'})
    assert whatsapp['urls']['universal'] == 'https://wa.me/85200000000?text=hi'
    assert whatsapp['urls']['scheme'].startswith('whatsapp://')
    assert [item['name'] for item in whatsapp['sent']][0] == 'whatsapp_click'
    assert 'dest_app' not in (whatsapp['sent'][0]['extra'] or {})
    unset = _run_destination(script, {})
    assert unset['urls']['scheme'].startswith('whatsapp://') and unset['sent'][0]['name'] == 'whatsapp_click'
    missing = _run_destination(script, {'contactApp': 'band', 'bandUrl': '   '})
    assert missing['urls']['universal'].startswith('https://wa.me/') and missing['sent'][0]['name'] == 'whatsapp_click'
    raw = b'function buildWhatsAppUrls(){return "https://wa.me/1"}'
    assert inject_dest_app(raw, 'app.js') == raw
    assert inject_dest_app(page.encode(), 'notes.txt') == page.encode()
