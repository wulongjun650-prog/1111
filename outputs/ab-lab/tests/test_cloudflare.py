import json

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
            assert body['proxied'] is True and body['content'] == ORIGIN
            saved = body | {'id': RECORD}
            self.records[zone_id] = [saved]
            return envelope(saved)
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
    assert not any(path.endswith('/settings/ssl') or path.endswith('/settings/development_mode') for _, path in api.calls)


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


def test_missing_cloudflare_config_rejects_the_option_without_saving(tmp_path, monkeypatch):
    monkeypatch.delenv('AB_CLOUDFLARE_TOKEN', raising=False)
    monkeypatch.delenv('AB_CLOUDFLARE_TEMPLATE', raising=False)
    http = session(tmp_path, server_ip=ORIGIN)
    assert http.get('/api/sites').json()['cloudflare_configured'] is False
    refused = http.post('/api/sites', json={'domain': 'new.example.com', 'cloudflare': True})
    assert refused.status_code == 400
    assert '取消勾选' in refused.json()['detail']
    assert all(site['domain'] != 'new.example.com' for site in http.get('/api/sites').json()['sites'])
