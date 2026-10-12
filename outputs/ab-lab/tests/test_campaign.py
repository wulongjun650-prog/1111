import json
import subprocess
import time

from fastapi.testclient import TestClient

from ablab.campaign import build_campaign, device_from_ua, fill_youtube_channels, placement_label, window_bounds, youtube_from
from ablab.web import create_admin, create_target, inject_dest_app


def test_open_app_names_wechat_and_huawei_before_chrome():
    from ablab.devices import describe_device, open_app
    wechat = 'Mozilla/5.0 (Linux; Android 12; NOH-AN00 Build/HUAWEINOH-AN00; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/107.0.5304.141 Mobile Safari/537.36 MicroMessenger/8.0.35.2360 WeChat/arm64 Weixin'
    huawei = 'Mozilla/5.0 (Linux; Android 10; LYA-AL00) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/83.0.4103.106 HuaweiBrowser/11.0.3.300 Mobile Safari/537.36'
    chrome = 'Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36'
    safari = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 Version/17.1 Mobile/15E148 Safari/604.1'
    assert open_app(wechat) == '微信'
    assert open_app(huawei) == '华为浏览器'
    assert open_app(chrome) == 'Chrome'
    assert open_app(safari) == 'Safari'
    assert open_app('') == '未知'
    assert describe_device(wechat)['browser'] == '微信'
    assert describe_device(huawei)['browser'] == '华为浏览器'
    assert describe_device(chrome)['browser'] == 'Chrome 120.0.0.0'


def test_placement_totals_keep_one_row_when_the_detail_splits_by_browser():
    chrome = 'Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36'
    huawei = 'Mozilla/5.0 (Linux; Android 10; LYA-AL00) AppleWebKit/537.36 Chrome/83.0.4103.106 HuaweiBrowser/11.0.3.300 Mobile Safari/537.36'

    def visit(gclid, placement, ua, ip):
        return {'domain': 'tpwrep.vip', 'query': f'gclid={gclid}&placement={placement}', 'ip': ip, 'ua': ua, 'path': '/', 'created': 1, 'referer': ''}

    def click(token, placement, ua):
        return {'domain': 'tpwrep.vip', 'id': token, 'created': 5, 'origin': 'tpwrep.vip', 'body': json.dumps({
            'event': 'whatsapp_click', 'transaction_id': token, 'gclid': token, 'placement': placement,
            'wa_env': 'android_browser', 'link_type': 'intent', 'device': 'mobile', 'ua': ua,
        })}

    visits = [
        visit('a1', 'mobileapp::2-com.aastocks.dzh', chrome, '203.0.113.1'),
        visit('a2', 'mobileapp::2-com.aastocks.dzh', huawei, '203.0.113.2'),
        visit('e1', '133.android.com.etnet.android.iq.adsenseformobileapps.com', chrome, '203.0.113.3'),
        visit('e2', '132.android.com.etnet.android.iq.adsenseformobileapps.com', chrome, '203.0.113.4'),
    ]
    events = [
        click('a1', 'mobileapp::2-com.aastocks.dzh', chrome),
        click('a1b', 'mobileapp::2-com.aastocks.dzh', chrome),
        {'domain': 'tpwrep.vip', 'id': 'open', 'created': 6, 'origin': 'tpwrep.vip', 'body': json.dumps({
            'event': 'wa_app_opened', 'transaction_id': 'a1', 'gclid': 'a1', 'placement': 'mobileapp::2-com.aastocks.dzh',
            'wa_env': 'android_browser', 'link_type': 'intent', 'ua': chrome,
        })},
    ]
    report = build_campaign(visits, events, {})
    assert sum(row['placement_label'] == 'Android App · com.aastocks.dzh' for row in report['rows']) >= 2
    apps = {row['placement']: row for row in report['placements']}
    stock = apps['Android App · com.aastocks.dzh']
    assert stock['visits'] == 2 and stock['clicks'] == 2 and stock['click_times'] == 2 and stock['opens'] == 1
    assert stock['open_rate'] == 0.5
    etnet = apps['Android App · com.etnet.android.iq']
    assert etnet['visits'] == 2 and etnet['clicks'] == 0 and etnet['opens'] == 0 and etnet['open_rate'] is None
    assert [row['placement'] for row in report['placements'][:2]] == ['Android App · com.aastocks.dzh', 'Android App · com.etnet.android.iq']


