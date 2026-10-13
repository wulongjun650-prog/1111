import hashlib
import json
import os
import socket
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from ablab.auth import Auth
from ablab.settings import Deployment
from ablab.speedcheck import SpeedChecker, host_is_public, normalize_target, parse_pagespeed, parse_probe
from ablab.store import Store
from ablab.web import create_admin


def psi_payload(score):
    def audit(value):
        return {'numericValue': value}

    return {
        'analysisUTCTimestamp': '2026-10-13T04:00:00.000Z',
        'lighthouseResult': {
            'categories': {'performance': {'score': score}},
            'audits': {
                'first-contentful-paint': audit(1100),
                'largest-contentful-paint': audit(2200),
                'total-blocking-time': audit(80),
                'cumulative-layout-shift': audit(0.021),
                'speed-index': audit(1800),
            },
        },
    }


def probe_payload(country, total, city='Seoul'):
    return {
        'id': 'meas1234',
        'type': 'http',
        'status': 'finished',
        'results': [{
            'probe': {'country': country, 'city': city, 'network': 'Korea Telecom', 'asn': 4766},
            'result': {
                'status': 'finished',
                'resolvedAddress': '93.184.216.34',
                'statusCode': 200,
                'timings': {'total': total, 'dns': 11.2, 'tcp': 22, 'tls': 33.5, 'firstByte': 90, 'download': 24},
                'headers': {'content-length': '1256'},
            },
        }],
    }


def scripted(handler):
    calls = []

    def transport(request):
        body = json.loads(request.content) if request.content else None
        calls.append({'method': request.method, 'host': request.url.host, 'params': dict(request.url.params), 'json': body})
        return handler(request, calls)

    return calls, httpx.MockTransport(transport)


def wait_job(client, job_id):
    deadline = time.time() + 5
    job = None
    while time.time() < deadline:
        job = client.get(f'/api/speedcheck/{job_id}').json()
        if job['status'] != 'running':
            return job
        time.sleep(0.01)
    return job


def local_client(tmp_path, transport):
    app = create_admin(tmp_path)
    app.state.speedcheck = SpeedChecker(transport=transport, sleeper=lambda _seconds: None)
    client = TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 9))
    client.headers.update({'Origin': 'http://127.0.0.1:8765', 'X-CSRF-Token': app.state.csrf})
    return client


def test_target_rejects_private_hosts_and_keeps_the_public_path():
    target = normalize_target('https://example.com/news?q=1')
    assert target == {'url': 'https://example.com/news?q=1', 'host': 'example.com', 'path': '/news', 'query': 'q=1', 'protocol': 'HTTPS', 'port': None}
    assert normalize_target('example.com')['url'] == 'https://example.com/'
    assert normalize_target('https://例子.com/a')['host'] == 'xn--fsqu00a.com'
    for raw in ('http://127.0.0.1/', 'http://10.0.0.1/', 'https://localhost/admin', 'http://169.254.169.254/', 'http://[::1]/', 'file:///etc/passwd', 'https://user:pass@example.com/', 'https://example.com:8080/'):
        with pytest.raises(ValueError):
            normalize_target(raw)


def test_pagespeed_score_is_a_percentage_and_a_foreign_probe_is_rejected():
    parsed = parse_pagespeed(psi_payload(0.91))
    assert parsed['score'] == 91
    assert parsed['fcp_ms'] == 1100
    assert parsed['cls'] == 0.021
    probe, error = parse_probe(probe_payload('KR', 180.2), 'KR', '韩国')
    assert error is None
    assert probe['city'] == 'Seoul' and probe['total_ms'] == 180.2 and probe['bytes'] == 1256
    missed, reason = parse_probe(probe_payload('US', 10, 'Ashburn'), 'KR', '韩国')
    assert missed is None and 'US' in reason and '韩国' in reason


