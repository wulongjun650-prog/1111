import io
import json
import time
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from ablab.cloudflare import Cloudflare, CloudflareError
from ablab.provisioning import inspect_pending
from ablab.sites import Registry
from ablab.web import create_admin


TOKEN = 'cf-token-secret'
TEMPLATE = 'a' * 32
ACCOUNT = 'b' * 32
ZONE = 'c' * 32
RECORD = 'd' * 32
ORIGIN = '8.8.8.8'
SERVERS = ['ada.ns.cloudflare.com', 'bob.ns.cloudflare.com']


def envelope(result, status=200):
    return httpx.Response(status, json={'success': True, 'errors': [], 'result': result})


def failure(code, message, status=400):
    return httpx.Response(status, json={'success': False, 'errors': [{'code': code, 'message': message}], 'result': None})


def zone(name, zone_id=ZONE, status='pending'):
    return {'id': zone_id, 'name': name, 'status': status, 'account': {'id': ACCOUNT}, 'name_servers': SERVERS}


class Api:
    def __init__(self, zones=None, records=None, polish='skip'):
        self.zones = {'rules.example.com': [zone('rules.example.com', TEMPLATE, 'active')]}
        if zones:
            self.zones.update(zones)
        self.records = records or {}
        self.polish = polish
        self.ssl = 'flexible'
        self.calls = []

    def transport(self):
        return httpx.MockTransport(self.respond)

    def respond(self, request):
        assert request.headers['authorization'] == 'Bearer ' + TOKEN
        assert TOKEN not in str(request.url)
        path = request.url.path
        self.calls.append((request.method, path))
        if request.method == 'GET' and path == '/client/v4/zones':
            return envelope(self.zones.get(request.url.params['name'], []))
        if request.method == 'POST' and path == '/client/v4/zones':
            body = json.loads(request.content)
            assert body['jump_start'] is False and body['account'] == {'id': ACCOUNT}
            created = zone(body['name'])
            self.zones[body['name']] = [created]
            return envelope(created)
        name = path.removeprefix('/client/v4/zones/')
        if request.method == 'GET' and name == TEMPLATE:
            return envelope(zone('rules.example.com', TEMPLATE, 'active'))
        if request.method == 'GET' and '/' not in name:
            found = next((item for rows in self.zones.values() for item in rows if item['id'] == name), None)
            if found is None:
                raise AssertionError(f'missing zone {name}')
            return envelope(found)
        zone_id, _, rest = name.partition('/')
        if rest == 'dns_records' and request.method == 'GET':
            return envelope(self.records.get(zone_id, []))
        if rest == 'dns_records' and request.method == 'POST':
            body = json.loads(request.content)
            assert body == {'type': 'A', 'name': body['name'], 'content': ORIGIN, 'proxied': True, 'ttl': 1}
            saved = body | {'id': RECORD}
            self.records[zone_id] = [saved]
            return envelope(saved)
        if rest.startswith('dns_records/') and request.method == 'PUT':
            body = json.loads(request.content)
            assert body['type'] == 'A' and body['ttl'] == 1 and isinstance(body.get('proxied'), bool)
            record_id = rest.removeprefix('dns_records/')
            current = next((item for item in self.records.get(zone_id, []) if item.get('id') == record_id), {})
            if body['proxied'] is True:
                assert body['content'] == ORIGIN
            else:
                assert body['content'] == current.get('content')
            saved = current | body | {'id': record_id}
            self.records[zone_id] = [saved]
            return envelope(saved)
        if rest == 'settings/ssl' and request.method == 'GET':
            return envelope({'id': 'ssl', 'value': self.ssl})
        if rest == 'settings/ssl' and request.method == 'PATCH':
            assert json.loads(request.content) == {'value': 'strict'}
            self.ssl = 'strict'
            return envelope({'id': 'ssl', 'value': 'strict'})
        if rest == 'settings' and request.method == 'GET':
            return envelope([
                {'id': 'brotli', 'value': 'on', 'editable': True},
                {'id': 'minify', 'value': {'css': 'on', 'html': 'off', 'js': 'on'}, 'editable': True},
                {'id': 'polish', 'value': 'lossless', 'editable': True},
                {'id': 'development_mode', 'value': 'on', 'editable': True},
                {'id': 'ssl', 'value': 'full', 'editable': True},
                {'id': 'rocket_loader', 'value': 'off', 'editable': False},
            ])
        if rest.startswith('settings/') and request.method == 'PATCH':
            setting = rest.removeprefix('settings/')
            assert setting in ('brotli', 'minify', 'polish')
            if setting == 'polish' and self.polish == 'skip':
                return failure(9000, 'plan does not include polish')
            return envelope({'id': setting, 'value': json.loads(request.content)['value']})
        if rest == 'rulesets/phases/http_request_cache_settings/entrypoint' and request.method == 'GET':
            return envelope({'rules': [{
                'id': 'e' * 32, 'expression': 'http.host eq "rules.example.com"', 'action': 'set_cache_settings',
                'action_parameters': {'cache': True}, 'description': 'cache host', 'enabled': True,
            }]})
        if rest == 'rulesets/phases/http_config_settings/entrypoint' and request.method == 'GET':
            return failure(10003, 'could not find entrypoint ruleset', 404)
        if rest == 'rulesets/phases/http_request_cache_settings/entrypoint' and request.method == 'PUT':
            body = json.loads(request.content)
            rule = body['rules'][0]
            assert set(body) == {'rules'} and 'id' not in rule
            assert rule['action'] == 'set_cache_settings' and 'rules.example.com' not in rule['expression']
            self.last_rule = rule
            return envelope(body)
        if rest == 'purge_cache' and request.method == 'POST':
            if getattr(self, 'purge_error', False):
                return failure(9000, 'cache is busy')
            assert json.loads(request.content) == {'purge_everything': True}
            return envelope({'id': zone_id})
        raise AssertionError(f'unexpected {request.method} {path}')


