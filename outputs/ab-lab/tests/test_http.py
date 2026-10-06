import io
import time
import zipfile
import pytest
from fastapi.testclient import TestClient
from ablab.web import create_admin, create_target


@pytest.fixture
def apps(tmp_path):
    admin_app = create_admin(tmp_path)
    target_app = create_target(tmp_path)
    admin = TestClient(admin_app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 50000))
    target = TestClient(target_app, base_url='http://127.0.0.1:8766', client=('127.0.0.1', 50001))
    admin.headers.update({'Origin': 'http://127.0.0.1:8765', 'X-CSRF-Token': admin_app.state.csrf})
    return admin, target


def upload(admin, slot, text):
    response = admin.post(f'/api/upload/{slot}?name=page.html', content=text.encode())
    assert response.status_code == 200, response.text
    version = response.json()['version']['id']
    assert admin.post(f'/api/publish/{slot}', json={'version_id': version}).status_code == 200
    return version


def configure(admin, **changes):
    state = admin.get('/api/state').json()
    state['config'].update(changes)
    response = admin.put('/api/config', json={'config': state['config'], 'revision': state['revision']})
    assert response.status_code == 200, response.text


def test_upload_publish_modes_and_restart_persist(apps, tmp_path):
    admin, target = apps
    upload(admin, 'A', '<h1>Alpha</h1>')
    upload(admin, 'B', '<h1>Beta</h1>')
    assert 'Beta' in target.get('/').text
    configure(admin, routing='FORCE_A')
    response = target.get('/')
    assert 'Alpha' in response.text
    assert response.headers['cache-control'] == 'no-store'
    assert 'sandbox allow-scripts allow-forms allow-same-origin' in response.headers['content-security-policy']
    restarted = TestClient(create_target(tmp_path), base_url='http://127.0.0.1:8766', client=('127.0.0.1', 55555))
    assert 'Alpha' in restarted.get('/').text


def test_switching_a_version_does_not_create_another_copy(apps):
    admin, _target = apps
    older = upload(admin, 'B', '<h1>Older</h1>')
    upload(admin, 'B', '<h1>Newer</h1>')
    before = [item['id'] for item in admin.get('/api/state').json()['versions'] if item['slot'] == 'B']
    assert admin.post('/api/publish/B', json={'version_id': older}).status_code == 200
    after = [item['id'] for item in admin.get('/api/state').json()['versions'] if item['slot'] == 'B']
    assert after == before
    assert admin.get('/api/state').json()['slots']['B'] == older


def test_b_version_delete_and_cleanup_keep_the_published_page(apps, tmp_path):
    admin, target = apps
    live = upload(admin, 'B', '<h1>Live</h1>')
    extra = admin.post('/api/upload/B?name=old.html', content=b'<h1>Old</h1>')
    assert extra.status_code == 200, extra.text
    extra_id = extra.json()['version']['id']
    alpha = upload(admin, 'A', '<h1>Alpha</h1>')
    refused = admin.delete(f'/api/versions/B/{live}')
    assert refused.status_code == 400
    assert '当前发布' in refused.json()['detail']
    assert admin.delete(f'/api/versions/B/{alpha}').status_code == 400
    assert admin.delete(f'/api/versions/B/{extra_id}').status_code == 200
    assert not (tmp_path / 'pages' / extra_id).exists()
    admin.post('/api/upload/B?name=old-2.html', content=b'<h1>Old 2</h1>')
    admin.post('/api/upload/B?name=old-3.html', content=b'<h1>Old 3</h1>')
    cleaned = admin.post('/api/versions/B/cleanup')
    assert cleaned.status_code == 200, cleaned.text
    assert cleaned.json() == {'deleted': 2}
    state = admin.get('/api/state').json()
    assert [item['id'] for item in state['versions'] if item['slot'] == 'B'] == [live]
    assert any(item['id'] == alpha for item in state['versions'] if item['slot'] == 'A')
    assert 'Live' in target.get('/').text
    assert any(item['action'] == 'b_version_deleted' for item in admin.get('/api/audit').json()['items'])