def test_campaign_rows_split_by_the_app_that_opened_the_page():
    wechat = 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 MicroMessenger/8.0.42'
    huawei = 'Mozilla/5.0 (Linux; Android 10; LYA-AL00) AppleWebKit/537.36 Chrome/83.0.4103.106 HuaweiBrowser/11.0.3.300 Mobile Safari/537.36'
    def event(token, ua, name='whatsapp_click'):
        body = {'event': name, 'transaction_id': token, 'wa_env': 'android_browser', 'link_type': 'universal', 'device': 'mobile', 'placement': 'news.example', 'gclid': token}
        if ua:
            body['ua'] = ua
        return {'domain': 'one.test', 'body': json.dumps(body), 'id': token, 'created': 2, 'origin': 'one.test'}
    report = build_campaign([
        {'domain': 'one.test', 'query': 'gclid=w', 'ip': '1.1.1.1', 'ua': wechat, 'path': '/', 'created': 1, 'referer': ''},
    ], [event('w', wechat), event('w', '', 'wa_app_opened'), event('h', huawei)], {})
    assert {row['opener'] for row in report['rows']} == {'微信', '华为浏览器'}
    wechat_row = next(row for row in report['rows'] if row['opener'] == '微信')
    assert wechat_row['clicks'] == 1 and wechat_row['opens'] == 1 and wechat_row['visits'] == 1
    only = build_campaign([], [event('w', wechat), event('h', huawei)], {'opener': '华为'})
    assert [row['opener'] for row in only['rows']] == ['华为浏览器']
    assert only['totals']['clicks'] == 1
    clipped = 'Mozilla/5.0 (Linux; Android 12; NOH-AN00; wv) AppleWebKit/537.36 Chrome/107.0.5304.141 Mobile Safari/537.36'
    full = clipped + ' MicroMessenger/8.0.42 WeChat/arm64 Weixin'
    rescued = build_campaign([
        {'domain': 'one.test', 'query': 'gclid=w', 'ip': '1.1.1.1', 'ua': full, 'path': '/', 'created': 1, 'referer': ''},
    ], [event('w', clipped)], {})
    assert next(row['opener'] for row in rescued['rows'] if row['clicks']) == '微信'


def test_placement_and_phone_labels():
    assert placement_label('') == '未知'
    assert placement_label('mobileapp::2-com.example.app') == 'Android App · com.example.app'
    assert placement_label('mobileapp::1-12345') == 'iOS App · 12345'
    assert placement_label('132.android.com.etnet.android.iq.adsenseformobileapps.com') == 'Android App · com.etnet.android.iq'
    assert placement_label('7.android.com.etnet.android.iq.adsenseformobileapps.com') == 'Android App · com.etnet.android.iq'
    assert placement_label('3.4.19.iphone.com.etnet.quote.iq-streaming.adsenseformobileapps.com') == 'iPhone App · com.etnet.quote.iq-streaming'
    assert placement_label('3.4.20.iphone.com.etnet.quote.iq-streaming.adsenseformobileapps.com') == 'iPhone App · com.etnet.quote.iq-streaming'
    assert placement_label('Android App · com.etnet.android.iq') == 'Android App · com.etnet.android.iq'
    assert placement_label('news.example') == 'news.example'
    assert youtube_from('https://www.youtube.com/@FinanceDaily')['channel'] == '@FinanceDaily'
    assert youtube_from('https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv')['channel'] == 'UCabcdefghijklmnopqrstuv'
    assert youtube_from('https://www.youtube.com/watch?v=dQw4w9WgXcQ')['video'] == 'dQw4w9WgXcQ'
    assert youtube_from('https://youtu.be/dQw4w9WgXcQ')['video'] == 'dQw4w9WgXcQ'
    assert youtube_from('https://www.youtube.com/shorts/dQw4w9WgXcQ')['video'] == 'dQw4w9WgXcQ'
    assert youtube_from('news.example') is None
    assert placement_label('https://www.youtube.com/@FinanceDaily') == 'YouTube · @FinanceDaily'
    assert device_from_ua('Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) Version/18.7 Mobile/15E148 Safari/604.1') == ('iPhone', 'iOS 18.7')
    assert device_from_ua('Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36') == ('Pixel 7', 'Android 13')