def client(cloudflare):
    return Cloudflare(TOKEN, 'rules.example.com', ORIGIN, cloudflare.transport())


def test_attach_copies_rules_and_proxies_to_origin():
    api = Api()
    attached = client(api).attach('NEW.Example.com')
    assert attached['zone_id'] == ZONE
    assert attached['nameservers'] == SERVERS
    assert attached['status'] == 'pending'
    assert 'ada.ns.cloudflare.com' in attached['detail']
    assert '1 项因套餐没套上' in attached['detail']
    assert api.records[ZONE][0]['proxied'] is True
    assert api.last_rule['expression'] == 'http.host eq "new.example.com"'
    assert api.ssl == 'strict'
    assert any(method == 'PATCH' and path.endswith('/settings/ssl') for method, path in api.calls)
    assert not any(path.endswith('/settings/development_mode') for _, path in api.calls)


def test_existing_grey_record_is_turned_orange_without_a_new_zone():
    api = Api(zones={'shop.example.com': [zone('shop.example.com', status='active')]}, records={
        ZONE: [{'id': RECORD, 'type': 'A', 'name': 'shop.example.com', 'content': '198.51.100.8', 'proxied': False}],
    })
    attached = client(api).attach('shop.example.com')
    assert attached['status'] == 'active'
    assert ('POST', '/client/v4/zones') not in api.calls
    assert any(method == 'PUT' and path.endswith('/dns_records/' + RECORD) for method, path in api.calls)
    assert api.records[ZONE][0]['content'] == ORIGIN


def test_matching_record_is_left_alone_and_errors_hide_the_token():
    api = Api(zones={'shop.example.com': [zone('shop.example.com')]}, records={
        ZONE: [{'id': RECORD, 'type': 'A', 'name': 'shop.example.com', 'content': ORIGIN, 'proxied': True}],
    })
    client(api).attach('shop.example.com')
    assert not any(method in ('POST', 'PUT') and 'dns_records' in path for method, path in api.calls)

    def explode(request):
        raise httpx.ConnectError(TOKEN, request=request)
    with pytest.raises(CloudflareError) as error:
        Cloudflare(TOKEN, 'rules.example.com', ORIGIN, httpx.MockTransport(explode)).attach('shop.example.com')
    assert TOKEN not in str(error.value)


