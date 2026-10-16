"""Optional Cloudflare attach. The token stays on the server.

Unchecked domains keep the existing A-record path. A checked domain is created
in the same account as the template zone, proxied back to this server, and
receives that zone's cache and speed rules.
"""
import ipaddress
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
# Cloudflare uses 0 for "Respect Existing Headers". Longer than an hour overrides the origin no-store.
BROWSER_CACHE_MAX = 3600
CHALLENGE_PHASE = 'http_request_firewall_custom'
CHALLENGE_DESCRIPTION = '双子星人机验证'
CHALLENGE_EXPRESSION = '(http.request.method eq "GET" and any(http.request.headers["accept"][*] contains "text/html"))'
BUTTON_QUERY = 'ab_human=1'
SECURITY_LEVELS = ('off', 'essentially_off', 'low', 'medium', 'high', 'under_attack')


class CloudflareError(ProvisioningError):
    def __init__(self, message, zone_id='', nameservers=(), code=0, status=0):
        super().__init__(message)
        self.zone_id = zone_id
        self.nameservers = tuple(nameservers)
        self.code = code
        self.status = status


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
                # Flexible SSL plus the origin's HTTP-to-HTTPS redirect loops forever.
                self._ensure_strict_ssl(client, zone['id'])
                self._ensure_record(client, zone['id'], domain)
                copied, skipped = self._copy(client, template, zone['id'], domain)
                self._ensure_strict_ssl(client, zone['id'])
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
            self._ensure_strict_ssl(client, zone['id'])
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
            self._limit_browser_cache(client, zone_id)
            self._call(client, 'POST', f'/zones/{zone_id}/purge_cache', payload={'purge_everything': True})

    def disable(self, zone_id, domain):
        """Turn the apex A record grey. The zone and its address stay in place."""
        domain = normalize_domain(domain)
        zone_id = _zone_id(zone_id)
        if not self.configured:
            raise CloudflareError('服务器未配置 Cloudflare')
        with self._client() as client:
            zone = _zone_view(self._call(client, 'GET', '/zones/' + zone_id))
            if zone['name'] != domain:
                raise CloudflareError('Cloudflare 站点与登记域名不一致')
            record = self._exact_record(client, zone_id, domain)
            if record is None:
                return {'proxied': False, 'address': ''}
            address = record.get('content')
            try:
                address = str(ipaddress.IPv4Address(address))
            except (ipaddress.AddressValueError, ValueError):
                raise CloudflareError('Cloudflare 上的 A 记录无法关闭代理') from None
            if record.get('proxied') is not True:
                return {'proxied': False, 'address': address}
            record_id = record.get('id')
            if not isinstance(record_id, str) or not re.fullmatch(r'[a-f0-9]{32}', record_id):
                raise CloudflareError('Cloudflare 解析记录无法更新')
            self._call(client, 'PUT', f'/zones/{zone_id}/dns_records/{record_id}', payload={
                'type': 'A', 'name': domain, 'content': address, 'proxied': False, 'ttl': 1,
            })
        return {'proxied': False, 'address': address}

    def set_human_challenge(self, zone_id, domain, entry, button, previous=''):
        """Turn the two human checks on or off for one domain.

        Entry checks the whole visit. Button checks only the address used after
        a click, and leaves the landing page itself alone. Cache rules are not
        part of either change.
        """
        if type(entry) is not bool or type(button) is not bool:
            raise CloudflareError('人机验证选项无效')
        if previous not in ('', 'off', 'essentially_off', 'low', 'medium', 'high'):
            raise CloudflareError('人机验证记录无效')
        domain = normalize_domain(domain)
        zone_id = _zone_id(zone_id)
        if not self.token:
            raise CloudflareError('服务器未配置 Cloudflare')
        with self._client() as client:
            zone = _zone_view(self._call(client, 'GET', '/zones/' + zone_id))
            if zone['name'] != domain:
                raise CloudflareError('Cloudflare 站点与登记域名不一致')
            self._set_button_rule(client, zone_id, domain, button)
            remembered = self._set_entry_check(client, zone_id, entry, previous)
        return {'entry': entry, 'button': button, 'previous': remembered}

    def delete_zone(self, zone_id, domain):
        """Delete the Cloudflare zone after its name matches this domain."""
        domain = normalize_domain(domain)
        zone_id = _zone_id(zone_id)
        if not self.token:
            raise CloudflareError('服务器未配置 Cloudflare')
        with self._client() as client:
            zone = self._call(client, 'GET', '/zones/' + zone_id, missing_ok=True)
            if zone is None:
                return
            if _zone_view(zone)['name'] != domain:
                raise CloudflareError('Cloudflare 站点与登记域名不一致')
            self._call(client, 'DELETE', '/zones/' + zone_id, missing_ok=True)

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
            message, failed_zone, nameservers, code = _safe_error(body, self.token)
            error = CloudflareError(message, failed_zone, nameservers, code, status)
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

    def ensure_strict_ssl(self, zone_id, domain):
        """Make Cloudflare connect to this server over HTTPS with a real certificate."""
        domain = normalize_domain(domain)
        zone_id = _zone_id(zone_id)
        if not self.token:
            raise CloudflareError('服务器未配置 Cloudflare')
        with self._client() as client:
            zone = _zone_view(self._call(client, 'GET', '/zones/' + zone_id))
            if zone['name'] != domain:
                raise CloudflareError('Cloudflare 站点与登记域名不一致')
            self._ensure_strict_ssl(client, zone_id)
        return {'ssl': 'strict'}

    def _ensure_strict_ssl(self, client, zone_id):
        current = self._call(client, 'GET', f'/zones/{zone_id}/settings/ssl')
        if isinstance(current, dict) and current.get('value') == 'strict':
            return
        updated = self._call(client, 'PATCH', f'/zones/{zone_id}/settings/ssl', payload={'value': 'strict'})
        if not isinstance(updated, dict) or updated.get('value') != 'strict':
            raise CloudflareError('Cloudflare 没有改成加密回源，已停止，避免网站无限跳转')

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
            if name == 'browser_cache_ttl':
                seconds = _ttl_seconds(value)
                if seconds is not None and seconds > BROWSER_CACHE_MAX:
                    value = 0
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
            body = [_rule(_cap_rule_browser_ttl(_rewrite(rule, template['name'], domain))) for rule in rules]
            wrote = self._call(client, 'PUT', f'/zones/{zone_id}/rulesets/phases/{phase}/entrypoint', payload={'rules': body}, soft=True)
            if wrote is None:
                skipped += 1
            else:
                copied += 1
        return copied, skipped

    def _limit_browser_cache(self, client, zone_id):
        """A template can say 72000 seconds. Browsers then keep the previous landing for 20 hours."""
        try:
            current = self._call(client, 'GET', f'/zones/{zone_id}/settings/browser_cache_ttl', soft=True)
            seconds = _ttl_seconds(current.get('value') if isinstance(current, dict) else None)
            if seconds is not None and seconds > BROWSER_CACHE_MAX:
                self._call(client, 'PATCH', f'/zones/{zone_id}/settings/browser_cache_ttl', payload={'value': 0}, soft=True)
            phase = 'http_request_cache_settings'
            current = self._call(client, 'GET', f'/zones/{zone_id}/rulesets/phases/{phase}/entrypoint', missing_ok=True, soft=True)
            rules = current.get('rules') if isinstance(current, dict) else None
            if not isinstance(rules, list) or len(rules) > 50:
                return
            body = []
            changed = False
            for rule in rules:
                capped = _cap_rule_browser_ttl(rule)
                changed = changed or capped is not rule
                body.append(_rule(capped))
            if changed:
                self._call(client, 'PUT', f'/zones/{zone_id}/rulesets/phases/{phase}/entrypoint', payload={'rules': body}, soft=True)
        except CloudflareError:
            return

    def _set_entry_check(self, client, zone_id, enabled, previous):
        ruled = self._try_challenge_rule(client, zone_id, enabled)
        if enabled and ruled is not None:
            if previous in SECURITY_LEVELS and previous != 'under_attack' and self._read_level(client, zone_id) == 'under_attack':
                self._write_level(client, zone_id, previous)
            return ''
        if enabled:
            return self._set_attack_mode(client, zone_id, True, previous)['previous']
        if previous:
            self._set_attack_mode(client, zone_id, False, previous)
        return ''

    def _try_challenge_rule(self, client, zone_id, enabled):
        try:
            return self._set_challenge_rule(client, zone_id, enabled)
        except CloudflareError as error:
            if not _denied(error):
                raise
            return None

    def _set_button_rule(self, client, zone_id, domain, enabled):
        pattern = f'*{domain}/*{BUTTON_QUERY}*'
        rules = self._page_rules(client, zone_id)
        ours = [rule for rule in rules if _button_rule(rule, pattern)]
        others = [rule for rule in rules if rule not in ours]
        if not enabled:
            for rule in ours:
                self._call(client, 'DELETE', f'/zones/{zone_id}/pagerules/{rule["id"]}')
            return
        if len(ours) == 1 and ours[0].get('status') == 'active' and ours[0].get('priority') == 1:
            return
        payload = {
            'targets': [{'target': 'url', 'constraint': {'operator': 'matches', 'value': pattern}}],
            'actions': [{'id': 'security_level', 'value': 'under_attack'}],
            'priority': 1,
            'status': 'active',
        }
        if len(ours) == 1:
            self._call(client, 'PATCH', f'/zones/{zone_id}/pagerules/{ours[0]["id"]}', payload=payload)
            return
        for rule in ours:
            self._call(client, 'DELETE', f'/zones/{zone_id}/pagerules/{rule["id"]}')
        if len(others) >= 50:
            raise CloudflareError('这条域名的页面规则已满，点击按钮的人机验证没有加上')
        self._call(client, 'POST', f'/zones/{zone_id}/pagerules', payload=payload)

    def _page_rules(self, client, zone_id):
        result = self._call(client, 'GET', f'/zones/{zone_id}/pagerules')
        if not isinstance(result, list) or len(result) > 50:
            raise CloudflareError('Cloudflare 页面规则无法读取，没有改动')
        kept = []
        for rule in result:
            if not isinstance(rule, dict):
                raise CloudflareError('Cloudflare 页面规则无法读取，没有改动')
            rule_id = rule.get('id')
            if not isinstance(rule_id, str) or not re.fullmatch(r'[a-f0-9]{32}', rule_id):
                raise CloudflareError('Cloudflare 页面规则无法读取，没有改动')
            kept.append(rule)
        return kept

    def _set_challenge_rule(self, client, zone_id, enabled):
        rules = self._firewall_rules(client, zone_id)
        others = [rule for rule in rules if rule.get('description') != CHALLENGE_DESCRIPTION]
        if enabled:
            matched = [rule for rule in rules if _same_challenge(rule)]
            if len(matched) == 1 and len(others) + 1 == len(rules):
                return {'enabled': True, 'mode': 'managed_challenge', 'previous': ''}
            body = others + [_challenge_rule()]
        else:
            if len(others) == len(rules):
                return {'enabled': False, 'mode': 'managed_challenge', 'previous': ''}
            body = others
        self._call(client, 'PUT', f'/zones/{zone_id}/rulesets/phases/{CHALLENGE_PHASE}/entrypoint', payload={'rules': body})
        return {'enabled': enabled, 'mode': 'managed_challenge', 'previous': ''}

    def _firewall_rules(self, client, zone_id):
        current = self._call(client, 'GET', f'/zones/{zone_id}/rulesets/phases/{CHALLENGE_PHASE}/entrypoint', missing_ok=True)
        if current is None:
            return []
        rules = current.get('rules') if isinstance(current, dict) else None
        if not isinstance(rules, list) or len(rules) > 50:
            raise CloudflareError('这条域名上的防火墙规则无法读取，没有改动')
        kept = []
        for rule in rules:
            if not isinstance(rule, dict):
                raise CloudflareError('这条域名上的防火墙规则无法读取，没有改动')
            kept.append(_kept_firewall_rule(rule))
        return kept

    def _set_attack_mode(self, client, zone_id, enabled, previous):
        current = self._read_level(client, zone_id)
        if enabled:
            remembered = current if current != 'under_attack' else previous or 'medium'
            if remembered not in SECURITY_LEVELS or remembered == 'under_attack':
                remembered = 'medium'
            if current != 'under_attack':
                self._write_level(client, zone_id, 'under_attack')
            return {'enabled': True, 'mode': 'under_attack', 'previous': remembered}
        if current != 'under_attack':
            return {'enabled': False, 'mode': 'under_attack', 'previous': ''}
        restore = previous if previous in SECURITY_LEVELS and previous != 'under_attack' else 'medium'
        self._write_level(client, zone_id, restore)
        return {'enabled': False, 'mode': 'under_attack', 'previous': ''}

    def _read_level(self, client, zone_id):
        current = self._call(client, 'GET', f'/zones/{zone_id}/settings/security_level')
        value = current.get('value') if isinstance(current, dict) else None
        if value not in SECURITY_LEVELS:
            raise CloudflareError('Cloudflare 安全级别无法读取')
        return value

    def _write_level(self, client, zone_id, value):
        updated = self._call(client, 'PATCH', f'/zones/{zone_id}/settings/security_level', payload={'value': value})
        if not isinstance(updated, dict) or updated.get('value') != value:
            raise CloudflareError('Cloudflare 没有改好人机验证')