def test_three_runs_use_the_selected_country_and_average_the_results(tmp_path, monkeypatch):
    monkeypatch.setenv('AB_PAGESPEED_API_KEY', 'psi-test-key')
    scores = [0.8, 0.9, 1]
    totals = [100, 200, 300]

    def handler(request, calls):
        if request.url.host == 'api.globalping.io' and request.method == 'POST':
            return httpx.Response(202, json={'id': f'meas{len(calls):04d}', 'probesCount': 1})
        if request.url.host == 'api.globalping.io':
            return httpx.Response(200, json=probe_payload('KR', totals.pop(0)))
        return httpx.Response(200, json=psi_payload(scores.pop(0)))

    calls, transport = scripted(handler)
    client = local_client(tmp_path, transport)
    page = client.get('/').text
    assert '网站测速' in page and '一键重测' in page and 'value="KR"' in page and '>3 次<' in page
    started = client.post('/api/speedcheck', json={'url': 'https://example.com/news?q=1', 'region': 'KR', 'repeats': 3, 'strategy': 'mobile'})
    assert started.status_code == 200, started.text
    job = wait_job(client, started.json()['id'])
    assert job['status'] == 'done'
    assert job['region_label'] == '韩国' and job['repeats'] == 3
    assert [run['index'] for run in job['runs']] == [1, 2, 3]
    assert [run['pagespeed']['score'] for run in job['runs']] == [80, 90, 100]
    assert [run['pagespeed']['source'] for run in job['runs']] == ['google', 'google', 'google']
    assert [run['probe']['country'] for run in job['runs']] == ['KR', 'KR', 'KR']
    assert job['summary']['score_avg'] == 90
    assert job['summary']['total_ms_avg'] == 200
    assert '不能指定香港或韩国' in job['pagespeed_note']
    assert 'psi-test-key' not in started.text and 'psi-test-key' not in json.dumps(job)
    posts = [item for item in calls if item['host'] == 'api.globalping.io' and item['method'] == 'POST']
    assert len(posts) == 3
    assert posts[0]['json']['target'] == 'example.com'
    assert posts[0]['json']['locations'] == [{'country': 'KR', 'limit': 1}]
    assert posts[0]['json']['measurementOptions']['request'] == {'method': 'GET', 'path': '/news', 'query': 'q=1'}
    assert posts[0]['json']['measurementOptions']['protocol'] == 'HTTPS'
    psi = [item for item in calls if item['host'] == 'www.googleapis.com']
    assert len(psi) == 3
    assert psi[0]['params']['url'] == 'https://example.com/news?q=1'
    assert psi[0]['params']['strategy'] == 'mobile'
    assert psi[0]['params']['key'] == 'psi-test-key'


def test_a_probe_outside_the_selected_country_is_not_counted(tmp_path):
    def handler(request, _calls):
        if request.url.host == 'api.globalping.io' and request.method == 'POST':
            return httpx.Response(202, json={'id': 'measabcd', 'probesCount': 1})
        if request.url.host == 'api.globalping.io':
            return httpx.Response(200, json=probe_payload('US', 10, 'Ashburn'))
        return httpx.Response(200, json=psi_payload(0.5))

    _calls, transport = scripted(handler)
    client = local_client(tmp_path, transport)
    started = client.post('/api/speedcheck', json={'url': 'https://example.com/', 'region': 'HK', 'repeats': 1, 'strategy': 'desktop'})
    job = wait_job(client, started.json()['id'])
    assert job['runs'][0]['probe'] is None
    assert 'US' in job['runs'][0]['probe_error']
    assert job['runs'][0]['pagespeed']['score'] == 50
    assert job['summary']['probe_count'] == 0
    assert job['summary']['score_avg'] == 50
    assert job['strategy'] == 'desktop'


def test_private_urls_and_unknown_regions_are_rejected(tmp_path):
    client = local_client(tmp_path, httpx.MockTransport(lambda request: httpx.Response(500)))
    for url in ('http://127.0.0.1/', 'http://10.1.1.1/', 'https://localhost/', 'file:///etc/passwd'):
        rejected = client.post('/api/speedcheck', json={'url': url, 'region': 'HK', 'repeats': 1, 'strategy': 'mobile'})
        assert rejected.status_code == 400, rejected.text
    assert client.post('/api/speedcheck', json={'url': 'https://example.com/', 'region': 'HK', 'repeats': 4, 'strategy': 'mobile'}).status_code == 422
    assert client.post('/api/speedcheck', json={'url': 'https://example.com/', 'region': 'CN', 'repeats': 1, 'strategy': 'mobile'}).status_code == 422


def test_private_addresses_are_refused_and_quota_falls_back_to_lighthouse(tmp_path):
    from ablab.speedcheck import _PublicProxy

    assert host_is_public('127.0.0.1') is False
    assert host_is_public('10.1.1.1') is False
    assert host_is_public('169.254.169.254') is False
    assert host_is_public('::1') is False
    proxy = _PublicProxy()
    try:
        for target in ('127.0.0.1:80', '169.254.169.254:80', '10.1.1.1:443'):
            sock = socket.create_connection(('127.0.0.1', proxy.port), timeout=2)
            sock.settimeout(2)
            sock.sendall(f'CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n'.encode())
            data = sock.recv(200)
            sock.close()
            assert data.startswith(b'HTTP/1.1 403'), data
    finally:
        proxy.close()

    def handler(request, _calls):
        if request.url.host == 'www.googleapis.com':
            return httpx.Response(429, json={'error': {'code': 429}})
        if request.method == 'POST':
            return httpx.Response(202, json={'id': 'measquota', 'probesCount': 1})
        return httpx.Response(200, json=probe_payload('HK', 40, 'Hong Kong'))

    def lighthouse(job):
        assert job['strategy'] == 'mobile'
        assert job['url'] == 'https://example.com/'
        return psi_payload(0.66), None

    _calls, transport = scripted(handler)
    app = create_admin(tmp_path)
    app.state.speedcheck = SpeedChecker(transport=transport, sleeper=lambda _seconds: None, lighthouse=lighthouse)
    client = TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 9))
    client.headers.update({'Origin': 'http://127.0.0.1:8765', 'X-CSRF-Token': app.state.csrf})
    started = client.post('/api/speedcheck', json={'url': 'https://example.com/', 'region': 'HK', 'repeats': 1, 'strategy': 'mobile'})
    job = wait_job(client, started.json()['id'])
    assert job['runs'][0]['pagespeed']['score'] == 66
    assert job['runs'][0]['pagespeed']['source'] == 'lighthouse'
    assert job['runs'][0]['probe']['country'] == 'HK'
    assert '本机 Lighthouse' in job['pagespeed_note']

    blocked = SpeedChecker(transport=transport, sleeper=lambda _seconds: None, lighthouse=False)
    app.state.speedcheck = blocked
    again = client.post('/api/speedcheck', json={'url': 'https://example.com/', 'region': 'HK', 'repeats': 1, 'strategy': 'mobile'})
    missed = wait_job(client, again.json()['id'])
    assert missed['runs'][0]['pagespeed'] is None
    assert '免费额度' in missed['runs'][0]['pagespeed_error']
    assert missed['runs'][0]['probe']['city'] == 'Hong Kong'