def test_deleting_a_b_version_uses_the_selected_domain(apps, tmp_path):
    admin, _target = apps
    other = admin.post('/api/sites', json={'domain': 'shop.example'}).json()['site']['id']
    uploaded = admin.post(f'/api/sites/{other}/upload/B?name=old.html', content=b'<h1>Old</h1>')
    assert uploaded.status_code == 200, uploaded.text
    version_id = uploaded.json()['version']['id']
    missed = admin.delete(f'/api/versions/B/{version_id}')
    assert missed.status_code == 400
    assert '只能删除未发布的 B 版本' in missed.json()['detail']
    assert (tmp_path / 'sites' / other / 'pages' / version_id).is_dir()
    removed = admin.delete(f'/api/sites/{other}/versions/B/{version_id}')
    assert removed.status_code == 200, removed.text
    state = admin.get(f'/api/sites/{other}/state').json()
    assert all(item['id'] != version_id for item in state['versions'])
    assert not (tmp_path / 'sites' / other / 'pages' / version_id).exists()
    kept = admin.post('/api/sites/default/upload/B?name=keep.html', content=b'<h1>Keep</h1>')
    assert kept.status_code == 200
    assert any(item['id'] == kept.json()['version']['id'] for item in admin.get('/api/state').json()['versions'])


def test_b_split_rewrites_the_page_before_it_is_sent(apps):
    admin, target = apps
    upload(admin, 'B', '<a href="https://old.example/landing">go</a>')
    preset = admin.post('/api/b-redirects/presets', json={'urls': ['https://split.example/landing'], 'note': ''})
    assert preset.status_code == 200, preset.text
    saved = admin.put('/api/b-redirects/split', json={'enabled': True, 'mode': 'random', 'members': [{'preset_id': preset.json()['presets'][0]['id'], 'weight': 1}]})
    assert saved.status_code == 200, saved.text
    first = target.get('/')
    assert first.status_code == 200
    assert 'https://split.example/landing' in first.text
    assert 'old.example' not in first.text
    assert first.cookies.get('ab_split')
    assert target.get('/').text == first.text
    admin.put('/api/b-redirects/split', json={'enabled': False, 'mode': 'random', 'members': [{'preset_id': preset.json()['presets'][0]['id'], 'weight': 1}]})
    assert 'old.example' in target.get('/').text
    configure(admin, routing='FORCE_A')
    upload(admin, 'A', '<a href="https://old.example/landing">go</a>')
    admin.put('/api/b-redirects/split', json={'enabled': True, 'mode': 'weighted', 'members': [{'preset_id': preset.json()['presets'][0]['id'], 'weight': 80}]})
    assert 'old.example' in target.get('/').text
    assert 'split.example' not in target.get('/').text


def test_clearing_logs_removes_only_the_selected_domain(tmp_path):
    app = create_admin(tmp_path)
    admin = TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 50000))
    admin.headers.update({'Origin': 'http://127.0.0.1:8765', 'X-CSRF-Token': app.state.csrf})
    other = admin.post('/api/sites', json={'domain': 'other.example.com'}).json()['site']['id']
    decision = {'device': 'mobile', 'slot': 'B', 'reason': 'allowed'}
    app.state.registry.store('default').event('203.0.113.10', 'US', decision, '/', 'PAGE')
    app.state.registry.store(other).event('203.0.113.20', 'HK', decision, '/', 'PAGE')
    assert admin.get('/api/sites/default/logs').json()['total'] == 1
    assert admin.get(f'/api/sites/{other}/logs').json()['total'] == 1
    cleared = admin.post(f'/api/sites/{other}/logs/clear', json={})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()['deleted'] == 1
    assert admin.get(f'/api/sites/{other}/logs').json()['total'] == 0
    assert admin.get('/api/sites/default/logs').json()['total'] == 1