def test_app_versions_merge_and_clicks_count_people():
    iphone = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X)'
    android = 'Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36'
    versions = [
        '3.4.19.iphone.com.etnet.quote.iq-streaming.adsenseformobileapps.com',
        '3.4.20.iphone.com.etnet.quote.iq-streaming.adsenseformobileapps.com',
        '132.android.com.etnet.android.iq.adsenseformobileapps.com',
        'mobileapp::2-com.etnet.android.iq',
    ]
    visits = [
        {'domain': 'one.test', 'query': f'gclid=person&placement={versions[index % 2]}', 'ip': '203.0.113.8', 'ua': iphone, 'path': '/', 'created': index}
        for index in range(5)
    ]
    visits.append({'domain': 'one.test', 'query': f'gclid=android-user&placement={versions[2]}', 'ip': '203.0.113.9', 'ua': android, 'path': '/', 'created': 6})
    events = [
        {'domain': 'one.test', 'id': index, 'created': index, 'origin': 'one.test', 'body': json.dumps({
            'event': 'whatsapp_click', 'transaction_id': f'click-{index}', 'wa_env': 'ios_safari', 'link_type': 'universal',
            'device': 'mobile', 'placement': versions[index % 2], 'gclid': 'person', 'ua': iphone,
        })}
        for index in range(5)
    ]
    events.append({'domain': 'one.test', 'id': 9, 'created': 9, 'origin': 'one.test', 'body': json.dumps({
        'event': 'wa_app_opened', 'transaction_id': 'click-0', 'wa_env': 'ios_safari', 'link_type': 'universal',
        'device': 'mobile', 'placement': versions[0], 'gclid': 'person', 'ua': iphone,
    })})
    report = build_campaign(visits, events, {})
    labels = {row['placement_label'] for row in report['rows']}
    assert 'iPhone App · com.etnet.quote.iq-streaming' in labels
    assert 'Android App · com.etnet.android.iq' in labels
    assert sum(row['placement_label'] == 'iPhone App · com.etnet.quote.iq-streaming' for row in report['rows']) == 1
    iphone_row = next(row for row in report['rows'] if row['placement_label'] == 'iPhone App · com.etnet.quote.iq-streaming' and row['clicks'])
    assert iphone_row['clicks'] == 1 and iphone_row['click_times'] == 5 and iphone_row['opens'] == 1
    assert report['totals']['clicks'] == 1 and report['totals']['click_times'] == 5
    systems = {row['system']: row for row in report['systems']}
    assert systems['iPhone']['clicks'] == 1 and systems['iPhone']['click_times'] == 5 and systems['iPhone']['visits'] == 1
    assert systems['iPhone']['alert'] is False
    assert systems['Android']['visits'] == 1 and systems['Android']['clicks'] == 0 and systems['Android']['alert'] is False
    apps = {row['placement']: row for row in report['placements']}
    assert apps['iPhone App · com.etnet.quote.iq-streaming']['visits'] == 1
    assert apps['iPhone App · com.etnet.quote.iq-streaming']['clicks'] == 1
    assert apps['iPhone App · com.etnet.quote.iq-streaming']['click_times'] == 5
    assert apps['iPhone App · com.etnet.quote.iq-streaming']['opens'] == 1
    assert apps['iPhone App · com.etnet.quote.iq-streaming']['open_rate'] == 1
    assert apps['Android App · com.etnet.android.iq']['visits'] == 1
    assert apps['Android App · com.etnet.android.iq']['clicks'] == 0
    assert apps['Android App · com.etnet.android.iq']['open_rate'] is None

    quiet = [
        {'domain': 'one.test', 'query': f'gclid=v{index}&placement={versions[0]}', 'ip': f'198.51.100.{index % 200}', 'ua': iphone, 'path': '/', 'created': index}
        for index in range(101)
    ]
    flagged = build_campaign(quiet, [], {})
    marked = next(row for row in flagged['systems'] if row['system'] == 'iPhone')
    assert marked['visits'] == 101 and marked['clicks'] == 0 and marked['alert'] is True
    assert '点击 0' in marked['alert_reason']
    under = build_campaign(quiet[:100], [], {})
    assert next(row for row in under['systems'] if row['system'] == 'iPhone')['alert'] is False


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
    android = next(row for row in report['rows'] if row['placement'] == 'Android App · com.foo' and row['clicks'])
    assert android['placement_label'] == 'Android App · com.foo'
    assert android['dest_app'] == 'whatsapp' and android['dest_label'] == 'WhatsApp'
    assert android['clicks'] == 1
    assert android['opens'] == 1
    assert android['visits'] == 1
    assert android['seen_at'] == 3 and android['first_at'] == 1
    assert report['totals']['visits'] == 3
    assert android['model'] == 'iPhone'
    news = next(row for row in report['rows'] if row['placement'] == 'news.example' and row['fallbacks'])
    assert news['fallbacks'] == 1
    assert news['fallback_click'] == 1 and news['copy_number'] == 1 and news['copy_message'] == 1
    assert news['model'] == 'Pixel 7'
    assert news['seen_at'] == 7
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
    assert '点击人数' in csv_text and '点击次数' in csv_text and '去向' in csv_text and 'WhatsApp' in csv_text and 'Android App · com.foo' in csv_text
    assert csv_text.splitlines()[0].lstrip('\ufeff').startswith('时间,')
    assert payload['rows'][0]['seen_at'] > 0
    assert admin.get('/api/campaign?range=custom&start=2026-01-02&end=2026-01-01').status_code == 400
    store.record_client_event('{"event":"whatsapp_click","transaction_id":"blank","wa_env":"ios_safari","device":"mobile"}', 'shop.example')
    exact = admin.get('/api/campaign/events?range=today&domain=shop.example&placement=&placement_exact=true&wa_env=ios_safari')
    assert [item['transaction_id'] for item in exact.json()] == ['blank']
    narrowed = admin.get('/api/campaign?range=today&model=iPhone')
    assert narrowed.json()['totals']['opens'] == 1
    opened, closed = window_bounds('today', '', '', 1_700_000_000)
    assert closed - opened == 86400


