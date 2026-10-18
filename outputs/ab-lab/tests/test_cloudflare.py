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
        self.browser_cache_ttl = 0
        self.template_browser_cache = None
        self.template_rule_browser_ttl = None
        self.firewall_rules = []
        self.page_rules = []
        self.security_level = 'medium'
        self.firewall_forbidden = False
        self.firewall_missing = False
        self.widgets = []
        self.widget_forbidden = False

    def transport(self):
        return httpx.MockTransport(self.respond)

    def respond(self, request):
        assert request.headers['authorization'] == 'Bearer ' + TOKEN
        assert TOKEN not in str(request.url)
        path = request.url.path
        self.calls.append((request.method, path))
        if path.startswith('/client/v4/accounts/'):
            return self._widgets_api(request, path.removeprefix('/client/v4/accounts/'))
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
            rows = [
                {'id': 'brotli', 'value': 'on', 'editable': True},
                {'id': 'minify', 'value': {'css': 'on', 'html': 'off', 'js': 'on'}, 'editable': True},
                {'id': 'polish', 'value': 'lossless', 'editable': True},
                {'id': 'development_mode', 'value': 'on', 'editable': True},
                {'id': 'ssl', 'value': 'full', 'editable': True},
                {'id': 'rocket_loader', 'value': 'off', 'editable': False},
            ]
            if self.template_browser_cache is not None:
                rows.append({'id': 'browser_cache_ttl', 'value': self.template_browser_cache, 'editable': True})
            return envelope(rows)
        if rest == 'settings/browser_cache_ttl' and request.method == 'GET':
            return envelope({'id': 'browser_cache_ttl', 'value': self.browser_cache_ttl, 'editable': True})
        if rest == 'settings/browser_cache_ttl' and request.method == 'PATCH':
            assert json.loads(request.content) == {'value': 0}
            self.browser_cache_ttl = 0
            return envelope({'id': 'browser_cache_ttl', 'value': 0})
        if rest == 'settings/security_level' and request.method == 'GET':
            return envelope({'id': 'security_level', 'value': self.security_level})
        if rest == 'settings/security_level' and request.method == 'PATCH':
            value = json.loads(request.content)['value']
            assert value in ('off', 'essentially_off', 'low', 'medium', 'high', 'under_attack')
            self.security_level = value
            return envelope({'id': 'security_level', 'value': value})
        if rest.startswith('settings/') and request.method == 'PATCH':
            setting = rest.removeprefix('settings/')
            assert setting in ('brotli', 'minify', 'polish')
            if setting == 'polish' and self.polish == 'skip':
                return failure(9000, 'plan does not include polish')
            return envelope({'id': setting, 'value': json.loads(request.content)['value']})
        if rest == 'rulesets/phases/http_request_cache_settings/entrypoint' and request.method == 'GET':
            parameters = {'cache': True}
            if self.template_rule_browser_ttl is not None:
                parameters['edge_ttl'] = {'mode': 'override_origin', 'default': 72000}
                parameters['browser_ttl'] = {'mode': 'override_origin', 'default': self.template_rule_browser_ttl}
            return envelope({'rules': [{
                'id': 'e' * 32, 'expression': 'http.host eq "rules.example.com"', 'action': 'set_cache_settings',
                'action_parameters': parameters, 'description': 'cache host', 'enabled': True,
            }]})
        if rest == 'rulesets/phases/http_config_settings/entrypoint' and request.method == 'GET':
            return failure(10003, 'could not find entrypoint ruleset', 404)
        if rest == 'rulesets/phases/http_request_cache_settings/entrypoint' and request.method == 'PUT':
            body = json.loads(request.content)
            rule = body['rules'][0]
            assert set(body) == {'rules'} and 'id' not in rule
            assert rule['action'] == 'set_cache_settings'
            self.last_rule = rule
            return envelope(body)
        if rest == 'pagerules' and request.method == 'GET':
            return envelope(self.page_rules)
        if rest == 'pagerules' and request.method == 'POST':
            body = json.loads(request.content)
            assert body['priority'] == 1 and body['status'] == 'active'
            assert body['actions'] == [{'id': 'security_level', 'value': 'under_attack'}]
            assert 'ab_human=1' in body['targets'][0]['constraint']['value']
            saved = {'id': 'f' * 32, 'targets': body['targets'], 'actions': body['actions'], 'priority': 1, 'status': 'active'}
            self.page_rules = [rule for rule in self.page_rules if 'ab_human=1' not in json.dumps(rule.get('targets'))]
            self.page_rules.insert(0, saved)
            return envelope(saved)
        if rest.startswith('pagerules/') and request.method == 'PATCH':
            body = json.loads(request.content)
            rule_id = rest.removeprefix('pagerules/')
            saved = {'id': rule_id, 'targets': body['targets'], 'actions': body['actions'], 'priority': body['priority'], 'status': body['status']}
            self.page_rules = [saved if rule.get('id') == rule_id else rule for rule in self.page_rules]
            return envelope(saved)
        if rest.startswith('pagerules/') and request.method == 'DELETE':
            rule_id = rest.removeprefix('pagerules/')
            self.page_rules = [rule for rule in self.page_rules if rule.get('id') != rule_id]
            return envelope({'id': rule_id})
        if rest == 'rulesets/phases/http_request_firewall_custom/entrypoint' and request.method == 'GET':
            if self.firewall_forbidden:
                return httpx.Response(403, json={'success': False, 'errors': [{'message': 'request is not authorized'}], 'result': None})
            if self.firewall_missing:
                return failure(10003, 'could not find entrypoint ruleset', 404)
            return envelope({'rules': self.firewall_rules})
        if rest == 'rulesets/phases/http_request_firewall_custom/entrypoint' and request.method == 'PUT':
            if self.firewall_forbidden:
                return failure(9109, 'token ' + TOKEN, 403)
            body = json.loads(request.content)
            assert set(body) == {'rules'}
            for rule in body['rules']:
                assert rule['action'] != 'set_cache_settings'
                assert 'browser_ttl' not in rule.get('action_parameters', {})
            self.firewall_rules = body['rules']
            return envelope({'rules': body['rules']})
        if rest == 'purge_cache' and request.method == 'POST':
            if getattr(self, 'purge_error', False):
                return failure(9000, 'cache is busy')
            assert json.loads(request.content) == {'purge_everything': True}
            return envelope({'id': zone_id})
        raise AssertionError(f'unexpected {request.method} {path}')

    def _widgets_api(self, request, rest):
        account, _, tail = rest.partition('/')
        assert account == ACCOUNT
        if self.widget_forbidden:
            return failure(10000, 'unauthorized', 403)
        if tail == 'challenges/widgets' and request.method == 'GET':
            return envelope(self.widgets)
        body = json.loads(request.content or b'{}')
        if tail == 'challenges/widgets' and request.method == 'POST':
            assert body['mode'] == 'non-interactive' and body['clearance_level'] == 'jschallenge'
            assert body['name'] == '双子星浏览时加载'
            assert 1 <= len(body['domains']) <= 10
            saved = {
                'sitekey': '0x4AAAAAAAB8lYQJSCAI6WbG' + str(len(self.widgets)),
                'secret': '0xSECRETSHOULDNOTLEAK',
                'name': body['name'],
                'domains': body['domains'],
                'mode': body['mode'],
                'clearance_level': body['clearance_level'],
            }
            self.widgets.append({key: value for key, value in saved.items() if key != 'secret'})
            return envelope(saved)
        if tail.startswith('challenges/widgets/') and request.method == 'PUT':
            sitekey = tail.removeprefix('challenges/widgets/')
            found = next(item for item in self.widgets if item['sitekey'] == sitekey)
            assert body['mode'] == 'non-interactive' and body['clearance_level'] == 'jschallenge'
            found['domains'] = body['domains']
            found['name'] = body['name']
            found['mode'] = body['mode']
            found['clearance_level'] = body['clearance_level']
            return envelope(dict(found, secret='0xSECRETSHOULDNOTLEAK'))
        if tail.startswith('challenges/widgets/') and request.method == 'DELETE':
            sitekey = tail.removeprefix('challenges/widgets/')
            self.widgets = [item for item in self.widgets if item['sitekey'] != sitekey]
            return envelope({'sitekey': sitekey})
        raise AssertionError(f'unexpected widget {request.method} {tail}')


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