def test_clearing_other_countries_keeps_displayed_hong_kong(tmp_path):
    app = create_admin(tmp_path)
    admin = TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 50000))
    admin.headers.update({'Origin': 'http://127.0.0.1:8765', 'X-CSRF-Token': app.state.csrf})
    other = admin.post('/api/sites', json={'domain': 'other.example.com'}).json()['site']['id']
    decision = {'device': 'mobile', 'slot': 'B', 'reason': 'allowed'}
    current = app.state.registry.store('default')
    with current.connect() as db:
        db.execute('INSERT INTO events(created,ip,country,device,slot,reason,path,mode) VALUES(?,?,?,?,?,?,?,?)', (time.time(), '172.64.215.0/24', 'JP', 'mobile', 'B', 'allowed', '/old-edge', 'PAGE'))
        db.execute('INSERT INTO events(created,ip,country,device,slot,reason,path,mode) VALUES(?,?,?,?,?,?,?,?)', (time.time(), '2400:cb00::/48', 'JP', 'mobile', 'B', 'allowed', '/old-edge6', 'PAGE'))
    current.event('1.32.192.8', 'HK', decision, '/hk', 'PAGE')
    current.event('104.16.1.2', 'hk', decision, '/hk-lower', 'PAGE')
    current.event('172.64.215.1', 'JP', decision, '/exact-edge', 'PAGE')
    current.event('203.0.113.9', 'JP', decision, '/jp', 'PAGE')
    current.event('8.8.8.8', 'US', decision, '/us', 'PAGE')
    current.event('203.0.113.50', None, decision, '/none', 'PAGE')
    elsewhere = app.state.registry.store(other)
    elsewhere.event('8.8.4.4', 'US', decision, '/other-us', 'PAGE')
    elsewhere.event('1.1.1.1', 'HK', decision, '/other-hk', 'PAGE')
    cleared = admin.post('/api/sites/default/logs/clear-foreign', json={})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json() == {'deleted': 4, 'kept': 4}
    shown = {row['path']: row['country'] for row in admin.get('/api/sites/default/logs').json()['items']}
    assert shown == {'/hk': 'HK', '/hk-lower': 'hk', '/old-edge': 'HK', '/old-edge6': 'HK'}
    stored = {row['path']: (row['country'], row['ip']) for row in current.logs()['items']}
    assert stored['/old-edge'] == ('JP', '172.64.215.0/24')
    assert stored['/old-edge6'] == ('JP', '2400:cb00::/48')
    assert stored['/hk'] == ('HK', '1.32.192.8')
    assert {row['path'] for row in elsewhere.logs()['items']} == {'/other-us', '/other-hk'}
    again = admin.post('/api/sites/default/logs/clear-foreign', json={})
    assert again.json() == {'deleted': 0, 'kept': 4}
    assert current.logs()['total'] == 4


def test_recent_count_is_only_the_last_minute_of_one_domain(tmp_path):
    app = create_admin(tmp_path)
    admin = TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 50000))
    admin.headers.update({'Origin': 'http://127.0.0.1:8765', 'X-CSRF-Token': app.state.csrf})
    other = admin.post('/api/sites', json={'domain': 'other.example.com'}).json()['site']['id']
    decision = {'device': 'mobile', 'slot': 'B', 'reason': 'allowed'}
    current = app.state.registry.store('default')
    current.event('203.0.113.10', 'US', decision, '/', 'PAGE')
    current.event('203.0.113.11', 'US', decision, '/', 'PAGE')
    app.state.registry.store(other).event('203.0.113.20', 'HK', decision, '/', 'PAGE')
    with current.connect() as db:
        db.execute('UPDATE events SET created=? WHERE id=(SELECT MIN(id) FROM events)', (time.time() - 120,))
    assert current.recent_count(60) == 1
    assert admin.get('/api/logs/rate').json() == {'count': 1, 'seconds': 60}
    assert admin.get(f'/api/sites/{other}/logs/rate').json()['count'] == 1


def test_missing_selected_version_never_falls_back(apps):
    admin, target = apps
    upload(admin, 'A', 'Alpha')
    assert target.get('/').status_code == 503