def test_campaign_follows_the_selected_domain(tmp_path):
    admin = TestClient(create_admin(tmp_path), base_url='http://127.0.0.1:8765', client=('127.0.0.1', 9))
    admin.headers.update({'Origin': 'http://127.0.0.1:8765', 'X-CSRF-Token': admin.app.state.csrf})
    page = admin.get('/').text
    assert 'id="campaign-domain" name="domain" readonly' in page
    assert 'id="campaign-clear"' in page and '一键清空' in page
    assert '只显示左上角当前管理站点' in page
    assert '可填微信或华为' in page
    registry = admin.app.state.registry
    one = registry.add('one.vip')
    two = registry.add('two.vip')
    click = '{"event":"whatsapp_click","transaction_id":"%s","wa_env":"ios_safari","link_type":"universal","device":"mobile","placement":"news.example"}'
    registry.store(one['id']).record_client_event(click % 'keep-one', 'one.vip')
    registry.store(two['id']).record_client_event(click % 'hide-two', 'two.vip')
    registry.store(one['id']).record_access('Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X)', '', '', '', '', '', 'gclid=g-one&placement=news.example', '', '', 'HK', '', 200, 1, '/', '203.0.113.8')
    registry.store(two['id']).record_access('Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X)', '', '', '', '', '', 'gclid=g-two&placement=news.example', '', '', 'HK', '', 200, 1, '/', '203.0.113.9')
    scoped = admin.get('/api/campaign?range=today&domain=one.vip')
    assert scoped.status_code == 200, scoped.text
    payload = scoped.json()
    assert {row['domain'] for row in payload['rows']} == {'one.vip'}
    assert payload['totals']['clicks'] == 1
    assert payload['totals']['visits'] == 1
    assert 'hide-two' not in scoped.text and 'two.vip' not in scoped.text
    csv_text = admin.get('/api/campaign.csv?range=today&domain=one.vip').text
    assert 'one.vip' in csv_text and 'two.vip' not in csv_text
    raw = admin.get('/api/campaign/events?range=today&domain=one.vip').text
    assert 'keep-one' in raw and 'hide-two' not in raw
    both = admin.get('/api/campaign?range=today').json()
    assert {'one.vip', 'two.vip'} <= {row['domain'] for row in both['rows']}
    with registry.store(one['id']).connect() as db:
        db.execute('INSERT INTO events(created,ip,country,device,slot,reason,path,mode) VALUES(?,?,?,?,?,?,?,?)',
                   (time.time(), '203.0.113.8', 'HK', 'mobile', 'B', 'allowed', '/', 'RULES'))
    assert admin.post('/api/campaign/clear').status_code == 400
    assert admin.post('/api/campaign/clear?domain=missing.vip').status_code == 404
    cleared = admin.post('/api/campaign/clear?domain=one.vip')
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()['domain'] == 'one.vip'
    assert cleared.json()['access'] == 1 and cleared.json()['events'] == 1
    emptied = admin.get('/api/campaign?range=today&domain=one.vip').json()
    assert emptied['totals']['visits'] == 0 and emptied['totals']['clicks'] == 0
    kept = admin.get('/api/campaign?range=today&domain=two.vip').json()
    assert kept['totals']['visits'] == 1 and kept['totals']['clicks'] == 1
    with registry.store(one['id']).connect() as db:
        assert db.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM access_log').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM client_events').fetchone()[0] == 0


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
    flags = served.headers['content-security-policy'].split(';')[0].split()
    assert 'allow-popups' in flags and 'allow-popups-to-escape-sandbox' in flags
    assert 'allow-top-navigation-to-custom-protocols' in flags and 'allow-top-navigation' not in flags
    stored = next((admin.app.state.store.pages / admin.app.state.store.slots()['B']).rglob('*.html')).read_text(encoding='utf-8')
    jump = 'function goWhatsApp(){ location.href = "https://wa.me/" + CONFIG.whatsappNumber; }'
    assert jump in stored and 'eventEndpoint: ""' in stored and 'ab-event-implant' in stored
    assert served.text == stored.replace('eventEndpoint: ""', f'eventEndpoint: "{url}"')
    kept = page.replace('eventEndpoint: ""', 'eventEndpoint: "https://other.example/off"')
    uploaded = admin.post('/api/upload/B?name=page.html', content=kept.encode())
    admin.post('/api/publish/B', json={'version_id': uploaded.json()['version']['id']})
    stored = next((admin.app.state.store.pages / admin.app.state.store.slots()['B']).rglob('*.html')).read_text(encoding='utf-8')
    assert 'eventEndpoint: "https://other.example/off"' in stored
    assert target.get('/').text == stored


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


