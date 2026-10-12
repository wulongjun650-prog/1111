"""Align landing visits with beacons. Nothing here changes a page or a jump."""
import csv
import io
import json
import re
from collections import defaultdict
from datetime import datetime
from urllib.parse import urlsplit

from .campaign import HK, _EVENT_KINDS, is_html_path, query_value
from .devices import open_app


_ANDROID_LABEL = re.compile(r'^(?:Android App · |Android · )(.+)$')
_IOS_LABEL = re.compile(r'^(?:iOS App · |iPhone App · |iPad App · |iOS · )(.+)$')
_MOBILE_ANDROID = re.compile(r'^mobileapp::2-(.+)$', re.I)
_MOBILE_IOS = re.compile(r'^mobileapp::1-(.+)$', re.I)
_ADSENSE = re.compile(r'^(?:(?:\d+)\.)*(android|iphone|ipad)\.(.+)\.adsenseformobileapps\.com$', re.I)
_HOST = re.compile(r'^(?:[a-z0-9-]+\.)+[a-z]{2,}$', re.I)
_SYSTEM_ORDER = ('iOS', 'Android', '桌面', '其他')
_ALIGN_WINDOW = 30 * 60
_FAST_CLICK = 3
_REPEAT_CLICKS = 8
_REPEAT_PEOPLE = 3
_VISIT_ALERT = 50
_WAKE_CLICKS = 10
_WAKE_GAP = 0.30
_QUIET_SECONDS = 15 * 60
_SUMMARY_FIELDS = ['视图', '名称', '系统', '进站人数', '点击人数', '点击次数', '拉起', '兜底', '拉起率', '进站数', '事件数', '标红', '原因']
_VISIT_FIELDS = ['访问时间', 'gclid', '版位原值', '规范版位', '完整UA', '系统', '浏览器', '分到的号码', '对齐', '归类', '事件']
_INTAKE_FIELDS = ['时间', 'IP', '完整UA', 'Origin', 'Referer', 'Content-Type', 'body长度', '状态码', '原因', '原始body']


def normalize_placement(value):
    """One display name. The caller keeps the original string beside it."""
    text = (value or '').strip()
    if not text:
        return '网站 · 未知'
    labeled = _ANDROID_LABEL.match(text)
    if labeled:
        return 'Android · ' + labeled.group(1).strip()
    labeled = _IOS_LABEL.match(text)
    if labeled:
        return 'iOS · ' + labeled.group(1).strip()
    android = _MOBILE_ANDROID.match(text)
    if android:
        return 'Android · ' + android.group(1).strip()
    ios = _MOBILE_IOS.match(text)
    if ios:
        return 'iOS · ' + ios.group(1).strip()
    host = _hostname(text) or text
    adsense = _ADSENSE.match(host)
    if adsense:
        platform = 'Android' if adsense.group(1).lower() == 'android' else 'iOS'
        return f'{platform} · {adsense.group(2).strip()}'
    domain = _hostname(text) or _bare_host(text)
    return '网站 · ' + (domain or '未知')


def flow_system(ua):
    text = ua or ''
    if re.search(r'iPhone|iPad|iPod', text, re.I):
        return 'iOS'
    if re.search(r'Android', text, re.I):
        return 'Android'
    if re.search(r'Windows NT|Macintosh|Mac OS X|CrOS|X11; Linux', text, re.I):
        return '桌面'
    return '其他'


def _hostname(value):
    raw = (value or '').strip()
    if '://' not in raw and not raw.startswith('//'):
        return ''
    if raw.startswith('//'):
        raw = 'https:' + raw
    try:
        return (urlsplit(raw).hostname or '')[:253]
    except ValueError:
        return ''


def _bare_host(value):
    host = (value or '').strip().split('/')[0].split('?')[0].split(':')[0]
    if _HOST.fullmatch(host):
        return host[:253]
    return ''


def _text(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool) or value is None:
        return ''
    if isinstance(value, (int, float)):
        return str(value)
    return ''


