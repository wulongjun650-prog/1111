"""Optional Cloudflare attach. The token stays on the server.

Unchecked domains keep the existing A-record path. A checked domain is created
in the same account as the template zone, proxied back to this server, and
receives that zone's cache and speed rules.
"""
import json
import re

import httpx

from .provisioning import ProvisioningError, public_ipv4
from .sites import normalize_domain


API = 'https://api.cloudflare.com/client/v4'
AUTH_CODES = {6003, 9106, 9109, 10000}
MISSING_CODES = {10003, 7003}
DUPLICATE_ZONE = 1061
OPTIMIZATION_SETTINGS = (
    'brotli', 'early_hints', 'http3', 'minify', 'rocket_loader', 'polish', 'mirage',
    'browser_cache_ttl', 'cache_level', 'always_online', 'websockets',
)
OPTIMIZATION_PHASES = ('http_request_cache_settings', 'http_config_settings')
RULE_KEYS = ('expression', 'action', 'action_parameters', 'description', 'enabled', 'logging')


class CloudflareError(ProvisioningError):
    def __init__(self, message, zone_id='', nameservers=(), code=0):
        super().__init__(message)
        self.zone_id = zone_id
        self.nameservers = tuple(nameservers)
        self.code = code


class Cloudflare:
    def __init__(self, token='', template='', origin_ip='', transport=None):
        self.token = token.strip() if isinstance(token, str) else ''
        self.template = template.strip().lower() if isinstance(template, str) else ''
        self.transport = transport
        try:
            self.origin_ip = public_ipv4(origin_ip) if origin_ip else ''
        except ValueError:
            self.origin_ip = ''

    @property
    def configured(self):
        return bool(self.token and self.template)

    @property
    def template_label(self):
        if not self.configured or re.fullmatch(r'[a-f0-9]{32}', self.template):
            return ''
        try:
            return normalize_domain(self.template)
        except ValueError:
            return ''

    def attach(self, domain):
        domain = normalize_domain(domain)
        if not self.configured:
            raise CloudflareError('服务器未配置 Cloudflare')
        if not self.origin_ip:
            raise CloudflareError('服务器公网 IP 未配置，无法把 Cloudflare 回源到本机')
        with self._client() as client:
            template = self._template_zone(client)
            if template['name'] == domain:
                raise CloudflareError('这条域名就是规则模板，不用再套一次')
            zone = self._ensure_zone(client, domain, template['account_id'])
            try:
                self._ensure_record(client, zone['id'], domain)
                copied, skipped = self._copy(client, template, zone['id'], domain)
            except CloudflareError as error:
                error.zone_id = error.zone_id or zone['id']
                error.nameservers = error.nameservers or zone['name_servers']
                raise
        status = 'active' if zone['status'] == 'active' else 'pending'
        return {
            'zone_id': zone['id'], 'nameservers': list(zone['name_servers']), 'status': status,
            'detail': _summary(copied, skipped, zone['name_servers'], status == 'active'),
        }

    def confirm(self, zone_id, domain):
        domain = normalize_domain(domain)
        if not self.origin_ip:
            raise CloudflareError('服务器公网 IP 未配置，无法核对 Cloudflare 回源')
        with self._client() as client:
            zone = _zone_view(self._call(client, 'GET', '/zones/' + _zone_id(zone_id)))
            if zone['name'] != domain:
                raise CloudflareError('Cloudflare 站点与登记域名不一致')
            record = self._exact_record(client, zone['id'], domain)
            if record is None or record.get('content') != self.origin_ip or record.get('proxied') is not True:
                raise CloudflareError('Cloudflare 回源记录还没有指向这台服务器')
        return {'active': zone['status'] == 'active', 'nameservers': list(zone['name_servers'])}

    def purge(self, zone_id, domain):
        domain = normalize_domain(domain)
        zone_id = _zone_id(zone_id)
        with self._client() as client:
            zone = _zone_view(self._call(client, 'GET', '/zones/' + zone_id))
            if zone['name'] != domain:
                raise CloudflareError('Cloudflare 站点与登记域名不一致')
            self._call(client, 'POST', f'/zones/{zone_id}/purge_cache', payload={'purge_everything': True})

    def _client(self):
        return httpx.Client(transport=self.transport, timeout=10, follow_redirects=False, trust_env=False)

    def _call(self, client, method, path, payload=None, params=None, missing_ok=False, soft=False):
        if not path.startswith('/') or '\n' in path or '\r' in path:
            raise CloudflareError('Cloudflare 请求无效')
        kwargs = {}
        if payload is not None:
            kwargs['json'] = payload
        if params is not None:
            kwargs['params'] = params
        try:
            with client.stream(method, API + path, headers={'Authorization': 'Bearer ' + self.token}, **kwargs) as response:
                chunks, length = [], 0
                for chunk in response.iter_bytes():
                    length += len(chunk)
                    if length > 512 * 1024:
                        raise CloudflareError('Cloudflare 响应超过安全大小限制')
                    chunks.append(chunk)
                status = response.status_code
        except CloudflareError:
            raise
        except httpx.HTTPError:
            raise CloudflareError('Cloudflare 暂时无法连接，请稍后重试') from None
        try:
            body = json.loads(b''.join(chunks) or b'{}')
        except ValueError:
            raise CloudflareError('Cloudflare 响应无法读取') from None
        if not isinstance(body, dict):
            raise CloudflareError('Cloudflare 响应无法读取')
        if missing_ok and (status == 404 or _code(body) in MISSING_CODES):
            return None
        if body.get('success') is not True:
            error = CloudflareError(*_safe_error(body, self.token))
            if soft and error.code not in AUTH_CODES:
                return None
            raise error
        return body.get('result')

    def _template_zone(self, client):
        if re.fullmatch(r'[a-f0-9]{32}', self.template):
            found = [self._call(client, 'GET', '/zones/' + self.template)]
        else:
            found = self._zones_named(client, normalize_domain(self.template))
        if len(found) != 1:
            raise CloudflareError('没有找到已有优化规则的 Cloudflare 站点')
        zone = _zone_view(found[0], account=True)
        return zone

    def _zones_named(self, client, domain):
        result = self._call(client, 'GET', '/zones', params={'name': domain, 'per_page': '5'})
        if not isinstance(result, list):
            raise CloudflareError('Cloudflare 站点列表无法读取')
        return result

    def _ensure_zone(self, client, domain, account_id):
        found = self._zones_named(client, domain)
        if len(found) > 1:
            raise CloudflareError('Cloudflare 里有多条同名站点')
        if len(found) == 1:
            return _zone_view(found[0])
        try:
            created = self._call(client, 'POST', '/zones', payload={
                'name': domain, 'type': 'full', 'jump_start': False, 'account': {'id': account_id},
            })
        except CloudflareError as error:
            if error.code != DUPLICATE_ZONE:
                raise
            found = self._zones_named(client, domain)
            if len(found) != 1:
                raise CloudflareError('Cloudflare 没有确认新建的站点') from None
            return _zone_view(found[0])
        return _zone_view(created)

    def _exact_record(self, client, zone_id, domain):
        result = self._call(client, 'GET', f'/zones/{zone_id}/dns_records', params={'type': 'A', 'name': domain, 'per_page': '20'})
        if not isinstance(result, list):
            raise CloudflareError('Cloudflare 解析记录无法读取')
        matches = [item for item in result if isinstance(item, dict) and item.get('name') == domain and item.get('type') == 'A']
        if len(matches) > 1:
            raise CloudflareError('Cloudflare 上这个域名有多条 A 记录，请先留一条再套用')
        return matches[0] if matches else None

    def _ensure_record(self, client, zone_id, domain):
        current = self._exact_record(client, zone_id, domain)
        payload = {'type': 'A', 'name': domain, 'content': self.origin_ip, 'proxied': True, 'ttl': 1}
        if current is None:
            self._call(client, 'POST', f'/zones/{zone_id}/dns_records', payload=payload)
            return
        if current.get('content') == self.origin_ip and current.get('proxied') is True:
            return
        record_id = current.get('id')
        if not isinstance(record_id, str) or not re.fullmatch(r'[a-f0-9]{32}', record_id):
            raise CloudflareError('Cloudflare 解析记录无法更新')
        self._call(client, 'PUT', f'/zones/{zone_id}/dns_records/{record_id}', payload=payload)

    def _copy(self, client, template, zone_id, domain):
        copied = skipped = 0
        settings = self._call(client, 'GET', f'/zones/{template["id"]}/settings')
        if not isinstance(settings, list):
            raise CloudflareError('模板上的优化设置无法读取')
        wanted = {item.get('id'): item for item in settings if isinstance(item, dict)}
        for name in OPTIMIZATION_SETTINGS:
            item = wanted.get(name)
            if not item or item.get('editable') is not True or 'value' not in item:
                continue
            value = _rewrite(item['value'], template['name'], domain)
            result = self._call(client, 'PATCH', f'/zones/{zone_id}/settings/{name}', payload={'value': value}, soft=True)
            if result is None:
                skipped += 1
            else:
                copied += 1
        for phase in OPTIMIZATION_PHASES:
            current = self._call(client, 'GET', f'/zones/{template["id"]}/rulesets/phases/{phase}/entrypoint', missing_ok=True)
            if current is None:
                continue
            rules = current.get('rules') if isinstance(current, dict) else None
            if not isinstance(rules, list) or len(rules) > 50:
                raise CloudflareError('模板上的优化规则过多或无法读取，没有整套复制')
            if not rules:
                continue
            body = [_rule(_rewrite(rule, template['name'], domain)) for rule in rules]
            wrote = self._call(client, 'PUT', f'/zones/{zone_id}/rulesets/phases/{phase}/entrypoint', payload={'rules': body}, soft=True)
            if wrote is None:
                skipped += 1
            else:
                copied += 1
        return copied, skipped