def test_template_browser_cache_over_an_hour_follows_the_origin():
    api = Api()
    api.browser_cache_ttl = 72000
    api.template_browser_cache = 72000
    api.template_rule_browser_ttl = 72000
    client(api).attach('new.example.com')
    assert api.browser_cache_ttl == 0
    assert any(method == 'PATCH' and path.endswith('/settings/browser_cache_ttl') for method, path in api.calls)
    assert api.last_rule['action_parameters']['cache'] is True
    assert api.last_rule['action_parameters']['edge_ttl'] == {'mode': 'override_origin', 'default': 72000}
    assert api.last_rule['action_parameters']['browser_ttl'] == {'mode': 'respect_origin'}
    assert 'rules.example.com' not in api.last_rule['expression']


def test_purge_rewrites_a_20_hour_browser_cache_then_clears_the_edge():
    api = Api(zones={'shop.example.com': [zone('shop.example.com', status='active')]}, records={
        ZONE: [{'id': RECORD, 'type': 'A', 'name': 'shop.example.com', 'content': ORIGIN, 'proxied': True}],
    })
    api.browser_cache_ttl = 72000
    api.template_rule_browser_ttl = 72000
    client(api).purge(ZONE, 'shop.example.com')
    assert api.browser_cache_ttl == 0
    assert api.last_rule['action_parameters']['cache'] is True
    assert api.last_rule['action_parameters']['edge_ttl']['default'] == 72000
    assert api.last_rule['action_parameters']['browser_ttl'] == {'mode': 'respect_origin'}
    methods = [(method, path) for method, path in api.calls]
    setting = methods.index(('PATCH', f'/client/v4/zones/{ZONE}/settings/browser_cache_ttl'))
    cleared = methods.index(('POST', f'/client/v4/zones/{ZONE}/purge_cache'))
    assert setting < cleared


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