def test_allowed_switch_persists_without_republishing_or_disabling_filters(apps, tmp_path):
    admin, target = apps
    upload(admin, 'A', 'Alpha')
    upload(admin, 'B', 'Beta')
    configure(admin, rules={'block_pc': True})
    original_slots = admin.get('/api/state').json()['slots']
    mobile = {'User-Agent': 'Mozilla/5.0 iPhone Mobile'}
    desktop = {'User-Agent': 'Mozilla/5.0 Windows NT 10.0'}
    for slot, content in [('A', 'Alpha'), ('B', 'Beta'), ('A', 'Alpha')]:
        configure(admin, allowed_slot=slot)
        assert content in target.get('/', headers=mobile).text
        assert 'Alpha' in target.get('/', headers=desktop).text
        state = admin.get('/api/state').json()
        assert state['config']['rules']['block_pc'] is True
        assert state['slots'] == original_slots
        logs = admin.get('/api/logs').json()['items']
        assert (logs[0]['slot'], logs[0]['reason']) == ('A', 'device')
        assert (logs[1]['slot'], logs[1]['reason']) == (slot, 'allowed')
    restarted = TestClient(create_target(tmp_path), base_url='http://127.0.0.1:8766', client=('127.0.0.1', 5))
    assert 'Alpha' in restarted.get('/', headers=mobile).text


def test_legacy_config_defaults_to_b_and_allowed_slot_applies_to_links(apps):
    admin, target = apps
    state = admin.get('/api/state').json()
    legacy = dict(state['config'])
    legacy.pop('allowed_slot', None)
    response = admin.put('/api/config', json={'config': legacy, 'revision': state['revision']})
    assert response.json()['config']['allowed_slot'] == 'B'
    for slot in ('A', 'B'):
        admin.post('/api/links', json={'slot': slot, 'urls': [f'https://example.com/{slot}']})
    configure(admin, content_mode='LINK', allowed_slot='A')
    assert target.get('/', follow_redirects=False).headers['location'] == 'https://example.com/A'


def test_no_admin_routes_on_target_and_cross_origin_mutations_blocked(apps, tmp_path):
    admin, target = apps
    assert target.get('/api/state').status_code == 404
    assert admin.post('/api/counters/reset', headers={'Origin': 'http://127.0.0.1:8766'}, json={}).status_code == 403
    assert admin.post('/api/counters/reset', headers={'X-CSRF-Token': 'wrong'}, json={}).status_code == 403
    assert admin.get('/api/state', headers={'Host': 'evil.example:8765'}).status_code == 403
    assert admin.get('/api/state', headers={'Origin': 'null'}).status_code == 403
    remote = TestClient(create_admin(tmp_path), base_url='http://127.0.0.1:8765', client=('203.0.113.9', 11111))
    assert remote.get('/').status_code == 403


def test_counts_do_not_include_head_assets_preview_or_simulation(apps):
    admin, target = apps
    upload(admin, 'A', 'Alpha')
    package = io.BytesIO()
    with zipfile.ZipFile(package, 'w') as z:
        z.writestr('index.html', '<link rel="stylesheet" href="style.css">Beta')
        z.writestr('style.css', 'body{color:blue}')
    version = admin.post('/api/upload/B?name=b.zip', content=package.getvalue()).json()['version']['id']
    admin.post('/api/publish/B', json={'version_id': version})
    configure(admin, rules={'max_visits': 1})
    assert target.head('/').status_code == 200
    preview = admin.post('/api/preview', json={'version_id': version}).json()['url']
    assert 'Beta' in target.get(preview).text
    assert target.get(preview.replace('index.html', 'style.css')).status_code == 200
    assert admin.post('/api/simulate', json={'ip': '127.0.0.1', 'visits': 2}).json()['slot'] == 'A'
    assert admin.get('/api/state').json()['stats']['total'] == 0
    assert 'Beta' in target.get('/').text
    assert 'blue' in target.get('/style.css').text
    assert 'Alpha' in target.get('/').text
    assert admin.get('/api/state').json()['stats']['total'] == 2
    admin.post('/api/counters/reset', json={})
    assert 'Beta' in target.get('/').text


