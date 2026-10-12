"""Read-only campaign totals. Nothing here changes a page, a jump, or a setting."""
from collections import defaultdict

from .devices import open_app

_GENERIC_OPENERS = {'', '未知', '浏览器', '应用内打开', 'Chrome', 'Safari', 'Edge', 'Firefox', 'Opera'}
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlsplit
import json
import re


HK = timezone(timedelta(hours=8))
_ANDROID = re.compile(r'^mobileapp::2-(.+)$', re.I)
_IOS = re.compile(r'^mobileapp::1-(.+)$', re.I)
_ADSENSE = re.compile(r'^(?:(?:\d+)\.)*(android|iphone|ipad)\.(.+)\.adsenseformobileapps\.com$', re.I)
_PLATFORM = {'android': 'Android', 'iphone': 'iPhone', 'ipad': 'iPad'}
_SYSTEM_ORDER = ('iPhone', 'iPad', 'iPod', 'Android', '其他')
_IOS_VERSION = re.compile(r'(?:CPU (?:iPhone |iPad )?OS|CPU OS)\s+(\d+)[_.](\d+)(?:[_.](\d+))?', re.I)
_ANDROID_VERSION = re.compile(r'Android\s+(\d+(?:\.\d+)*)', re.I)
_ANDROID_MODEL = re.compile(r'Android\s+[\d.]+;\s*([^;)]+)')