class _OkChecker:
    def check(self, url):
        return {'status': 'normal', 'platform': '网站', 'detail': 'ok',
                'checked_at': 1.0, 'http_status': 200, 'final_url': url}


def test_b_page_number_and_link_edits_each_purge_the_cache(tmp_path):
    api = Api()
    http = session(tmp_path, client(api))
    http.app.state.link_checker = _OkChecker()
    added = http.post('/api/sites', json={'domain': 'new.example.com', 'cloudflare': True})
    assert added.status_code == 200, added.text
    site_id = added.json()['site']['id']
    base = f'/api/sites/{site_id}'

    page = ('<!doctype html><body>'
            '<a href="https://wa.me/85200000001">wa</a>'
            '<a href="https://old.test/go">link</a>'
            '</body>')
    up = http.post(f'{base}/upload/B?name=index.html', content=page.encode())
    assert up.status_code == 200, up.text
    version_id = up.json()['version']['id']
    assert http.post(f'{base}/publish/B', json={'version_id': version_id}).status_code == 200

    def purges():
        return [path for _, path in api.calls if path.endswith('/purge_cache')]

    number = http.post(f'{base}/b-redirects/numbers',
                       json={'phones': ['85200000002'], 'note': '', 'display_name': '小美'})
    assert number.status_code == 200, number.text
    number_id = number.json()['numbers'][0]['id']

    before = len(purges())
    split_on = http.put(f'{base}/b-redirects/numbers/split',
                        json={'enabled': True, 'mode': 'random', 'members': [{'number_id': number_id, 'weight': 1}]})
    assert split_on.status_code == 200, split_on.text
    assert split_on.json()['cloudflare'] == {'ok': True}
    assert len(purges()) == before + 1

    before = len(purges())
    removed = http.delete(f'{base}/b-redirects/numbers/{number_id}')
    assert removed.status_code == 200, removed.text
    assert removed.json()['cloudflare'] == {'ok': True}
    assert len(purges()) == before + 1

    before = len(purges())
    link_split = http.put(f'{base}/b-redirects/split', json={'enabled': False, 'mode': 'random', 'members': []})
    assert link_split.status_code == 200, link_split.text
    assert link_split.json()['cloudflare'] == {'ok': True}
    assert len(purges()) == before + 1

    scan = http.get(f'{base}/b-redirects').json()
    anchor = next(item for item in scan['occurrences'] if item['kind'] == 'anchor')
    preset = http.post(f'{base}/b-redirects/presets', json={'urls': ['https://safe.example/new'], 'note': ''})
    assert preset.status_code == 200, preset.text
    preset_id = preset.json()['presets'][0]['id']
    before = len(purges())
    applied = http.post(f'{base}/b-redirects/apply', json={
        'version_id': scan['version']['id'], 'preset_id': preset_id,
        'occurrence_ids': [anchor['id']], 'expected_published': scan['published_version']})
    assert applied.status_code == 200, applied.text
    assert applied.json()['changed'] == 1
    assert applied.json()['cloudflare'] == {'ok': True}
    assert len(purges()) == before + 1


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


def test_delete_zone_checks_the_name_before_deleting():
    calls = []

    def respond(request):
        calls.append((request.method, request.url.path))
        assert request.headers['authorization'] == 'Bearer ' + TOKEN
        if request.method == 'GET':
            return envelope(zone('shop.example.com', status='active'))
        assert request.method == 'DELETE'
        return envelope({'id': ZONE})

    Cloudflare(TOKEN, 'rules.example.com', ORIGIN, httpx.MockTransport(respond)).delete_zone(ZONE, 'shop.example.com')
    assert calls == [('GET', '/client/v4/zones/' + ZONE), ('DELETE', '/client/v4/zones/' + ZONE)]