def test_duplicate_zone_is_reused():
    api = Api()
    original = api.respond

    def respond(request):
        if request.method == 'POST' and request.url.path == '/client/v4/zones':
            api.zones['new.example.com'] = [zone('new.example.com')]
            return failure(1061, 'zone already exists ' + TOKEN)
        return original(request)
    api.respond = respond
    attached = client(api).attach('new.example.com')
    assert attached['zone_id'] == ZONE
    assert TOKEN not in attached['detail']


def test_cloudflare_domain_skips_public_a_check(tmp_path):
    registry = Registry(tmp_path)
    site = registry.add('new.example.com')
    registry.set_cloudflare(site['id'], ZONE, ','.join(SERVERS), 'pending', '等待 NS')
    api = Api(zones={'new.example.com': [zone('new.example.com', status='active')]}, records={
        ZONE: [{'id': RECORD, 'type': 'A', 'name': 'new.example.com', 'content': ORIGIN, 'proxied': True}],
    })

    class Spy:
        def check(self, domain, expected):
            raise AssertionError(domain)

    inspect_pending(registry, ORIGIN, Spy(), client(api))
    saved = registry.get(site['id'])
    assert saved['stage'] == 'unsupported'
    assert saved['cf_status'] == 'active'
    inspect_pending(registry, ORIGIN, Spy(), None)
    registry.set_cloudflare(site['id'], ZONE, ','.join(SERVERS), 'pending', '等待 NS')
    registry.control(site['id'], 'retry')
    inspect_pending(registry, ORIGIN, Spy(), None)
    assert '没有 Cloudflare 配置' in registry.get(site['id'])['error']


def test_pending_zone_stays_on_a_one_minute_check(tmp_path):
    registry = Registry(tmp_path)
    site = registry.add('new.example.com')
    registry.set_cloudflare(site['id'], ZONE, ','.join(SERVERS), 'pending', '等待 NS')
    api = Api(zones={'new.example.com': [zone('new.example.com', status='pending')]}, records={
        ZONE: [{'id': RECORD, 'type': 'A', 'name': 'new.example.com', 'content': ORIGIN, 'proxied': True}],
    })
    inspect_pending(registry, ORIGIN, cloudflare=client(api))
    saved = registry.get(site['id'])
    assert saved['stage'] == 'waiting_dns'
    assert saved['cf_status'] == 'pending'
    assert 0 < saved['next_attempt'] - time.time() <= 60


def session(tmp_path, cloudflare=None, server_ip=ORIGIN):
    app = create_admin(tmp_path, server_ip=server_ip, cloudflare=cloudflare)
    http = TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 10000))
    http.headers.update({'origin': 'http://127.0.0.1:8765', 'x-csrf-token': app.state.csrf})
    return http


def test_unchecked_add_keeps_the_old_path_and_checked_is_optional(tmp_path):
    api = Api()
    http = session(tmp_path, client(api))
    catalog = http.get('/api/sites').json()
    assert catalog['cloudflare_configured'] is True
    assert catalog['cloudflare_template'] == 'rules.example.com'
    plain = http.post('/api/sites', json={'domain': 'plain.example.com'})
    assert plain.status_code == 200
    assert plain.json()['cloudflare'] is None
    assert plain.json()['site']['cf_status'] == ''
    assert api.calls == []
    added = http.post('/api/sites', json={'domain': 'new.example.com', 'cloudflare': True})
    assert added.status_code == 200, added.text
    body = added.json()
    assert body['cloudflare']['ok'] is True
    assert body['site']['cf_nameservers'] == ','.join(SERVERS)
    assert body['site']['stage'] == 'waiting_dns'
    refused = http.post('/api/sites', json={'domain': 'bad.example.com', 'cloudflare': 'yes'})
    assert refused.status_code == 400
    assert http.post('/api/sites/default/cloudflare', json={}).status_code == 400
    purged = http.post(f"/api/sites/{body['site']['id']}/cloudflare/purge", json={})
    assert purged.status_code == 200
    assert any(path.endswith('/purge_cache') for _, path in api.calls)


