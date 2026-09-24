from hashlib import sha256
from pathlib import Path

import pytest

from ablab.provisioning import ProvisioningError
from test_panel_sites import IDENTITY


DIGEST = 'd' * 64
PEER = b'presented-leaf-certificate'
BODY = ('{"schema":1,"site_id":"' + IDENTITY['site_id']
        + '","domain":"' + IDENTITY['domain'] + '"}').encode()


class Versions:
    def __init__(self, fingerprint=sha256(PEER).hexdigest()):
        self.fingerprint = fingerprint
        self.calls = []

    def load(self, identity, panel_id, digest):
        self.calls.append((identity, panel_id, digest))
        certificate = type('Certificate', (), {'fingerprint': self.fingerprint})()
        return Path('/private/' + digest), certificate


def response(*, peer=PEER, status=200, headers=None, body=BODY):
    return peer, status, headers or [('Content-Type', 'application/json'),
                                     ('Cache-Control', 'private, no-store')], body


def test_acceptance_binds_fixed_ip_sni_host_fingerprint_and_route_identity():
    from ablab.certificate_acceptance import CertificateAcceptance, TlsRouteProbe
    calls = []

    def transport(ip, domain, path, timeout, limit):
        calls.append((ip, domain, path, timeout, limit))
        return response()

    versions = Versions()
    probe = TlsRouteProbe('8.8.8.8', transport=transport)
    acceptance = CertificateAcceptance(versions, probe)
    assert acceptance.verify(IDENTITY, 22, DIGEST, lambda: True) == {
        'https': True, 'route': True, 'renewal': False}
    assert versions.calls == [(IDENTITY, 22, DIGEST)]
    assert calls == [('8.8.8.8', IDENTITY['domain'],
                      '/.well-known/ab-lab-route/' + IDENTITY['site_id'], 10, 4096)]


@pytest.mark.parametrize('result', [
    response(peer=b'wrong'),
    response(status=301),
    response(headers=[('Content-Type', 'text/html'), ('Cache-Control', 'no-store')]),
    response(headers=[('Content-Type', 'application/json'), ('Cache-Control', 'public')]),
    response(headers=[('Content-Type', 'application/json'), ('Content-Type', 'application/json'),
                      ('Cache-Control', 'no-store')]),
    response(body=b'{}'),
    response(body=b'x' * 4097),
])
def test_mismatched_tls_or_route_evidence_fails_closed(result):
    from ablab.certificate_acceptance import CertificateAcceptance, TlsRouteProbe
    acceptance = CertificateAcceptance(
        Versions(), TlsRouteProbe('8.8.8.8', transport=lambda *args: result))
    with pytest.raises(ProvisioningError):
        acceptance.verify(IDENTITY, 22, DIGEST, lambda: True)


def test_authorization_is_rechecked_after_slow_network_boundary():
    from ablab.certificate_acceptance import CertificateAcceptance, TlsRouteProbe
    allowed = True

    def transport(*args):
        nonlocal allowed
        allowed = False
        return response()

    acceptance = CertificateAcceptance(
        Versions(), TlsRouteProbe('8.8.8.8', transport=transport))
    with pytest.raises(ProvisioningError):
        acceptance.verify(IDENTITY, 22, DIGEST, lambda: allowed)


@pytest.mark.parametrize('value', [False, None, 0, 1, 'yes'])
def test_non_literal_authorization_never_loads_or_connects(value):
    from ablab.certificate_acceptance import CertificateAcceptance, TlsRouteProbe
    versions, calls = Versions(), []
    acceptance = CertificateAcceptance(
        versions, TlsRouteProbe('8.8.8.8', transport=lambda *args: calls.append(args)))
    with pytest.raises(ProvisioningError):
        acceptance.verify(IDENTITY, 22, DIGEST, lambda: value)
    assert versions.calls == [] and calls == []


def test_literal_renewal_checker_can_complete_the_third_proof():
    from ablab.certificate_acceptance import CertificateAcceptance, TlsRouteProbe

    class Renewal:
        def verify(self, identity, panel_id, digest, authorize):
            assert (identity, panel_id, digest) == (IDENTITY, 22, DIGEST)
            assert authorize() is True
            return True

    acceptance = CertificateAcceptance(
        Versions(), TlsRouteProbe('8.8.8.8', transport=lambda *args: response()),
        renewal=Renewal())
    assert acceptance.verify(IDENTITY, 22, DIGEST, lambda: True)['renewal'] is True


def test_default_transport_uses_system_context_sni_and_exact_host(monkeypatch):
    from ablab import certificate_acceptance as module
    events = []

    class Raw:
        def close(self):
            events.append('raw-close')

    class TLS:
        def sendall(self, request):
            events.append(request)

        def getpeercert(self, binary_form=False):
            assert binary_form is True
            return PEER

        def close(self):
            events.append('tls-close')

    class Context:
        def wrap_socket(self, raw, server_hostname):
            events.append(('sni', raw, server_hostname))
            return TLS()

    class HTTPResponse:
        status = 200

        def __init__(self, stream, method):
            assert isinstance(stream, TLS) and method == 'GET'
            self.headers = [('Content-Type', 'application/json'), ('Cache-Control', 'no-store')]

        def begin(self):
            events.append('begin')

        def getheaders(self):
            return self.headers

        def read(self, amount):
            assert amount == 4097
            return BODY

        def close(self):
            events.append('response-close')

    raw = Raw()
    monkeypatch.setattr(module.socket, 'create_connection',
                        lambda address, timeout: (events.append(('connect', address, timeout)) or raw))
    monkeypatch.setattr(module.ssl, 'create_default_context',
                        lambda: (events.append('default-context') or Context()))
    monkeypatch.setattr(module.http.client, 'HTTPResponse', HTTPResponse)

    result = module._https_exchange('8.8.8.8', IDENTITY['domain'],
                                    '/.well-known/ab-lab-route/' + IDENTITY['site_id'], 10, 4096)
    assert result == response(headers=[('Content-Type', 'application/json'),
                                       ('Cache-Control', 'no-store')])
    request = next(item for item in events if isinstance(item, bytes))
    assert request.startswith(b'GET /.well-known/ab-lab-route/')
    assert b'Host: new.example.com\r\n' in request
    assert ('connect', ('8.8.8.8', 443), 10) in events
    assert ('sni', raw, IDENTITY['domain']) in events
    assert events.count('default-context') == 1