def test_delete_zone_stops_when_the_name_or_the_zone_does_not_match():
    calls = []

    def mismatch(request):
        calls.append(request.method)
        return envelope(zone('other.example.com', status='active'))

    with pytest.raises(CloudflareError, match='不一致'):
        Cloudflare(TOKEN, 'rules.example.com', ORIGIN, httpx.MockTransport(mismatch)).delete_zone(ZONE, 'shop.example.com')
    assert calls == ['GET']

    def missing(request):
        calls.append(request.method)
        return failure(10003, 'not found', 404)

    Cloudflare(TOKEN, 'rules.example.com', ORIGIN, httpx.MockTransport(missing)).delete_zone(ZONE, 'shop.example.com')
    assert calls == ['GET', 'GET']


def _ready(tmp_path, api, domain='shop.example.com'):
    http = session(tmp_path, client(api))
    added = http.post('/api/sites', json={'domain': domain})
    assert added.status_code == 200, added.text
    assert added.json()['site']['cf_challenge'] == 0
    site_id = added.json()['site']['id']
    site = Registry(tmp_path).get(site_id)
    assert Registry(tmp_path).transition(site_id, site['generation'], 'active', '本机 HTTPS 已接入')
    attached = http.post(f'/api/sites/{site_id}/cloudflare', json={})
    assert attached.status_code == 200, attached.text
    api.calls.clear()
    return http, site_id


def test_human_check_keeps_other_firewall_rules_and_leaves_cache_alone():
    other = {
        'id': 'e' * 32, 'expression': 'ip.src eq 192.0.2.1', 'action': 'block',
        'description': 'keep me', 'enabled': True,
    }
    api = Api(zones={'shop.example.com': [zone('shop.example.com', status='active')]}, records={
        ZONE: [{'id': RECORD, 'type': 'A', 'name': 'shop.example.com', 'content': ORIGIN, 'proxied': True}],
    })
    api.firewall_rules = [other]
    opened = client(api).set_human_challenge(ZONE, 'shop.example.com', True, False)
    assert opened == {'entry': True, 'button': False, 'browse': False, 'previous': '', 'sitekey': ''}
    assert api.firewall_rules[0]['id'] == 'e' * 32
    assert api.firewall_rules[0]['action'] == 'block'
    challenge = api.firewall_rules[1]
    assert challenge['action'] == 'managed_challenge'
    assert challenge['description'] == '双子星人机验证'
    assert 'text/html' in challenge['expression']
    assert 'id' not in challenge
    assert api.page_rules == []
    again = client(api).set_human_challenge(ZONE, 'shop.example.com', True, False)
    assert again['entry'] is True
    puts = [path for method, path in api.calls if method == 'PUT']
    assert puts == [f'/client/v4/zones/{ZONE}/rulesets/phases/http_request_firewall_custom/entrypoint']
    assert not any('cache' in path or path.endswith('/settings/security_level') for _, path in api.calls)
    closed = client(api).set_human_challenge(ZONE, 'shop.example.com', False, False)
    assert closed['entry'] is False
    assert api.firewall_rules == [other]
    assert api.security_level == 'medium'


def test_missing_firewall_entrypoint_can_be_created_without_touching_cache():
    api = Api(zones={'shop.example.com': [zone('shop.example.com', status='active')]})
    api.firewall_missing = True
    opened = client(api).set_human_challenge(ZONE, 'shop.example.com', True, False)
    assert opened['entry'] is True and opened['previous'] == ''
    assert api.firewall_rules[0]['action'] == 'managed_challenge'
    assert not any('http_request_cache_settings' in path or 'browser_cache_ttl' in path for _, path in api.calls)


def test_firewall_permission_falls_back_to_under_attack_and_restores_the_old_level():
    api = Api(zones={'shop.example.com': [zone('shop.example.com', status='active')]})
    api.firewall_forbidden = True
    api.security_level = 'high'
    opened = client(api).set_human_challenge(ZONE, 'shop.example.com', True, False)
    assert opened == {'entry': True, 'button': False, 'browse': False, 'previous': 'high', 'sitekey': ''}
    assert api.security_level == 'under_attack'
    assert api.firewall_rules == []
    assert api.page_rules == []
    repeated = client(api).set_human_challenge(ZONE, 'shop.example.com', True, False, 'high')
    assert repeated['previous'] == 'high'
    patches = [path for method, path in api.calls if method == 'PATCH' and path.endswith('/settings/security_level')]
    assert patches == [f'/client/v4/zones/{ZONE}/settings/security_level']
    closed = client(api).set_human_challenge(ZONE, 'shop.example.com', False, False, 'high')
    assert closed['entry'] is False
    assert api.security_level == 'high'
    assert not any('cache' in path for _, path in api.calls)