_YT_HOSTS = {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be', 'www.youtu.be'}


def youtube_from(value):
    """Channel handle, channel id, or video id carried by a YouTube address."""
    raw = unquote((value or '').strip())
    if not raw:
        return None
    candidate = raw
    if '://' not in candidate:
        folded = candidate.lower()
        if folded.startswith('//'):
            candidate = 'https:' + candidate
        elif folded.startswith(('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be', 'www.youtu.be')):
            candidate = 'https://' + candidate
        else:
            return None
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    host = (parts.hostname or '').lower()
    if host not in _YT_HOSTS and not host.endswith('.youtube.com'):
        return None
    path = parts.path or '/'
    segments = [unquote(part) for part in path.split('/') if part]
    channel = ''
    video = ''
    if host.endswith('youtu.be'):
        video = segments[0] if segments else ''
    elif segments and segments[0].startswith('@'):
        channel = segments[0]
    elif len(segments) >= 2 and segments[0] == 'channel':
        channel = segments[1]
    elif len(segments) >= 2 and segments[0] in ('c', 'user'):
        channel = segments[1]
    elif len(segments) >= 2 and segments[0] in ('shorts', 'embed', 'live', 'v'):
        video = segments[1]
    elif segments and segments[0] == 'watch':
        video = (parse_qs(parts.query).get('v') or [''])[0]
    return {'channel': channel.strip()[:120], 'video': video.strip()[:32], 'url': candidate[:300]}


def placement_label(value):
    text = (value or '').strip()
    if not text:
        return '未知'
    if text.startswith(('Android App · ', 'iPhone App · ', 'iPad App · ', 'iOS App · ', 'YouTube')):
        return text
    android = _ANDROID.match(text)
    if android:
        return 'Android App · ' + android.group(1).strip()
    ios = _IOS.match(text)
    if ios:
        return 'iOS App · ' + ios.group(1).strip()
    adsense = _ADSENSE.match(text)
    if adsense:
        return f'{_PLATFORM[adsense.group(1).lower()]} App · {adsense.group(2).strip()}'
    parsed = youtube_from(text)
    if parsed:
        if parsed['channel']:
            return 'YouTube · ' + parsed['channel']
        if parsed['video']:
            return 'YouTube · 视频 ' + parsed['video']
        return 'YouTube'
    return text


def canonical_placement(value):
    """One label per app. Version prefixes on adsense hosts no longer split the row."""
    text = (value or '').strip()
    label = placement_label(text)
    if label.startswith(('Android App · ', 'iPhone App · ', 'iPad App · ', 'iOS App · ')):
        return label
    return text


def system_name(item):
    model = item.get('model') or ''
    system = item.get('os') or ''
    if model == 'iPad':
        return 'iPad'
    if model == 'iPod':
        return 'iPod'
    if model == 'iPhone' or system.startswith('iOS'):
        return 'iPhone'
    if system.startswith('Android') or model == 'Android':
        return 'Android'
    return '其他'


def device_from_ua(ua):
    text = ua or ''
    ios = _IOS_VERSION.search(text)
    if ios and re.search(r'iPhone|iPad|iPod', text, re.I):
        kind = 'iPad' if re.search(r'iPad', text, re.I) else ('iPod' if re.search(r'iPod', text, re.I) else 'iPhone')
        version = f'{int(ios.group(1))}.{int(ios.group(2))}'
        if ios.group(3):
            version += f'.{int(ios.group(3))}'
        return kind, f'iOS {version}'
    android = _ANDROID_VERSION.search(text)
    if android:
        model = ''
        found = _ANDROID_MODEL.search(text)
        if found:
            model = re.sub(r'\s+Build/.*$', '', found.group(1)).strip()
            if model in ('K', 'wv', ''):
                model = ''
        return model or 'Android', f'Android {android.group(1)}'
    return '未知', '未知'


def query_value(query, name):
    for part in (query or '').split('&'):
        if not part:
            continue
        key, _, raw = part.partition('=')
        if unquote(key) == name:
            return unquote(raw.replace('+', ' '))[:300]
    return ''


def is_html_path(path):
    text = (path or '/').split('?', 1)[0].lower()
    return text in ('', '/') or text.endswith(('.html', '.htm', '/'))


def window_bounds(preset, start, end, now):
    """Inclusive Hong Kong dates. Near-7 includes today."""
    local = datetime.fromtimestamp(now, HK)
    today = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if preset == 'today':
        opened, closed = today, today + timedelta(days=1)
    elif preset == 'yesterday':
        opened, closed = today - timedelta(days=1), today
    elif preset == '7d':
        opened, closed = today - timedelta(days=6), today + timedelta(days=1)
    elif preset == 'custom':
        opened = _day(start)
        closed = _day(end) + timedelta(days=1)
        if closed <= opened:
            raise ValueError('结束日期要晚于开始日期')
        if (closed - opened).days > 90:
            raise ValueError('日期范围最多 90 天')
    else:
        raise ValueError('日期范围无效')
    return opened.timestamp(), closed.timestamp()


def _day(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('日期格式应为 YYYY-MM-DD')
    year, month, day = (int(part) for part in value.split('-'))
    try:
        return datetime(year, month, day, tzinfo=HK)
    except ValueError:
        raise ValueError('日期无效') from None


def _text(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool) or value is None:
        return ''
    if isinstance(value, (int, float)):
        return str(value)
    return ''


def _ratio(part, whole):
    if not whole:
        return None
    return round(part / whole, 4)


def _percent(value):
    return f'{value * 100:.1f}%'


_DEST_APPS = ('whatsapp', 'band', 'kakao')
_DEST_LABELS = {'whatsapp': 'WhatsApp', 'band': 'BAND', 'kakao': 'Kakao'}
_EVENT_KINDS = {
    'whatsapp_click': 'click',
    'band_click': 'click',
    'kakao_click': 'click',
    'wa_app_opened': 'open',
    'band_app_opened': 'open',
    'kakao_app_opened': 'open',
    'wa_fallback_shown': 'fallback',
    'band_fallback_shown': 'fallback',
    'kakao_fallback_shown': 'fallback',
    'wa_fallback_click': 'fallback_click',
    'band_fallback_click': 'fallback_click',
    'kakao_fallback_click': 'fallback_click',
    'wa_copy_number': 'copy_number',
    'band_copy_number': 'copy_number',
    'kakao_copy_number': 'copy_number',
    'wa_copy_message': 'copy_message',
    'band_copy_message': 'copy_message',
    'kakao_copy_message': 'copy_message',
}


def dest_label(value):
    return _DEST_LABELS.get(value or '', '未知')


def _dest_app(payload):
    raw = _text(payload.get('dest_app')).lower()
    if raw in _DEST_APPS:
        return raw
    name = _text(payload.get('event'))
    if name.startswith('band_'):
        return 'band'
    if name.startswith('kakao_'):
        return 'kakao'
    if name.startswith('wa_') or name.startswith('whatsapp_'):
        return 'whatsapp'
    return ''


def parse_event(row):
    try:
        payload = json.loads(row['body'])
    except (TypeError, ValueError):
        payload = None
    if not isinstance(payload, dict):
        payload = {}
    raw_placement = _text(payload.get('placement')) or _text(payload.get('placement_app'))
    placement = canonical_placement(raw_placement)
    ua = _text(payload.get('ua'))
    model, system = device_from_ua(ua)
    transaction_id = _text(payload.get('transaction_id'))
    return {
        'id': row.get('id'),
        'created': row.get('created') or 0,
        'domain': _text(row.get('domain')),
        'placement': placement,
        'placement_raw': raw_placement,
        'device': _text(payload.get('device')) or '未知',
        'wa_env': _text(payload.get('wa_env')) or '未知',
        'link_type': _text(payload.get('link_type')) or '未知',
        'model': model,
        'os': system,
        'ua': ua,
        'opener': open_app(ua),
        'event': _text(payload.get('event')),
        'dest_app': _dest_app(payload),
        'network': _text(payload.get('network')).lower(),
        'youtube_channel': _text(payload.get('youtube_channel')),
        'youtube_video': _text(payload.get('youtube_video')),
        'youtube_referrer': _text(payload.get('youtube_referrer')),
        'transaction_id': transaction_id,
        'tx_key': transaction_id or f'#{row.get("id")}',
        'gclid': _text(payload.get('gclid')),
        'body': row.get('body') if isinstance(row.get('body'), str) else '',
    }


def parse_visit(row):
    gclid = query_value(row.get('query'), 'gclid')
    raw_placement = query_value(row.get('query'), 'placement')
    placement = canonical_placement(raw_placement)
    referer = _text(row.get('referer'))
    referer_youtube = youtube_from(referer)
    if not placement and referer_youtube:
        placement = referer
    network = query_value(row.get('query'), 'network').lower()
    ua = row.get('ua') or ''
    model, system = device_from_ua(ua)
    domain = _text(row.get('domain'))
    ip = _text(row.get('ip'))
    ua = row.get('ua') or ''
    key = ('gclid', domain, gclid) if gclid else ('ipua', domain, ip, ua)
    return {
        'created': row.get('created') or 0,
        'domain': domain,
        'placement': placement,
        'placement_raw': raw_placement,
        'model': model,
        'os': system,
        'ua': ua,
        'opener': open_app(ua),
        'device': '未知',
        'wa_env': '未知',
        'link_type': '未知',
        'dest_app': '',
        'network': network if network in ('ytv', 'yts') else '',
        'youtube_channel': '',
        'youtube_video': '',
        'youtube_referrer': referer if referer_youtube else '',
        'gclid': gclid,
        'key': key,
    }


def annotate_youtube(item):
    parsed = youtube_from(item.get('placement')) or youtube_from(item.get('youtube_referrer'))
    if parsed:
        if not item.get('youtube_channel'):
            item['youtube_channel'] = parsed['channel']
        if not item.get('youtube_video'):
            item['youtube_video'] = parsed['video']
    item['youtube_label'] = item.get('youtube_channel') or ''
    return item


def fill_youtube_channels(rows, lookup):
    """Replace a bare video id with the channel name. Lookup failures leave the video visible."""
    found = {}
    for row in rows:
        video = row.get('youtube_video') or ''
        if not video or row.get('youtube_channel'):
            continue
        if video not in found:
            name = ''
            if lookup and len(found) < 8:
                try:
                    name = _text(lookup(video))[:120]
                except Exception:
                    name = ''
            found[video] = name
        if found[video]:
            row['youtube_channel'] = found[video]
            row['youtube_label'] = found[video]
            row['placement_label'] = 'YouTube · ' + found[video]
    return rows


def _matches(item, filters, fields):
    for field in fields:
        if field in ('placement', 'model', 'youtube', 'opener'):
            continue
        wanted = (filters.get(field) or '').strip()
        if field == 'dest_app':
            wanted = wanted.lower()
        if wanted and item.get(field) != wanted:
            return False
    if 'placement' in fields:
        placement = filters.get('placement') if filters.get('placement_exact') else (filters.get('placement') or '').strip()
        hay = ' '.join((item.get('placement') or '', item.get('placement_raw') or '', placement_label(item.get('placement') or '')))
        if filters.get('placement_exact'):
            if (item.get('placement') or '') != (placement or '') and (item.get('placement_raw') or '') != (placement or ''):
                return False
        elif placement and placement not in hay:
            return False
    if 'youtube' in fields:
        needle = (filters.get('youtube') or '').strip()
        if needle:
            hay = ' '.join((
                item.get('youtube_channel') or '',
                item.get('youtube_video') or '',
                item.get('youtube_label') or '',
                item.get('placement') or '',
                placement_label(item.get('placement')),
            ))
            if needle not in hay:
                return False
    if 'model' in fields:
        model = (filters.get('model') or '').strip()
        if model and model not in item.get('model', '') and model not in item.get('os', ''):
            return False
    if 'opener' in fields:
        opener = (filters.get('opener') or '').strip()
        if opener and opener not in (item.get('opener') or ''):
            return False
    return True


def _blank_row(item):
    app = item.get('dest_app') or ''
    return {
        'domain': item['domain'],
        'dest_app': app,
        'dest_label': dest_label(app),
        'placement': item['placement'],
        'placement_label': placement_label(item['placement']),
        'youtube_channel': item.get('youtube_channel') or '',
        'youtube_video': item.get('youtube_video') or '',
        'youtube_label': item.get('youtube_label') or '',
        'network': item.get('network') or '',
        'device': item['device'],
        'wa_env': item['wa_env'],
        'link_type': item['link_type'],
        'model': item['model'],
        'os': item['os'],
        'opener': item.get('opener') or '未知',
        'visits': 0,
        'clicks': 0,
        'click_times': 0,
        'opens': 0,
        'fallbacks': 0,
        'fallback_click': 0,
        'copy_number': 0,
        'copy_message': 0,
        'click_ids': set(),
        'click_people': set(),
        'visit_keys': set(),
        'seen_at': 0,
        'first_at': 0,
        'alert': False,
        'alert_reason': '',
    }


def _note_time(row, created):
    try:
        stamp = float(created or 0)
    except (TypeError, ValueError):
        return
    if stamp <= 0:
        return
    if not row['seen_at'] or stamp > row['seen_at']:
        row['seen_at'] = stamp
    if not row['first_at'] or stamp < row['first_at']:
        row['first_at'] = stamp


def _row_key(item):
    return (item['domain'], item['placement'], item['device'], item['wa_env'], item['link_type'], item['model'], item['os'], item.get('dest_app') or '', item.get('opener') or '')


def _click_person(item):
    gclid = item.get('gclid') or ''
    if gclid:
        return gclid
    return 'tx:' + str(item.get('tx_key') or '')


def _add_event(row, item):
    _note_time(row, item.get('created'))
    kind = _EVENT_KINDS.get(item['event'])
    if kind == 'click':
        row['click_ids'].add(item['tx_key'])
        row['click_people'].add(_click_person(item))
    elif kind == 'open':
        row['opens'] += 1
    elif kind == 'fallback':
        row['fallbacks'] += 1
    elif kind == 'fallback_click':
        row['fallback_click'] += 1
    elif kind == 'copy_number':
        row['copy_number'] += 1
    elif kind == 'copy_message':
        row['copy_message'] += 1


def _options(events, visits):
    def values(rows, field):
        return sorted({row[field] for row in rows if row.get(field)})
    return {
        'domains': values(events + visits, 'domain'),
        'placements': values(events + visits, 'placement'),
        'devices': values(events, 'device'),
        'wa_envs': values(events, 'wa_env'),
        'link_types': values(events, 'link_type'),
        'dest_apps': values(events, 'dest_app'),
    }


def inherit_transaction(events):
    """An open beacon often omits the phone and the placement already sent with the click."""
    known_device = {}
    known_fields = {}
    names = ('device', 'wa_env', 'link_type', 'placement', 'gclid', 'dest_app', 'network', 'youtube_channel', 'youtube_video', 'youtube_referrer', 'ua')
    for item in events:
        token = item['transaction_id']
        if not token:
            continue
        if item['model'] != '未知':
            known_device.setdefault(token, (item['model'], item['os']))
        fields = known_fields.setdefault(token, {})
        for name in names:
            value = item.get(name) or ''
            if value and value != '未知':
                fields.setdefault(name, value)
    for item in events:
        token = item['transaction_id']
        if not token:
            continue
        inherited = known_device.get(token)
        if inherited and item['model'] == '未知':
            item['model'], item['os'] = inherited
        for name, value in known_fields.get(token, {}).items():
            current = item.get(name) or ''
            if not current or current == '未知':
                item[name] = value
        if item.get('ua'):
            item['opener'] = open_app(item['ua'])
    return events


def _env_rates(events):
    clicks = defaultdict(set)
    opens = defaultdict(int)
    for item in events:
        kind = _EVENT_KINDS.get(item['event'])
        key = (item['dest_app'], item['wa_env'])
        if kind == 'click':
            clicks[key].add(item['tx_key'])
        elif kind == 'open':
            opens[key] += 1
    rates = {}
    for key in set(clicks) | set(opens):
        app, env = key
        rates.setdefault(app, {})[env] = _ratio(opens[key], len(clicks[key]))
    return rates


def _mark_alerts(rows, events):
    """Red means look at it. The rate is for one destination app in that wa_env."""
    rates = _env_rates(events)
    parent = defaultdict(lambda: {'clicks': 0, 'opens': 0})
    for row in rows:
        bucket = parent[(row['domain'], row['placement'], row['wa_env'], row['dest_app'])]
        bucket['clicks'] += row['clicks']
        bucket['opens'] += row['opens']
    for row in rows:
        bucket = parent[(row['domain'], row['placement'], row['wa_env'], row['dest_app'])]
        rate = _ratio(bucket['opens'], bucket['clicks'])
        env_rate = rates.get(row['dest_app'], {}).get(row['wa_env'])
        low_absolute = rate is not None and rate < 0.5
        low_relative = rate is not None and env_rate is not None and rate < env_rate * 0.6
        if bucket['clicks'] >= 30 and (low_absolute or low_relative):
            parts = [f'拉起率 {_percent(rate)}']
            if low_absolute:
                parts.append('低于 50%')
            if low_relative:
                parts.append(f'低于同一去向、同一 wa_env 平均 {_percent(env_rate)} 的 60%')
            row['alert'] = True
            row['alert_reason'] = '，'.join(parts)
    return rates


def build_campaign(visit_rows, event_rows, filters=None):
    filters = filters or {}
    events = inherit_transaction([parse_event(row) for row in event_rows])
    visits = [parse_visit(row) for row in visit_rows if is_html_path(row.get('path'))]
    for item in events:
        annotate_youtube(item)
    for item in visits:
        annotate_youtube(item)
    chosen_events = [item for item in events if _matches(item, filters, ('domain', 'placement', 'model', 'device', 'wa_env', 'link_type', 'dest_app', 'youtube', 'opener'))]
    chosen_visits = []
    seen = set()
    for item in visits:
        if item['key'] in seen or not _matches(item, filters, ('domain', 'placement', 'model', 'youtube', 'opener')):
            continue
        seen.add(item['key'])
        chosen_visits.append(item)
    options = _options(chosen_events, chosen_visits)
    rows = {}
    for item in chosen_events:
        key = _row_key(item)
        row = rows.get(key)
        if row is None:
            row = _blank_row(item)
            rows[key] = row
        _add_event(row, item)
    joined = defaultdict(set)
    for item in chosen_events:
        if item['gclid']:
            joined[(item['domain'], item['gclid'])].add(_row_key(item))
    event_filters = any((filters.get(field) or '').strip() for field in ('device', 'wa_env', 'link_type', 'dest_app', 'youtube'))
    counted = set()
    for visit in chosen_visits:
        targets = joined.get((visit['domain'], visit['gclid']), set()) if visit['gclid'] else set()
        if not targets and not event_filters:
            targets = {_row_key(visit)}
        for key in targets:
            row = rows.get(key)
            if row is None:
                row = _blank_row(visit if key == _row_key(visit) else {
                    'domain': key[0], 'placement': key[1], 'device': key[2], 'wa_env': key[3],
                    'link_type': key[4], 'model': key[5], 'os': key[6], 'dest_app': key[7], 'opener': key[8],
                })
                rows[key] = row
            row['visit_keys'].add(visit['key'])
            if row.get('opener') in _GENERIC_OPENERS and visit.get('opener') not in _GENERIC_OPENERS:
                row['opener'] = visit['opener']
            _note_time(row, visit.get('created'))
            counted.add(visit['key'])
    table = []
    for row in rows.values():
        row['visits'] = len(row['visit_keys'])
        row['clicks'] = len(row['click_people'])
        row['click_times'] = len(row['click_ids'])
        row['click_rate'] = _ratio(row['clicks'], row['visits'])
        row['open_rate'] = _ratio(row['opens'], row['clicks'])
        del row['click_ids']
        del row['click_people']
        del row['visit_keys']
        table.append(row)
    env_rates = _mark_alerts(table, events)
    table.sort(key=lambda item: (item['domain'], item['dest_app'], item['placement_label'], item['wa_env'], item['link_type'], item['device'], item['model'], -item['clicks']))

    def counted_events(kind):
        chosen = [item for item in chosen_events if _EVENT_KINDS.get(item['event']) == kind]
        if kind == 'click':
            return len({_click_person(item) for item in chosen})
        return len(chosen)

    click_events = [item for item in chosen_events if _EVENT_KINDS.get(item['event']) == 'click']
    totals = {
        'visits': len(counted),
        'clicks': counted_events('click'),
        'click_times': len({item['tx_key'] for item in click_events}),
        'opens': counted_events('open'),
        'fallbacks': counted_events('fallback'),
        'fallback_click': counted_events('fallback_click'),
        'copy_number': counted_events('copy_number'),
        'copy_message': counted_events('copy_message'),
    }
    totals['click_rate'] = _ratio(totals['clicks'], totals['visits'])
    totals['open_rate'] = _ratio(totals['opens'], totals['clicks'])
    return {'rows': table, 'totals': totals, 'options': options, 'env_open_rates': env_rates, 'systems': _systems(chosen_visits, chosen_events), 'placements': _placements(chosen_visits, chosen_events)}


def _systems(visits, events):
    """iPhone / Android totals for the current filter. A system with visits and no clicks is marked."""
    by_gclid = {}
    for visit in visits:
        if visit.get('gclid'):
            by_gclid.setdefault(visit['gclid'], system_name(visit))
    buckets = {name: {'visits': set(), 'people': set(), 'times': set(), 'opens': 0} for name in _SYSTEM_ORDER}
    for visit in visits:
        buckets[system_name(visit)]['visits'].add(visit['key'])
    for item in events:
        kind = _EVENT_KINDS.get(item['event'])
        if kind not in ('click', 'open'):
            continue
        name = system_name(item)
        if name == '其他' and item.get('gclid') in by_gclid:
            name = by_gclid[item['gclid']]
        bucket = buckets[name]
        if kind == 'click':
            bucket['people'].add(_click_person(item))
            bucket['times'].add(item['tx_key'])
        else:
            bucket['opens'] += 1
    rows = []
    for name in _SYSTEM_ORDER:
        bucket = buckets[name]
        visit_count = len(bucket['visits'])
        click_count = len(bucket['people'])
        if not visit_count and not click_count and not bucket['times'] and not bucket['opens']:
            continue
        alert = visit_count > 100 and click_count == 0
        rows.append({
            'system': name,
            'visits': visit_count,
            'clicks': click_count,
            'click_times': len(bucket['times']),
            'opens': bucket['opens'],
            'alert': alert,
            'alert_reason': f'{name} 进站 {visit_count}，点击 0' if alert else '',
        })
    return rows


def _placements(visits, events):
    """One row per app. Versioned adsense hosts share the label already stored on each visit."""
    buckets = {}

    def bucket(label):
        return buckets.setdefault(label, {'visits': set(), 'people': set(), 'times': set(), 'opens': 0})

    for visit in visits:
        bucket(placement_label(visit.get('placement') or ''))['visits'].add(visit['key'])
    for item in events:
        kind = _EVENT_KINDS.get(item['event'])
        if kind not in ('click', 'open'):
            continue
        row = bucket(placement_label(item.get('placement') or ''))
        if kind == 'click':
            row['people'].add(_click_person(item))
            row['times'].add(item['tx_key'])
        else:
            row['opens'] += 1
    rows = []
    for label, item in buckets.items():
        visit_count = len(item['visits'])
        click_count = len(item['people'])
        if not visit_count and not click_count and not item['times'] and not item['opens']:
            continue
        rows.append({
            'placement': label,
            'visits': visit_count,
            'clicks': click_count,
            'click_times': len(item['times']),
            'opens': item['opens'],
            'open_rate': _ratio(item['opens'], click_count),
        })
    rows.sort(key=lambda item: (-item['visits'], -item['click_times'], item['placement']))
    return rows


def export_events(event_rows, filters=None):
    filters = filters or {}
    chosen = []
    for item in inherit_transaction([parse_event(row) for row in event_rows]):
        annotate_youtube(item)
        if _matches(item, filters, ('domain', 'placement', 'model', 'device', 'wa_env', 'link_type', 'dest_app', 'youtube', 'opener')):
            chosen.append(item)
    chosen.sort(key=lambda item: (item['transaction_id'] == '', item['transaction_id'], item['created'], item['id'] or 0))
    return [
        {'received': item['created'], 'domain': item['domain'], 'transaction_id': item['transaction_id'], 'body': item['body']}
        for item in chosen
    ]