def test_a_second_check_waits_until_the_first_finishes(tmp_path):
    release = threading.Event()
    started = threading.Event()

    def handler(request, _calls):
        if request.url.host == 'api.globalping.io' and request.method == 'POST':
            started.set()
            release.wait(5)
            return httpx.Response(202, json={'id': 'measwait1', 'probesCount': 1})
        if request.url.host == 'api.globalping.io':
            return httpx.Response(200, json=probe_payload('HK', 40, 'Hong Kong'))
        return httpx.Response(200, json=psi_payload(0.7))

    _calls, transport = scripted(handler)
    client = local_client(tmp_path, transport)
    try:
        first = client.post('/api/speedcheck', json={'url': 'https://example.com/', 'region': 'HK', 'repeats': 1, 'strategy': 'mobile'})
        assert first.status_code == 200, first.text
        assert started.wait(2)
        second = client.post('/api/speedcheck', json={'url': 'https://example.com/', 'region': 'KR', 'repeats': 1, 'strategy': 'mobile'})
        assert second.status_code == 409
        assert second.json()['detail'] == '上一次检测还在进行'
    finally:
        release.set()
    assert wait_job(client, first.json()['id'])['status'] == 'done'


def test_login_is_required_and_another_account_cannot_read_the_job(tmp_path, monkeypatch):
    monkeypatch.setattr('ablab.auth.password_hash', lambda password, salt: hashlib.sha256((salt + password).encode()).hexdigest())

    def handler(request, _calls):
        if request.url.host == 'api.globalping.io' and request.method == 'POST':
            return httpx.Response(202, json={'id': 'measowner', 'probesCount': 1})
        if request.url.host == 'api.globalping.io':
            return httpx.Response(200, json=probe_payload('KR', 15))
        return httpx.Response(200, json=psi_payload(0.6))

    _calls, transport = scripted(handler)
    auth = Auth(Store(tmp_path))
    auth.set_password('admin', 'strong-password')
    auth.create_observer('watcher', 'strong-password')
    settings = Deployment('https://admin.example', 'https://target.example')
    app = create_admin(tmp_path, deployment=settings)
    app.state.speedcheck = SpeedChecker(transport=transport, sleeper=lambda _seconds: None)

    def login(username):
        client = TestClient(app, base_url=settings.admin_origin, client=('127.0.0.1', 4000))
        client.headers.update({'Origin': settings.admin_origin, 'X-Real-IP': '203.0.113.10', 'X-Forwarded-Proto': 'https', 'X-CSRF-Token': app.state.csrf})
        result = client.post('/api/login', json={'username': username, 'password': 'strong-password'})
        assert result.status_code == 200, result.text
        client.headers['X-CSRF-Token'] = result.json()['csrf']
        return client

    guest = TestClient(app, base_url=settings.admin_origin, client=('127.0.0.1', 4000))
    guest.headers.update({'Origin': settings.admin_origin, 'X-Real-IP': '203.0.113.9', 'X-Forwarded-Proto': 'https'})
    assert guest.post('/api/speedcheck', json={'url': 'https://example.com/', 'region': 'KR', 'repeats': 1, 'strategy': 'mobile'}).status_code == 401
    watcher = login('watcher')
    owner = login('admin')
    started = watcher.post('/api/speedcheck', json={'url': 'https://example.com/', 'region': 'KR', 'repeats': 1, 'strategy': 'mobile'})
    assert started.status_code == 200, started.text
    job_id = started.json()['id']
    assert wait_job(watcher, job_id)['status'] == 'done'
    hidden = owner.get(f'/api/speedcheck/{job_id}')
    assert hidden.status_code == 404
    assert watcher.get('/').text.count('data-tab="speed"') == 1