def test_button_check_uses_a_page_rule_and_keeps_the_landing_level():
    api = Api(zones={'shop.example.com': [zone('shop.example.com', status='active')]})
    api.firewall_forbidden = True
    api.security_level = 'medium'
    kept = {
        'id': 'a' * 32, 'priority': 2, 'status': 'active',
        'targets': [{'target': 'url', 'constraint': {'operator': 'matches', 'value': '*shop.example.com/keep*'}}],
        'actions': [{'id': 'browser_cache_ttl', 'value': 1200}],
    }
    api.page_rules = [kept]
    opened = client(api).set_human_challenge(ZONE, 'shop.example.com', False, True)
    assert opened == {'entry': False, 'button': True, 'browse': False, 'previous': '', 'sitekey': ''}
    assert api.security_level == 'medium'
    assert api.page_rules[0]['targets'][0]['constraint']['value'] == '*shop.example.com/*ab_human=1*'
    assert api.page_rules[0]['actions'] == [{'id': 'security_level', 'value': 'under_attack'}]
    assert api.page_rules[1] == kept
    again = client(api).set_human_challenge(ZONE, 'shop.example.com', False, True)
    assert again['button'] is True
    posts = [path for method, path in api.calls if method == 'POST' and path.endswith('/pagerules')]
    assert posts == [f'/client/v4/zones/{ZONE}/pagerules']
    assert not any('cache' in path or path.endswith('/settings/security_level') for _, path in api.calls)
    closed = client(api).set_human_challenge(ZONE, 'shop.example.com', False, False)
    assert closed['button'] is False
    assert api.page_rules == [kept]
    assert api.security_level == 'medium'


def test_a_rejected_challenge_rule_does_not_change_security_level():
    api = Api(zones={'shop.example.com': [zone('shop.example.com', status='active')]})
    original = api.respond

    def respond(request):
        if request.method == 'PUT' and request.url.path.endswith('/http_request_firewall_custom/entrypoint'):
            return failure(1001, 'invalid filter expression ' + TOKEN)
        return original(request)

    api.respond = respond
    with pytest.raises(CloudflareError) as error:
        client(api).set_human_challenge(ZONE, 'shop.example.com', True, False)
    assert TOKEN not in str(error.value)
    assert api.security_level == 'medium'
    assert not any(method == 'PATCH' and path.endswith('/settings/security_level') for method, path in api.calls)


def test_domain_switch_starts_off_and_only_changes_that_domain(tmp_path):
    api = Api(zones={'shop.example.com': [zone('shop.example.com', status='active')]}, records={
        ZONE: [{'id': RECORD, 'type': 'A', 'name': 'shop.example.com', 'content': ORIGIN, 'proxied': True}],
    })
    http, site_id = _ready(tmp_path, api)
    assert http.post('/api/sites/default/cloudflare/challenge', json={'entry': True, 'button': False}).status_code == 400
    plain = http.post('/api/sites', json={'domain': 'plain.example.com'})
    assert http.post(f"/api/sites/{plain.json()['site']['id']}/cloudflare/challenge", json={'entry': True, 'button': False}).status_code == 400
    assert http.post(f'/api/sites/{site_id}/cloudflare/challenge', json={'enabled': 'yes'}).status_code == 422
    opened = http.post(f'/api/sites/{site_id}/cloudflare/challenge', json={'entry': True, 'button': False})
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert body['entry'] is True and body['button'] is False
    assert body['site']['cf_challenge'] == 1
    assert body['site']['cf_challenge_level'] == ''
    assert '进入落地页' in body['detail']
    assert TOKEN not in opened.text
    assert api.firewall_rules[0]['action'] == 'managed_challenge'
    assert api.security_level == 'medium'
    assert api.page_rules == []
    assert not any('cache' in path or path.endswith('/settings/browser_cache_ttl') for _, path in api.calls)
    again = http.post(f'/api/sites/{site_id}/cloudflare/challenge', json={'entry': True, 'button': False})
    assert again.status_code == 200
    assert [method for method, path in api.calls if method == 'PUT' and 'firewall_custom' in path] == ['PUT']
    closed = http.post(f'/api/sites/{site_id}/cloudflare/challenge', json={'entry': False, 'button': False})
    assert closed.status_code == 200, closed.text
    assert closed.json()['entry'] is False and closed.json()['button'] is False
    assert closed.json()['site']['cf_challenge'] == 0
    assert api.firewall_rules == []
    assert '直接进入' in closed.json()['detail']
    events = http.get(f'/api/sites/{site_id}/provision/events').json()['items']
    assert any(item['detail'] == '已打开进入落地人机验证' for item in events)
    assert any(item['detail'] == '已关闭人机验证' for item in events)


