"""DNS inspection and read-only panel preflight. No automatic panel writes yet.

Contract gaps are explicit: this module cannot create sites, edit Nginx, or issue
certificates. Never promote a DNS match or static-version response to ACTIVE.
"""
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

import httpx

from .sites import normalize_domain


class ProvisioningError(ValueError):
    """Only locally authored, non-sensitive user messages belong here."""


def public_ipv4(value):
    address = ipaddress.IPv4Address(value)
    if not address.is_global or address.is_multicast or address.is_reserved:
        raise ValueError('服务器地址必须是公网单播 IPv4')
    return str(address)


def bounded_json(client, method, url, **kwargs):
    try:
        with client.stream(method, url, **kwargs) as response:
            response.raise_for_status()
            chunks, length = [], 0
            for chunk in response.iter_bytes():
                length += len(chunk)
                if length > 128 * 1024:
                    raise ProvisioningError('远程响应超过安全大小限制')
                chunks.append(chunk)
            result = json.loads(b''.join(chunks))
            if not isinstance(result, dict):
                raise ValueError('invalid response')
            return result
    except (httpx.HTTPError, ValueError, UnicodeError):
        raise ProvisioningError('远程服务不可用、超时或响应格式不兼容；请检查服务器网络与配置') from None


class DNSChecker:
    def __init__(self, transport=None):
        self.transport = transport

    def check(self, domain, expected_ip):
        domain, expected_ip = normalize_domain(domain), public_ipv4(expected_ip)
        records = {}
        # Fixed public DoH endpoint: domain is a query value, never a fetch URL.
        with httpx.Client(transport=self.transport, timeout=5, follow_redirects=False, trust_env=False) as client:
            for kind in (1, 28):
                data = bounded_json(client, 'GET', 'https://dns.google/resolve', params={
                    'name': domain, 'type': str(kind), 'edns_client_subnet': '0.0.0.0/0',
                })
                if type(data.get('Status')) is not int or data['Status'] != 0 or data.get('TC'):
                    raise ProvisioningError('公共 DNS 尚未返回完整有效记录，请检查解析并稍后重试')
                answers = data.get('Answer', [])
                if not isinstance(answers, list) or any(not isinstance(item, dict) for item in answers):
                    raise ProvisioningError('公共 DNS 响应格式不兼容')
                try:
                    addresses = [ipaddress.ip_address(item['data']) for item in answers if item.get('type') == kind]
                    if any(address.version != (4 if kind == 1 else 6) for address in addresses):
                        raise ValueError('wrong family')
                except (KeyError, ValueError, TypeError):
                    raise ProvisioningError('公共 DNS 返回无效地址') from None
                records[kind] = sorted({str(address) for address in addresses})
        if records[1] != [expected_ip]:
            raise ProvisioningError('A 记录尚未全部指向配置的服务器公网 IP；请移除冲突的 A 记录')
        if records[28]:
            raise ProvisioningError('检测到 AAAA 记录；当前接入仅配置 IPv4，请先移除 AAAA 记录')
        return records[1]


class PanelPreflight:
    """Only the documented v2 static-version capability; no write methods."""
    def __init__(self, address, key, transport=None):
        url = urlsplit(address)
        try:
            loopback = ipaddress.ip_address(url.hostname or '').is_loopback
        except ValueError:
            loopback = False
        if (not url.hostname or url.username or url.password or url.path or url.query or url.fragment
                or url.scheme not in ('http', 'https') or (url.scheme == 'http' and not loopback)):
            raise ValueError('面板需使用 HTTPS 根地址；仅本机回环 IP 可用 HTTP，不能携带账号或路径')
        if not isinstance(key, str) or not key:
            raise ValueError('面板 API 密钥未配置')
        self.address, self.transport = address, transport
        self._key_digest = hashlib.md5(key.encode()).hexdigest()

    @classmethod
    def from_local_config(cls, address, path=Path('/www/server/panel/config/api.json'), transport=None):
        """Read the installed panel's internal digest, only for exact loopback.

        Never reset keys, change permissions or decrypt/export the UI key.
        This must run in the privileged server process, not the web service.
        """
        instance = cls(address, 'local-config', transport)
        if urlsplit(address).hostname != '127.0.0.1':
            raise ValueError('本机面板配置只能用于 127.0.0.1，不能发送到其他地址')
        try:
            with Path(path).open('rb') as source:
                content = source.read(128 * 1024 + 1)
            if len(content) > 128 * 1024:
                raise ValueError('oversized')
            config = json.loads(content)
            if (not isinstance(config, dict) or config.get('open') not in (True, 1)
                    or config.get('limit_addr') != ['127.0.0.1']
                    or not isinstance(config.get('token'), str)
                    or not re.fullmatch('[a-f0-9]{32}', config['token'])):
                raise ValueError('invalid configuration')
        except (OSError, ValueError, UnicodeError):
            raise ProvisioningError('本机 API 配置不可读取或不兼容；需开启 API 并仅允许 127.0.0.1。未修改面板配置') from None
        instance._key_digest = config['token']
        return instance

    def _request(self, endpoint, action, fields=None):
        # Seconds + stored digest verified on the target aaPanel 8.0.4.
        timestamp = str(int(time.time()))
        token = hashlib.md5((timestamp + self._key_digest).encode()).hexdigest()
        with httpx.Client(transport=self.transport, timeout=10, follow_redirects=False, trust_env=False) as client:
            data = bounded_json(client, 'POST', self.address + endpoint,
                                params={'action': action},
                                data=dict(fields or {}, request_time=timestamp, request_token=token))
        if type(data.get('status')) is not int or data['status'] != 0:
            raise ProvisioningError('面板请求失败或响应格式不兼容；如已发出写请求须核对结果，不能盲目重试')
        return data.get('message')

    def check(self):
        versions = self._request('/v2/site', 'GetPHPVersion')
        if not isinstance(versions, list) or not any(isinstance(row, dict) and row.get('version') == '00' for row in versions):
            raise ProvisioningError('面板未返回已知静态站点能力；未执行任何写入')
        return {'static_available': True, 'automatic_writes_supported': False,
                'detail': '只读静态能力检查通过；反代、配置检查与证书续期契约尚未验证，自动写入保持禁用'}


def inspect_pending(registry, server_ip, checker=None):
    if server_ip:
        server_ip = public_ipv4(server_ip)
    checker = checker or DNSChecker()
    for site in registry.list():
        if site['id'] == 'default' or not site['enabled'] or site['stage'] in ('active', 'paused', 'unsupported') or site['next_attempt'] > time.time():
            continue
        site_id, generation = site['id'], site['generation']
        if not server_ip:
            registry.transition(site_id, generation, 'unconfigured', '服务器公网 IP 未配置；未创建宝塔站点或证书', 300)
            continue
        try:
            checker.check(site['domain'], server_ip)
        except ProvisioningError as error:
            registry.transition(site_id, generation, 'waiting_dns', str(error), min(3600, 60 * 2 ** min(site['attempts'], 6)))
            continue
        if registry.transition(site_id, generation, 'dns_verified', '公共 DNS 的 A 记录一致，未检测到 AAAA'):
            registry.transition(site_id, generation, 'unsupported', '解析条件已满足；面板自动写入适配尚未完成，未创建目录、站点或证书')
