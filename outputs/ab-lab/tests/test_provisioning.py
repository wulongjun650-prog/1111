import hashlib
import json
from urllib.parse import parse_qs
import httpx
import pytest

from ablab.provisioning import DNSChecker, PanelPreflight, ProvisioningError, inspect_pending, public_ipv4
from ablab.sites import Registry


def dns_transport(addresses, ipv6=(), status=0):
    def respond(request):
        kind = int(request.url.params['type'])
        return httpx.Response(200, json={'Status': status, 'Answer': [
            {'type': kind, 'data': value} for value in (addresses if kind == 1 else ipv6)
        ]})
    return httpx.MockTransport(respond)


@pytest.mark.parametrize('bad', ['127.0.0.1', '10.0.0.1', '192.168.1.2', '::1', '8.8.8.8:80', '224.0.0.1'])
def test_server_ip_must_be_public_unicast(bad):
    with pytest.raises(ValueError):
        public_ipv4(bad)


def test_dns_all_records_must_match():
    assert DNSChecker(dns_transport(['8.8.8.8'])).check('lab.example.com', '8.8.8.8') == ['8.8.8.8']
    for a, aaaa in [([], []), (['8.8.8.8', '1.1.1.1'], []), (['8.8.8.8'], ['2001:4860:4860::8888'])]:
        with pytest.raises(ProvisioningError):
            DNSChecker(dns_transport(a, aaaa)).check('lab.example.com', '8.8.8.8')
    with pytest.raises(ProvisioningError):
        DNSChecker(dns_transport([], status=2)).check('lab.example.com', '8.8.8.8')


def test_dns_errors_redacted_and_timeout_is_bounded():
    def failure(request):
        assert request.extensions['timeout']['read'] == 5
        raise httpx.ConnectError('secret-network-diagnostic', request=request)
    with pytest.raises(ProvisioningError) as error:
        DNSChecker(httpx.MockTransport(failure)).check('lab.example.com', '8.8.8.8')
    assert 'secret-network-diagnostic' not in str(error.value)


def test_panel_preflight_is_read_only_and_version_zero_is_not_false():
    calls = []
    def response(request):
        calls.append(request.url.path)
        assert request.url.path == '/v2/site'
        assert request.url.params['action'] == 'GetPHPVersion'
        assert set(request.url.params) == {'action'}
        body = parse_qs(request.content.decode())
        timestamp = body['request_time'][0]
        expected = hashlib.md5((timestamp + hashlib.md5(b'test-secret').hexdigest()).encode()).hexdigest()
        assert body['request_token'][0] == expected
        return httpx.Response(200, json={'status': 0, 'message': [{'version': '00', 'name': 'Static'}]})
    result = PanelPreflight('https://panel.example.com:8888', 'test-secret', httpx.MockTransport(response)).check()
    assert result['static_available'] is True
    assert result['automatic_writes_supported'] is False
    assert calls == ['/v2/site']


@pytest.mark.parametrize('address', ['http://panel.example.com', 'https://user:password@panel.example.com', 'https://panel.example.com/path', 'http://10.0.0.1'])
def test_panel_address_rejects_remote_cleartext_and_credentials(address):
    with pytest.raises(ValueError):
        PanelPreflight(address, 'secret')


def test_panel_errors_do_not_expose_response_or_secret():
    for payload in [{'status': False, 'message': 'test-secret'}, {'status': 0, 'message': 'test-secret'}, {'status': 0, 'message': []}]:
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
        with pytest.raises(ProvisioningError) as error:
            PanelPreflight('http://127.0.0.1:8888', 'test-secret', transport).check()
        assert 'test-secret' not in str(error.value)


def test_local_panel_config_uses_internal_digest_without_double_hash(tmp_path):
    path = tmp_path / 'api.json'
    digest = hashlib.md5(b'test-secret').hexdigest()
    path.write_text(json.dumps({'open': True, 'token': digest, 'token_crypt': 'unused', 'limit_addr': ['127.0.0.1']}))

    def response(request):
        assert request.url.host == '127.0.0.1'
        assert set(request.url.params) == {'action'}
        body = parse_qs(request.content.decode())
        expected = hashlib.md5((body['request_time'][0] + digest).encode()).hexdigest()
        assert body['request_token'] == [expected]
        return httpx.Response(200, json={'status': 0, 'message': [{'version': '00', 'name': 'Static'}]})

    before = path.read_bytes()
    result = PanelPreflight.from_local_config('http://127.0.0.1:34734', path, httpx.MockTransport(response)).check()
    assert result['static_available'] is True
    assert path.read_bytes() == before
    assert digest not in json.dumps(result) and 'test-secret' not in json.dumps(result)


@pytest.mark.parametrize('changes', [
    {'open': False}, {'token': ''}, {'token': 'plaintext-secret'},
    {'limit_addr': ['127.0.0.1', '8.8.8.8']}, {'limit_addr': []},
])
def test_local_config_fails_closed_without_changing_panel(tmp_path, changes):
    path = tmp_path / 'api.json'
    config = {'open': True, 'token': 'a' * 32, 'limit_addr': ['127.0.0.1']}
    config.update(changes)
    path.write_text(json.dumps(config))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        PanelPreflight.from_local_config('http://127.0.0.1:34734', path)
    assert path.read_bytes() == before


@pytest.mark.parametrize('address', ['https://panel.example.com', 'http://localhost:8888', 'http://127.0.0.2:8888'])
def test_local_config_is_never_sent_to_remote_or_unlisted_loopback(tmp_path, address):
    with pytest.raises(ValueError):
        PanelPreflight.from_local_config(address, tmp_path / 'unreadable.json')


def test_dns_worker_persists_retry_and_does_not_fake_creation(tmp_path):
    registry = Registry(tmp_path)
    site = registry.add('new.example.com')
    inspect_pending(registry, '')
    assert registry.get(site['id'])['stage'] == 'unconfigured'
    registry.control(site['id'], 'retry')
    inspect_pending(registry, '8.8.8.8', DNSChecker(dns_transport(['1.1.1.1'])))
    failed = registry.get(site['id'])
    assert failed['stage'] == 'waiting_dns'
    assert failed['next_attempt'] > 0
    assert Registry(tmp_path).get(site['id'])['next_attempt'] == failed['next_attempt']
    registry.control(site['id'], 'retry')
    inspect_pending(registry, '8.8.8.8', DNSChecker(dns_transport(['8.8.8.8'])))
    checked = registry.get(site['id'])
    assert checked['stage'] == 'unsupported'
    assert checked['panel_id'] is None
    assert checked['managed_path'] == ''
    assert any(item['stage'] == 'dns_verified' for item in registry.events(site['id']))
    registry.control(site['id'], 'pause')
    inspect_pending(registry, '8.8.8.8', DNSChecker(dns_transport(['8.8.8.8'])))
    assert registry.get(site['id'])['stage'] == 'paused'