def _publish_button_page(http, site_id):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr('index.html', '<!doctype html><html><body><a id="go" href="https://example.com/go">打开</a><button id="plain">按钮</button></body></html>')
    uploaded = http.post(f'/api/sites/{site_id}/upload/B?name=page.zip', content=archive.getvalue())
    assert uploaded.status_code == 200, uploaded.text
    version_id = uploaded.json()['version']['id']
    published = http.post(f'/api/sites/{site_id}/publish/B', json={'version_id': version_id})
    assert published.status_code == 200, published.text
    return version_id


def test_button_switch_writes_the_published_page_and_not_the_zone_level(tmp_path):
    api = Api(zones={'shop.example.com': [zone('shop.example.com', status='active')]}, records={
        ZONE: [{'id': RECORD, 'type': 'A', 'name': 'shop.example.com', 'content': ORIGIN, 'proxied': True}],
    })
    api.firewall_forbidden = True
    http, site_id = _ready(tmp_path, api)
    _publish_button_page(http, site_id)
    api.calls.clear()
    opened = http.post(f'/api/sites/{site_id}/cloudflare/challenge', json={'entry': False, 'button': True})
    assert opened.status_code == 200, opened.text
    assert opened.json()['button'] is True and opened.json()['entry'] is False
    assert opened.json()['site']['cf_challenge'] == 2
    assert '一点按钮' in opened.json()['detail']
    assert api.security_level == 'medium'
    assert api.page_rules[0]['actions'] == [{'id': 'security_level', 'value': 'under_attack'}]
    assert not any(method == 'PUT' and 'cache' in path for method, path in api.calls)
    page = Registry(tmp_path).store(site_id).pages / Registry(tmp_path).store(site_id).slots()['B'] / 'index.html'
    text = page.read_text(encoding='utf-8')
    assert 'id="ab-human-click"' in text
    assert 'https://example.com/go' in text
    again = http.post(f'/api/sites/{site_id}/cloudflare/challenge', json={'entry': False, 'button': True})
    assert again.status_code == 200
    assert [method for method, path in api.calls if method == 'POST' and path.endswith('/pagerules')] == ['POST']
    both = http.post(f'/api/sites/{site_id}/cloudflare/challenge', json={'entry': True, 'button': True})
    assert both.status_code == 200, both.text
    assert both.json()['site']['cf_challenge'] == 3
    assert api.security_level == 'under_attack'
    assert '进入落地页' in both.json()['detail'] and '一点按钮' in both.json()['detail']
    closed = http.post(f'/api/sites/{site_id}/cloudflare/challenge', json={'entry': False, 'button': False})
    assert closed.status_code == 200, closed.text
    assert closed.json()['site']['cf_challenge'] == 0
    assert api.page_rules == []
    assert api.security_level == 'medium'
    assert 'ab-human-click' not in page.read_text(encoding='utf-8')


