import json
from datetime import datetime, timedelta, timezone
from threading import Event, Thread
from urllib.parse import parse_qs

import httpx
from fastapi.testclient import TestClient

from ablab.reputation import GoogleReputation
from ablab.settings import Deployment
from ablab.sites import Registry
from ablab.web import create_admin


def fixture(tmp_path, key='test-key', handler=None):
    registry = Registry(tmp_path)
    site = registry.add('example.com')
    requests = []

    def respond(request):
        requests.append(request)
        return handler(request) if handler else httpx.Response(200, json={})

    checker = GoogleReputation(registry, key, transport=httpx.MockTransport(respond))
    return registry, site, checker, requests


def test_missing_key_and_empty_default_domain_never_call_google(tmp_path):
    registry, site, _, requests = fixture(tmp_path, key='')
    checker = GoogleReputation(registry, '', transport=httpx.MockTransport(lambda request: requests.append(request)))
    assert checker.check(site['id'])['status'] == 'unconfigured'
    assert checker.check('default')['status'] == 'unavailable'
    assert requests == []


def test_clean_requires_both_root_urls_and_uses_fixed_google_endpoint(tmp_path):
    registry, site, checker, requests = fixture(tmp_path)
    result = checker.check(site['id'])
    assert result['status'] == 'clean'
    assert result['checked_at'] is not None
    assert result['expires_at'] > result['checked_at']
    assert {parse_qs(request.url.query.decode())['uri'][0] for request in requests} == {'https://example.com/', 'http://example.com/'}
    for request in requests:
        assert request.method == 'GET'
        assert str(request.url).startswith('https://webrisk.googleapis.com/v1/uris:search?')
        params = parse_qs(request.url.query.decode())
        assert params['threatTypes'] == ['MALWARE', 'SOCIAL_ENGINEERING', 'UNWANTED_SOFTWARE']
        assert 'key' not in params
        assert request.headers['x-goog-api-key'] == 'test-key'
    assert checker.check(site['id'])['status'] == 'clean'
    assert len(requests) == 2
    assert GoogleReputation(registry, 'test-key').get(site['id'])['status'] == 'clean'


def test_empty_threat_object_is_a_clean_google_response(tmp_path):
    _, site, checker, _ = fixture(
        tmp_path, handler=lambda request: httpx.Response(200, json={'threat': {}}))
    assert checker.check(site['id'])['status'] == 'clean'


def test_fresh_flagged_survives_other_url_error_and_is_not_refetched(tmp_path):
    expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace('+00:00', 'Z')

    def respond(request):
        if parse_qs(request.url.query.decode())['uri'][0].startswith('https:'):
            return httpx.Response(200, json={'threat': {'threatTypes': ['MALWARE'], 'expireTime': expiry}})
        return httpx.Response(503, json={'error': {'message': 'test-key secret'}})

    registry, site, checker, requests = fixture(tmp_path, handler=respond)
    result = checker.check(site['id'])
    assert result['status'] == 'flagged'
    assert result['threats'] == ['MALWARE']
    assert len(requests) == 2
    assert checker.check(site['id'])['status'] == 'flagged'
    assert len(requests) == 2


def test_partial_failure_or_malformed_json_is_never_clean_and_never_echoes_secret(tmp_path):
    def respond(request):
        if parse_qs(request.url.query.decode())['uri'][0].startswith('https:'):
            return httpx.Response(200, json={})
        return httpx.Response(200, content=b'{"error":"test-key"}')

    _, site, checker, requests = fixture(tmp_path, handler=respond)
    result = checker.check(site['id'])
    assert result['status'] == 'error'
    assert 'test-key' not in json.dumps(result)
    assert checker.check(site['id'])['status'] == 'error'
    assert len(requests) == 2


def test_simultaneous_checks_for_one_site_do_not_duplicate_requests(tmp_path):
    started, release = Event(), Event()

    def respond(request):
        started.set()
        assert release.wait(5)
        return httpx.Response(200, json={})

    _, site, checker, requests = fixture(tmp_path, handler=respond)
    thread = Thread(target=lambda: checker.check(site['id']))
    thread.start()
    try:
        assert started.wait(5)
        assert checker.check(site['id'])['status'] == 'unchecked'
    finally:
        release.set()
        thread.join(5)
    assert checker.get(site['id'])['status'] == 'clean'
    assert len(requests) == 2


