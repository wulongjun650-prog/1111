"""Work-order checks and buyer cost lines. Secrets stay out of this file."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from urllib.parse import unquote, urlsplit


ONLINE_LIMIT = 3
FEE = Decimal('1.15')


class DeskError(ValueError):
    pass


def ticket_code(url):
    text = str(url or '').strip()
    parts = urlsplit(text)
    fragment = parts.fragment or text
    match = re.search(r'accountshow/([^/?#]+)', fragment)
    if not match:
        raise DeskError('工单链接不对')
    code = unquote(match.group(1)).strip()
    if not code or len(code) > 80:
        raise DeskError('工单链接不对')
    return code


def spreadsheet_id(url):
    match = re.search(r'/spreadsheets/d/([A-Za-z0-9_-]+)', str(url or ''))
    if not match:
        raise DeskError('表格链接不对')
    return match.group(1)


def _whole(value, label):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise DeskError(f'{label}不是整数') from None
    if number < 0:
        raise DeskError(f'{label}不能为负')
    return number


def _decimal(value, label):
    try:
        number = Decimal(str(value).replace(',', '').strip())
    except (InvalidOperation, AttributeError):
        raise DeskError(f'{label}不是数字') from None
    if not number.is_finite():
        raise DeskError(f'{label}不是数字')
    return number


def _phone(value):
    digits = re.sub(r'\D', '', str(value or ''))
    if not re.fullmatch(r'\d{8,15}', digits):
        raise DeskError('号码需为 8 到 15 位数字')
    return digits


def _plain(number):
    text = format(number, 'f')
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return text or '0'


def _cents(number):
    return number.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _grouped(number):
    rounded = _cents(number)
    sign = '-' if rounded < 0 else ''
    whole, frac = f'{abs(rounded):.2f}'.split('.')
    groups = []
    while whole:
        groups.append(whole[-3:])
        whole = whole[:-3]
    return sign + ','.join(reversed(groups)) + '.' + frac


def should_switch(online, offline):
    """Return why the published number should change, or an empty string."""
    if offline:
        return 'offline'
    if _whole(online, '在线人数') > ONLINE_LIMIT:
        return 'online'
    return ''


def _haiwang_body(payload):
    """Return the Haiwang list body, or None when this is the simple local shape.

    Status 2 is the green 在线 row. The account list's online_num stays 0 on a
    live ticket, so the online APP count comes from share statistics instead.
    """
    if not isinstance(payload, dict):
        return None
    body = payload
    if 'data' in payload or 'code' in payload:
        if payload.get('code') not in (None, 1):
            raise DeskError('工单数据无法读取')
        data = payload.get('data')
        if not isinstance(data, dict):
            raise DeskError('工单数据无法读取')
        body = data
    items = body.get('items')
    stats = body.get('shareStatistics')
    if not isinstance(items, list) or not isinstance(stats, dict):
        return None
    if items and not isinstance(items[0], dict):
        raise DeskError('工单号码无法读取')
    if items and 'acclist_account' not in items[0]:
        return None
    return body


def _from_haiwang(body):
    stats = body.get('shareStatistics') or {}
    apps = stats.get('sharecode_statistics_applist') or []
    app = next((item for item in apps if isinstance(item, dict)), {})
    items = body.get('items') or []
    listed = body.get('total')
    if listed is not None and _whole(listed, '账号数') > len(items):
        raise DeskError('工单号码没有读全')
    online_value = stats.get('sharecode_statistics_online_account', app.get('account_total_online'))
    online = _whole(online_value if online_value is not None else sum(item.get('acclist_status') == 2 for item in items), '在线人数')
    total_value = stats.get('sharecode_statistics_total_account', app.get('account_total', body.get('total', len(items))))
    total_accounts = _whole(total_value, '账号数')
    accounts = []
    for item in items:
        if not isinstance(item, dict):
            raise DeskError('工单号码无法读取')
        offline = item.get('acclist_status') != 2
        accounts.append({
            'phone': item.get('acclist_account'),
            'offline': offline,
            'online_count': 0 if offline else 1,
            'leads': item.get('account_statistics_today_effective', item.get('account_statistics_today_total', 0)),
        })
    return {
        'online': online,
        'offline_apps': max(total_accounts - online, 0),
        'name': str(body.get('remark') or '').strip(),
        'accounts': accounts,
    }


def parse_ticket(payload, url, name=''):
    if not isinstance(payload, dict):
        raise DeskError('工单数据无法读取')
    haiwang = _haiwang_body(payload)
    if haiwang is not None:
        payload = _from_haiwang(haiwang)
    code = ticket_code(url)
    online = _whole(payload.get('online', payload.get('在线APP', 0)), '在线人数')
    offline_apps = _whole(payload.get('offline_apps', payload.get('离线APP', 0)), '离线人数')
    label = str(name or payload.get('name') or payload.get('备注') or code).strip() or code
    if len(label) > 80:
        raise DeskError('工单名称过长')
    accounts = []
    for item in payload.get('accounts') or []:
        if not isinstance(item, dict):
            raise DeskError('工单号码无法读取')
        account_offline = bool(item.get('offline'))
        raw_online = item.get('online_count', 0 if isinstance(item.get('online'), bool) else item.get('online', 0))
        accounts.append({
            'phone': _phone(item.get('phone')),
            'offline': account_offline,
            'online': 0 if account_offline else _whole(raw_online, '在线人数'),
            'leads': _decimal(item.get('leads', item.get('leads_today', 0)), '进线'),
        })
    offline = bool(payload.get('offline')) or (online == 0 and offline_apps > 0 and not accounts)
    return {
        'code': code,
        'name': label,
        'online': online,
        'offline_apps': offline_apps,
        'offline': offline,
        'accounts': accounts,
    }


def decision_for(ticket, phone):
    """Switch the live number when its work order is over 3 online, or that number is offline.

    A work order that lists numbers only applies to the number it actually contains.
    """
    digits = _phone(phone) if phone else ''
    accounts = ticket.get('accounts') or []
    account = next((item for item in accounts if item['phone'] == digits), None) if digits else None
    if digits and accounts and account is None:
        return ''
    if account and account['offline']:
        return 'offline'
    if account and account['online'] > ONLINE_LIMIT:
        return 'online'
    if int(ticket.get('online') or 0) > ONLINE_LIMIT:
        return 'online'
    if ticket.get('offline'):
        return 'offline'
    return ''


def lead_report(tickets):
    rows = []
    total = Decimal(0)
    for ticket in tickets:
        for account in ticket.get('accounts') or []:
            leads = account['leads']
            total += leads
            if leads == 0:
                continue
            rows.append({
                'name': ticket['name'],
                'code': ticket['code'],
                'phone': account['phone'],
                'leads': _plain(leads),
            })
    return {'rows': rows, 'total': _plain(total)}


def review_tickets(tickets, phone, reader):
    if not isinstance(tickets, list) or not 1 <= len(tickets) <= 20:
        raise DeskError('请填写 1 到 20 个工单')
    parsed = []
    seen = set()
    for item in tickets:
        if not isinstance(item, dict):
            raise DeskError('工单链接不对')
        url = str(item.get('url') or '').strip()
        code = ticket_code(url)
        if code in seen:
            raise DeskError('工单链接重复')
        seen.add(code)
        password = str(item.get('password') or '')
        name = str(item.get('name') or '').strip()
        loaded = reader(url, password, name)
        if not isinstance(loaded, dict):
            raise DeskError('工单数据无法读取')
        parsed.append(parse_ticket(loaded, url, name))
    report = lead_report(parsed)
    offline_phones = []
    for ticket in parsed:
        for account in ticket.get('accounts') or []:
            if account.get('offline') and account['phone'] not in offline_phones:
                offline_phones.append(account['phone'])
    report['offline_phones'] = offline_phones
    switch = ''
    online = None
    for ticket in parsed:
        reason = decision_for(ticket, phone)
        if reason:
            switch = reason
            online = ticket['online']
            break
    report['switch'] = switch
    report['online'] = online
    return report


def buyer_quote(fields):
    if not isinstance(fields, dict):
        raise DeskError('金额内容无法读取')
    date = str(fields.get('date') or '').strip()
    project = str(fields.get('project') or '').strip()
    buyer = str(fields.get('buyer') or '').strip()
    phone = str(fields.get('phone') or '').strip()
    if not date or len(date) > 20:
        raise DeskError('请填写日期')
    if not project or len(project) > 40:
        raise DeskError('请填写项目')
    if not buyer or len(buyer) > 40:
        raise DeskError('请填写投手')
    _phone(phone)
    recharge = _decimal(fields.get('recharge', 0), '充值')
    balance = _decimal(fields.get('balance', 0), '余额')
    spend = _decimal(fields.get('spend', 0), '消耗')
    leads = _decimal(fields.get('leads', 0), '进线')
    if spend < 0 or recharge < 0 or leads < 0:
        raise DeskError('金额不能为负')
    adjusted = _cents(spend * FEE)
    cost = _cents(adjusted / leads) if leads > 0 else None
    recharge_text = _plain(recharge) if recharge == recharge.to_integral() else f'{_cents(recharge):.2f}'
    lines = [
        date,
        project,
        buyer,
        f'{phone} 充值{recharge_text} 余额：{_grouped(balance)}',
        f'消耗：{adjusted:.2f}',
        f'进线：{_plain(leads)}',
        f'成本：{cost:.2f}' if cost is not None else '成本：—',
    ]
    row = [date, buyer, f'{_cents(spend):.2f}', f'{adjusted:.2f}', _plain(leads), '' if cost is None else f'{cost:.2f}']
    return {
        'text': '\n'.join(lines),
        'row': row,
        'adjusted': f'{adjusted:.2f}',
        'cost': '' if cost is None else f'{cost:.2f}',
    }


def unavailable_reader(url, password, name=''):
    raise DeskError('工单页面暂时读不到')


class SheetWriter:
    def __init__(self, token='', transport=None):
        self.token = token.strip() if isinstance(token, str) else ''
        self.transport = transport

    def append(self, spreadsheet_url, row):
        if not self.token:
            raise DeskError('表格还没接上')
        if not isinstance(row, list) or len(row) != 6:
            raise DeskError('表格一行需要日期、投手、图床消耗、消耗、进线和成本')
        sheet = spreadsheet_id(spreadsheet_url)
        url = f'https://sheets.googleapis.com/v4/spreadsheets/{sheet}/values/A:F:append?valueInputOption=USER_ENTERED'
        headers = {'Authorization': f'Bearer {self.token}', 'Content-Type': 'application/json'}
        body = {'values': [row]}
        if self.transport is None:
            import httpx
            response = httpx.post(url, headers=headers, json=body, timeout=20)
            status, payload = response.status_code, response.json()
        else:
            status, payload = self.transport(url, headers, body)
        if status != 200:
            raise DeskError('表格没有写进去')
        return {'ok': True, 'updated': (payload or {}).get('updates', {}).get('updatedRows', 1)}