def _stamp(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _wa_ms(payload):
    for key in ('wa_ms', 'since_load_ms'):
        raw = payload.get(key)
        if isinstance(raw, bool) or raw in (None, ''):
            continue
        try:
            number = int(float(raw))
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return None


def _clock(stamp):
    if not stamp:
        return ''
    return datetime.fromtimestamp(float(stamp), HK).strftime('%Y-%m-%d %H:%M:%S')


def _bucket(stamp):
    local = datetime.fromtimestamp(float(stamp), HK).replace(second=0, microsecond=0)
    return local.replace(minute=(local.minute // 10) * 10)


def _ratio(part, whole):
    if not whole:
        return None
    return round(part / whole, 4)


def _percent(value):
    if value is None:
        return ''
    return f'{value * 100:.1f}%'


def _csv_cell(value):
    text = '' if value is None else str(value)
    return "'" + text if text.startswith(('=', '+', '-', '@', '\t', '\r')) else text


def _domain_ok(row, domain):
    if not domain:
        return True
    return (row.get('domain') or '').lower() == domain.lower()


def _ua_match(left, right):
    first = (left or '').strip()
    second = (right or '').strip()
    if not first or not second:
        return False
    if first == second:
        return True
    short, long = (second, first) if len(second) <= len(first) else (first, second)
    return len(short) >= 40 and long.startswith(short)


def parse_flow_visit(row):
    ua = row.get('ua') or ''
    return {
        'created': _stamp(row.get('created')),
        'domain': row.get('domain') or '',
        'gclid': query_value(row.get('query'), 'gclid'),
        'placement_raw': query_value(row.get('query'), 'placement'),
        'ua': ua,
        'ip': row.get('ip') or '',
        'number': row.get('wa_number') or '',
        'system': flow_system(ua),
        'browser': open_app(ua),
        'events': [],
        'test': False,
        'test_reason': '',
    }


def parse_flow_event(row):
    try:
        payload = json.loads(row.get('body') or '')
    except (TypeError, ValueError):
        payload = None
    if not isinstance(payload, dict):
        payload = {}
    name = _text(payload.get('event'))
    transaction = _text(payload.get('transaction_id'))
    ua = _text(payload.get('ua'))
    return {
        'id': row.get('id'),
        'created': _stamp(row.get('created')),
        'domain': row.get('domain') or '',
        'body': row.get('body') if isinstance(row.get('body'), str) else '',
        'gclid': _text(payload.get('gclid')),
        'placement_raw': _text(payload.get('placement')) or _text(payload.get('placement_app')),
        'event': name,
        'transaction_id': transaction,
        'tx_key': transaction or f'#{row.get("id")}',
        'link_type': _text(payload.get('link_type')),
        'wa_ms': _wa_ms(payload),
        'number': _text(payload.get('number')),
        'ua': ua,
        'ip': '',
        'request_ua': '',
        'kind': _EVENT_KINDS.get(name, ''),
        'visit': None,
        'test': False,
        'test_reason': '',
        'align': '',
    }


def attach_intake(events, intake_rows):
    """Copy the request IP onto the beacon that carried the same body."""
    groups = defaultdict(list)
    for row in intake_rows:
        if int(row.get('status') or 0) != 204:
            continue
        groups[row.get('body') or ''].append(row)
    for rows in groups.values():
        rows.sort(key=lambda item: _stamp(item.get('created')))
    used = set()
    for event in sorted(events, key=lambda item: (item['created'], item.get('id') or 0)):
        best = None
        best_delta = 5.0
        for row in groups.get(event['body'], []):
            marker = id(row)
            if marker in used:
                continue
            delta = abs(_stamp(row.get('created')) - event['created'])
            if delta <= best_delta:
                best = row
                best_delta = delta
        if best is None:
            continue
        used.add(id(best))
        event['ip'] = best.get('ip') or ''
        event['request_ua'] = best.get('ua') or ''


def _pick_gclid(event, visits):
    options = [item for item in visits if item['domain'] == event['domain'] and item['gclid'] and item['gclid'] == event['gclid']]
    if not options:
        return None
    earlier = [item for item in options if item['created'] <= event['created'] + 2]
    pool = earlier or options
    pool.sort(key=lambda item: item['created'])
    return pool[-1] if earlier else pool[0]


def _pick_ipua(event, visits):
    ip = event.get('ip') or ''
    ua = event.get('request_ua') or event.get('ua') or ''
    if not ip or not ua:
        return None
    best = None
    best_delta = _ALIGN_WINDOW + 1
    for visit in visits:
        if visit['domain'] != event['domain'] or visit['ip'] != ip:
            continue
        if not _ua_match(visit['ua'], ua):
            continue
        delta = event['created'] - visit['created']
        if delta < -2 or delta > _ALIGN_WINDOW or delta >= best_delta:
            continue
        best = visit
        best_delta = delta
    return best


def align_events(visits, events):
    unmatched = []
    for event in events:
        visit = _pick_gclid(event, visits) if event.get('gclid') else None
        how = 'gclid' if visit else ''
        if visit is None:
            visit = _pick_ipua(event, visits)
            how = 'ipua' if visit else ''
        if visit is None:
            event['align'] = '未对齐'
            unmatched.append(event)
            continue
        event['align'] = how
        event['visit'] = visit
        visit['events'].append(event)
    for visit in visits:
        visit['events'].sort(key=lambda item: (item['created'], item.get('id') or 0))
    return unmatched


def inherit_visit(events):
    for event in events:
        visit = event.get('visit')
        if visit is None:
            continue
        if not event['gclid']:
            event['gclid'] = visit['gclid']
        if not event['placement_raw']:
            event['placement_raw'] = visit['placement_raw']
        if not event['number']:
            event['number'] = visit['number']
        if visit['number'] == '' and event['number']:
            visit['number'] = event['number']


def _fast_click(visit, event):
    if event.get('wa_ms'):
        return event['wa_ms'] <= _FAST_CLICK * 1000
    return 0 <= event['created'] - visit['created'] <= _FAST_CLICK


def mark_tests(visits, events):
    """Test traffic stays in the detail and stays out of the totals."""
    clicks = defaultdict(lambda: {'tx': set(), 'gclid': set()})
    for event in events:
        if event['kind'] != 'click':
            continue
        visit = event.get('visit')
        ip = (visit or event).get('ip') or ''
        ua = (visit.get('ua') if visit else '') or event.get('request_ua') or event.get('ua') or ''
        if not ip or not ua:
            continue
        bucket = clicks[(event['domain'], ip, ua)]
        bucket['tx'].add(event['tx_key'])
        if event.get('gclid'):
            bucket['gclid'].add(event['gclid'])
    repeated = {key for key, bucket in clicks.items() if len(bucket['tx']) >= _REPEAT_CLICKS or len(bucket['gclid']) >= _REPEAT_PEOPLE}
    for visit in visits:
        reasons = []
        if not visit['gclid']:
            reasons.append('无 gclid')
        if any(_fast_click(visit, event) for event in visit['events'] if event['kind'] == 'click'):
            reasons.append('打开后 3 秒内点击')
        if (visit['domain'], visit['ip'], visit['ua']) in repeated:
            reasons.append('同一设备反复点击')
        visit['test'] = bool(reasons)
        visit['test_reason'] = '、'.join(reasons)
    for event in events:
        reasons = []
        visit = event.get('visit')
        if visit is not None and visit['test']:
            reasons.append(visit['test_reason'] or '测试访问')
        elif not event.get('gclid'):
            reasons.append('无 gclid')
        ip = (visit or event).get('ip') or ''
        ua = (visit.get('ua') if visit else '') or event.get('request_ua') or event.get('ua') or ''
        if (event['domain'], ip, ua) in repeated and '同一设备反复点击' not in '、'.join(reasons):
            reasons.append('同一设备反复点击')
        event['test'] = bool(reasons)
        event['test_reason'] = '、'.join(reasons)


def _event_system(event):
    own = flow_system(event.get('ua') or event.get('request_ua') or '')
    visit = event.get('visit')
    if own != '其他':
        return own
    if visit is not None:
        return visit['system']
    return '其他'


def _event_placement(event):
    raw = event.get('placement_raw') or ''
    visit = event.get('visit')
    if not raw and visit is not None:
        raw = visit.get('placement_raw') or ''
    return raw, normalize_placement(raw)


def _blank_metrics():
    return {'people': set(), 'click_people': set(), 'click_times': set(), 'opens': 0, 'fallbacks': 0}


def _add_metrics(bucket, event):
    if event['kind'] == 'click':
        if event.get('gclid'):
            bucket['click_people'].add(event['gclid'])
        bucket['click_times'].add(event['tx_key'])
    elif event['kind'] == 'open':
        bucket['opens'] += 1
    elif event['kind'] == 'fallback':
        bucket['fallbacks'] += 1


def _finish_metrics(bucket, name, system):
    clicks = len(bucket['click_people'])
    rate = _ratio(bucket['opens'], clicks)
    return {
        'name': name,
        'system': system,
        'visits': len(bucket['people']),
        'clicks': clicks,
        'click_times': len(bucket['click_times']),
        'opens': bucket['opens'],
        'fallbacks': bucket['fallbacks'],
        'open_rate': rate,
        'alert': False,
        'alert_reason': '',
    }


def _zero_click(row, label):
    if row['visits'] >= _VISIT_ALERT and row['clicks'] == 0:
        row['alert'] = True
        row['alert_reason'] = f'疑似点击或上报失效：{label} 进站 {row["visits"]}，点击 0'


def _wake_alert(row, peers):
    if row['clicks'] < _WAKE_CLICKS or row['open_rate'] is None:
        return
    others = [item for item in peers if item is not row and item['clicks']]
    other_clicks = sum(item['clicks'] for item in others)
    other_opens = sum(item['opens'] for item in others)
    if not other_clicks:
        return
    baseline = other_opens / other_clicks
    if row['open_rate'] < baseline - _WAKE_GAP:
        row['alert'] = True
        row['alert_reason'] = f'疑似唤醒失败：拉起率 {_percent(row["open_rate"])}，同系统其他版位 {_percent(baseline)}'


def summarize(visits, events):
    systems = defaultdict(_blank_metrics)
    placements = defaultdict(_blank_metrics)
    for visit in visits:
        if visit['test'] or not visit['gclid']:
            continue
        systems[visit['system']]['people'].add((visit['domain'], visit['gclid']))
        raw = visit['placement_raw']
        placements[(normalize_placement(raw), visit['system'])]['people'].add((visit['domain'], visit['gclid']))
    for event in events:
        if event['test']:
            continue
        system = _event_system(event)
        raw, norm = _event_placement(event)
        _add_metrics(systems[system], event)
        _add_metrics(placements[(norm, system)], event)
        if raw and event.get('visit') is not None and not event['visit']['test']:
            pass
    system_rows = []
    for name in _SYSTEM_ORDER:
        if name not in systems:
            continue
        row = _finish_metrics(systems[name], name, name)
        _zero_click(row, name)
        system_rows.append(row)
    for name, bucket in systems.items():
        if name not in _SYSTEM_ORDER:
            row = _finish_metrics(bucket, name, name)
            _zero_click(row, name)
            system_rows.append(row)
    placement_rows = []
    grouped = defaultdict(list)
    for (norm, system), bucket in placements.items():
        row = _finish_metrics(bucket, norm, system)
        _zero_click(row, f'{norm} · {system}')
        grouped[system].append(row)
        placement_rows.append(row)
    for system, rows in grouped.items():
        for row in rows:
            if row['clicks'] == 0:
                continue
            _wake_alert(row, rows)
    placement_rows.sort(key=lambda item: (-item['visits'], -item['clicks'], item['system'], item['name']))
    return system_rows, placement_rows


def reception_gaps(visits, events):
    visit_times = sorted(item['created'] for item in visits if not item['test'])
    event_times = sorted(item['created'] for item in events if not item['test'])
    if len(visit_times) < 1:
        return []
    gaps = []
    if not event_times:
        if visit_times[-1] - visit_times[0] >= _QUIET_SECONDS:
            gaps.append((visit_times[0], visit_times[-1]))
        return gaps
    before = [stamp for stamp in visit_times if stamp < event_times[0]]
    if before and event_times[0] - before[0] >= _QUIET_SECONDS:
        gaps.append((before[0], event_times[0]))
    for left, right in zip(event_times, event_times[1:]):
        if right - left < _QUIET_SECONDS:
            continue
        if any(left < stamp < right for stamp in visit_times):
            gaps.append((left, right))
    after = [stamp for stamp in visit_times if stamp > event_times[-1]]
    if after and after[-1] - event_times[-1] >= _QUIET_SECONDS:
        gaps.append((event_times[-1], after[-1]))
    return gaps


def build_trend(visits, events, gaps):
    stamps = [item['created'] for item in visits if not item['test']]
    stamps += [item['created'] for item in events if not item['test']]
    if not stamps:
        return []
    start = _bucket(min(stamps))
    end = _bucket(max(stamps))
    buckets = {}
    cursor = start
    while cursor <= end:
        buckets[cursor] = {'visits': 0, 'events': 0}
        cursor = cursor.fromtimestamp(cursor.timestamp() + 600, HK)
    for visit in visits:
        if visit['test']:
            continue
        buckets.setdefault(_bucket(visit['created']), {'visits': 0, 'events': 0})['visits'] += 1
    for event in events:
        if event['test']:
            continue
        buckets.setdefault(_bucket(event['created']), {'visits': 0, 'events': 0})['events'] += 1
    rows = []
    for cursor in sorted(buckets):
        item = buckets[cursor]
        opened = cursor.timestamp()
        closed = opened + 600
        quiet = item['events'] == 0 and item['visits'] > 0 and any(opened < gap[1] and closed > gap[0] for gap in gaps)
        rows.append({
            'time': opened,
            'label': cursor.strftime('%Y-%m-%d %H:%M'),
            'visits': item['visits'],
            'events': item['events'],
            'alert': quiet,
            'alert_reason': '接收中断' if quiet else '',
        })
    return rows


def _chain(events):
    if not events:
        return '无事件'
    parts = []
    for event in events:
        bits = [event['event'] or '未知事件']
        if event.get('transaction_id'):
            bits.append('tx=' + event['transaction_id'])
        if event.get('wa_ms') is not None:
            bits.append('wa_ms=' + str(event['wa_ms']))
        if event.get('link_type'):
            bits.append('link_type=' + event['link_type'])
        parts.append(' '.join(bits))
    return ' | '.join(parts)


def _detail_from_visit(visit):
    raw = visit['placement_raw']
    kind = '测试' if visit['test'] else '正式'
    if visit['test'] and visit['test_reason']:
        kind = '测试：' + visit['test_reason']
    how = visit['events'][0]['align'] if visit['events'] else ('无事件' if not visit['test'] else '')
    if not visit['events']:
        how = '无事件'
    return {
        'time': visit['created'],
        'gclid': visit['gclid'],
        'placement_raw': raw,
        'placement_norm': normalize_placement(raw),
        'ua': visit['ua'],
        'system': visit['system'],
        'browser': visit['browser'],
        'number': visit['number'],
        'align': how,
        'kind': kind,
        'test': visit['test'],
        'chain': _chain(visit['events']),
    }


def _detail_from_event(event):
    raw, norm = _event_placement(event)
    kind = '测试：' + event['test_reason'] if event['test'] and event['test_reason'] else ('测试' if event['test'] else '未对齐')
    return {
        'time': event['created'],
        'gclid': event.get('gclid') or '',
        'placement_raw': raw,
        'placement_norm': norm,
        'ua': event.get('request_ua') or event.get('ua') or '',
        'system': _event_system(event),
        'browser': open_app(event.get('request_ua') or event.get('ua') or ''),
        'number': event.get('number') or '',
        'align': '未对齐',
        'kind': kind,
        'test': event['test'],
        'chain': _chain([event]),
    }


def _alert_entry(scope, name, system, reason):
    return {'scope': scope, 'name': name, 'system': system, 'reason': reason}


def build_flow(visit_rows, event_rows, intake_rows=None, domain=''):
    intake_rows = intake_rows or []
    wanted = (domain or '').strip()
    visits = [parse_flow_visit(row) for row in visit_rows if is_html_path(row.get('path')) and _domain_ok(row, wanted)]
    events = [parse_flow_event(row) for row in event_rows if _domain_ok(row, wanted)]
    intake = [row for row in intake_rows if _domain_ok(row, wanted)]
    attach_intake(events, intake)
    unmatched = align_events(visits, events)
    inherit_visit(events)
    mark_tests(visits, events)
    systems, placements = summarize(visits, events)
    gaps = reception_gaps(visits, events)
    trend = build_trend(visits, events, gaps)
    alerts = []
    for row in systems:
        if row['alert']:
            alerts.append(_alert_entry('系统', row['name'], row['system'], row['alert_reason']))
    for row in placements:
        if row['alert']:
            alerts.append(_alert_entry('版位', row['name'], row['system'], row['alert_reason']))
    for opened, closed in gaps:
        alerts.append(_alert_entry('接收', _clock(opened), '', f'接收中断：{_clock(opened)} 到 {_clock(closed)} 进站仍在，事件为 0'))
    detail = [_detail_from_visit(visit) for visit in visits]
    detail += [_detail_from_event(event) for event in unmatched]
    detail.sort(key=lambda item: item['time'], reverse=True)
    test_visits = [visit for visit in visits if visit['test']]
    test_clicks = [event for event in events if event['test'] and event['kind'] == 'click']
    return {
        'systems': systems,
        'placements': placements,
        'trend': trend,
        'alerts': alerts,
        'visits': detail,
        'test': {
            'visits': len(test_visits),
            'clicks': len({event['gclid'] or event['tx_key'] for event in test_clicks}),
            'click_times': len({event['tx_key'] for event in test_clicks}),
        },
    }


def summary_csv(report):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(_SUMMARY_FIELDS)
    for row in report['systems']:
        writer.writerow([_csv_cell(value) for value in (
            '系统', row['name'], row['system'], row['visits'], row['clicks'], row['click_times'], row['opens'], row['fallbacks'],
            _percent(row['open_rate']), '', '', '是' if row['alert'] else '', row['alert_reason'],
        )])
    for row in report['placements']:
        writer.writerow([_csv_cell(value) for value in (
            '版位', row['name'], row['system'], row['visits'], row['clicks'], row['click_times'], row['opens'], row['fallbacks'],
            _percent(row['open_rate']), '', '', '是' if row['alert'] else '', row['alert_reason'],
        )])
    for row in report['trend']:
        writer.writerow([_csv_cell(value) for value in (
            '趋势', row['label'], '', '', '', '', '', '', '', row['visits'], row['events'], '是' if row['alert'] else '', row['alert_reason'],
        )])
    for row in report['alerts']:
        writer.writerow([_csv_cell(value) for value in ('报警', row['name'], row['system'], '', '', '', '', '', '', '', '', '是', row['reason'])])
    test = report['test']
    writer.writerow(['测试', '不计入汇总', '', test['visits'], test['clicks'], test['click_times'], '', '', '', '', '', '', '测试流量单独归类'])
    return stream.getvalue()


def visits_csv(report):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(_VISIT_FIELDS)
    for row in report['visits']:
        writer.writerow([_csv_cell(value) for value in (
            _clock(row['time']), row['gclid'], row['placement_raw'], row['placement_norm'], row['ua'], row['system'],
            row['browser'], row['number'], row['align'], row['kind'], row['chain'],
        )])
    return stream.getvalue()


def intake_csv(rows):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(_INTAKE_FIELDS)
    for row in rows:
        writer.writerow([_csv_cell(value) for value in (
            _clock(row.get('created')), row.get('ip') or '', row.get('ua') or '', row.get('origin') or '', row.get('referer') or '',
            row.get('content_type') or '', row.get('body_length') or 0, row.get('status') or '', row.get('reason') or '', row.get('body') or '',
        )])
    return stream.getvalue()