def test_expired_flagged_result_is_never_presented_as_current(tmp_path, monkeypatch):
    import ablab.reputation as reputation
    now = 1000.0
    monkeypatch.setattr(reputation.time, 'time', lambda: now)
    expiry = datetime.fromtimestamp(now + 60, timezone.utc).isoformat().replace('+00:00', 'Z')
    _, site, checker, _ = fixture(tmp_path, handler=lambda request: httpx.Response(200, json={
        'threat': {'threatTypes': ['MALWARE'], 'expireTime': expiry}}))
    assert checker.check(site['id'])['status'] == 'flagged'
    now = 1061.0
    stale = checker.get(site['id'])
    assert stale['status'] == 'expired'
    assert stale['threats'] == []


def test_one_expired_match_does_not_hide_or_extend_another_fresh_match(tmp_path, monkeypatch):
    import ablab.reputation as reputation
    now = 1000.0
    monkeypatch.setattr(reputation.time, 'time', lambda: now)

    def respond(request):
        secure = parse_qs(request.url.query.decode())['uri'][0].startswith('https:')
        expiry = datetime.fromtimestamp(now + (60 if secure else 3600), timezone.utc).isoformat().replace('+00:00', 'Z')
        return httpx.Response(200, json={'threat': {
            'threatTypes': ['MALWARE' if secure else 'SOCIAL_ENGINEERING'], 'expireTime': expiry}})

    _, site, checker, _ = fixture(tmp_path, handler=respond)
    assert checker.check(site['id'])['threats'] == ['MALWARE', 'SOCIAL_ENGINEERING']
    now = 1061.0
    current = checker.get(site['id'])
    assert current['status'] == 'flagged'
    assert current['threats'] == ['SOCIAL_ENGINEERING']


def test_clean_snapshot_expires_after_five_minutes(tmp_path, monkeypatch):
    import ablab.reputation as reputation
    now = 1000.0
    monkeypatch.setattr(reputation.time, 'time', lambda: now)
    _, site, checker, requests = fixture(tmp_path)
    assert checker.check(site['id'])['status'] == 'clean'
    assert len(requests) == 2
    now = 1301.0
    assert checker.get(site['id'])['status'] == 'expired'
    assert checker.check(site['id'])['status'] == 'clean'
    assert len(requests) == 4


def test_failed_check_retries_only_after_one_minute(tmp_path, monkeypatch):
    import ablab.reputation as reputation
    now = 1000.0
    monkeypatch.setattr(reputation.time, 'time', lambda: now)
    _, site, checker, requests = fixture(
        tmp_path, handler=lambda request: httpx.Response(503, json={'error': 'unavailable'}))
    assert checker.check(site['id'])['status'] == 'error'
    now = 1059.0
    assert checker.check(site['id'])['status'] == 'error'
    assert len(requests) == 2
    now = 1060.0
    assert checker.check(site['id'])['status'] == 'error'
    assert len(requests) == 4


def test_empty_default_domain_is_unavailable_even_with_key(tmp_path):
    registry, _, checker, requests = fixture(tmp_path)
    assert checker.check('default')['status'] == 'unavailable'
    assert requests == []


def test_admin_catalog_and_refresh_route_are_authenticated_and_csrf_protected(tmp_path, monkeypatch):
    monkeypatch.setenv('AB_GOOGLE_WEB_RISK_KEY', 'test-key')
    deployment = Deployment('https://admin.example.com', 'https://target.example.com')
    from ablab.store import Store
    from ablab.auth import Auth
    Auth(Store(tmp_path)).set_password('admin', 'strong password')
    app = create_admin(tmp_path, deployment=deployment)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={})

    app.state.reputation.transport = httpx.MockTransport(respond)
    site = app.state.registry.add('example.com')
    client = TestClient(app, base_url=deployment.admin_origin, client=('127.0.0.1', 1000))
    client.headers.update({'X-Real-IP': '203.0.113.9', 'X-Forwarded-Proto': 'https', 'Origin': deployment.admin_origin})
    assert client.get('/api/sites').status_code == 401
    assert client.post(f"/api/sites/{site['id']}/reputation/check").status_code == 401
    login = client.post('/api/login', json={'username': 'admin', 'password': 'strong password'}, headers={'X-CSRF-Token': app.state.csrf})
    assert login.status_code == 200
    assert client.post(f"/api/sites/{site['id']}/reputation/check").status_code == 403
    client.headers['X-CSRF-Token'] = login.json()['csrf']
    catalog = client.get('/api/sites').json()
    assert catalog['google_reputation_configured'] is True
    assert next(row for row in catalog['sites'] if row['id'] == site['id'])['google_reputation']['status'] == 'unchecked'
    assert requests == []
    assert client.post(f"/api/sites/{site['id']}/reputation/check").json()['result']['status'] == 'clean'
    assert next(row for row in client.get('/api/sites').json()['sites'] if row['id'] == site['id'])['google_reputation']['status'] == 'clean'
    assert len(requests) == 2