def _zone_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}', value):
        raise CloudflareError('Cloudflare 站点记录无效')
    return value


def _zone_view(result, account=False):
    if not isinstance(result, dict):
        raise CloudflareError('Cloudflare 站点响应无法读取')
    zone_id = _zone_id(result.get('id'))
    name = result.get('name')
    try:
        name = normalize_domain(name)
    except ValueError:
        raise CloudflareError('Cloudflare 站点响应无法读取') from None
    status = result.get('status')
    if status not in ('active', 'pending', 'initializing'):
        raise CloudflareError('这条域名在 Cloudflare 的状态暂时不能套用规则')
    view = {'id': zone_id, 'name': name, 'status': status, 'name_servers': _nameservers(result.get('name_servers'))}
    if account:
        account_id = result.get('account', {}).get('id') if isinstance(result.get('account'), dict) else None
        if not isinstance(account_id, str) or not re.fullmatch(r'[a-f0-9]{32}', account_id):
            raise CloudflareError('Cloudflare 账号无法确认')
        view['account_id'] = account_id
    return view


def _nameservers(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 4:
        raise CloudflareError('Cloudflare 没有返回可用的 NS')
    servers = []
    for item in value:
        if not isinstance(item, str):
            raise CloudflareError('Cloudflare 没有返回可用的 NS')
        server = item.strip().lower().rstrip('.')
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+', server):
            raise CloudflareError('Cloudflare 没有返回可用的 NS')
        servers.append(server)
    return tuple(servers)


def _code(body):
    errors = body.get('errors') if isinstance(body, dict) else None
    if isinstance(errors, list) and errors and isinstance(errors[0], dict) and type(errors[0].get('code')) is int:
        return errors[0]['code']
    return 0


def _safe_error(body, token):
    code = _code(body)
    if code in AUTH_CODES:
        return 'Cloudflare 令牌无效或权限不足', '', (), code
    errors = body.get('errors') if isinstance(body, dict) else None
    message = ''
    if isinstance(errors, list) and errors and isinstance(errors[0], dict) and isinstance(errors[0].get('message'), str):
        message = errors[0]['message']
    if not message or (token and token in message) or len(message) > 180:
        message = 'Cloudflare 没有完成这次操作'
    return message, '', (), code


def _rewrite(value, source, target):
    if isinstance(value, str):
        return value.replace(source, target)
    if isinstance(value, list):
        return [_rewrite(item, source, target) for item in value]
    if isinstance(value, dict):
        return {key: _rewrite(item, source, target) for key, item in value.items()}
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    raise CloudflareError('优化规则里有无法复制的内容')


def _rule(rule):
    if not isinstance(rule, dict) or not isinstance(rule.get('expression'), str) or not isinstance(rule.get('action'), str):
        raise CloudflareError('优化规则缺少匹配条件或动作')
    kept = {key: rule[key] for key in RULE_KEYS if key in rule}
    return kept


def _summary(copied, skipped, nameservers, active):
    if copied and skipped:
        head = f'已套用能用的规则，有 {skipped} 项因套餐没套上。'
    elif copied:
        head = '已套用缓存和优化规则。'
    elif skipped:
        head = '模板上的优化项这次没套上。'
    else:
        head = '模板上没有可套用的优化规则。'
    if active:
        return head + 'Cloudflare 已在解析这条域名。'
    return head + '请到域名注册商把 NS 改为：' + '、'.join(nameservers)
