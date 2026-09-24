"""Read-only HTTPS and application-route acceptance.

This proves the certificate actually presented by the configured server IP and
an application-owned route. Renewal remains a separate proof and defaults to
false. No method in this module issues certificates or changes Nginx.
"""
from hashlib import sha256
import http.client
import json
import re
import socket
import ssl

from .panel_sites import validate_identity
from .provisioning import ProvisioningError, public_ipv4


def _https_exchange(server_ip, domain, path, timeout, limit):
    """Direct HTTPS exchange using system trust, domain SNI and a fixed IP."""
    context = ssl.create_default_context()
    raw = tls = response = None
    try:
        raw = socket.create_connection((server_ip, 443), timeout=timeout)
        tls = context.wrap_socket(raw, server_hostname=domain)
        raw = None  # The wrapped socket now owns it.
        request = (f'GET {path} HTTP/1.1\r\nHost: {domain}\r\n'
                   'Accept: application/json\r\nConnection: close\r\n\r\n').encode('ascii')
        tls.sendall(request)
        peer = tls.getpeercert(binary_form=True)
        response = http.client.HTTPResponse(tls, method='GET')
        response.begin()
        body = response.read(limit + 1)
        return peer, response.status, response.getheaders(), body
    finally:
        if response is not None:
            response.close()
        if tls is not None:
            tls.close()
        elif raw is not None:
            raw.close()


class TlsRouteProbe:
    def __init__(self, server_ip, *, transport=_https_exchange, timeout=10, limit=4096):
        self.server_ip = public_ipv4(server_ip)
        if type(timeout) is not int or not 1 <= timeout <= 30:
            raise ValueError('HTTPS 验收超时无效')
        if type(limit) is not int or not 512 <= limit <= 16384:
            raise ValueError('HTTPS 验收响应上限无效')
        self.transport, self.timeout, self.limit = transport, timeout, limit

    def verify(self, identity, fingerprint, authorize):
        validate_identity(identity)
        if not isinstance(fingerprint, str) or not re.fullmatch('[0-9a-f]{64}', fingerprint):
            raise ValueError('证书指纹无效')
        if authorize() is not True:
            raise ProvisioningError('HTTPS 验收已暂停或归属未确认')
        path = '/.well-known/ab-lab-route/' + identity['site_id']
        try:
            peer, status, headers, body = self.transport(
                self.server_ip, identity['domain'], path, self.timeout, self.limit)
            if authorize() is not True:
                raise ProvisioningError('HTTPS 验收期间站点已暂停或归属变化')
            if (not isinstance(peer, bytes) or sha256(peer).hexdigest() != fingerprint
                    or status != 200 or not isinstance(headers, (list, tuple))
                    or not isinstance(body, bytes) or len(body) > self.limit):
                raise ValueError('mismatched HTTPS evidence')
            critical = {}
            for name, value in headers:
                key = name.lower()
                if key in ('content-type', 'cache-control'):
                    if key in critical or not isinstance(value, str):
                        raise ValueError('duplicate or invalid header')
                    critical[key] = value
            if critical.get('content-type', '').lower() != 'application/json':
                raise ValueError('unexpected content type')
            directives = {item.strip().lower() for item in critical.get('cache-control', '').split(',')}
            if 'no-store' not in directives:
                raise ValueError('route proof may be cached')
            expected = dict(schema=1, site_id=identity['site_id'], domain=identity['domain'])
            if json.loads(body.decode('utf-8')) != expected:
                raise ValueError('wrong route identity')
            return True
        except ProvisioningError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException, ValueError, TypeError,
                UnicodeError, json.JSONDecodeError):
            raise ProvisioningError('HTTPS 或应用路由验收未通过') from None


class CertificateAcceptance:
    def __init__(self, versions, route_probe, *, renewal=None):
        self.versions, self.route_probe, self.renewal = versions, route_probe, renewal

    def verify(self, identity, panel_id, digest, authorize):
        validate_identity(identity)
        if type(panel_id) is not int or panel_id <= 0:
            raise ValueError('面板站点 ID 无效')
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('证书版本摘要无效')
        if authorize() is not True:
            raise ProvisioningError('证书验收已暂停或归属未确认')
        version, certificate = self.versions.load(identity, panel_id, digest)
        if version.name != digest or authorize() is not True:
            raise ProvisioningError('证书版本或站点归属在验收前发生变化')
        if self.route_probe.verify(identity, certificate.fingerprint, authorize) is not True:
            raise ProvisioningError('HTTPS 或应用路由验收未通过')
        renewal = False
        if self.renewal is not None:
            renewal = self.renewal.verify(identity, panel_id, digest, authorize) is True
            if authorize() is not True:
                raise ProvisioningError('续期验收期间站点已暂停或归属变化')
        return {'https': True, 'route': True, 'renewal': renewal}