def test_forged_ip_country_and_config_conflicts(apps):
    admin, target = apps
    upload(admin, 'A', 'Alpha')
    upload(admin, 'B', 'Beta')
    configure(admin, rules={'blacklist': ['127.0.0.1'], 'whitelist': ['203.0.113.1']})
    assert 'Alpha' in target.get('/', headers={'X-Forwarded-For': '203.0.113.1', 'CF-IPCountry': 'US'}).text
    state = admin.get('/api/state').json()
    assert admin.put('/api/config', json={'config': state['config'], 'revision': state['revision'] - 1}).status_code == 409


def test_links_redirect_and_unsafe_url_rejected(apps):
    admin, target = apps
    assert admin.post('/api/links', json={'slot': 'B', 'urls': ['javascript:alert(1)']}).status_code == 422
    assert admin.post('/api/links', json={'slot': 'B', 'urls': ['https://example.com/one', 'https://example.com/two']}).status_code == 200
    configure(admin, content_mode='LINK')
    assert target.get('/', follow_redirects=False).headers['location'] == 'https://example.com/one'
    assert target.get('/', follow_redirects=False).headers['location'] == 'https://example.com/two'


def test_rollback_preview_tamper_and_private_paths(apps):
    admin, target = apps
    first = upload(admin, 'B', 'original')
    upload(admin, 'B', 'updated')
    admin.post('/api/publish/B', json={'version_id': first})
    assert 'original' in target.get('/').text
    preview = admin.post('/api/preview', json={'version_id': first}).json()['url']
    assert target.get(preview.replace(first, '0'*32)).status_code == 404
    assert target.get('/%252e%252e/app.db').status_code == 404
    assert target.get('/data/app.db').status_code == 404


def test_logs_do_not_store_query_secrets(apps):
    admin, target = apps
    upload(admin, 'B', 'Beta')
    target.get('/?password=not-for-log')
    logs = admin.get('/api/logs').json()
    assert logs['items'][0]['path'] == '/'
    assert logs['items'][0]['ip'] == '127.0.0.1'
    assert 'not-for-log' not in admin.get('/api/logs.csv').text


def test_invalid_upload_leaves_current_version_unchanged(apps):
    admin, target = apps
    upload(admin, 'B', 'Beta')
    assert admin.post('/api/upload/B?name=bad.zip', content=b'notzip').status_code == 400
    assert 'Beta' in target.get('/').text


def test_directory_redirect_counts_once_and_preserves_relative_assets(apps):
    admin, target = apps
    for slot in ('A', 'B'):
        package = io.BytesIO()
        with zipfile.ZipFile(package, 'w') as z:
            z.writestr('index.html', slot)
            z.writestr('offer/index.html', f'<script src="app.js"></script>{slot} offer')
            z.writestr('offer/app.js', f'console.log("{slot}")')
        version = admin.post(f'/api/upload/{slot}?name=site.zip', content=package.getvalue()).json()['version']['id']
        admin.post(f'/api/publish/{slot}', json={'version_id': version})
    configure(admin, rules={'max_visits': 1})
    first = target.get('/offer', follow_redirects=False)
    assert first.status_code == 307
    assert first.headers['location'] == '/offer/'
    assert admin.get('/api/state').json()['stats']['total'] == 0
    assert 'B offer' in target.get('/offer/', follow_redirects=False).text
    assert 'console.log("B")' in target.get('/offer/app.js').text
    assert 'A offer' in target.get('/offer', follow_redirects=True).text
    assert admin.get('/api/state').json()['stats']['total'] == 2
    preview = admin.post('/api/preview', json={'version_id': version}).json()['url'].replace('index.html', 'offer')
    response = target.get(preview, follow_redirects=False)
    assert response.status_code == 307
    assert '/_preview/' in response.headers['location']
    assert response.headers['location'].endswith('/offer/')
    assert 'B offer' in target.get(preview, follow_redirects=True).text
    assert admin.get('/api/state').json()['stats']['total'] == 2