def _run_destination(script, config, prelude=''):
    program = prelude + """
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
    channel_page = _run_destination(script, {'whatsappNumber': '85200000000'}, '''
const location = { search: "?placement=" + encodeURIComponent("https://www.youtube.com/@FinanceDaily") + "&network=ytv" };
const document = { referrer: "" };
''')
    assert channel_page['urls']['universal'].startswith('https://wa.me/')
    assert channel_page['sent'][0]['name'] == 'whatsapp_click'
    assert channel_page['sent'][0]['extra']['youtube_channel'] == '@FinanceDaily'
    assert channel_page['sent'][0]['extra']['network'] == 'ytv'
    assert 'dest_app' not in channel_page['sent'][0]['extra']
    referred = _run_destination(script, {}, '''
const location = { search: "" };
const document = { referrer: "https://www.youtube.com/watch?v=dQw4w9WgXcQ" };
''')
    assert referred['sent'][0]['name'] == 'whatsapp_click'
    assert referred['sent'][0]['extra']['youtube_video'] == 'dQw4w9WgXcQ'
    assert referred['sent'][0]['extra']['youtube_referrer'].startswith('https://www.youtube.com/watch?v=')
    band_channel = _run_destination(script, {'contactApp': 'band', 'bandUrl': 'https://band.us/n/example'}, '''
const location = { search: "?placement=" + encodeURIComponent("https://www.youtube.com/@FinanceDaily") };
const document = { referrer: "" };
''')
    assert band_channel['urls']['universal'] == 'https://band.us/n/example'
    assert band_channel['sent'][0]['name'] == 'band_click'
    assert band_channel['sent'][1]['name'] == 'band_app_opened'
    assert band_channel['sent'][0]['extra']['dest_app'] == 'band'
    assert band_channel['sent'][0]['extra']['youtube_channel'] == '@FinanceDaily'


def test_youtube_channel_stays_with_the_band_wake():
    video = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
    events = [
        _event('one.test', {'event': 'band_click', 'transaction_id': 'b1', 'wa_env': 'android_chrome', 'link_type': 'universal', 'device': 'mobile', 'placement': video, 'dest_app': 'band'}, 1),
        _event('one.test', {'event': 'band_app_opened', 'transaction_id': 'b1', 'dest_app': 'band'}, 2),
        _event('one.test', {'event': 'kakao_click', 'transaction_id': 'k1', 'wa_env': 'android_chrome', 'link_type': 'universal', 'device': 'mobile', 'placement': 'https://www.youtube.com/@OtherShow', 'dest_app': 'kakao'}, 3),
        _event('one.test', {'event': 'kakao_app_opened', 'transaction_id': 'k1'}, 4),
    ]
    visits = [{
        'domain': 'one.test', 'query': '', 'ip': '203.0.113.8', 'ua': 'Mozilla/5.0',
        'path': '/', 'created': 1, 'referer': 'https://www.youtube.com/@FinanceDaily',
    }]
    report = build_campaign(visits, events, {})
    band = next(row for row in report['rows'] if row['dest_app'] == 'band')
    kakao = next(row for row in report['rows'] if row['dest_app'] == 'kakao')
    assert band['clicks'] == 1 and band['opens'] == 1
    assert band['youtube_video'] == 'dQw4w9WgXcQ' and band['youtube_label'] == ''
    assert band['placement_label'] == 'YouTube · 视频 dQw4w9WgXcQ'
    assert kakao['clicks'] == 1 and kakao['opens'] == 1 and kakao['youtube_channel'] == '@OtherShow'
    fill_youtube_channels(report['rows'], lambda video_id: '财经日报' if video_id == 'dQw4w9WgXcQ' else '')
    assert band['youtube_channel'] == '财经日报' and band['youtube_label'] == '财经日报'
    assert band['placement_label'] == 'YouTube · 财经日报'
    narrowed = build_campaign(visits, events, {'youtube': '@OtherShow', 'dest_app': 'kakao'})
    assert len(narrowed['rows']) == 1 and narrowed['rows'][0]['opens'] == 1
    referred = next(row for row in report['rows'] if row['placement'] == 'https://www.youtube.com/@FinanceDaily')
    assert referred['youtube_channel'] == '@FinanceDaily'
