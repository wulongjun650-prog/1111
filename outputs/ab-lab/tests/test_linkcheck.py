"""Synthetic destination checks; never request or join a real invitation."""
import io
import json
import socket
import ssl
import time
from threading import Event

import pytest


PUBLIC_IP = '8.8.8.8'


def checker_with(responses, addresses=None):
    from ablab.linkcheck import LinkChecker
    calls, lookups = [], []

    def resolve(host, port):
        lookups.append((host, port))
        return addresses if addresses is not None else [PUBLIC_IP]

    def fetch(url, ip, deadline):
        calls.append((url, ip, deadline))
        answer = responses[len(calls) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return LinkChecker(resolver=resolve, fetch=fetch), calls, lookups


def test_public_page_reports_connection_success_with_only_result_fields():
    checker, calls, lookups = checker_with([(200, {'Set-Cookie': 'secret-cookie'}, b'private page text')])
    result = checker.check('https://example.com/path?q=1#section')
    assert result['status'] == 'normal'
    assert result['platform'] == '网站'
    assert '连接' in result['detail']
    assert result['http_status'] == 200
    assert result['final_url'] == 'https://example.com/path?q=1#section'
    assert isinstance(result['checked_at'], float)
    assert set(result) == {'status', 'platform', 'detail', 'checked_at', 'http_status', 'final_url'}
    assert 'secret-cookie' not in json.dumps(result)
    assert 'private page text' not in json.dumps(result)
    assert lookups == [('example.com', 443)]
    assert calls[0][1] == PUBLIC_IP


@pytest.mark.parametrize('url', [
    'http://localhost/', 'http://a.localhost/', 'http://127.0.0.1/',
    'http://10.0.0.1/', 'https://169.254.169.254/', 'http://192.168.1.1/',
    'http://100.64.0.1/', 'http://0.0.0.0/', 'http://224.0.0.1/',
    'http://[::1]/', 'http://[::ffff:127.0.0.1]/', 'http://[fe80::1]/',
    'http://[2001:db8::1]/', 'http://8.8.8.8:8080/',
    'https://user:pass@example.com/', 'https://@example.com/',
    'https://example.com\\@127.0.0.1/', 'https://example.com/\r\nX-Test: yes',
    'https://exa\tmple.com/', 'https://example.com:abc/',
    'https://example.com/%0d%0a/', 'https://%31%32%37.0.0.1/',
    'https://example.com:0/', 'http://[fe80::1%25eth0]/',
])
def test_unsafe_destinations_never_resolve_or_fetch(url):
    from ablab.linkcheck import normalize_http_url
    checker, calls, lookups = checker_with([])
    with pytest.raises(ValueError):
        normalize_http_url(url)
    assert checker.check(url)['status'] == 'unknown'
    assert calls == [] and lookups == []


@pytest.mark.parametrize('url, platform', [
    ('whatsapp://chat?code=abc', 'WhatsApp'), ('bandapp://band/123', 'BAND'),
    ('file:///etc/passwd', '网站'), ('javascript:alert(1)', '网站'),
])
def test_app_schemes_stay_unknown_without_network_access(url, platform):
    checker, calls, lookups = checker_with([])
    result = checker.check(url)
    assert result['status'] == 'unknown' and result['platform'] == platform
    assert calls == [] and lookups == []


@pytest.mark.parametrize('addresses', [
    [PUBLIC_IP, '127.0.0.1'], [PUBLIC_IP, '10.1.2.3'],
    [PUBLIC_IP, '::1'], [PUBLIC_IP, '169.254.1.1'], ['invalid-ip'], [],
])
def test_every_dns_answer_must_be_public_before_fetch(addresses):
    checker, calls, _ = checker_with([], addresses=addresses)
    assert checker.check('https://example.com/')['status'] == 'unknown'
    assert calls == []


@pytest.mark.parametrize('error, expected', [
    (socket.gaierror(socket.EAI_NONAME, 'secret dns failure'), 'abnormal'),
    (socket.gaierror(socket.EAI_AGAIN, 'secret dns failure'), 'unknown'),
])
def test_dns_nonexistence_differs_from_temporary_failure(error, expected):
    from ablab.linkcheck import LinkChecker
    calls = []

    def resolve(host, port):
        raise error

    result = LinkChecker(resolver=resolve, fetch=lambda *args: calls.append(args)).check('https://example.com/')
    assert result['status'] == expected
    assert result['http_status'] is None
    assert 'secret' not in json.dumps(result) and calls == []


def test_public_literal_address_is_pinned_without_dns_lookup():
    checker, calls, lookups = checker_with([(200, {}, b'ok')])
    assert checker.check('https://8.8.8.8/')['status'] == 'normal'
    assert lookups == [] and calls[0][1] == PUBLIC_IP


@pytest.mark.parametrize('location', [
    'http://127.0.0.1/private', 'http://169.254.169.254/metadata',
    'https://example.com:8443/', '//user:secret@example.com/',
    'file:///private', 'https://example.com/\r\nInjected: secret',
])
def test_redirect_destination_is_validated_before_another_request(location):
    checker, calls, _ = checker_with([(302, {'Location': location}, b'')])
    assert checker.check('https://example.com/')['status'] == 'unknown'
    assert len(calls) == 1


def test_redirect_resolves_new_host_and_uses_its_vetted_address():
    from ablab.linkcheck import LinkChecker
    calls, lookups = [], []

    def resolve(host, port):
        lookups.append(host)
        return [PUBLIC_IP] if host == 'example.com' else ['1.1.1.1']

    def fetch(url, ip, deadline):
        calls.append((url, ip))
        return (302, {'Location': 'https://other.example/next'}, b'') if len(calls) == 1 else (200, {}, b'ok')

    result = LinkChecker(resolver=resolve, fetch=fetch).check('https://example.com/')
    assert result['status'] == 'normal' and result['final_url'] == 'https://other.example/next'
    assert calls == [('https://example.com/', PUBLIC_IP), ('https://other.example/next', '1.1.1.1')]
    assert lookups == ['example.com', 'other.example']


def test_redirect_to_mixed_private_dns_is_blocked():
    from ablab.linkcheck import LinkChecker
    calls = []

    def fetch(*args):
        calls.append(args)
        return 302, {'Location': 'https://other.example/'}, b''

    checker = LinkChecker(resolver=lambda host, port: [PUBLIC_IP] if host == 'example.com' else [PUBLIC_IP, '10.0.0.1'], fetch=fetch)
    assert checker.check('https://example.com/')['status'] == 'unknown' and len(calls) == 1


def test_redirect_budget_allows_three_hops_and_stops_the_fourth():
    checker, calls, _ = checker_with([(302, {'Location': '/hop' + str(i)}, b'') for i in range(5)])
    result = checker.check('https://example.com/')
    assert result['status'] == 'unknown' and len(calls) == 4
    assert all(call[2] == calls[0][2] for call in calls)


@pytest.mark.parametrize('status, expected', [
    (204, 'normal'), (404, 'abnormal'), (410, 'abnormal'),
    (401, 'unknown'), (403, 'unknown'), (429, 'unknown'), (500, 'unknown'), (503, 'unknown'),
])
def test_http_failure_mapping_does_not_call_transient_failures_broken(status, expected):
    checker, _, _ = checker_with([(status, {}, b'private diagnostic text')])
    result = checker.check('https://example.com/')
    assert result['status'] == expected and result['http_status'] == status
    assert 'diagnostic' not in json.dumps(result)


@pytest.mark.parametrize('error', [TimeoutError('secret timeout'), ssl.SSLError('secret tls'), OSError('secret network')])
def test_network_uncertainty_is_unknown_and_never_echoes_exception(error):
    checker, _, _ = checker_with([error])
    result = checker.check('https://example.com/')
    assert result['status'] == 'unknown' and 'secret' not in json.dumps(result)


@pytest.mark.parametrize('url, platform', [
    ('https://chat.whatsapp.com/AbCdEfGhIjKlMnOpQrStUv', 'WhatsApp'),
    ('https://wa.me/123456789', 'WhatsApp'), ('https://api.whatsapp.com/send?phone=123', 'WhatsApp'),
    ('https://band.us/n/a123abc', 'BAND'), ('https://www.band.us/n/a123abc', 'BAND'),
])
def test_platform_http_200_alone_never_proves_an_invite_or_registered_phone(url, platform):
    checker, _, _ = checker_with([(200, {}, b'<html><title>Welcome</title><p>Download our app</p></html>')])
    result = checker.check(url)
    assert result['status'] == 'unknown' and result['platform'] == platform


@pytest.mark.parametrize('host', ['band.us.evil.example', 'notband.us', 'whatsapp.com.evil.example', 'notwhatsapp.com', 'wa.me.evil.example'])
def test_official_platform_names_require_a_hostname_boundary(host):
    checker, _, _ = checker_with([(200, {}, b'ok')])
    result = checker.check('https://' + host + '/')
    assert result['platform'] == '网站' and result['status'] == 'normal'


def test_group_invite_redirect_to_generic_page_stays_unknown():
    checker, _, _ = checker_with([(302, {'Location': 'https://example.com/'}, b''), (200, {}, b'ok')])
    result = checker.check('https://chat.whatsapp.com/AbCdEfGhIjKlMnOpQrStUv')
    assert result['platform'] == 'WhatsApp' and result['status'] == 'unknown'


@pytest.mark.parametrize('body', [
    b'<h1>This invite link is not valid</h1>', b'<p>This invite link has been reset.</p>',
    b'<div>This invite link has expired.</div>',
])
def test_explicit_official_invalid_whatsapp_invite_is_abnormal(body):
    checker, _, _ = checker_with([(200, {}, body)])
    result = checker.check('https://chat.whatsapp.com/AbCdEfGhIjKlMnOpQrStUv')
    assert result['status'] == 'abnormal'


@pytest.mark.parametrize('url, body', [
    ('https://chat.whatsapp.com/AbCdEfGhIjKlMnOpQrStUv',
     b"<p>You can't join the group or community because the invite link was reset</p>"),
    ('https://band.us/n/a123abc', b'<h1>This invitation has expired</h1>'),
])
def test_documented_official_reset_and_expired_invite_sentences_are_abnormal(url, body):
    checker, _, _ = checker_with([(200, {}, body)])
    assert checker.check(url)['status'] == 'abnormal'


def test_documented_reset_sentence_accepts_html_typographic_apostrophe():
    body = '<p>You can’t join the group or community because the invite link was reset</p>'.encode()
    checker, _, _ = checker_with([(200, {}, body)])
    assert checker.check('https://chat.whatsapp.com/AbCdEfGhIjKlMnOpQrStUv')['status'] == 'abnormal'


def test_band_private_group_authorization_is_uncertainty_not_invalidity():
    checker, _, _ = checker_with([(200, {}, b'<h1>You are not authorized.</h1>')])
    assert checker.check('https://band.us/n/a123abc')['status'] == 'unknown'


@pytest.mark.parametrize('body', [
    b'<p>Learn how to reset an invite link when it is invalid.</p>',
    b'<script>const message="This invite link is not valid";</script><h1>WhatsApp</h1>',
    b'<p>FAQ: What if this invite link has expired?</p>',
])
def test_incidental_error_words_and_javascript_do_not_prove_invalid_invite(body):
    checker, _, _ = checker_with([(200, {}, body)])
    assert checker.check('https://chat.whatsapp.com/AbCdEfGhIjKlMnOpQrStUv')['status'] == 'unknown'


def test_whatsapp_named_group_with_matching_join_action_is_positive_invite_evidence():
    code = 'AbCdEfGhIjKlMnOpQrStUv'
    body = ('<meta property="og:title" content="Friday Friends"><h2>WhatsApp Group Invite</h2>'
            '<a href="whatsapp://chat?code=' + code + '">Join group</a>').encode()
    checker, _, _ = checker_with([(200, {}, body)])
    result = checker.check('https://chat.whatsapp.com/' + code)
    assert result['status'] == 'normal' and '邀请页面' in result['detail']


def test_generic_invite_title_or_different_join_code_is_not_positive_evidence():
    for title, code in [('WhatsApp Group Invite', 'AbCdEfGhIjKlMnOpQrStUv'), ('Friends', 'different-code')]:
        body = ('<meta property="og:title" content="' + title + '"><h2>WhatsApp Group Invite</h2>'
                '<a href="whatsapp://chat?code=' + code + '">Join group</a>').encode()
        checker, _, _ = checker_with([(200, {}, body)])
        assert checker.check('https://chat.whatsapp.com/AbCdEfGhIjKlMnOpQrStUv')['status'] == 'unknown'


@pytest.mark.parametrize('body', [b'<h1>Verify you are human</h1>', b'<h1>Sign in to continue</h1>', b'<title>Just a moment...</title>'])
def test_anti_bot_or_login_page_is_unknown_even_with_http_200(body):
    checker, _, _ = checker_with([(200, {}, body)])
    assert checker.check('https://example.com/')['status'] == 'unknown'


@pytest.mark.parametrize('body', [
    b'<form><input type="password" name="password"></form>',
    b'<div class="h-captcha" data-sitekey="public-key"></div>',
    b'<iframe src="https://www.google.com/recaptcha/api2/anchor"></iframe>',
    b'<h1>Internal Server Error</h1>', b'<h1>Service Unavailable</h1>',
])
def test_login_captcha_markup_and_error_pages_do_not_report_connection_normal(body):
    checker, _, _ = checker_with([(200, {}, body)])
    assert checker.check('https://example.com/')['status'] == 'unknown'


@pytest.mark.parametrize('attrs', ['hidden', 'aria-hidden="true"', 'style="display: none"'])
def test_hidden_localized_error_copy_is_not_an_explicit_invite_failure(attrs):
    body = ('<div ' + attrs + '><p>This invitation has expired</p></div><h1>BAND</h1>').encode()
    checker, _, _ = checker_with([(200, {}, body)])
    assert checker.check('https://band.us/n/a123abc')['status'] == 'unknown'


def test_valueless_html_attributes_never_crash_the_checker():
    body = b'<input type><meta property="og:title" content><div style aria-hidden>WhatsApp</div>'
    checker, _, _ = checker_with([(200, {}, body)])
    assert checker.check('https://chat.whatsapp.com/AbCdEfGhIjKlMnOpQrStUv')['status'] == 'unknown'


def test_adversarial_html_nesting_is_bounded_and_stays_unknown():
    checker, _, _ = checker_with([(200, {}, b'<div>' * 1000 + b'</unknown>' * 2000)])
    start = time.monotonic()
    assert checker.check('https://example.com/')['status'] == 'unknown'
    assert time.monotonic() - start < 0.5


def test_oversized_or_compressed_response_stays_unknown():
    for headers, body in [({}, b'x' * (512 * 1024 + 1)), ({'Content-Encoding': 'gzip'}, b'opaque compressed response')]:
        checker, _, _ = checker_with([(200, headers, body)])
        assert checker.check('https://example.com/')['status'] == 'unknown'


def test_dns_and_fetch_share_one_bounded_deadline(monkeypatch):
    from ablab import linkcheck
    ticks = [100.0]
    calls = []
    monkeypatch.setattr(linkcheck.time, 'monotonic', lambda: ticks[0])

    def resolve(host, port):
        ticks[0] = 110.0
        return [PUBLIC_IP]

    checker = linkcheck.LinkChecker(resolver=resolve, fetch=lambda *args: calls.append(args))
    assert checker.check('https://example.com/')['status'] == 'unknown' and calls == []


def test_slow_dns_returns_at_deadline_without_waiting_for_resolver(monkeypatch):
    from ablab import linkcheck
    release = Event()
    monkeypatch.setattr(linkcheck, 'TIMEOUT', 0.03)

    def resolve(host, port):
        release.wait(1)
        return [PUBLIC_IP]

    start = time.monotonic()
    try:
        result = linkcheck.LinkChecker(resolver=resolve, fetch=lambda *args: pytest.fail('DNS timed out')).check('https://example.com/')
        assert result['status'] == 'unknown' and time.monotonic() - start < 0.5
    finally:
        release.set()


def test_slow_response_returns_at_deadline_without_waiting_for_fetch(monkeypatch):
    from ablab import linkcheck
    release = Event()
    monkeypatch.setattr(linkcheck, 'TIMEOUT', 0.03)

    def fetch(*args):
        release.wait(1)
        return 200, {}, b'ok'

    start = time.monotonic()
    try:
        result = linkcheck.LinkChecker(resolver=lambda host, port: [PUBLIC_IP], fetch=fetch).check('https://example.com/')
        assert result['status'] == 'unknown' and time.monotonic() - start < 0.5
    finally:
        release.set()


def test_dns_public_ipv6_scope_identifier_is_never_used_for_connection():
    checker, calls, _ = checker_with([], addresses=['2606:4700:4700::1111%eth0'])
    assert checker.check('https://example.com/')['status'] == 'unknown' and calls == []


def test_default_transport_pins_ip_preserves_tls_sni_host_and_sends_only_get(monkeypatch):
    from ablab import linkcheck
    events = []

    class Stream:
        def __init__(self):
            self.body = io.BytesIO(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: text/plain\r\n\r\nok')

        def settimeout(self, timeout):
            events.append(('timeout', timeout))

        def sendall(self, data):
            events.append(data)

        def makefile(self, mode):
            return self.body

        def shutdown(self, how):
            events.append('shutdown')

        def close(self):
            events.append('close')

    stream = Stream()

    class Context:
        def wrap_socket(self, raw, server_hostname):
            assert raw is stream
            events.append(('sni', server_hostname))
            return stream

    monkeypatch.setattr(linkcheck.socket, 'create_connection', lambda address, timeout: (events.append(('connect', address)) or stream))
    monkeypatch.setattr(linkcheck.ssl, 'create_default_context', lambda: Context())
    checker = linkcheck.LinkChecker(resolver=lambda host, port: [PUBLIC_IP])
    result = checker.check('https://example.com/path?q=1')
    assert result['status'] == 'normal'
    assert ('connect', (PUBLIC_IP, 443)) in events and ('sni', 'example.com') in events
    request = b''.join(item for item in events if isinstance(item, bytes))
    assert request.startswith(b'GET /path?q=1 HTTP/1.1\r\n')
    assert b'Host: example.com\r\n' in request and b'Accept-Encoding: identity\r\n' in request
    assert b'Cookie:' not in request and b'Authorization:' not in request and b'Proxy-' not in request
    assert 'close' in events


def test_default_transport_stops_reading_at_response_limit(monkeypatch):
    from ablab import linkcheck
    reads = []

    class Response:
        status = 200

        def __init__(self, stream, method):
            assert method == 'GET'

        def begin(self):
            pass

        def getheaders(self):
            return []

        def read(self, amount):
            reads.append(amount)
            return b'x' * amount

        def close(self):
            pass

    class Stream:
        def sendall(self, data):
            pass

        def settimeout(self, timeout):
            pass

        def shutdown(self, how):
            pass

        def close(self):
            pass

    monkeypatch.setattr(linkcheck.socket, 'create_connection', lambda address, timeout: Stream())
    monkeypatch.setattr(linkcheck.http.client, 'HTTPResponse', Response)
    result = linkcheck.LinkChecker(resolver=lambda host, port: [PUBLIC_IP]).check('http://example.com/')
    assert result['status'] == 'unknown'
    assert sum(reads) <= 512 * 1024 + 1


def test_default_transport_aborts_a_slow_response_socket_at_deadline(monkeypatch):
    from ablab import linkcheck
    release, closed = Event(), Event()
    monkeypatch.setattr(linkcheck, 'TIMEOUT', 0.03)

    class Stream:
        def sendall(self, data):
            pass

        def settimeout(self, timeout):
            pass

        def shutdown(self, how):
            release.set()

        def close(self):
            closed.set()

    class Response:
        status = 200

        def __init__(self, stream, method):
            pass

        def begin(self):
            pass

        def getheaders(self):
            return []

        def read(self, amount):
            assert release.wait(1)
            return b'ok'

        def close(self):
            pass

    monkeypatch.setattr(linkcheck.socket, 'create_connection', lambda address, timeout: Stream())
    monkeypatch.setattr(linkcheck.http.client, 'HTTPResponse', Response)
    result = linkcheck.LinkChecker(resolver=lambda host, port: [PUBLIC_IP]).check('http://example.com/')
    assert result['status'] == 'unknown'
    assert closed.wait(0.5)