def _denied(error):
    return error.code in AUTH_CODES or error.status in (401, 403)


def _challenge_rule():
    return {
        'action': 'managed_challenge',
        'expression': CHALLENGE_EXPRESSION,
        'description': CHALLENGE_DESCRIPTION,
        'enabled': True,
    }


def _button_rule(rule, pattern):
    targets = rule.get('targets') if isinstance(rule.get('targets'), list) else []
    actions = rule.get('actions') if isinstance(rule.get('actions'), list) else []
    matched = False
    for target in targets:
        constraint = target.get('constraint') if isinstance(target, dict) else None
        if isinstance(constraint, dict) and constraint.get('value') == pattern:
            matched = True
    levels = [item.get('value') for item in actions if isinstance(item, dict) and item.get('id') == 'security_level']
    return matched and levels == ['under_attack']


def _same_challenge(rule):
    return (
        rule.get('action') == 'managed_challenge'
        and rule.get('expression') == CHALLENGE_EXPRESSION
        and rule.get('description') == CHALLENGE_DESCRIPTION
        and rule.get('enabled') is True
    )


def _kept_firewall_rule(rule):
    if not isinstance(rule.get('expression'), str) or not isinstance(rule.get('action'), str):
        raise CloudflareError('这条域名上已有的防火墙规则无法保留，没有改动')
    if len(rule['expression']) > 4000 or len(rule['action']) > 64:
        raise CloudflareError('这条域名上已有的防火墙规则无法保留，没有改动')
    kept = {}
    rule_id = rule.get('id')
    if isinstance(rule_id, str) and re.fullmatch(r'[a-f0-9]{32}', rule_id):
        kept['id'] = rule_id
    for key in RULE_KEYS:
        if key in rule:
            kept[key] = rule[key]
    return kept


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


def _ttl_seconds(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _cap_rule_browser_ttl(rule):
    if not isinstance(rule, dict):
        return rule
    params = rule.get('action_parameters')
    ttl = params.get('browser_ttl') if isinstance(params, dict) else None
    seconds = _ttl_seconds(ttl.get('default') if isinstance(ttl, dict) else None)
    if not isinstance(ttl, dict) or ttl.get('mode') != 'override_origin' or seconds is None or seconds <= BROWSER_CACHE_MAX:
        return rule
    updated = dict(rule)
    updated['action_parameters'] = dict(params)
    updated['action_parameters']['browser_ttl'] = {'mode': 'respect_origin'}
    return updated


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


def repair_strict_ssl(registry, cloudflare):
    """Keep every managed zone off Flexible SSL. One zone failing does not stop the others."""
    if cloudflare is None or not cloudflare.token:
        return 0
    repaired = 0
    for site in registry.list():
        zone_id = site.get('cf_zone_id') or ''
        if site['id'] == 'default' or not zone_id:
            continue
        try:
            cloudflare.ensure_strict_ssl(zone_id, site['domain'])
        except CloudflareError:
            continue
        repaired += 1
    return repaired


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