def test_saving_the_visible_slot_purges_that_domains_cache(tmp_path):
    api = Api()
    http = session(tmp_path, client(api))
    added = http.post('/api/sites', json={'domain': 'new.example.com', 'cloudflare': True})
    assert added.status_code == 200, added.text
    site_id = added.json()['site']['id']
    before = [path for _, path in api.calls if path.endswith('/purge_cache')]
    current = http.get(f'/api/sites/{site_id}/state').json()
    current['config']['routing'] = 'RULES'
    current['config']['allowed_slot'] = 'A'
    saved = http.put(f'/api/sites/{site_id}/config', json={'config': current['config'], 'revision': current['revision']})
    assert saved.status_code == 200, saved.text
    assert saved.json()['config']['allowed_slot'] == 'A'
    assert saved.json()['cloudflare'] == {'ok': True}
    after = [path for _, path in api.calls if path.endswith('/purge_cache')]
    assert len(after) == len(before) + 1
    published = http.post(f'/api/sites/{site_id}/upload/A?name=page.html', content=b'<h1>Alpha</h1>')
    assert published.status_code == 200, published.text
    version_id = published.json()['version']['id']
    went_live = http.post(f'/api/sites/{site_id}/publish/A', json={'version_id': version_id})
    assert went_live.status_code == 200, went_live.text
    assert went_live.json()['cloudflare'] == {'ok': True}
    finished = [path for _, path in api.calls if path.endswith('/purge_cache')]
    assert len(finished) == len(after) + 1


def test_open_page_checks_pending_nameservers_until_the_zone_is_active(tmp_path):
    api = Api(zones={'new.example.com': [zone('new.example.com', status='pending')]}, records={
        ZONE: [{'id': RECORD, 'type': 'A', 'name': 'new.example.com', 'content': ORIGIN, 'proxied': True}],
    })
    http = session(tmp_path, client(api))
    added = http.post('/api/sites', json={'domain': 'new.example.com', 'cloudflare': True})
    site_id = added.json()['site']['id']
    waiting = http.post(f'/api/sites/{site_id}/cloudflare/status', json={})
    assert waiting.status_code == 200, waiting.text
    assert waiting.json()['cloudflare']['status'] == 'pending'
    assert waiting.json()['site']['cf_status'] == 'pending'
    assert '正在自动检查' in waiting.json()['cloudflare']['detail']
    api.zones['new.example.com'][0]['status'] = 'active'
    ready = http.post(f'/api/sites/{site_id}/cloudflare/status', json={})
    assert ready.status_code == 200, ready.text
    assert ready.json()['cloudflare'] == {
        'ok': True, 'status': 'active', 'active': True, 'nameservers': SERVERS,
        'detail': 'Cloudflare 已生效，回源指向服务器',
    }
    assert ready.json()['site']['cf_status'] == 'active'
    assert ready.json()['site']['stage'] == 'unsupported'
    plain = http.post('/api/sites', json={'domain': 'other.example.com'})
    assert plain.status_code == 200
    missing = http.post(f"/api/sites/{plain.json()['site']['id']}/cloudflare/status", json={})
    assert missing.status_code == 400


def test_status_check_keeps_pending_when_cloudflare_cannot_confirm(tmp_path):
    api = Api(zones={'new.example.com': [zone('new.example.com', status='pending')]}, records={
        ZONE: [{'id': RECORD, 'type': 'A', 'name': 'new.example.com', 'content': ORIGIN, 'proxied': True}],
    })
    http = session(tmp_path, client(api))
    added = http.post('/api/sites', json={'domain': 'new.example.com', 'cloudflare': True})
    site_id = added.json()['site']['id']
    api.records[ZONE][0]['content'] = '198.51.100.9'
    checked = http.post(f'/api/sites/{site_id}/cloudflare/status', json={})
    assert checked.status_code == 200, checked.text
    body = checked.json()
    assert body['cloudflare']['ok'] is False
    assert body['site']['cf_status'] == 'pending'
    assert TOKEN not in checked.text