def test_browse_switch_keeps_the_page_open_and_adds_a_corner_check(tmp_path):
    api = Api(zones={'shop.example.com': [zone('shop.example.com', status='active')]}, records={
        ZONE: [{'id': RECORD, 'type': 'A', 'name': 'shop.example.com', 'content': ORIGIN, 'proxied': True}],
    })
    api.firewall_forbidden = True
    api.security_level = 'medium'
    http, site_id = _ready(tmp_path, api)
    version_id = _publish_button_page(http, site_id)
    page = Registry(tmp_path).store(site_id).pages / version_id / 'index.html'
    api.calls.clear()
    opened = http.post(f'/api/sites/{site_id}/cloudflare/challenge', json={'entry': False, 'button': False, 'browse': True})
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert body['browse'] is True and body['entry'] is False and body['button'] is False
    assert body['site']['cf_challenge'] == 4
    assert body['site']['cf_turnstile_sitekey'].startswith('0x')
    assert '最下方' in body['detail']
    assert 'SECRETSHOULDNOTLEAK' not in opened.text
    assert api.security_level == 'medium'
    assert api.page_rules == []
    assert api.widgets[0]['domains'] == ['shop.example.com']
    assert api.widgets[0]['mode'] == 'non-interactive'
    assert api.widgets[0]['clearance_level'] == 'jschallenge'
    assert '不用点按钮' in body['detail']
    assert not any(method == 'PUT' and 'cache' in path for method, path in api.calls)
    text = page.read_text(encoding='utf-8')
    script = page.with_name('ab-human-browse.js').read_text(encoding='utf-8')
    assert 'id="ab-human-browse"' in text
    assert 'src="ab-human-browse.js"' in text
    assert 'position:fixed' not in text
    assert 'margin:28px 0 20px' in text
    assert 'function show' not in text
    assert 'https://challenges.cloudflare.com/turnstile/v0/api.js' not in text
    assert body['site']['cf_turnstile_sitekey'] in script
    assert 'https://challenges.cloudflare.com/turnstile/v0/api.js' in script
    assert '请稍等' not in script
    assert 'defer' in text
    assert 'ab-human-click' not in text
    assert 'preventDefault' not in text
    assert 'https://example.com/go' in text
    again = http.post(f'/api/sites/{site_id}/cloudflare/challenge', json={'entry': False, 'button': False, 'browse': True})
    assert again.status_code == 200, again.text
    assert [method for method, path in api.calls if method == 'POST' and path.endswith('/challenges/widgets')] == ['POST']
    fresh = io.BytesIO()
    with zipfile.ZipFile(fresh, 'w') as bundle:
        bundle.writestr('index.html', '<!doctype html><html><body><p>新的一版</p></body></html>')
    uploaded = http.post(f'/api/sites/{site_id}/upload/B?name=next.zip', content=fresh.getvalue())
    assert uploaded.status_code == 200, uploaded.text
    published = http.post(f'/api/sites/{site_id}/publish/B', json={'version_id': uploaded.json()['version']['id']})
    assert published.status_code == 200, published.text
    live = Registry(tmp_path).store(site_id).pages / uploaded.json()['version']['id'] / 'index.html'
    assert 'id="ab-human-browse"' in live.read_text(encoding='utf-8')
    assert 'function show' not in live.read_text(encoding='utf-8')
    assert live.with_name('ab-human-browse.js').is_file()
    assert '新的一版' in live.read_text(encoding='utf-8')
    closed = http.post(f'/api/sites/{site_id}/cloudflare/challenge', json={'entry': False, 'button': False, 'browse': False})
    assert closed.status_code == 200, closed.text
    assert closed.json()['browse'] is False
    assert closed.json()['site']['cf_challenge'] == 0
    assert closed.json()['site']['cf_turnstile_sitekey'] == ''
    assert api.widgets == []
    assert 'ab-human-browse' not in live.read_text(encoding='utf-8')
    assert not live.with_name('ab-human-browse.js').exists()
    assert api.security_level == 'medium'


def test_wait_switch_blocks_the_click_until_the_check_finishes(tmp_path):
    api = Api(zones={'shop.example.com': [zone('shop.example.com', status='active')]}, records={
        ZONE: [{'id': RECORD, 'type': 'A', 'name': 'shop.example.com', 'content': ORIGIN, 'proxied': True}],
    })
    api.firewall_forbidden = True
    api.security_level = 'medium'
    http, site_id = _ready(tmp_path, api)
    version_id = _publish_button_page(http, site_id)
    page = Registry(tmp_path).store(site_id).pages / version_id / 'index.html'
    opened = http.post(f'/api/sites/{site_id}/cloudflare/challenge', json={'entry': False, 'button': False, 'browse': False, 'wait': True})
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert body['wait'] is True and body['browse'] is False
    assert body['site']['cf_challenge'] == 8
    assert '请稍等，正在验证是否是真人' in body['detail']
    text = page.read_text(encoding='utf-8')
    script = page.with_name('ab-human-browse.js').read_text(encoding='utf-8')
    assert '请稍等，正在验证是否是真人' not in text
    assert '请稍等，正在验证是否是真人' in script
    assert 'abHumanPending' in script
    assert 'src="ab-human-browse.js"' in text
    assert api.widgets[0]['mode'] == 'non-interactive'
    kept = http.post(f'/api/sites/{site_id}/cloudflare/challenge', json={'entry': False, 'button': False, 'browse': True, 'wait': True})
    assert kept.status_code == 200, kept.text
    assert kept.json()['site']['cf_challenge'] == 12
    assert '请稍等，正在验证是否是真人' in page.with_name('ab-human-browse.js').read_text(encoding='utf-8')
    closed = http.post(f'/api/sites/{site_id}/cloudflare/challenge', json={'entry': False, 'button': False, 'browse': True, 'wait': False})
    assert closed.status_code == 200, closed.text
    assert closed.json()['wait'] is False and closed.json()['site']['cf_challenge'] == 4
    assert '请稍等' not in page.with_name('ab-human-browse.js').read_text(encoding='utf-8')