def test_swapping_a_whatsapp_number_purges_that_domains_cache(tmp_path):
    api = Api(zones={'shop.example.com': [zone('shop.example.com', status='active')]}, records={
        ZONE: [{'id': RECORD, 'type': 'A', 'name': 'shop.example.com', 'content': ORIGIN, 'proxied': True}],
    })
    http = session(tmp_path, client(api))
    added = http.post('/api/sites', json={'domain': 'shop.example.com', 'cloudflare': True})
    assert added.status_code == 200, added.text
    site_id = added.json()['site']['id']
    prefix = f'/api/sites/{site_id}'
    archive = io.BytesIO()
    page = """<!doctype html><script>
const CONFIG = { whatsappNumber: '85264150954' };
function go(){ location.href = 'https://wa.me/' + String(CONFIG.whatsappNumber).replace(/\\D/g,''); }
</script>"""
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr('index.html', page)
    uploaded = http.post(prefix + '/upload/B?name=wa.zip', content=archive.getvalue())
    assert uploaded.status_code == 200, uploaded.text
    version = uploaded.json()['version']['id']
    assert http.post(prefix + '/publish/B', json={'version_id': version}).status_code == 200
    scan = http.get(prefix + '/b-redirects').json()
    numbers = [item for item in scan['occurrences'] if item['kind'] == 'whatsapp_number']
    assert [item['url'] for item in numbers] == ['85264150954']
    pool = http.post(prefix + '/b-redirects/numbers', json={'phones': ['85211112222'], 'note': ''})
    assert pool.status_code == 200, pool.text
    before = [path for _, path in api.calls if path.endswith('/purge_cache')]
    swapped = http.post(prefix + '/b-redirects/numbers/apply', json={
        'version_id': scan['version']['id'], 'number_id': pool.json()['numbers'][0]['id'],
        'occurrence_ids': [numbers[0]['id']], 'expected_published': scan['published_version'],
    })
    assert swapped.status_code == 200, swapped.text
    assert swapped.json()['cloudflare'] == {'ok': True}
    assert swapped.json()['changed'] == 1
    after = [path for _, path in api.calls if path.endswith('/purge_cache')]
    assert len(after) == len(before) + 1
    published = http.get(prefix + '/b-redirects').json()
    assert [item['url'] for item in published['occurrences'] if item['kind'] == 'whatsapp_number'] == ['85211112222']

    api.purge_error = True
    again = http.get(prefix + '/b-redirects').json()
    number = [item for item in again['occurrences'] if item['kind'] == 'whatsapp_number'][0]
    kept = http.post(prefix + '/b-redirects/numbers/apply', json={
        'version_id': again['version']['id'], 'number_id': pool.json()['numbers'][0]['id'],
        'occurrence_ids': [number['id']], 'expected_published': again['published_version'],
    })
    assert kept.status_code == 200, kept.text
    assert kept.json()['changed'] == 0
    assert kept.json()['version']['id'] == again['published_version']
    assert kept.json()['cloudflare'] is None
    assert [path for _, path in api.calls if path.endswith('/purge_cache')] == after
    assert TOKEN not in kept.text
    assert [item['url'] for item in http.get(prefix + '/b-redirects').json()['occurrences'] if item['kind'] == 'whatsapp_number'] == ['85211112222']


def test_ready_origin_can_attach_cloudflare_and_stays_active(tmp_path):
    api = Api()
    http = session(tmp_path, client(api))
    added = http.post('/api/sites', json={'domain': 'ready.example.com'})
    assert added.status_code == 200, added.text
    site_id = added.json()['site']['id']
    early = http.post(f'/api/sites/{site_id}/cloudflare', json={})
    assert early.status_code == 400
    assert '证书' in early.json()['detail']
    site = Registry(tmp_path).get(site_id)
    assert Registry(tmp_path).transition(site_id, site['generation'], 'active', '本机 HTTPS 已接入')
    attached = http.post(f'/api/sites/{site_id}/cloudflare', json={})
    assert attached.status_code == 200, attached.text
    body = attached.json()
    assert body['site']['stage'] == 'active'
    assert body['site']['cf_status'] == 'pending'
    assert body['cloudflare']['ok'] is True
    assert body['cloudflare']['nameservers'] == SERVERS
    assert TOKEN not in attached.text


def test_failed_attach_keeps_the_domain_and_hides_the_token(tmp_path):
    def explode(request):
        raise httpx.ConnectError(TOKEN, request=request)
    broken = Cloudflare(TOKEN, 'rules.example.com', ORIGIN, httpx.MockTransport(explode))
    http = session(tmp_path, broken)
    response = http.post('/api/sites', json={'domain': 'new.example.com', 'cloudflare': True})
    assert response.status_code == 200
    body = response.json()
    assert body['cloudflare']['ok'] is False
    assert TOKEN not in response.text
    assert body['site']['cf_status'] == 'failed'
    assert body['site']['cf_zone_id'] == ''


def test_strict_https_is_kept_and_not_rewritten():
    api = Api(zones={'shop.example.com': [zone('shop.example.com', status='active')]})
    api.ssl = 'strict'
    turned = client(api).ensure_strict_ssl(ZONE, 'shop.example.com')
    assert turned == {'ssl': 'strict'}
    assert not any(method == 'PATCH' and path.endswith('/settings/ssl') for method, path in api.calls)
    api.ssl = 'flexible'
    again = client(api).ensure_strict_ssl(ZONE, 'shop.example.com')
    assert again == {'ssl': 'strict'}
    assert api.ssl == 'strict'
    patches = [path for method, path in api.calls if method == 'PATCH' and path.endswith('/settings/ssl')]
    assert patches == ['/client/v4/zones/' + ZONE + '/settings/ssl']


def test_disable_turns_the_apex_grey_and_keeps_its_address():
    api = Api(zones={'shop.example.com': [zone('shop.example.com', status='active')]}, records={
        ZONE: [{'id': RECORD, 'type': 'A', 'name': 'shop.example.com', 'content': '198.51.100.8', 'proxied': True}],
    })
    turned = client(api).disable(ZONE, 'shop.example.com')
    assert turned == {'proxied': False, 'address': '198.51.100.8'}
    assert api.records[ZONE][0]['content'] == '198.51.100.8'
    assert api.records[ZONE][0]['proxied'] is False
    assert not any(method == 'DELETE' for method, _path in api.calls)
    again = client(api).disable(ZONE, 'shop.example.com')
    assert again == {'proxied': False, 'address': '198.51.100.8'}
    puts = [path for method, path in api.calls if method == 'PUT' and path.endswith('/dns_records/' + RECORD)]
    assert len(puts) == 1


def test_disable_without_an_apex_record_leaves_the_zone():
    api = Api(zones={'shop.example.com': [zone('shop.example.com', status='active')]})
    turned = client(api).disable(ZONE, 'shop.example.com')
    assert turned == {'proxied': False, 'address': ''}
    assert not any(method in ('PUT', 'POST', 'DELETE') and 'dns_records' in path for method, path in api.calls)


def test_closing_cloudflare_returns_an_unfinished_site_to_direct_dns(tmp_path):
    api = Api(zones={'new.example.com': [zone('new.example.com', status='pending')]}, records={
        ZONE: [{'id': RECORD, 'type': 'A', 'name': 'new.example.com', 'content': ORIGIN, 'proxied': True}],
    })
    http = session(tmp_path, client(api))
    added = http.post('/api/sites', json={'domain': 'new.example.com', 'cloudflare': True})
    site_id = added.json()['site']['id']
    api.zones['new.example.com'][0]['status'] = 'active'
    ready = http.post(f'/api/sites/{site_id}/cloudflare/status', json={})
    assert ready.json()['site']['stage'] == 'unsupported'
    closed = http.post(f'/api/sites/{site_id}/cloudflare/disable', json={})
    assert closed.status_code == 200, closed.text
    body = closed.json()
    assert body['cloudflare']['ok'] is True
    assert body['cloudflare']['proxied'] is False
    assert body['record_address'] == ORIGIN
    assert body['server_ip'] == ORIGIN
    assert body['site']['stage'] == 'waiting_dns'
    assert body['site']['cf_zone_id'] == ''
    assert body['site']['cf_status'] == ''
    assert body['site']['cf_nameservers'] == ''
    assert '请把 A 记录指到 8.8.8.8' in body['site']['error']
    assert api.records[ZONE][0]['proxied'] is False
    assert api.records[ZONE][0]['content'] == ORIGIN
    assert TOKEN not in closed.text
    missing = http.post('/api/sites/default/cloudflare/disable', json={})
    assert missing.status_code == 400
    plain = http.post('/api/sites', json={'domain': 'plain.example.com'})
    assert http.post(f"/api/sites/{plain.json()['site']['id']}/cloudflare/disable", json={}).status_code == 400