def test_browse_switch_rewrites_a_click_widget_so_the_check_finishes_itself(tmp_path):
    api = Api(zones={'shop.example.com': [zone('shop.example.com', status='active')]}, records={
        ZONE: [{'id': RECORD, 'type': 'A', 'name': 'shop.example.com', 'content': ORIGIN, 'proxied': True}],
    })
    api.firewall_forbidden = True
    api.security_level = 'medium'
    sitekey = '0x4AAAAAAAB8lYQJSCAI6WbGexisting1'
    api.widgets = [{
        'sitekey': sitekey,
        'name': '双子星浏览时加载',
        'domains': ['shop.example.com'],
        'mode': 'managed',
        'clearance_level': 'managed',
    }]
    http, site_id = _ready(tmp_path, api)
    opened = http.post(f'/api/sites/{site_id}/cloudflare/challenge', json={'entry': False, 'button': False, 'browse': True})
    assert opened.status_code == 200, opened.text
    assert api.widgets[0]['sitekey'] == sitekey
    assert api.widgets[0]['mode'] == 'non-interactive'
    assert api.widgets[0]['clearance_level'] == 'jschallenge'
    assert [method for method, path in api.calls if method == 'PUT' and path.endswith('/challenges/widgets/' + sitekey)] == ['PUT']
    again = http.post(f'/api/sites/{site_id}/cloudflare/challenge', json={'entry': False, 'button': False, 'browse': True})
    assert again.status_code == 200, again.text
    assert [method for method, path in api.calls if method == 'PUT' and path.endswith('/challenges/widgets/' + sitekey)] == ['PUT']


def test_browse_switch_stays_off_when_the_widget_is_forbidden(tmp_path):
    api = Api(zones={'shop.example.com': [zone('shop.example.com', status='active')]}, records={
        ZONE: [{'id': RECORD, 'type': 'A', 'name': 'shop.example.com', 'content': ORIGIN, 'proxied': True}],
    })
    api.firewall_forbidden = True
    api.widget_forbidden = True
    http, site_id = _ready(tmp_path, api)
    version_id = _publish_button_page(http, site_id)
    page = Registry(tmp_path).store(site_id).pages / version_id / 'index.html'
    refused = http.post(f'/api/sites/{site_id}/cloudflare/challenge', json={'entry': False, 'button': False, 'browse': True})
    assert refused.status_code == 400
    assert '密钥' in refused.json()['detail']
    assert 'SECRET' not in refused.text
    saved = http.get('/api/sites').json()['sites']
    site = next(item for item in saved if item['id'] == site_id)
    assert site['cf_challenge'] == 0
    assert 'ab-human-browse' not in page.read_text(encoding='utf-8')


def test_closing_cloudflare_turns_the_human_check_off_before_greying(tmp_path):
    api = Api(zones={'shop.example.com': [zone('shop.example.com', status='active')]}, records={
        ZONE: [{'id': RECORD, 'type': 'A', 'name': 'shop.example.com', 'content': ORIGIN, 'proxied': True}],
    })
    api.firewall_forbidden = True
    api.security_level = 'medium'
    http, site_id = _ready(tmp_path, api)
    opened = http.post(f'/api/sites/{site_id}/cloudflare/challenge', json={'entry': True, 'button': False})
    assert opened.status_code == 200, opened.text
    assert opened.json()['site']['cf_challenge'] == 1
    assert opened.json()['site']['cf_challenge_level'] == 'medium'
    assert api.security_level == 'under_attack'
    api.calls.clear()
    closed = http.post(f'/api/sites/{site_id}/cloudflare/disable', json={})
    assert closed.status_code == 200, closed.text
    assert closed.json()['site']['cf_zone_id'] == ''
    assert closed.json()['site']['cf_challenge'] == 0
    assert closed.json()['site']['cf_challenge_level'] == ''
    assert api.security_level == 'medium'
    assert api.records[ZONE][0]['proxied'] is False
    methods = [(method, path) for method, path in api.calls]
    restored = methods.index(('PATCH', f'/client/v4/zones/{ZONE}/settings/security_level'))
    greyed = methods.index(('PUT', f'/client/v4/zones/{ZONE}/dns_records/{RECORD}'))
    assert restored < greyed
    assert not any('cache' in path for _, path in api.calls)


def test_missing_cloudflare_config_rejects_the_option_without_saving(tmp_path, monkeypatch):
    monkeypatch.delenv('AB_CLOUDFLARE_TOKEN', raising=False)
    monkeypatch.delenv('AB_CLOUDFLARE_TEMPLATE', raising=False)
    http = session(tmp_path, server_ip=ORIGIN)
    assert http.get('/api/sites').json()['cloudflare_configured'] is False
    refused = http.post('/api/sites', json={'domain': 'new.example.com', 'cloudflare': True})
    assert refused.status_code == 400
    assert '取消勾选' in refused.json()['detail']
    assert all(site['domain'] != 'new.example.com' for site in http.get('/api/sites').json()['sites'])