def test_closing_cloudflare_keeps_a_finished_site_and_a_failed_call_keeps_state(tmp_path):
    api = Api(zones={'ready.example.com': [zone('ready.example.com', status='active')]}, records={
        ZONE: [{'id': RECORD, 'type': 'A', 'name': 'ready.example.com', 'content': '198.51.100.20', 'proxied': True}],
    })
    http = session(tmp_path, client(api))
    added = http.post('/api/sites', json={'domain': 'ready.example.com'})
    site_id = added.json()['site']['id']
    site = Registry(tmp_path).get(site_id)
    assert Registry(tmp_path).transition(site_id, site['generation'], 'active', '本机 HTTPS 已接入')
    attached = http.post(f'/api/sites/{site_id}/cloudflare', json={})
    assert attached.status_code == 200, attached.text
    api.records[ZONE] = [{'id': RECORD, 'type': 'A', 'name': 'ready.example.com', 'content': '198.51.100.20', 'proxied': True}]
    closed = http.post(f'/api/sites/{site_id}/cloudflare/disable', json={})
    assert closed.status_code == 200, closed.text
    body = closed.json()
    assert body['site']['stage'] == 'active'
    assert body['site']['cf_status'] == ''
    assert body['record_address'] == '198.51.100.20'
    assert '本机站点保持不变' in body['cloudflare']['detail']
    assert api.records[ZONE][0]['proxied'] is False
    assert api.records[ZONE][0]['content'] == '198.51.100.20'

    api.records[ZONE] = [{'id': RECORD, 'type': 'A', 'name': 'ready.example.com', 'content': '198.51.100.20', 'proxied': True}]
    Registry(tmp_path).set_cloudflare(site_id, ZONE, ','.join(SERVERS), 'active', 'Cloudflare 已生效')

    def refuse(request):
        if request.method == 'PUT':
            return failure(9109, 'token ' + TOKEN, 403)
        return api.respond(request)
    broken = Cloudflare(TOKEN, 'rules.example.com', ORIGIN, httpx.MockTransport(refuse))
    http = session(tmp_path, broken)
    refused = http.post(f'/api/sites/{site_id}/cloudflare/disable', json={})
    assert refused.status_code == 400
    assert TOKEN not in refused.text
    kept = Registry(tmp_path).get(site_id)
    assert kept['stage'] == 'active'
    assert kept['cf_zone_id'] == ZONE
    assert kept['cf_status'] == 'active'


def test_missing_cloudflare_config_rejects_the_option_without_saving(tmp_path, monkeypatch):
    monkeypatch.delenv('AB_CLOUDFLARE_TOKEN', raising=False)
    monkeypatch.delenv('AB_CLOUDFLARE_TEMPLATE', raising=False)
    http = session(tmp_path, server_ip=ORIGIN)
    assert http.get('/api/sites').json()['cloudflare_configured'] is False
    refused = http.post('/api/sites', json={'domain': 'new.example.com', 'cloudflare': True})
    assert refused.status_code == 400
    assert '取消勾选' in refused.json()['detail']
    assert all(site['domain'] != 'new.example.com' for site in http.get('/api/sites').json()['sites'])
